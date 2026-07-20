from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import pandas as pd
import pytest

from football_cups.collector.config import CollectorConfig
from football_cups.collector.markets import parse_market_workbook
from football_cups.collector.markets import (
    decode_page_with_evidence,
    normalize_handicap_line,
    normalize_total_line,
    parse_market_html_v2,
)
from football_cups.collector.http import ObservedResponse
from football_cups.collector.reporting import build_daily_report, build_window_report, day_bounds
from football_cups.collector.state import StateStore
from football_cups.collector.service import CollectorService
from football_cups.collector.storage import DataStore
from football_cups.collector.timeutil import iso_utc


def config_for(tmp_path):
    return CollectorConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data" / "500",
        backup_dir=None,
        oss_backup_dir=None,
    )


def workbook_bytes(rows) -> bytes:
    output = io.BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False, header=False, engine="openpyxl")
    return output.getvalue()


def observed_html(content: bytes, *, encoding: str = "ISO-8859-1") -> ObservedResponse:
    observed = datetime(2026, 7, 15, 2, tzinfo=timezone.utc)
    return ObservedResponse(
        method="GET",
        url="https://odds.500.com/fenxi/yazhi-123.shtml",
        status_code=200,
        headers={"content-type": "text/html"},
        content=content,
        request_started_at=observed - timedelta(seconds=1),
        response_received_at=observed,
        source_encoding=encoding,
    )


def market_html(rows: list[list[str]], *, market: str = "yazhi") -> bytes:
    header = '<div xls="header"><span row="1">header</span></div>'
    body = []
    for values in rows:
        cells = "".join(f'<span row="1">{value}</span>' for value in values)
        body.append(f'<div xls="row">{cells}</div>')
    page = f'<html><head><meta charset="gb2312"></head><body>{header}{"".join(body)}</body></html>'
    return page.encode("gb18030")


def test_market_decoder_prefers_declared_chinese_encoding_over_latin1() -> None:
    content = market_html(
        [["公司一", "0.90", "平手/半球", "0.95", "07-15 10:00", "0.88", "半球", "0.97", "07-14 09:00"]]
    )
    text, evidence = decode_page_with_evidence(observed_html(content))
    assert "平手/半球" in text
    assert evidence["selected_encoding"] == "gb18030"
    assert evidence["encoding_source"] == "meta"


def test_market_decoder_never_selects_declared_latin1_for_chinese_page() -> None:
    content = market_html(
        [["公司一", "0.90", "平手", "0.95", "07-15 10:00", "0.88", "半球", "0.97", "07-14 09:00"]]
    ).replace(b"gb2312", b"iso-8859-1")
    text, evidence = decode_page_with_evidence(observed_html(content))
    assert "公司一" in text
    assert evidence["selected_encoding"] == "gb18030"
    assert evidence["encoding_source"] == "gb18030_fallback"


@pytest.mark.parametrize(
    ("raw", "expected", "movement"),
    [
        ("平手", "0", "none"),
        ("平手/半球", "-0.25", "none"),
        ("半球/一球 升", "-0.75", "up"),
        ("受一球/球半 降", "1.25", "down"),
    ],
)
def test_handicap_line_normalization(raw, expected, movement) -> None:
    value, actual_movement, error = normalize_handicap_line(raw)
    assert error is None
    assert value["decimal"] == expected
    assert actual_movement == movement


@pytest.mark.parametrize(("raw", "expected"), [("2", "2"), ("2/2.5", "2.25"), ("2.5/3", "2.75")])
def test_total_line_normalization(raw, expected) -> None:
    value, error = normalize_total_line(raw)
    assert error is None
    assert value["decimal"] == expected


def test_html_market_parser_requires_three_complete_bookmakers() -> None:
    rows = [
        [name, "0.90", "平手/半球", "0.95", "07-15 10:00", "0.88", "半球", "0.97", "07-14 09:00"]
        for name in ("公司一", "公司二", "公司三")
    ]
    response = observed_html(market_html(rows))
    snapshot, parsed, normalization = parse_market_html_v2(
        response,
        fixture_id="123",
        market="yazhi",
        target="T-24h",
        kickoff_at="2026-07-16T03:00:00Z",
        timezone_name="Asia/Shanghai",
        raw_sha256="abc",
    )
    assert snapshot["parser_version"] == "500-market-v2"
    assert len(parsed) == 3
    assert parsed[0]["current"]["line"]["decimal"] == "-0.25"
    assert normalization["valid_bookmaker_rows"] == 3
    assert normalization["status"] == "accepted"


