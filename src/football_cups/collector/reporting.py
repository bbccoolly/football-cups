from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import SCHEMA_VERSION
from .config import CollectorConfig
from .state import StateStore
from .storage import DataStore, json_dumps, make_run_id
from .timeutil import iso_utc, utc_now


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def day_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = (datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


def build_daily_report(
    config: CollectorConfig,
    state: StateStore,
    day: date,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or utc_now()
    start, end = day_bounds(day, config.timezone_name)
    events = state.events_for_day(start, end)
    runs = state.runs_for_day(start, end)
    event_counts = Counter((event["event_type"], event["status"]) for event in events)
    market_by_competition: dict[str, Counter[str]] = defaultdict(Counter)
    cutoff_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        if event["event_type"] == "market_capture":
            market_by_competition[event.get("competition") or "unknown"][event["status"]] += 1
        if event["event_type"] == "snapshot_batch":
            cutoff_counts[event.get("cutoff") or "unknown"][event["status"]] += 1

    full_discovery = event_counts[("discovery_poll", "full")]
    partial_discovery = event_counts[("discovery_poll", "partial")]
    http_success = event_counts[("http_request", "success")]
    http_failure = event_counts[("http_request", "failure")]
    parser_success = event_counts[("parser", "success")]
    parser_failure = event_counts[("parser", "failure")]
    result_success = event_counts[("result_candidate", "success")]
    result_missing = event_counts[("result_candidate", "missing")]

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "DailyQualityReport",
        "report_date": day.isoformat(),
        "generated_at": iso_utc(generated),
        "last_heartbeat": state.get_meta("last_heartbeat_at"),
        "runs": {
            "total": len(runs),
            "by_status": dict(Counter(run["status"] for run in runs)),
        },
        "metrics": {
            "discovery_full_success_rate": _ratio(
                full_discovery, full_discovery + partial_discovery
            ),
            "http_acquisition_success_rate": _ratio(
                http_success, http_success + http_failure
            ),
            "parser_success_rate": _ratio(parser_success, parser_success + parser_failure),
            "result_candidate_coverage": _ratio(
                result_success, result_success + result_missing
            ),
        },
        "event_counts": {
            f"{kind}:{status}": count for (kind, status), count in sorted(event_counts.items())
        },
        "market_by_competition": {
            key: dict(value) for key, value in sorted(market_by_competition.items())
        },
        "cutoff_status": {key: dict(value) for key, value in sorted(cutoff_counts.items())},
    }


def write_daily_report(
    config: CollectorConfig,
    state: StateStore,
    data_store: DataStore,
    day: date,
) -> tuple[Path, Path, dict[str, Any]]:
    generated = utc_now()
    report = build_daily_report(config, state, day, generated_at=generated)
    run_id = make_run_id(generated)
    folder = config.data_dir / "reports" / "daily" / day.strftime("%Y") / day.strftime("%m")
    json_path = folder / f"{day.isoformat()}-{run_id}.json"
    md_path = folder / f"{day.isoformat()}-{run_id}.md"
    data_store._atomic_write(json_path, (json_dumps(report, indent=2) + "\n").encode("utf-8"))
    metrics = report["metrics"]
    lines = [
        f"# 500 采集质量日报 {day.isoformat()}",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 最后心跳：{report['last_heartbeat'] or '无'}",
        f"- 运行次数：{report['runs']['total']}",
        f"- 完整发现成功率：{metrics['discovery_full_success_rate']}",
        f"- HTTP 成功率：{metrics['http_acquisition_success_rate']}",
        f"- 解析成功率：{metrics['parser_success_rate']}",
        f"- 候选赛果覆盖率：{metrics['result_candidate_coverage']}",
        "",
        "## 事件",
        "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in report["event_counts"].items())
    data_store._atomic_write(md_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return json_path, md_path, report

