from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from football_cups.collector.storage import json_dumps

from .config import DatabaseConfig
from .connection import apply_migrations, connect
from .importer import (
    AppendOnlyViolation,
    ImportAlreadyRunning,
    ImportContractError,
    import_lock,
    import_jsonl_tree,
    import_manifests,
    parse_time,
)
from .queries import as_of_audit, database_status, market_rows_as_of


def add_workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Football Cups PostgreSQL analysis database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("init", "import-files", "import-jsonl", "status"):
        subparser = subparsers.add_parser(name)
        add_workspace_argument(subparser)

    as_of = subparsers.add_parser("as-of")
    add_workspace_argument(as_of)
    as_of.add_argument("--fixture-id", required=True)
    as_of.add_argument("--cutoff", required=True, help="RFC 3339 timestamp with timezone")
    as_of.add_argument("--limit", type=int, default=1000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    config = DatabaseConfig.from_workspace(args.workspace)
    try:
        if args.command == "init":
            with connect(config) as connection:
                applied = apply_migrations(connection)
            print(json_dumps({"status": "initialized", "migrations_applied": applied}, indent=2))
            return 0

        if args.command in {"import-files", "import-jsonl"}:
            with connect(config) as connection:
                apply_migrations(connection)
                with import_lock(connection):
                    manifests = (
                        import_manifests(connection, config.data_dir)
                        if args.command == "import-files"
                        else None
                    )
                    summary = import_jsonl_tree(connection, config.normalized_dir)
            payload = summary.public_dict()
            if manifests is not None:
                payload["manifests"] = {
                    "files_seen": manifests.files_seen,
                    "inserted": manifests.manifests_inserted,
                    "existing": manifests.manifests_existing,
                }
            print(json_dumps(payload, indent=2))
            return 0

        if args.command == "status":
            with connect(config) as connection:
                status = database_status(connection)
            print(json_dumps(status, indent=2))
            return 0

        if args.command == "as-of":
            if not args.fixture_id.isdigit():
                raise ValueError("fixture-id must be numeric")
            if args.limit < 1 or args.limit > 10000:
                raise ValueError("limit must be between 1 and 10000")
            cutoff = parse_time(args.cutoff)
            if cutoff is None:
                raise ValueError("cutoff is required")
            with connect(config) as connection:
                rows = market_rows_as_of(
                    connection,
                    fixture_id=args.fixture_id,
                    prediction_cutoff=cutoff,
                    limit=args.limit,
                )
                audit = as_of_audit(
                    connection,
                    fixture_id=args.fixture_id,
                    prediction_cutoff=cutoff,
                )
            print(
                json_dumps(
                    {
                        "fixture_id": args.fixture_id,
                        "prediction_cutoff": cutoff,
                        "audit": audit,
                        "rows": rows,
                    },
                    indent=2,
                )
            )
            return 0
    except (ValueError, ImportContractError) as exc:
        print(json_dumps({"status": "invalid", "error": str(exc)}, indent=2))
        return 2
    except ImportAlreadyRunning:
        print(json_dumps({"status": "skipped_locked"}, indent=2))
        return 0
    except (AppendOnlyViolation, psycopg.Error, OSError, RuntimeError) as exc:
        print(
            json_dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                indent=2,
            )
        )
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
