from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .backup import (
    BackupConsistencyError,
    BackupLockTimeout,
    backup_health,
    run_backup,
    run_oss_backup,
    verify_oss_backup,
)
from .config import CollectorConfig
from .reporting import write_daily_report, write_window_report
from .service import CollectorService, rebuild_state
from .state import StateStore
from .storage import DataStore, SingleInstanceLock, json_dumps, make_run_id
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
        "reconcile-results",
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
            subparser.add_argument("--completed-kickoff", help="RFC3339 kickoff; defaults to state or now")
        if name == "reconcile-results":
            subparser.add_argument("--since", required=True, help="RFC3339 inclusive kickoff start")
            subparser.add_argument("--until", required=True, help="RFC3339 exclusive kickoff end")
    verify = subparsers.add_parser("verify-results")
    add_workspace_argument(verify)
    verify.add_argument("--input", type=Path, required=True)
    return parser.parse_args(argv)


def _locked(config: CollectorConfig) -> SingleInstanceLock:
    return SingleInstanceLock(config.lock_path)


def _record_locked_skip(config: CollectorConfig, command: str) -> None:
    occurred_at = utc_now()
    run_id = make_run_id(occurred_at)
    DataStore(config).write_manifest(
        "runner-skip",
        run_id,
        {
            "schema_version": 1,
            "record_type": "RunnerSkip",
            "run_id": run_id,
            "command": command,
            "status": "skipped_locked",
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        },
        occurred_at,
    )


