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


def _runner_skip_count(config: CollectorConfig, start: datetime, end: datetime) -> int:
    count = 0
    current = start.date()
    while current <= end.date():
        root = (
            config.data_dir
            / "manifests"
            / current.strftime("%Y")
            / current.strftime("%m")
            / current.strftime("%d")
        )
        if root.is_dir():
            for path in root.glob("*-runner-skip.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    occurred_at = parse_iso(str(payload["occurred_at"]))
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
                if start <= occurred_at < end and payload.get("status") == "skipped_locked":
                    count += 1
        current += timedelta(days=1)
    return count


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
            "'result_conflict','result_scope_ambiguous','result_cancelled','fixture_invalidated')",
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
            elif event_type in {"result_cancelled", "fixture_invalidated"}:
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


def _market_v2_metrics(
    events: list[dict[str, Any]], timezone_name: str
) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    grouped: dict[tuple[str, str, str, str], Counter[str]] = defaultdict(Counter)
    for event in events:
        if event["event_type"] != "market_capture" or event["status"] != "success":
            continue
        details = event.get("details") or {}
        if details.get("parser_version") != "500-market-v2":
            continue
        local_date = parse_iso(str(event["occurred_at"])).astimezone(zone).date().isoformat()
        key = (
            local_date,
            str(event.get("competition") or "unknown"),
            str(event.get("market") or "unknown"),
            str(event.get("cutoff") or "unknown"),
        )
        aggregate = grouped[key]
        aggregate["snapshots"] += 1
        aggregate["valid_bookmaker_rows"] += int(details.get("valid_bookmaker_rows") or 0)
        aggregate["bookmaker_rows"] += int(details.get("bookmaker_rows") or 0)
        aggregate["line_parse_failure_count"] += int(
            details.get("line_parse_failure_count") or 0
        )
        aggregate["source_event_time_rows"] += int(
            details.get("source_event_time_rows") or 0
        )

    by_market: dict[str, Counter[str]] = defaultdict(Counter)
    breakdown: list[dict[str, Any]] = []
    for (local_date, competition, market, cutoff), aggregate in sorted(grouped.items()):
        by_market[market].update(aggregate)
        bookmaker_rows = aggregate["bookmaker_rows"]
        line_denominator = bookmaker_rows if market in {"yazhi", "daxiao"} else 0
        breakdown.append(
            {
                "date": local_date,
                "competition": competition,
                "market": market,
                "cutoff": cutoff,
                **dict(aggregate),
                "market_line_parse_success_rate": _ratio(
                    line_denominator - aggregate["line_parse_failure_count"],
                    line_denominator,
                ),
                "source_event_time_coverage": _ratio(
                    aggregate["source_event_time_rows"], bookmaker_rows
                ),
            }
        )

    model_events = [event for event in events if event["event_type"] == "model_snapshot"]
    model_grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in model_events:
        local_date = parse_iso(str(event["occurred_at"])).astimezone(zone).date().isoformat()
        model_grouped[
            (
                local_date,
                str(event.get("competition") or "unknown"),
                str(event.get("cutoff") or "unknown"),
            )
        ].append(event)
    model_breakdown: list[dict[str, Any]] = []
    for (local_date, competition, cutoff), selected in sorted(model_grouped.items()):
        group_reasons = Counter(
            str(reason)
            for event in selected
            for reason in ((event.get("details") or {}).get("ineligibility_reasons") or [])
        )
        collection_eligible = sum(
            "collection_not_strict_eligible"
            not in ((event.get("details") or {}).get("ineligibility_reasons") or [])
            for event in selected
        )
        data_complete = sum(
            not any(
                reason != "collection_not_strict_eligible"
                for reason in ((event.get("details") or {}).get("ineligibility_reasons") or [])
            )
            for event in selected
        )
        model_eligible = sum(event["status"] == "strict_eligible" for event in selected)
        model_breakdown.append(
            {
                "date": local_date,
                "competition": competition,
                "cutoff": cutoff,
                "total": len(selected),
                "collection_eligible": collection_eligible,
                "data_complete": data_complete,
                "model_strict_eligible": model_eligible,
                "market_data_complete_rate": _ratio(data_complete, len(selected)),
                "model_eligible_rate": _ratio(model_eligible, len(selected)),
                "ineligibility_reasons": dict(sorted(group_reasons.items())),
            }
        )
    model_by_cutoff: dict[str, dict[str, int | float | None]] = {}
    complete_by_cutoff: dict[str, float | None] = {}
    for cutoff in sorted({str(event.get("cutoff") or "unknown") for event in model_events}):
        selected = [
            event for event in model_events if str(event.get("cutoff") or "unknown") == cutoff
        ]
        eligible = sum(event["status"] == "strict_eligible" for event in selected)
        complete = sum(
            not any(
                reason != "collection_not_strict_eligible"
                for reason in ((event.get("details") or {}).get("ineligibility_reasons") or [])
            )
            for event in selected
        )
        model_by_cutoff[cutoff] = {
            "eligible": eligible,
            "total": len(selected),
            "rate": _ratio(eligible, len(selected)),
        }
        complete_by_cutoff[cutoff] = _ratio(complete, len(selected))

    reason_counts = Counter(
        str(reason)
        for event in model_events
        for reason in ((event.get("details") or {}).get("ineligibility_reasons") or [])
    )
    complete_events = sum(
        not any(
            reason != "collection_not_strict_eligible"
            for reason in ((event.get("details") or {}).get("ineligibility_reasons") or [])
        )
        for event in model_events
    )
    mojibake_grouped: Counter[tuple[str, str, str, str]] = Counter()
    for event in events:
        if "mojibake" not in json_dumps(event.get("details") or {}).lower():
            continue
        local_date = parse_iso(str(event["occurred_at"])).astimezone(zone).date().isoformat()
        mojibake_grouped[
            (
                local_date,
                str(event.get("competition") or "unknown"),
                str(event.get("market") or "unknown"),
                str(event.get("cutoff") or "unknown"),
            )
        ] += 1
    mojibake = sum(mojibake_grouped.values())
    return {
        "market_data_complete_rate": _ratio(complete_events, len(model_events)),
        "market_data_complete_rate_by_cutoff": complete_by_cutoff,
        "valid_bookmaker_rows_by_market": {
            market: {
                "snapshots": values["snapshots"],
                "total": values["valid_bookmaker_rows"],
                "average": (
                    round(values["valid_bookmaker_rows"] / values["snapshots"], 6)
                    if values["snapshots"]
                    else None
                ),
            }
            for market, values in sorted(by_market.items())
        },
        "market_line_parse_success_rate": {
            market: _ratio(
                values["bookmaker_rows"] - values["line_parse_failure_count"],
                values["bookmaker_rows"],
            )
            for market, values in sorted(by_market.items())
            if market in {"yazhi", "daxiao"}
        },
        "mojibake_detected_count": mojibake,
        "source_event_time_coverage": {
            market: _ratio(values["source_event_time_rows"], values["bookmaker_rows"])
            for market, values in sorted(by_market.items())
            if values["bookmaker_rows"]
        },
        "model_eligible_rate_by_cutoff": model_by_cutoff,
        "collection_eligible_but_data_incomplete": sum(
            "collection_not_strict_eligible"
            not in ((event.get("details") or {}).get("ineligibility_reasons") or [])
            and event["status"] != "strict_eligible"
            for event in model_events
        ),
        "ineligibility_reasons": dict(sorted(reason_counts.items())),
        "market_v2_breakdown": breakdown,
        "model_v2_breakdown": model_breakdown,
        "mojibake_breakdown": [
            {
                "date": key[0],
                "competition": key[1],
                "market": key[2],
                "cutoff": key[3],
                "count": count,
            }
            for key, count in sorted(mojibake_grouped.items())
        ],
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
    events = [
        event
        for event in state.events_for_day(start, end)
        if (event.get("details") or {}).get("event_origin") != "reprocess"
    ]
    runs = state.runs_for_day(start, end)
    event_counts = Counter((event["event_type"], event["status"]) for event in events)
    locked_skips = _runner_skip_count(config, start, end)
    if locked_skips:
        event_counts[("runner", "skipped_locked")] += locked_skips
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
    market_v2_metrics = _market_v2_metrics(events, config.timezone_name)

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "DailyQualityReport",
        "report_date": day.isoformat(),
        "generated_at": iso_utc(generated),
        "last_heartbeat": state.get_meta("last_heartbeat_at"),
        "runs": {
            "total": len(runs) + locked_skips,
            "by_status": dict(
                Counter(run["status"] for run in runs) + Counter({"skipped_locked": locked_skips})
            ),
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
            **market_v2_metrics,
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
    events = [
        event
        for event in state.events_for_range(start, end)
        if (event.get("details") or {}).get("event_origin") != "reprocess"
    ]
    runs = state.runs_for_range(start, end)
    event_counts = Counter((event["event_type"], event["status"]) for event in events)
    locked_skips = _runner_skip_count(config, start, end)
    if locked_skips:
        event_counts[("runner", "skipped_locked")] += locked_skips
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
    market_v2_metrics = _market_v2_metrics(events, config.timezone_name)

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "WindowQualityReport",
        "start": iso_utc(start),
        "end": iso_utc(end),
        "generated_at": iso_utc(generated),
        "timezone": config.timezone_name,
        "last_heartbeat": state.get_meta("last_heartbeat_at"),
        "runs": {
            "total": len(runs) + locked_skips,
            "by_status": dict(
                Counter(run["status"] for run in runs) + Counter({"skipped_locked": locked_skips})
            ),
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
            **market_v2_metrics,
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
