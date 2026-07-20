from __future__ import annotations

import json
from datetime import datetime, timezone

from football_cups.collector.config import CollectorConfig
from football_cups.collector.http import ObservedResponse
from football_cups.collector.service import CollectorService
from football_cups.collector.storage import DataStore
from football_cups.collector.sporttery import (
    SPORTTERY_RESULT_PAGE_URL,
    accepted_mapping,
    audit_sporttery_evidence,
    load_mapping_identities,
    official_scope_present,
    parse_detail,
    parse_inventory,
    sporttery_fixed_bonus_url,
    sporttery_head_url,
    sporttery_inventory_url,
)
from football_cups.collector.timeutil import iso_utc


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
        headers={"content-type": "application/json; charset=utf-8"},
        content=content,
        request_started_at=at,
        response_received_at=at,
        source_encoding="utf-8",
    )


def payload(value):
    return json.dumps({"errorCode": "0", "value": value}, ensure_ascii=False).encode("utf-8")


def test_official_scope_inventory_detail_and_mapping_contract() -> None:
    scope = "<h1>竞彩足球赛果</h1><th>全场比分（90分钟）</th><p>全场比分（90分钟）包含伤停补时阶段</p>"
    assert official_scope_present(scope.encode("utf-8"))
    inventory = parse_inventory(
        payload(
            {
                "totalPage": 1,
                "matchResult": [
                    {
                        "sportteryMatchId": 2040541,
                        "matchNum": "周日104",
                        "matchDateTime": "2026-07-20 03:00",
                        "homeTeamShortName": "西班牙",
                        "awayTeamShortName": "阿根廷",
                        "fullCourtGoal": "0:0",
                        "matchResultStatusCn": "已开奖",
                        "isCancel": 0,
                    }
                ],
            }
        )
    )
    assert len(inventory) == 1
    assert inventory[0].score == (0, 0)
    fixture = {
        "fixture_id": "1359167",
        "match_number": "周日104",
        "kickoff_at": "2026-07-19T19:00:00Z",
        "home_team_name": "西班牙",
        "away_team_name": "阿根廷",
    }
    mapping = accepted_mapping(fixture, inventory)
    assert mapping.status == "accepted"

    detail = parse_detail(
        payload(
            {
                "sportteryMatchId": 2040541,
                "matchNum": "周日104",
                "matchDateTime": "2026-07-20 03:00",
                "homeTeamShortName": "西班牙",
                "awayTeamShortName": "阿根廷",
                "fullCourtGoal": "0:0",
            }
        ),
        payload({"matchId": 2040541, "sectionsNo999": "0:0", "isCancel": 0}),
    )
    assert detail.head_score == (0, 0)
    assert detail.fixed_score == (0, 0)


