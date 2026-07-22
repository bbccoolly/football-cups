from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from .config import ResearchConfig
from .database import ResearchImportError, run_database_import
from .http import AccessPolicyError, BudgetExceeded, IntegrityError, ResearchHttpError, fetch_assets
from .modeling import (
    CHANNEL_DEFAULT,
    ResearchModelError,
    evaluate_shadow_predictions,
    publish_shadow_predictions,
    train_devig_consensus_model,
    write_model_dataset,
)
from .k1_guardrail import (
    K1GuardrailError,
    analyze_k1,
    blind_test_k1_guardrail,
    dry_run_k1_guardrail,
    evaluate_k1_guardrail_forward,
    evaluate_k1_guardrail_history,
)
from .k1_history_context import render_k1_analysis
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

    dataset = subparsers.add_parser("build-model-dataset")
    _workspace(dataset)
    dataset.add_argument("--training-before-date", required=True)

    train = subparsers.add_parser("train-model")
    _workspace(train)
    train.add_argument("--training-before-date", required=True)
    train.add_argument("--channel", default=CHANNEL_DEFAULT)
    train.add_argument("--activate", action="store_true")

    shadow = subparsers.add_parser("shadow-predict")
    _workspace(shadow)
    shadow.add_argument("--channel", default=CHANNEL_DEFAULT)
    shadow.add_argument(
        "--target",
        action="append",
        choices=["T-24h", "T-6h", "T-60m", "T-10m"],
        help="May be repeated. Defaults to all product cutoffs.",
    )
    shadow.add_argument("--dry-run", action="store_true")
    shadow.add_argument("--lookahead-hours", type=int, default=48)
    shadow.add_argument("--lookback-hours", type=int, default=2)

    shadow_eval = subparsers.add_parser("evaluate-shadow")
    _workspace(shadow_eval)
    shadow_eval.add_argument("--channel", default=CHANNEL_DEFAULT)
    history_guardrail = subparsers.add_parser("evaluate-k1-guardrail-history")
    _workspace(history_guardrail)
    guardrail = subparsers.add_parser("k1-guardrail")
    _workspace(guardrail)
    guardrail.add_argument("--fixture-id", required=True)
    guardrail.add_argument("--target", required=True, choices=["T-24h", "T-6h", "T-60m", "T-10m"])
    guardrail.add_argument("--dry-run", action="store_true", required=True)
    forward_guardrail = subparsers.add_parser("evaluate-k1-guardrail-forward")
    _workspace(forward_guardrail)
    forward_guardrail.add_argument("--channel", default=CHANNEL_DEFAULT)
    analyze = subparsers.add_parser("analyze-k1")
    _workspace(analyze)
    analyze.add_argument("--fixture-id", required=True)
    analyze_target = analyze.add_mutually_exclusive_group(required=True)
    analyze_target.add_argument("--target", choices=["T-24h", "T-6h", "T-60m", "T-10m"])
    analyze_target.add_argument("--latest-available-target", action="store_true")
    analyze.add_argument("--format", choices=["detailed", "summary", "json"], default="detailed")
    analyze.add_argument("--audit", action="store_true")
    analyze.add_argument("--dry-run", action="store_true", required=True)
    blind = subparsers.add_parser("blind-test-k1-guardrail")
    _workspace(blind)
    blind.add_argument("--fixture-id", action="append")
    blind.add_argument("--since")
    blind.add_argument("--until")
    blind.add_argument("--target", action="append", choices=["T-24h", "T-6h", "T-60m", "T-10m"])
    blind.add_argument("--reveal-result", action="store_true")
    return parser.parse_args(argv)


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _rfc3339(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("RFC3339 value must include timezone")
    return parsed


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
        if args.command == "build-model-dataset":
            result = write_model_dataset(
                config,
                training_before_date=_date(args.training_before_date),
            )
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "train-model":
            result = train_devig_consensus_model(
                config,
                training_before_date=_date(args.training_before_date),
                activate=bool(args.activate),
                channel=args.channel,
            )
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "shadow-predict":
            result = publish_shadow_predictions(
                config,
                channel=args.channel,
                targets=args.target or ["T-24h", "T-6h", "T-60m", "T-10m"],
                dry_run=bool(args.dry_run),
                lookahead_hours=args.lookahead_hours,
                lookback_hours=args.lookback_hours,
            )
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "evaluate-shadow":
            result = evaluate_shadow_predictions(config, channel=args.channel)
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "evaluate-k1-guardrail-history":
            result = evaluate_k1_guardrail_history(config)
            print(json_dumps(result, indent=2))
            return 0
        if args.command == "k1-guardrail":
            print(json_dumps(dry_run_k1_guardrail(config, fixture_id=args.fixture_id, target=args.target), indent=2))
            return 0
        if args.command == "evaluate-k1-guardrail-forward":
            print(json_dumps(evaluate_k1_guardrail_forward(config, channel=args.channel), indent=2))
            return 0
        if args.command == "analyze-k1":
            if args.audit and args.format == "summary":
                raise ValueError("--audit cannot be combined with --format summary")
            result = analyze_k1(
                config, fixture_id=args.fixture_id, target=args.target,
                latest_available_target=bool(args.latest_available_target),
                audit=bool(args.audit),
            )
            if args.format == "json":
                print(json_dumps(result, indent=2))
            else:
                print(render_k1_analysis(
                    result, workspace=config.workspace,
                    summary=args.format == "summary", audit=bool(args.audit),
                ), end="")
            return 0
        if args.command == "blind-test-k1-guardrail":
            if bool(args.fixture_id) == bool(args.since or args.until):
                raise ValueError("use either --fixture-id or the complete --since/--until range")
            print(json_dumps(blind_test_k1_guardrail(
                config, fixture_ids=args.fixture_id,
                since=_rfc3339(args.since), until=_rfc3339(args.until),
                targets=args.target or ["T-24h", "T-6h", "T-60m", "T-10m"],
                reveal_result=bool(args.reveal_result),
            ), indent=2))
            return 0
    except (ValueError, ResearchNormalizeError, ResearchImportError, ResearchModelError, K1GuardrailError) as exc:
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