def test_asian_market_rows_keep_raw_and_inferred_source_time() -> None:
    content = workbook_bytes(
        [
            ["company", "current", "line", "away", "time", "open", "line", "away", "time"],
            [None] * 9,
            ["Bookmaker", "0.90", "half", "0.95", "07-15 10:00", "0.88", "half", "0.97", "07-14 09:00"],
        ]
    )
    snapshot, rows = parse_market_workbook(
        content,
        fixture_id="123",
        market="yazhi",
        target="T-24h",
        observed_at=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
        kickoff_at="2026-07-16T03:00:00Z",
        timezone_name="Asia/Shanghai",
        raw_sha256="abc",
    )
    assert snapshot["source_market_available"]
    assert snapshot["bookmaker_count"] == 1
    assert rows[0]["source_bookmaker_name"] == "Bookmaker"
    assert rows[0]["current"]["home"]["decimal"] == "0.90"
    assert rows[0]["source_event_time"]["inference"] == "year_inferred_from_kickoff"


def test_daily_report_keeps_failure_denominators_separate(tmp_path) -> None:
    config = config_for(tmp_path)
    day = date(2026, 7, 15)
    start, _ = day_bounds(day, config.timezone_name)
    with StateStore(config) as state:
        state.add_event("discovery_poll", "full", {}, occurred_at=start)
        state.add_event("http_request", "success", {}, occurred_at=start)
        state.add_event("market_capture", "source_market_unavailable", {}, occurred_at=start)
        report = build_daily_report(config, state, day, generated_at=start)
    assert report["metrics"]["discovery_full_success_rate"] == 1.0
    assert report["metrics"]["http_acquisition_success_rate"] == 1.0
    assert report["event_counts"]["market_capture:source_market_unavailable"] == 1


def test_window_report_uses_exact_bounds(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=24)
    with StateStore(config) as state:
        state.add_event("discovery_poll", "full", {}, occurred_at=start - timedelta(seconds=1))
        state.add_event("discovery_poll", "full", {}, occurred_at=start)
        state.add_event("discovery_poll", "partial", {}, occurred_at=end - timedelta(seconds=1))
        state.add_event("discovery_poll", "partial", {}, occurred_at=end)
        report = build_window_report(config, state, start, end, generated_at=end)
    assert report["metrics"]["discovery_full_success_rate"] == 0.5
    assert report["event_counts"]["discovery_poll:full"] == 1
    assert report["event_counts"]["discovery_poll:partial"] == 1


