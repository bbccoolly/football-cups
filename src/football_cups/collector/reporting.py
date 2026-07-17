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
from .timeutil import iso_utc, parse_iso, utc_now


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def day_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = (datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


def _result_metrics(state: StateStore, start: datetime, end: datetime) -> dict[str, Any]:
    deadline_start = start - timedelta(hours=24)
    deadline_end = end - timedelta(hours=24)
    fixture_rows = state.connection.execute(
        "SELECT fixture_id, kickoff_at FROM fixtures "
        "WHERE kickoff_at IS NOT NULL AND kickoff_at>=? AND kickoff_at<?",
        (iso_utc(deadline_start), iso_utc(deadline_end)),
    ).fetchall()
    kickoff_by_fixture = {
        str(row["fixture_id"]): parse_iso(str(row["kickoff_at"])) for row in fixture_rows
    }
    fixture_ids = set(kickoff_by_fixture)
    candidate_at: dict[str, datetime] = {}
    verified_ids: set[str] = set()
    unresolved_ids: set[str] = set()
    conflict_ids: set[str] = set()
    ambiguous_ids: set[str] = set()
    cancelled_ids: set[str] = set()
    target_attempts: dict[str, set[str]] = defaultdict(set)
    target_success: dict[str, set[str]] = defaultdict(set)
    if fixture_ids:
        placeholders = ",".join("?" for _ in fixture_ids)
        rows = state.connection.execute(
            f"SELECT fixture_id, event_type, status, occurred_at, cutoff, details_json FROM events "
            f"WHERE fixture_id IN ({placeholders}) AND occurred_at<? "
            "AND event_type IN ('result_candidate','verified_result','result_unresolved',"
            "'result_conflict','result_scope_ambiguous','result_cancelled')",
            [*sorted(fixture_ids), iso_utc(end)],
        ).fetchall()
        for row in rows:
            fixture_id = str(row["fixture_id"])
            event_type = str(row["event_type"])
            status = str(row["status"])
            occurred_at = parse_iso(str(row["occurred_at"]))
            if event_type == "result_candidate" and status == "success":
                prior = candidate_at.get(fixture_id)
                if prior is None or occurred_at < prior:
                    candidate_at[fixture_id] = occurred_at
            if event_type == "result_candidate":
                details = json.loads(row["details_json"])
                target = str(row["cutoff"] or details.get("target") or "unknown")
                target_attempts[target].add(fixture_id)
                if status == "success":
                    target_success[target].add(fixture_id)
            elif event_type == "verified_result" and status == "accepted":
                verified_ids.add(fixture_id)
            elif event_type == "result_unresolved":
                unresolved_ids.add(fixture_id)
            elif event_type == "result_conflict":
                conflict_ids.add(fixture_id)
            elif event_type == "result_scope_ambiguous":
                ambiguous_ids.add(fixture_id)
            elif event_type == "result_cancelled":
                cancelled_ids.add(fixture_id)
    eligible_fixture_ids = fixture_ids - cancelled_ids
    candidate_within_24h = {
        fixture_id
        for fixture_id, observed_at in candidate_at.items()
        if fixture_id in eligible_fixture_ids
        and observed_at <= kickoff_by_fixture[fixture_id] + timedelta(hours=24)
    }
    verified_ids.difference_update(conflict_ids)
    verified_ids.intersection_update(eligible_fixture_ids)

    strict_by_cutoff: dict[str, int] = {}
    if verified_ids:
        placeholders = ",".join("?" for _ in verified_ids)
        rows = state.connection.execute(
            f"SELECT cutoff, count(DISTINCT fixture_id) AS count FROM events "
            f"WHERE event_type='snapshot_batch' AND status='strict_eligible' "
            f"AND occurred_at<? AND fixture_id IN ({placeholders}) GROUP BY cutoff",
            [iso_utc(end), *sorted(verified_ids)],
        ).fetchall()
        strict_by_cutoff = {str(row["cutoff"] or "unknown"): int(row["count"]) for row in rows}

    denominator = len(eligible_fixture_ids)
    return {
        "result_candidate_coverage_24h": _ratio(len(candidate_within_24h), denominator),
        "verified_result_coverage": _ratio(len(verified_ids), denominator),
        "result_fixture_denominator": denominator,
        "result_candidate_within_24h_count": len(candidate_within_24h),
        "verified_result_count": len(verified_ids),
        "result_unresolved_count": len(unresolved_ids),
        "result_conflict_count": len(conflict_ids),
        "result_scope_ambiguous_count": len(ambiguous_ids),
        "result_cancelled_count": len(cancelled_ids),
        "result_success_rate_by_target": {
            target: _ratio(len(target_success[target]), len(fixtures))
            for target, fixtures in sorted(target_attempts.items())
        },
        "strict_fixture_result_count_by_cutoff": strict_by_cutoff,
    }


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
    result_metrics = _result_metrics(state, start, end)

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
            "result_candidate_coverage": result_metrics["result_candidate_coverage_24h"],
            **result_metrics,
        },
        "event_counts": {
            f"{kind}:{status}": count for (kind, status), count in sorted(event_counts.items())
        },
        "market_by_competition": {
            key: dict(value) for key, value in sorted(market_by_competition.items())
        },
        "cutoff_status": {key: dict(value) for key, value in sorted(cutoff_counts.items())},
    }


