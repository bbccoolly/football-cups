from __future__ import annotations

from datetime import datetime, timedelta, timezone

from football_cups.collector.config import CollectorConfig
from football_cups.collector.http import ObservedResponse
from football_cups.collector.state import StateStore
from football_cups.collector.storage import DataStore
from football_cups.collector.timeutil import iso_utc
from football_cups.collector.service import rebuild_state


def config_for(tmp_path):
    return CollectorConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data" / "500",
        backup_dir=None,
    )


def test_blob_store_deduplicates_content_but_manifests_are_unique(tmp_path) -> None:
    config = config_for(tmp_path)
    store = DataStore(config)
    observed = datetime(2026, 7, 15, tzinfo=timezone.utc)
    response = ObservedResponse(
        method="GET",
        url="https://example.test/page",
        status_code=200,
        headers={"content-type": "text/html"},
        content=b"<html>same</html>",
        request_started_at=observed,
        response_received_at=observed,
        source_encoding="utf-8",
    )
    first = store.store_response(response, default_extension="html")
    second = store.store_response(response, default_extension="html")
    assert first["path"] == second["path"]
    assert len(list((config.data_dir / "raw" / "blobs").rglob("*.html"))) == 1

    store.write_manifest("test", "run-one", {"value": 1}, observed)
    store.write_manifest("test", "run-two", {"value": 1}, observed)
    assert len(list((config.data_dir / "manifests").rglob("*.json"))) == 2


def test_scheduler_marks_old_cutoffs_and_versions_kickoff_changes(tmp_path) -> None:
    config = config_for(tmp_path)
    now = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    identity = {
        "fixture_id": "123",
        "competition_name": "League",
        "competition_id": "30",
        "home_team_id": "10",
        "away_team_id": "20",
        "kickoff_at": iso_utc(now + timedelta(hours=25)),
        "buy_end_at": None,
    }
    with StateStore(config) as state:
        assert state.upsert_fixture(identity, now, identity_conflict=False) == "new"
        state.schedule_fixture(identity, now, is_new=True)
        statuses = {
            row["target"]: row["status"]
            for row in state.connection.execute("SELECT target, status FROM jobs WHERE job_type='market'")
        }
        assert statuses["T-48h"] == "missed_before_discovery"
        assert statuses["T-24h"] == "pending"
        assert statuses["first_seen"] == "pending"

        changed = identity | {"kickoff_at": iso_utc(now + timedelta(hours=26))}
        assert state.upsert_fixture(changed, now + timedelta(minutes=1), identity_conflict=False) == "kickoff_changed"
        state.schedule_fixture(changed, now + timedelta(minutes=1), is_new=False)
        superseded = state.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='superseded'"
        ).fetchone()[0]
        assert superseded > 0


def test_record_claim_is_idempotent(tmp_path) -> None:
    config = config_for(tmp_path)
    now = datetime.now(timezone.utc)
    with StateStore(config) as state:
        assert state.claim_record("same", "Test", now)
        assert not state.claim_record("same", "Test", now)


def test_state_rebuild_uses_discovery_file_facts(tmp_path) -> None:
    config = config_for(tmp_path)
    store = DataStore(config)
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    identity = {
        "fixture_id": "123",
        "competition_name": "League",
        "competition_id": "30",
        "home_team_id": "10",
        "away_team_id": "20",
        "kickoff_at": iso_utc(now + timedelta(days=2)),
        "buy_end_at": None,
    }
    store.write_discovery_summary(
        "rebuild-source",
        {
            "finished_at": iso_utc(now),
            "fixtures": [identity],
            "identity_conflicts": {},
        },
        now,
    )
    with StateStore(config):
        pass
    result = rebuild_state(config)
    assert result["manifests_processed"] == 1
    assert result["fixtures_rebuilt"] == 1
    assert result["previous_state_backup"] is not None
    with StateStore(config) as state:
        assert state.all_fixtures()[0]["fixture_id"] == "123"
