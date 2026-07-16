from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
from datetime import date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .backup import run_backup, run_oss_backup, verify_oss_backup
from .config import CollectorConfig
from .reporting import write_daily_report, write_window_report
from .service import CollectorService, rebuild_state
from .state import StateStore
from .storage import DataStore, SingleInstanceLock, json_dumps
from .timeutil import parse_iso, utc_now


def configure_logging(config: CollectorConfig) -> None:
    log_path = config.data_dir / "logs" / "collector.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    if not root.handlers:
        file_handler = TimedRotatingFileHandler(
            log_path, when="midnight", backupCount=30, encoding="utf-8"
        )
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        root.addHandler(stream_handler)


def add_workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="500 足球竞彩全赛事长期采集器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "discover",
        "run-once",
        "report-daily",
        "report-window",
        "backup",
        "backup-oss",
        "verify-oss-backup",
        "rebuild-state",
        "health",
        "smoke-live",
    ):
        subparser = subparsers.add_parser(name)
        add_workspace_argument(subparser)
        if name == "report-daily":
            subparser.add_argument("--date", help="Asia/Shanghai 日期 YYYY-MM-DD，默认今天")
        if name == "report-window":
            subparser.add_argument("--start", required=True, help="RFC3339 UTC/offset start")
            subparser.add_argument("--end", required=True, help="RFC3339 UTC/offset end")
        if name == "verify-oss-backup":
            subparser.add_argument("--run-id", required=True)
            subparser.add_argument("--target", type=Path, required=True)
        if name == "smoke-live":
            subparser.add_argument("--active-fixture-id", required=True)
            subparser.add_argument("--completed-fixture-id", required=True)
    verify = subparsers.add_parser("verify-results")
    add_workspace_argument(verify)
    verify.add_argument("--input", type=Path, required=True)
    return parser.parse_args(argv)


def _locked(config: CollectorConfig) -> SingleInstanceLock:
    return SingleInstanceLock(config.lock_path)


def _health(config: CollectorConfig) -> dict[str, object]:
    config.ensure_directories()
    with StateStore(config) as state:
        quick_check = state.connection.execute("PRAGMA quick_check").fetchone()[0]
        pending_jobs = state.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending'"
        ).fetchone()[0]
        last_run = state.connection.execute(
            "SELECT run_type, status, started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        usage = shutil.disk_usage(config.data_dir)
        return {
            "status": "ok" if quick_check == "ok" else "failed",
            "checked_at": utc_now().isoformat().replace("+00:00", "Z"),
            "data_dir": str(config.data_dir),
            "state_quick_check": quick_check,
            "last_heartbeat": state.get_meta("last_heartbeat_at"),
            "last_full_discovery": state.get_meta("last_full_discovery_at"),
            "pending_jobs": pending_jobs,
            "last_run": dict(last_run) if last_run else None,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "backup_dir_configured": config.backup_dir is not None,
            "oss_backup_dir_configured": config.oss_backup_dir is not None,
        }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    config = CollectorConfig.from_workspace(args.workspace)
    config.ensure_directories()
    configure_logging(config)

    if args.command == "init":
        with StateStore(config):
            pass
        print(json_dumps({"status": "initialized", "data_dir": str(config.data_dir)}, indent=2))
        return 0

    if args.command == "rebuild-state":
        with _locked(config) as lock:
            if not lock.acquired:
                print(json_dumps({"status": "skipped_locked"}, indent=2))
                return 0
            result = rebuild_state(config)
        print(json_dumps(result, indent=2))
        return 0

    if args.command == "backup":
        try:
            result = run_backup(config)
        except (OSError, ValueError) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 2
        print(json_dumps(result, indent=2))
        return 0

    if args.command == "backup-oss":
        try:
            result = run_oss_backup(config)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 2
        print(json_dumps(result, indent=2))
        return 0

    if args.command == "verify-oss-backup":
        try:
            result = verify_oss_backup(config, run_id=args.run_id, target=args.target.resolve())
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 2
        print(json_dumps(result, indent=2))
        return 0

    if args.command == "health":
        try:
            result = _health(config)
        except (OSError, sqlite3.Error) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 3
        print(json_dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 3

    with _locked(config) as lock:
        if not lock.acquired:
            print(json_dumps({"status": "skipped_locked"}, indent=2))
            return 0
        with CollectorService(config) as service:
            if args.command == "discover":
                result = service.discover()
                print(json_dumps(result, indent=2))
                return 0 if result["status"] == "full" else 1
            if args.command == "run-once":
                code, result = service.run_once()
                print(json_dumps(result, indent=2))
                return code
            if args.command == "verify-results":
                code, result = service.verify_results(args.input.resolve())
                print(json_dumps(result, indent=2))
                return code
            if args.command == "smoke-live":
                code, result = service.smoke_live(
                    active_fixture_id=str(args.active_fixture_id),
                    completed_fixture_id=str(args.completed_fixture_id),
                )
                print(json_dumps(result, indent=2))
                return code
            if args.command == "report-daily":
                local_today = datetime.now(ZoneInfo(config.timezone_name)).date()
                report_day = date.fromisoformat(args.date) if args.date else local_today
                json_path, md_path, report = write_daily_report(
                    config, service.state, service.data, report_day
                )
                print(
                    json_dumps(
                        {
                            "status": "written",
                            "json_path": str(json_path),
                            "markdown_path": str(md_path),
                            "metrics": report["metrics"],
                        },
                        indent=2,
                    )
                )
                return 0
            if args.command == "report-window":
                start = parse_iso(args.start)
                end = parse_iso(args.end)
                if end <= start:
                    print(json_dumps({"status": "failed", "error": "end must be after start"}, indent=2))
                    return 2
                json_path, md_path, report = write_window_report(
                    config, service.state, service.data, start, end
                )
                print(
                    json_dumps(
                        {
                            "status": "written",
                            "json_path": str(json_path),
                            "markdown_path": str(md_path),
                            "metrics": report["metrics"],
                        },
                        indent=2,
                    )
                )
                return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
