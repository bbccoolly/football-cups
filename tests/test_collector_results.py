from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from football_cups.collector.config import CollectorConfig
from football_cups.collector.http import ObservedResponse
from football_cups.collector.results import (
    ResultParseError,
    import_verified_results,
    is_blocked_result_page,
    make_candidate,
    parse_analysis_page,
    parse_live_result,
    parse_live_result_feed,
    result_feed_url,
    result_page_url,
)
from football_cups.collector.service import CollectorService
from football_cups.collector.storage import DataStore
from football_cups.collector.timeutil import iso_utc


LIVE = b"""
<html><head><meta charset="utf-8"></head><body><table>
<tr id="a123" fid="123" status="4">
 <td><span class="mainName">Home</span></td>
 <td><div class="pk"><a class="clt1">2</a><span>-</span><a class="clt3">1</a></div></td>
 <td><span class="clientName">Away</span></td><td class="red">1 - 0</td>
</tr></table></body></html>
"""

ANALYSIS = b"""
<input type="hidden" id="id" value="123" />
<span class="odds_hd_team"><a>Home</a></span>
<p class="odds_hd_bf"><strong>2:1</strong></p>
<span class="odds_hd_team odds_hd_team2"><a>Away</a></span>
"""

FEED = b'[[123,4,"2,1,0,0","1,0,0,0","1900-01-01 00:00:00",123]]'


def config_for(tmp_path):
    return CollectorConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data" / "500",
        backup_dir=None,
        oss_backup_dir=None,
    )


def response(url: str, content: bytes, at: datetime) -> ObservedResponse:
    return ObservedResponse(
        method="GET",
        url=url,
        status_code=200,
        headers={"content-type": "text/html; charset=gbk"},
        content=content,
        request_started_at=at,
        response_received_at=at,
        source_encoding="gb18030",
    )


def test_live_result_contract_and_beijing_date() -> None:
    live = parse_live_result(LIVE, "123")
    assert (live.home_goals, live.away_goals, live.status_code) == (2, 1, "4")
    assert result_page_url("2026-12-31T17:30:00Z", "Asia/Shanghai") == (
        "https://live.500.com/?e=20270101"
    )
    assert result_feed_url("2026-12-31T17:30:00Z").endswith("jczq/20261231Full.txt")
    feed = parse_live_result_feed(FEED, "123")
    assert (feed.home_goals, feed.away_goals, feed.status_code) == (2, 1, "4")
    analysis = parse_analysis_page(ANALYSIS, "123")
    assert analysis is not None
    assert (analysis.home_goals, analysis.away_goals) == (2, 1)


@pytest.mark.parametrize(
    ("page", "code"),
    [
        (b"<table></table>", "fixture_missing"),
        (LIVE.replace(b'status="4"', b'status="2"'), "not_finished"),
        (LIVE.replace(b">2</a>", b">07-17</a>"), "score_not_integer"),
        (LIVE + LIVE, "fixture_duplicate"),
    ],
)
def test_live_result_rejects_ambiguous_or_invalid_rows(page: bytes, code: str) -> None:
    with pytest.raises(ResultParseError) as raised:
        parse_live_result(page, "123")
    assert raised.value.code == code


def test_block_page_and_analysis_fixture_mismatch_are_rejected() -> None:
    blocked = b'<div id="statusCode">567</div>Tencent Cloud EdgeOne Restricted Access'
    assert is_blocked_result_page(blocked)
    with pytest.raises(ResultParseError) as raised:
        parse_live_result(blocked, "123")
    assert raised.value.code == "blocked_page"
    assert parse_analysis_page(ANALYSIS.replace(b'value="123"', b'value="999"'), "123") is None