def _health(config: CollectorConfig, *, now: datetime | None = None) -> dict[str, object]:
    checked_at = now or utc_now()
    mount_required = config.required_mount is not None
    mount_ready = config.required_mount_ready()
    mount = {
        "required": mount_required,
        "path": str(config.required_mount) if config.required_mount else None,
        "mounted": mount_ready if mount_required else None,
    }
    if mount_required and not mount_ready:
        return {
            "status": "failed",
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "data_dir": str(config.data_dir),
            "required_mount": mount,
            "issues": [
                {
                    "code": "required_mount_unavailable",
                    "severity": "failed",
                    "message": f"required data mount is unavailable: {config.required_mount}",
                }
            ],
        }

    config.ensure_directories()
    issues: list[dict[str, str]] = []
    backup_state, backup_issues = backup_health(config, now=checked_at)
    issues.extend(backup_issues)
    with StateStore(config) as state:
        quick_check = state.connection.execute("PRAGMA quick_check").fetchone()[0]
        pending_jobs = state.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending'"
        ).fetchone()[0]
        overdue_before = checked_at - timedelta(minutes=config.health_heartbeat_max_age_minutes)
        overdue_jobs = state.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending' AND due_at<?",
            (overdue_before.isoformat().replace("+00:00", "Z"),),
        ).fetchone()[0]
        last_run = state.connection.execute(
            "SELECT run_type, status, started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        timestamps = {
            "last_heartbeat": state.get_meta("last_heartbeat_at"),
            "last_full_discovery": state.get_meta("last_full_discovery_at"),
            "last_clock_check": state.get_meta("last_clock_check_at"),
        }
        last_clock_drift = state.get_meta("last_clock_drift_at")
        last_clock_drift_seconds = state.get_meta("last_clock_drift_seconds")
        parsed_clock_drift_seconds: float | None = None
        if last_clock_drift_seconds is not None:
            try:
                parsed_clock_drift_seconds = float(last_clock_drift_seconds)
            except ValueError:
                issues.append(
                    {
                        "code": "last_clock_drift_seconds_invalid",
                        "severity": "failed",
                        "message": "last_clock_drift_seconds is not numeric",
                    }
                )

        if quick_check != "ok":
            issues.append(
                {
                    "code": "state_quick_check_failed",
                    "severity": "failed",
                    "message": f"SQLite quick_check returned {quick_check}",
                }
            )

        age_limits = {
            "last_heartbeat": config.health_heartbeat_max_age_minutes,
            "last_full_discovery": config.health_discovery_max_age_minutes,
            "last_clock_check": config.health_clock_max_age_minutes,
        }
        ages: dict[str, float | None] = {}
        parsed_timestamps: dict[str, datetime] = {}
        for name, value in timestamps.items():
            if value is None:
                ages[f"{name}_age_seconds"] = None
                issues.append(
                    {
                        "code": f"{name}_missing",
                        "severity": "warning",
                        "message": f"{name} has not been recorded",
                    }
                )
                continue
            try:
                parsed = parse_iso(value)
            except ValueError:
                ages[f"{name}_age_seconds"] = None
                issues.append(
                    {
                        "code": f"{name}_invalid",
                        "severity": "failed",
                        "message": f"{name} is not a valid RFC 3339 timestamp",
                    }
                )
                continue
            parsed_timestamps[name] = parsed
            age_seconds = (checked_at - parsed).total_seconds()
            ages[f"{name}_age_seconds"] = round(age_seconds, 3)
            if age_seconds < -config.clock_drift_limit_seconds:
                issues.append(
                    {
                        "code": f"{name}_in_future",
                        "severity": "failed",
                        "message": f"{name} is ahead of the current clock",
                    }
                )
            elif age_seconds > age_limits[name] * 60:
                issues.append(
                    {
                        "code": f"{name}_stale",
                        "severity": "failed",
                        "message": f"{name} exceeds the {age_limits[name]} minute limit",
                    }
                )

        if last_clock_drift is not None:
            try:
                drift_at = parse_iso(last_clock_drift)
                last_full = parsed_timestamps.get("last_full_discovery")
                if last_full is None or drift_at > last_full:
                    issues.append(
                        {
                            "code": "unresolved_clock_drift",
                            "severity": "failed",
                            "message": "a clock drift event occurred after the last full discovery",
                        }
                    )
            except ValueError:
                issues.append(
                    {
                        "code": "last_clock_drift_invalid",
                        "severity": "failed",
                        "message": "last_clock_drift is not a valid RFC 3339 timestamp",
                    }
                )

        if overdue_jobs:
            issues.append(
                {
                    "code": "overdue_jobs",
                    "severity": "warning",
                    "message": (
                        f"{overdue_jobs} pending jobs are overdue by more than "
                        f"{config.health_heartbeat_max_age_minutes} minutes"
                    ),
                }
            )

        usage = shutil.disk_usage(config.data_dir)
        warning_bytes, critical_bytes = config.disk_thresholds(usage.total)
        if usage.free < critical_bytes:
            disk_status = "critical"
            issues.append(
                {
                    "code": "disk_space_critical",
                    "severity": "failed",
                    "message": "free disk space is below the critical threshold",
                }
            )
        elif usage.free < warning_bytes:
            disk_status = "warning"
            issues.append(
                {
                    "code": "disk_space_warning",
                    "severity": "warning",
                    "message": "free disk space is below the warning threshold",
                }
            )
        else:
            disk_status = "ok"

        status = "failed" if any(item["severity"] == "failed" for item in issues) else (
            "warning" if issues else "ok"
        )
        return {
            "status": status,
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "data_dir": str(config.data_dir),
            "required_mount": mount,
            "state_quick_check": quick_check,
            **timestamps,
            **ages,
            "last_clock_drift": last_clock_drift,
            "last_clock_drift_seconds": parsed_clock_drift_seconds,
            "pending_jobs": pending_jobs,
            "overdue_jobs": overdue_jobs,
            "last_run": dict(last_run) if last_run else None,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "disk_status": disk_status,
            "disk_warning_bytes": warning_bytes,
            "disk_critical_bytes": critical_bytes,
            "backup_dir_configured": config.backup_dir is not None,
            "oss_backup_dir_configured": config.oss_backup_dir is not None,
            **backup_state,
            "issues": issues,
        }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        config = CollectorConfig.from_workspace(args.workspace)
    except ValueError as exc:
        print(json_dumps({"status": "invalid", "error": str(exc)}, indent=2))
        return 2

    if args.command == "health":
        try:
            result = _health(config)
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 3
        print(json_dumps(result, indent=2))
        return {"ok": 0, "warning": 1, "failed": 3}[str(result["status"])]

    try:
        config.ensure_directories()
        configure_logging(config)
    except OSError as exc:
        print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 3

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
        except BackupLockTimeout as exc:
            print(json_dumps({"status": "skipped_locked", "error": str(exc)}, indent=2))
            return 1
        except ValueError as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 2
        except (BackupConsistencyError, OSError, sqlite3.Error) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 3
        print(json_dumps(result, indent=2))
        return 0

    if args.command == "backup-oss":
        try:
            result = run_oss_backup(config)
        except BackupLockTimeout as exc:
            print(json_dumps({"status": "skipped_locked", "error": str(exc)}, indent=2))
            return 1
        except ValueError as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 2
        except (BackupConsistencyError, OSError, sqlite3.Error) as exc:
            print(json_dumps({"status": "failed", "error": str(exc)}, indent=2))
            return 3
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

    with _locked(config) as lock:
        if not lock.acquired:
            if args.command == "run-once":
                _record_locked_skip(config, args.command)
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
                    completed_kickoff_at=args.completed_kickoff,
                )
                print(json_dumps(result, indent=2))
                return code
            if args.command == "reconcile-results":
                start = parse_iso(args.since)
                end = parse_iso(args.until)
                result = service.reconcile_results(start, end)
                print(json_dumps(result, indent=2))
                return 0 if not result["counts"].get("failure") else 1
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