def test_sporttery_reconcile_dry_run_does_not_write_result_files(tmp_path, monkeypatch) -> None:
    config = config_for(tmp_path)
    kickoff = datetime(2026, 7, 19, 19, tzinfo=timezone.utc)
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    identity = {
        "fixture_id": "1359167",
        "competition_name": "世界杯",
        "competition_id": "101",
        "match_number": "周日104",
        "home_team_id": "1",
        "home_team_name": "西班牙",
        "away_team_id": "2",
        "away_team_name": "阿根廷",
        "kickoff_at": iso_utc(kickoff),
    }
    inventory_url = sporttery_inventory_url("2026-07-20", "2026-07-20", page_no=1, page_size=100)
    head_url = sporttery_head_url("2040541")
    fixed_url = sporttery_fixed_bonus_url("2040541")
    with CollectorService(config) as service:
        service.state.upsert_fixture(identity, now, identity_conflict=False)
        service.data.write_result(
            "verified",
            {
                "record_type": "VerifiedResult",
                "record_id": "manual-verified-1359167",
                "fixture_id": "1359167",
                "home_goals": 0,
                "away_goals": 0,
                "verification_method": "project-owner-manual-declaration",
            },
            now,
        )

        def fake_request(_method, url, **_kwargs):
            if url == SPORTTERY_RESULT_PAGE_URL:
                return response(
                    url,
                    "全场比分（90分钟）包含伤停补时阶段".encode("utf-8"),
                    now,
                )
            if url == inventory_url:
                return response(
                    url,
                    payload(
                        {
                            "totalPage": 1,
                            "matchResult": [
                                {
                                    "sportteryMatchId": 2040541,
                                    "matchNum": "周日104",
                                    "matchDateTime": "2026-07-20 03:00",
                                    "homeTeamShortName": "西班牙",
                                    "awayTeamShortName": "阿根廷",
                                    "fullCourtGoal": "0:0",
                                    "matchResultStatusCn": "已开奖",
                                    "isCancel": 0,
                                }
                            ],
                        }
                    ),
                    now,
                )
            if url == head_url:
                return response(
                    url,
                    payload(
                        {
                            "sportteryMatchId": 2040541,
                            "matchNum": "周日104",
                            "matchDateTime": "2026-07-20 03:00",
                            "homeTeamShortName": "西班牙",
                            "awayTeamShortName": "阿根廷",
                            "fullCourtGoal": "0:0",
                        }
                    ),
                    now,
                )
            if url == fixed_url:
                return response(url, payload({"matchId": 2040541, "sectionsNo999": "0:0", "isCancel": 0}), now)
            raise AssertionError(url)

        monkeypatch.setattr(service.http, "request", fake_request)
        result = service.reconcile_results(
            datetime(2026, 7, 19, tzinfo=timezone.utc),
            datetime(2026, 7, 21, tzinfo=timezone.utc),
            source="sporttery",
            apply=False,
        )

    assert result["counts"]["candidate"] == 1
    assert result["counts"]["verified"] == 1
    assert not list((config.data_dir / "results").rglob("candidates/*.json"))
    assert len(list((config.data_dir / "results").rglob("verified/*.json"))) == 1


def test_sporttery_reconcile_filters_requested_fixture(tmp_path, monkeypatch) -> None:
    config = config_for(tmp_path)
    kickoff = datetime(2026, 7, 19, 19, tzinfo=timezone.utc)
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    identities = (
        {
            "fixture_id": "1359167",
            "competition_name": "世界杯",
            "competition_id": "101",
            "match_number": "周日104",
            "home_team_id": "1",
            "home_team_name": "西班牙",
            "away_team_id": "2",
            "away_team_name": "阿根廷",
            "kickoff_at": iso_utc(kickoff),
        },
        {
            "fixture_id": "1359168",
            "competition_name": "世界杯",
            "competition_id": "101",
            "match_number": "周日105",
            "home_team_id": "3",
            "home_team_name": "法国",
            "away_team_id": "4",
            "away_team_name": "德国",
            "kickoff_at": iso_utc(kickoff),
        },
    )
    with CollectorService(config) as service:
        for identity in identities:
            service.state.upsert_fixture(identity, now, identity_conflict=False)
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_scope",
            lambda: ({"url": SPORTTERY_RESULT_PAGE_URL, "sha256": "a" * 64}, now),
        )
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_inventory",
            lambda *_args, **_kwargs: {
                "batch": {
                    "record_id": "batch-1",
                    "observed_at": iso_utc(now),
                    "complete": True,
                    "row_count": 0,
                },
                "rows": [],
                "inventory_blob": {"url": "https://example.invalid", "sha256": "b" * 64},
            },
        )
        result = service.reconcile_results(
            datetime(2026, 7, 19, tzinfo=timezone.utc),
            datetime(2026, 7, 21, tzinfo=timezone.utc),
            source="sporttery",
            apply=False,
            fixture_ids={"1359167"},
        )

    assert result["fixtures_queued"] == 1
    assert result["requested_fixture_ids"] == ["1359167"]
    assert result["counts"] == {"missing": 1}