def test_window_report_includes_v2_market_quality_breakdown(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 17, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with StateStore(config) as state:
        state.add_event(
            "market_capture",
            "success",
            {
                "parser_version": "500-market-v2",
                "valid_bookmaker_rows": 3,
                "bookmaker_rows": 4,
                "line_parse_failure_count": 1,
                "source_event_time_rows": 2,
            },
            occurred_at=start,
            competition="League",
            market="yazhi",
            cutoff="T-24h",
        )
        state.add_event(
            "model_snapshot",
            "ineligible",
            {
                "event_origin": "live",
                "ineligibility_reasons": ["yazhi:insufficient_complete_bookmakers"],
            },
            occurred_at=start,
            competition="League",
            cutoff="T-24h",
        )
        report = build_window_report(config, state, start, end, generated_at=end)

    metrics = report["metrics"]
    assert metrics["valid_bookmaker_rows_by_market"]["yazhi"]["total"] == 3
    assert metrics["market_line_parse_success_rate"]["yazhi"] == 0.75
    assert metrics["source_event_time_coverage"]["yazhi"] == 0.5
    assert metrics["market_data_complete_rate"] == 0.0
    assert metrics["collection_eligible_but_data_incomplete"] == 1
    assert metrics["ineligibility_reasons"] == {
        "yazhi:insufficient_complete_bookmakers": 1
    }
    assert metrics["market_v2_breakdown"][0]["competition"] == "League"
    assert metrics["model_v2_breakdown"] == [
        {
            "date": "2026-07-17",
            "competition": "League",
            "cutoff": "T-24h",
            "total": 1,
            "collection_eligible": 1,
            "data_complete": 0,
            "model_strict_eligible": 0,
            "market_data_complete_rate": 0.0,
            "model_eligible_rate": 0.0,
            "ineligibility_reasons": {"yazhi:insufficient_complete_bookmakers": 1},
        }
    ]
    assert metrics["mojibake_breakdown"] == []


def test_window_report_counts_runner_lock_skips_from_immutable_manifests(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=24)
    DataStore(config).write_manifest(
        "runner-skip",
        "locked-run",
        {
            "record_type": "RunnerSkip",
            "status": "skipped_locked",
            "occurred_at": iso_utc(start + timedelta(minutes=5)),
        },
        start + timedelta(minutes=5),
    )
    with StateStore(config) as state:
        report = build_window_report(config, state, start, end, generated_at=end)

    assert report["runs"]["total"] == 1
    assert report["runs"]["by_status"] == {"skipped_locked": 1}
    assert report["event_counts"]["runner:skipped_locked"] == 1


def test_result_metrics_count_unique_fixtures_and_24h_deadlines(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 17, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    kickoff = start - timedelta(hours=12)
    with StateStore(config) as state:
        for fixture_id in ("101", "102"):
            identity = {
                "fixture_id": fixture_id,
                "competition_name": "League",
                "competition_id": "1",
                "home_team_id": f"h{fixture_id}",
                "away_team_id": f"a{fixture_id}",
                "kickoff_at": iso_utc(kickoff),
                "buy_end_at": None,
            }
            state.upsert_fixture(identity, kickoff - timedelta(days=1), identity_conflict=False)
        state.add_event(
            "result_candidate", "success", {"target": "T+3h"},
            occurred_at=kickoff + timedelta(hours=3), fixture_id="101", cutoff="T+3h"
        )
        state.add_event(
            "result_candidate", "success", {"target": "T+6h"},
            occurred_at=kickoff + timedelta(hours=6), fixture_id="101", cutoff="T+6h"
        )
        state.add_event(
            "result_candidate", "missing", {"target": "T+3h"},
            occurred_at=kickoff + timedelta(hours=3), fixture_id="102", cutoff="T+3h"
        )
        state.add_event(
            "verified_result", "accepted", {"verification_method": "automatic-test"},
            occurred_at=kickoff + timedelta(hours=6), fixture_id="101"
        )
        state.add_event(
            "snapshot_batch", "strict_eligible", {},
            occurred_at=kickoff - timedelta(hours=1), fixture_id="101", cutoff="T-60m"
        )
        report = build_window_report(config, state, start, end, generated_at=end)
    metrics = report["metrics"]
    assert metrics["result_fixture_denominator"] == 2
    assert metrics["result_candidate_coverage_24h"] == 0.5
    assert metrics["verified_result_coverage"] == 0.5
    assert metrics["automatic_verified_result_coverage"] == 0.5
    assert metrics["manual_declared_result_coverage"] == 0.0
    assert metrics["verified_result_count_by_method"] == {"automatic-test": 1}
    assert metrics["result_success_rate_by_target"]["T+3h"] == 0.5
    assert metrics["strict_fixture_result_count_by_cutoff"] == {"T-60m": 1}


def test_invalidated_fixture_is_excluded_from_result_denominator(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 17, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    kickoff = start - timedelta(hours=12)
    identity = {
        "fixture_id": "101",
        "competition_name": "League",
        "competition_id": "1",
        "home_team_id": "h1",
        "away_team_id": "a1",
        "kickoff_at": iso_utc(kickoff),
        "buy_end_at": None,
    }
    with StateStore(config) as state:
        state.upsert_fixture(identity, kickoff - timedelta(days=1), identity_conflict=False)
        state.add_event(
            "fixture_invalidated",
            "excluded",
            {"reason": "invalid_match"},
            occurred_at=start,
            fixture_id="101",
        )
        report = build_window_report(config, state, start, end, generated_at=end)

    metrics = report["metrics"]
    assert metrics["result_fixture_denominator"] == 0
    assert metrics["result_cancelled_count"] == 1


def test_result_conflict_removes_fixture_from_verified_coverage(tmp_path) -> None:
    config = config_for(tmp_path)
    start = datetime(2026, 7, 17, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    kickoff = start - timedelta(hours=12)
    identity = {
        "fixture_id": "101",
        "competition_name": "League",
        "competition_id": "1",
        "home_team_id": "h1",
        "away_team_id": "a1",
        "kickoff_at": iso_utc(kickoff),
        "buy_end_at": None,
    }
    with StateStore(config) as state:
        state.upsert_fixture(identity, kickoff - timedelta(days=1), identity_conflict=False)
        state.add_event(
            "result_candidate", "success", {},
            occurred_at=kickoff + timedelta(hours=3), fixture_id="101", cutoff="T+3h"
        )
        state.add_event(
            "verified_result", "accepted", {},
            occurred_at=kickoff + timedelta(hours=4), fixture_id="101"
        )
        state.add_event(
            "result_conflict", "failure", {},
            occurred_at=kickoff + timedelta(hours=5), fixture_id="101"
        )
        report = build_window_report(config, state, start, end, generated_at=end)
    assert report["metrics"]["verified_result_coverage"] == 0.0
    assert report["metrics"]["result_conflict_count"] == 1


def test_cached_market_date_uses_recent_discovery_clock_check(tmp_path) -> None:
    config = config_for(tmp_path)
    now = datetime(2026, 7, 15, 10, tzinfo=timezone.utc)
    blob = {
        "observed_at": iso_utc(now),
        "http_status": 200,
        "url": "https://odds.500.com/fenxi/yazhi-123.shtml",
        "sha256": "abc",
        "headers": {"date": format_datetime(now - timedelta(minutes=62), usegmt=True)},
    }
    with CollectorService(config) as service:
        service.state.set_meta("last_clock_check_at", iso_utc(now))
        assert service._observe_http(blob, context="market:yazhi")
        stale_events = service.state.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='source_http_date_stale'"
        ).fetchone()[0]
        assert stale_events == 1
