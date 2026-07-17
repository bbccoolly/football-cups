from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from .config import ResearchConfig
from .database import ResearchImportError, run_database_import
from .http import AccessPolicyError, BudgetExceeded, IntegrityError, ResearchHttpError, fetch_assets
from .normalize import (
    ResearchIntegrityError,
    ResearchNormalizeError,
    import_k1_dataset,
    normalize_available_assets,
)
from .registry import ASSET_BY_ID, ASSETS, ROBOTS_URLS, assets_for_source
from .reporting import coverage_report, evaluate_baseline
from .storage import json_dumps


def _workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Public historical football research pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog")
    _workspace(catalog)

    fetch = subparsers.add_parser("fetch")
    _workspace(fetch)
    fetch.add_argument("--source", required=True, choices=["football-data"])
    fetch.add_argument("--since", default="2025-01-01")
    fetch.add_argument("--asset", choices=sorted(ASSET_BY_ID))

    import_k1 = subparsers.add_parser("import-k1")
    _workspace(import_k1)
    import_k1.add_argument("--input", type=Path, required=True)
    import_k1.add_argument("--metadata", type=Path, required=True)

    normalize = subparsers.add_parser("normalize")
    _workspace(normalize)
    normalize.add_argument("--since", default="2025-01-01")

    for command in ("db-import", "report-coverage", "evaluate-baseline"):
        subparser = subparsers.add_parser(command)
        _workspace(subparser)
    return parser.parse_args(argv)


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        config = ResearchConfig.from_workspace(args.workspace)
        if args.command == "catalog":
            print(
                json_dumps(
                    {
                        "assets": [asset.__dict__ for asset in ASSETS],
                        "robots": ROBOTS_URLS,
                        "forbidden_sources": ["500-historical", "centroquote-bulk", "oddsharvester-bulk"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "fetch":
            since = _date(args.since)
            assets = [ASSET_BY_ID[args.asset]] if args.asset else assets_for_source(args.source)
            run_id, results, errors = fetch_assets(config, assets)
            print(
                json_dumps(
                    {
                        "run_id": run_id,
                        "since": since,
                        "results": [result.__dict__ for result in results],
                        "errors": errors,
                    },
                    indent=2,
                )
            )
            if not errors:
                return 0
            severe = {"AccessPolicyError", "BudgetExceeded", "IntegrityError"}
            return 3 if any(error["error_type"] in severe for error in errors) else 1
        if args.command == "import-k1":
            result = import_k1_dataset(config, args.input.resolve(), args.metadata.resolve())
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "normalize":
            result = normalize_available_assets(config, since=_date(args.since))
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "db-import":
            print(json_dumps(run_database_import(config), indent=2))
            return 0
        if args.command == "report-coverage":
            path, payload = coverage_report(config)
            print(json_dumps({"path": str(path), **payload}, indent=2))
            return 0
        if args.command == "evaluate-baseline":
            path, payload = evaluate_baseline(config)
            print(
                json_dumps(
                    {
                        "path": str(path),
                        "dataset_hash": payload["dataset_hash"],
                        "training_fixtures": len(payload["training_fixture_ids"]),
                        "evaluation_fixtures": len(payload["evaluation_fixture_ids"]),
                        "metric_rows": len(payload["metrics"]),
                    },
                    indent=2,
                )
            )
            return 0
    except (ValueError, ResearchNormalizeError, ResearchImportError) as exc:
        print(json_dumps({"status": "invalid", "error_type": type(exc).__name__, "error": str(exc)}, indent=2))
        return 2
    except (AccessPolicyError, BudgetExceeded, IntegrityError, ResearchIntegrityError) as exc:
        print(json_dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}, indent=2))
        return 3
    except (ResearchHttpError, OSError, psycopg.Error, RuntimeError) as exc:
        print(json_dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}, indent=2))
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