def test_sporttery_reconcile_rechecks_manual_but_skips_automatic_results(
    tmp_path, monkeypatch
) -> None:
    config = config_for(tmp_path)
    kickoff = datetime(2026, 7, 19, 19, tzinfo=timezone.utc)
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    with CollectorService(config) as service:
        for fixture_id in ("1359167", "1359168"):
            service.state.upsert_fixture(
                {
                    "fixture_id": fixture_id,
                    "competition_name": "世界杯",
                    "competition_id": "101",
                    "match_number": "周日104",
                    "home_team_id": f"h-{fixture_id}",
                    "home_team_name": "Home",
                    "away_team_id": f"a-{fixture_id}",
                    "away_team_name": "Away",
                    "kickoff_at": iso_utc(kickoff),
                },
                now,
                identity_conflict=False,
            )
        for fixture_id, method in (
            ("1359167", "project-owner-manual-declaration"),
            ("1359168", "500-two-page-regular-time-competition"),
        ):
            service.data.write_result(
                "verified",
                {
                    "record_type": "VerifiedResult",
                    "record_id": f"verified-{fixture_id}",
                    "fixture_id": fixture_id,
                    "home_goals": 1,
                    "away_goals": 0,
                    "verification_method": method,
                },
                now,
            )
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_scope",
            lambda: ({"url": SPORTTERY_RESULT_PAGE_URL, "sha256": "a" * 64}, now),
        )
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_inventory",
            lambda *_args, **_kwargs: {
                "batch": {
                    "record_id": "batch-1",
                    "observed_at": iso_utc(now),
                    "complete": True,
                    "row_count": 0,
                },
                "rows": [],
                "inventory_blob": {"url": "https://example.invalid", "sha256": "b" * 64},
            },
        )
        result = service.reconcile_results(
            datetime(2026, 7, 19, tzinfo=timezone.utc),
            datetime(2026, 7, 21, tzinfo=timezone.utc),
            source="sporttery",
            apply=False,
        )

    assert result["fixtures_queued"] == 1
    assert result["counts"] == {"missing": 1}


def test_audit_sporttery_evidence_validates_complete_chain(tmp_path) -> None:
    config = config_for(tmp_path)
    config.ensure_directories()
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    records = (
        (
            "sporttery_inventory_batches",
            {
                "record_type": "SportteryInventoryBatch",
                "record_id": "batch-1",
                "complete": True,
            },
        ),
        (
            "sporttery_scope_evidence",
            {
                "record_type": "SportteryScopeEvidence",
                "record_id": "scope-1",
                "status": "accepted",
                "scope": "90-minutes-including-stoppage",
            },
        ),
        (
            "fixture_identities",
            {
                "record_type": "FixtureIdentity",
                "record_id": "identity-1",
                "fixture_id": "1359167",
            },
        ),
        (
            "sporttery_fixture_links",
            {
                "record_type": "SportteryFixtureLink",
                "record_id": "link-1",
                "fixture_id": "1359167",
                "mapping_status": "accepted",
                "source_fixture_identity_record_id": "identity-1",
            },
        ),
        (
            "sporttery_result_observations",
            {
                "record_type": "SportteryResultObservation",
                "record_id": "observation-1",
                "fixture_id": "1359167",
                "home_goals": 0,
                "away_goals": 0,
                "scope_evidence_record_id": "scope-1",
                "inventory_batch_record_id": "batch-1",
                "fixture_link_record_id": "link-1",
            },
        ),
        (
            "result_candidates",
            {
                "record_type": "ResultCandidate",
                "record_id": "candidate-1",
                "fixture_id": "1359167",
                "home_goals": 0,
                "away_goals": 0,
                "official_scope": "90-minutes-including-stoppage",
                "sporttery_result_observation_id": "observation-1",
                "sporttery_fixture_link_id": "link-1",
            },
        ),
        (
            "verified_results",
            {
                "record_type": "VerifiedResult",
                "record_id": "verified-1",
                "fixture_id": "1359167",
                "home_goals": 0,
                "away_goals": 0,
                "candidate_id": "candidate-1",
                "verification_method": "sporttery-official-90-minute",
            },
        ),
    )
    store = DataStore(config)
    for stream, record in records:
        store.append_normalized(stream, record, now)

    audit = audit_sporttery_evidence(config.data_dir, fixture_id="1359167")

    assert audit["status"] == "ok"
    assert audit["counts"]["official_verified_results"] == 1
    assert audit["errors"] == []


