from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .backup import run_backup
from .config import CollectorConfig
from .reporting import write_daily_report
from .service import CollectorService, rebuild_state
from .state import StateStore
from .storage import DataStore, SingleInstanceLock, json_dumps


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
    for name in ("init", "discover", "run-once", "report-daily", "backup", "rebuild-state"):
        subparser = subparsers.add_parser(name)
        add_workspace_argument(subparser)
        if name == "report-daily":
            subparser.add_argument("--date", help="Asia/Shanghai 日期 YYYY-MM-DD，默认今天")
    verify = subparsers.add_parser("verify-results")
    add_workspace_argument(verify)
    verify.add_argument("--input", type=Path, required=True)
    return parser.parse_args(argv)


def _locked(config: CollectorConfig) -> SingleInstanceLock:
    return SingleInstanceLock(config.lock_path)


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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