def test_live_candidate_does_not_require_analysis(tmp_path) -> None:
    observed = datetime(2026, 7, 17, 1, tzinfo=timezone.utc)
    live = parse_live_result(LIVE, "123")
    blob = {"sha256": "abc", "url": "https://live.500.com/?e=20260717"}
    candidate = make_candidate(
        live,
        kickoff_at="2026-07-16T22:00:00Z",
        observed_at=observed,
        live_blob=blob,
        analysis_blob=None,
        analysis_consistency="unavailable",
    )
    assert candidate["analysis_page_sha256"] is None
    assert candidate["analysis_consistency"] == "unavailable"
    assert candidate["scope"] == "candidate-full-time-scope-not-yet-confirmed"


@pytest.mark.parametrize(
    ("competition", "competition_format", "verified_count", "isolated_count"),
    [
        ("挪超", "regular_time_only", 1, 0),
        ("欧冠", "may_have_extra_time", 0, 1),
        ("新赛事", "unknown", 0, 1),
    ],
)
def test_service_auto_verifies_or_isolates_by_competition(
    tmp_path, monkeypatch, competition, competition_format, verified_count, isolated_count
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "competition-formats.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "competitions": {competition: competition_format},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = config_for(tmp_path)
    kickoff = datetime(2026, 7, 16, 22, tzinfo=timezone.utc)
    observed = datetime(2026, 7, 17, 1, tzinfo=timezone.utc)
    identity = {
        "fixture_id": "123",
        "competition_name": competition,
        "competition_id": "10",
        "home_team_id": "1",
        "away_team_id": "2",
        "kickoff_at": iso_utc(kickoff),
        "buy_end_at": None,
    }
    with CollectorService(config) as service:
        service.state.upsert_fixture(identity, kickoff, identity_conflict=False)
        service.state.sync_competition_formats(service.competition_formats)
        live_url = result_page_url(iso_utc(kickoff), config.timezone_name)
        feed_url = result_feed_url(iso_utc(kickoff))
        analysis_url = "https://odds.500.com/fenxi/shuju-123.shtml"

        def fake_request(_method, url, **_kwargs):
            if url == live_url:
                return response(url, b"<html><body></body></html>" if competition == "挪超" else LIVE, observed)
            if url == feed_url:
                return response(url, FEED, observed)
            if url == analysis_url:
                return response(url, ANALYSIS, observed)
            raise AssertionError(url)

        monkeypatch.setattr(service.http, "request", fake_request)
        counts = service._process_result_jobs(
            [
                {
                    "job_id": "result:test",
                    "fixture_id": "123",
                    "target": "T+3h",
                    "attempts": 0,
                    "payload": {"fixture": identity, "kickoff_at": iso_utc(kickoff)},
                }
            ]
        )

    assert counts["candidate"] == 1
    assert counts["verified"] == verified_count
    assert counts["isolated"] == isolated_count
    assert len(list((config.data_dir / "results").rglob("candidates/*.json"))) == 1
    assert len(list((config.data_dir / "results").rglob("verified/*.json"))) == verified_count


def test_manual_verified_result_conflict_is_not_overwritten(tmp_path) -> None:
    store = DataStore(config_for(tmp_path))
    first = tmp_path / "first.csv"
    first.write_text(
        "fixture_id,home_goals,away_goals,source_url,confirmed_at,notes\n"
        "123,2,1,https://source.test,2026-07-15T10:00:00Z,checked\n",
        encoding="utf-8",
    )
    imported, conflicts = import_verified_results(first, store)
    assert len(imported) == 1
    assert conflicts == []

    second = tmp_path / "second.csv"
    second.write_text(
        "fixture_id,home_goals,away_goals,source_url,confirmed_at,notes\n"
        "123,3,1,https://source.test,2026-07-15T11:00:00Z,conflict\n",
        encoding="utf-8",
    )
    imported, conflicts = import_verified_results(second, store)
    assert imported == []
    assert conflicts[0]["existing"] == [2, 1]
    verified_files = list((store.config.data_dir / "results").rglob("verified/*.json"))
    assert len(verified_files) == 1