def test_scheduled_sporttery_reconciliation_is_rate_limited(tmp_path, monkeypatch) -> None:
    config = config_for(tmp_path)
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    calls = []
    with CollectorService(config) as service:
        monkeypatch.setattr(
            service,
            "reconcile_sporttery_results",
            lambda start, end, *, apply, fixture_ids=None: calls.append((start, end, apply))
            or {"status": "completed", "counts": {}, "fixtures_queued": 0},
        )
        first = service._scheduled_sporttery_reconciliation(now)
        second = service._scheduled_sporttery_reconciliation(now)

    assert first["status"] == "completed"
    assert second["status"] == "not_due"
    assert len(calls) == 1
    assert calls[0][2] is True


def test_mapping_identity_loader_prefers_latest_valid_record(tmp_path) -> None:
    config = config_for(tmp_path)
    config.ensure_directories()
    store = DataStore(config)
    first = datetime(2026, 7, 16, tzinfo=timezone.utc)
    second = datetime(2026, 7, 17, tzinfo=timezone.utc)
    valid = {
        "record_type": "FixtureIdentity",
        "record_id": "identity-valid",
        "fixture_id": "1359167",
        "observed_at": iso_utc(first),
        "match_number": "周日104",
        "kickoff_at": "2026-07-19T19:00:00Z",
        "home_team_name": "西班牙",
        "away_team_name": "阿根廷",
    }
    invalid = valid | {
        "record_id": "identity-invalid",
        "observed_at": iso_utc(second),
        "match_number": "��104",
        "home_team_name": "������",
    }
    store.append_normalized("fixture_identities", valid, first)
    store.append_normalized("fixture_identities", invalid, second)

    selected = load_mapping_identities(config.data_dir, {"1359167"})

    assert selected["1359167"]["record_id"] == "identity-valid"


def test_incomplete_sporttery_inventory_is_source_failure(tmp_path, monkeypatch) -> None:
    config = config_for(tmp_path)
    kickoff = datetime(2026, 7, 19, 19, tzinfo=timezone.utc)
    now = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    identity = {
        "fixture_id": "1359167",
        "competition_name": "世界杯",
        "competition_id": "101",
        "match_number": "周日104",
        "home_team_id": "1",
        "home_team_name": "西班牙",
        "away_team_id": "2",
        "away_team_name": "阿根廷",
        "kickoff_at": iso_utc(kickoff),
    }
    with CollectorService(config) as service:
        service.state.upsert_fixture(identity, now, identity_conflict=False)
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_scope",
            lambda: ({"url": SPORTTERY_RESULT_PAGE_URL, "sha256": "a" * 64}, now),
        )
        monkeypatch.setattr(
            service,
            "_fetch_sporttery_inventory",
            lambda *_args, **_kwargs: {
                "batch": {
                    "record_id": "batch-blocked",
                    "observed_at": iso_utc(now),
                    "complete": False,
                    "row_count": 0,
                },
                "rows": [],
                "inventory_blob": {"url": "https://example.invalid", "sha256": "b" * 64},
                "failure_reason": "blocked_response",
            },
        )
        result = service.reconcile_results(
            datetime(2026, 7, 19, tzinfo=timezone.utc),
            datetime(2026, 7, 21, tzinfo=timezone.utc),
            source="sporttery",
            apply=False,
        )

    assert result["status"] == "partial"
    assert result["counts"] == {"failure": 1}
    assert not list((config.data_dir / "normalized").rglob("sporttery_fixture_links.jsonl"))