def build_window_report(
    config: CollectorConfig,
    state: StateStore,
    start: datetime,
    end: datetime,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or utc_now()
    events = state.events_for_range(start, end)
    runs = state.runs_for_range(start, end)
    event_counts = Counter((event["event_type"], event["status"]) for event in events)
    cutoff_counts: dict[str, Counter[str]] = defaultdict(Counter)
    market_by_competition: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        if event["event_type"] == "snapshot_batch":
            cutoff_counts[event.get("cutoff") or "unknown"][event["status"]] += 1
        if event["event_type"] == "market_capture":
            market_by_competition[event.get("competition") or "unknown"][event["status"]] += 1

    full_discovery = event_counts[("discovery_poll", "full")]
    partial_discovery = event_counts[("discovery_poll", "partial")]
    http_success = event_counts[("http_request", "success")]
    http_failure = event_counts[("http_request", "failure")]
    parser_success = event_counts[("parser", "success")]
    parser_failure = event_counts[("parser", "failure")]
    result_success = event_counts[("result_candidate", "success")]
    result_missing = event_counts[("result_candidate", "missing")]
    result_metrics = _result_metrics(state, start, end)

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "WindowQualityReport",
        "start": iso_utc(start),
        "end": iso_utc(end),
        "generated_at": iso_utc(generated),
        "timezone": config.timezone_name,
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
            "result_candidate_coverage": result_metrics["result_candidate_coverage_24h"],
            **result_metrics,
        },
        "event_counts": {
            f"{kind}:{status}": count for (kind, status), count in sorted(event_counts.items())
        },
        "market_by_competition": {
            key: dict(value) for key, value in sorted(market_by_competition.items())
        },
        "cutoff_status": {key: dict(value) for key, value in sorted(cutoff_counts.items())},
    }


def write_window_report(
    config: CollectorConfig,
    state: StateStore,
    data_store: DataStore,
    start: datetime,
    end: datetime,
) -> tuple[Path, Path, dict[str, Any]]:
    generated = utc_now()
    report = build_window_report(config, state, start, end, generated_at=generated)
    run_id = make_run_id(generated)
    folder = config.data_dir / "reports" / "windows" / generated.strftime("%Y") / generated.strftime("%m")
    json_path = folder / f"{run_id}.json"
    md_path = folder / f"{run_id}.md"
    data_store._atomic_write(json_path, (json_dumps(report, indent=2) + "\n").encode("utf-8"))
    metrics = report["metrics"]
    lines = [
        "# 500 采集精确窗口报告",
        "",
        f"- 窗口开始：{report['start']}",
        f"- 窗口结束：{report['end']}",
        f"- 生成时间：{report['generated_at']}",
        f"- 最后心跳：{report['last_heartbeat'] or '无'}",
        f"- 运行次数：{report['runs']['total']}",
        f"- 完整发现成功率：{metrics['discovery_full_success_rate']}",
        f"- HTTP 成功率：{metrics['http_acquisition_success_rate']}",
        f"- 解析成功率：{metrics['parser_success_rate']}",
        f"- 候选赛果覆盖率：{metrics['result_candidate_coverage']}",
        f"- 已验证赛果覆盖率：{metrics['verified_result_coverage']}",
        f"- 未解决赛果：{metrics['result_unresolved_count']}",
        f"- 赛果冲突：{metrics['result_conflict_count']}",
        "",
        "## 事件",
        "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in report["event_counts"].items())
    data_store._atomic_write(md_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return json_path, md_path, report


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
        f"- 已验证赛果覆盖率：{metrics['verified_result_coverage']}",
        f"- 未解决赛果：{metrics['result_unresolved_count']}",
        f"- 赛果冲突：{metrics['result_conflict_count']}",
        "",
        "## 事件",
        "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in report["event_counts"].items())
    data_store._atomic_write(md_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return json_path, md_path, report
