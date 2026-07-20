from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from football_cups.collector.config import CollectorConfig
from football_cups.collector.reparse import collect_market_reparse, publish_market_reparse
from football_cups.collector.state import StateStore
from football_cups.database.importer import AppendOnlyViolation, _validate_repair_run


def config_for(tmp_path) -> CollectorConfig:
    return CollectorConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data" / "500",
        backup_dir=None,
        oss_backup_dir=None,
    )


def test_empty_reparse_is_deterministic_and_atomically_completed(tmp_path) -> None:
    config = config_for(tmp_path)
    with StateStore(config):
        pass
    start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    end = datetime(2026, 7, 16, tzinfo=timezone.utc)
    records, dry_run, repair_id = collect_market_reparse(config, start=start, end=end)
    assert dry_run["network_requests"] == 0
    assert all(not values for values in records.values())

    first = publish_market_reparse(config, start=start, end=end)
    run_dir = config.data_dir / "normalized" / "repairs" / repair_id
    assert first["status"] == "completed"
    assert run_dir.is_dir()
    assert Path(first["report_markdown"]).is_file()
    assert not (config.data_dir / "state" / "reparse-staging" / repair_id).exists()
    _validate_repair_run(run_dir)

    second = publish_market_reparse(config, start=start, end=end)
    assert second["status"] == "unchanged"


def test_repair_validation_rejects_changed_jsonl(tmp_path) -> None:
    config = config_for(tmp_path)
    with StateStore(config):
        pass
    start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    end = datetime(2026, 7, 16, tzinfo=timezone.utc)
    result = publish_market_reparse(config, start=start, end=end)
    run_dir = config.data_dir / "normalized" / "repairs" / result["repair_id"]
    target = run_dir / "market_normalizations.jsonl"
    target.write_text("changed\n", encoding="utf-8")
    with pytest.raises(AppendOnlyViolation):
        _validate_repair_run(run_dir)
    with pytest.raises(ValueError):
        publish_market_reparse(config, start=start, end=end)


def test_reparse_emits_one_assessment_for_retried_snapshot_batch(tmp_path) -> None:
    config = config_for(tmp_path)
    with StateStore(config):
        pass
    manifest_dir = config.data_dir / "manifests" / "2026" / "07" / "15"
    manifest_dir.mkdir(parents=True)
    batch = {
        "record_id": "batch-record-1",
        "fixture_id": "1234567",
        "target": "T-24h",
        "strict_eligible": False,
        "completed_at": "2026-07-15T01:00:00Z",
        "market_results": {},
    }
    for index, completed_at in enumerate(
        ("2026-07-15T01:00:00Z", "2026-07-15T01:05:00Z"), start=1
    ):
        payload = {
            "schema_version": 1,
            "record_type": "MarketCaptureManifest",
            "batch": batch | {"completed_at": completed_at},
            "captures": [],
        }
        (manifest_dir / f"retry-{index}-market.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    (manifest_dir / "live-v2-market.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "MarketCaptureManifest",
                "batch": batch | {"record_id": "live-v2-batch"},
                "captures": [],
                "eligibility_assessment": {"assessment_version": 2},
            }
        ),
        encoding="utf-8",
    )

    records, summary, _ = collect_market_reparse(
        config,
        start=datetime(2026, 7, 15, tzinfo=timezone.utc),
        end=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assessments = records["snapshot_eligibility_assessments"]
    assert len(assessments) == 1
    assert assessments[0]["snapshot_batch_record_id"] == "batch-record-1"
    assert summary["counts"]["assessments"] == 1
    assert summary["manifests_seen"] == 2
