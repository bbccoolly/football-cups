from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import apply_migrations, connect
from football_cups.research.config import ResearchConfig
from football_cups.research.database import ResearchImportIntegrityError, import_research_files
from football_cups.research.europe_guardrail import (
    _bookmaker_keys,
    _unavailable_record,
    assess_europe_features,
    build_europe_features,
    load_europe_guardrail_policy,
    normalize_bookmaker_name,
    validate_europe_assessment_record,
)
from football_cups.research.k1_guardrail import verify_shadow_manifest
from football_cups.research.storage import ResearchStore


ROOT = Path(__file__).resolve().parents[1]


def _row(name: str, index: int, market: str) -> dict:
    common = {
        "record_id": f"{market}-{name}-{index}",
        "source_bookmaker_name": name,
        "source_bookmaker_id": None,
        "source_row_index": index,
    }
    if market == "ouzhi":
        return common | {
            "opening_home": 2.0, "opening_draw": 3.2, "opening_away": 4.0,
            "current_home": 1.9, "current_draw": 3.3, "current_away": 4.2,
        }
    if market == "yazhi":
        return common | {
            "opening_home": 0.92, "opening_line": -0.5, "opening_away": 0.92,
            "current_home": 0.88, "current_line": -0.75, "current_away": 0.98,
        }
    return common | {
        "opening_over": 0.92, "opening_line": 2.5, "opening_under": 0.92,
        "current_over": 0.88, "current_line": 2.75, "current_under": 0.98,
    }


def _selected(count: int = 5) -> dict[str, dict[str, dict]]:
    return {
        market: {
            f"name:{name}": _row(name, index, market)
            for index, name in enumerate(("A", "B", "C", "D", "E")[:count], start=1)
        }
        for market in ("ouzhi", "yazhi", "daxiao")
    }


def _trajectory() -> dict:
    return {
        "observation_count": 3,
        "observation_span_seconds": 1800,
        "response_hashes": ["a", "b", "c"],
        "company_states": {},
    }


def test_policy_and_bookmaker_name_contract() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    assert policy.competition_ids == ("63", "101")
    assert policy.targets == ("T-24h", "T-6h", "T-60m", "T-10m")
    assert normalize_bookmaker_name("  A\u00a0  Book  ") == "A Book"


def test_bookmaker_id_name_conflict_excludes_all_affected_rows() -> None:
    rows = {
        "ouzhi": [
            {"source_bookmaker_id": "10", "source_bookmaker_name": "Alpha"},
            {"source_bookmaker_id": "10", "source_bookmaker_name": "Alpha Alt"},
        ],
        "yazhi": [{"source_bookmaker_id": None, "source_bookmaker_name": "Alpha"}],
    }
    keys, warnings = _bookmaker_keys(rows)
    assert keys == {}
    assert "bookmaker_id_maps_multiple_names:10" in warnings


def test_features_use_equal_weight_consensus_and_same_company_rows() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    features, hard = build_europe_features(_selected(), _trajectory(), policy)
    assert hard == []
    assert features["base_direction"] == "home"
    assert features["paired_bookmaker_count"] == 5
    assert features["euro_support_ratio"] == 1.0
    assert features["asian_confirm_ratio"] == 1.0
    assert set(features["institution_details"]) == {"name:A", "name:B", "name:C", "name:D", "name:E"}
    result = assess_europe_features(features, policy, hard)
    assert result["proposed_action"] == "keep"


def test_anomaly_and_leave_one_out_do_not_delete_institution() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    selected = _selected()
    selected["ouzhi"]["name:E"] = _row("E", 5, "ouzhi") | {
        "current_home": 7.0, "current_draw": 4.0, "current_away": 1.2,
    }
    features, hard = build_europe_features(selected, _trajectory(), policy)
    assert hard == []
    assert "name:E" in features["institution_details"]
    assert features["institution_details"]["name:E"]["source_row_record_ids"]
    result = assess_europe_features(features, policy, hard)
    assert result["proposed_action"] in {"downgrade", "abstain"}
    assert result["rule_evaluations"]["r8_institution_anomaly"]["status"] == "matched"


def test_trajectory_unchanged_and_one_off_spike_are_distinct_evidence() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    unchanged, hard = build_europe_features(
        _selected(), _trajectory() | {"company_states": {"name:A": ["unchanged_confirmed"]}}, policy
    )
    assert hard == []
    unchanged_result = assess_europe_features(unchanged, policy, hard)
    assert unchanged_result["proposed_action"] == "caution"
    assert unchanged_result["rule_evaluations"]["r10_unchanged_evidence"]["status"] == "matched"

    spike, hard = build_europe_features(
        _selected(), _trajectory() | {"company_states": {"name:A": ["one_off_spike"]}}, policy
    )
    spike_result = assess_europe_features(spike, policy, hard)
    assert spike_result["proposed_action"] == "downgrade"
    assert spike_result["rule_evaluations"]["r8_institution_anomaly"]["status"] == "matched"


def test_draw_direction_is_never_kept() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    features = {
        "base_probabilities": {"home": 0.3, "draw": 0.4, "away": 0.3},
        "base_direction": "draw",
        "direction_gap": 0.1,
        "bookmaker_dispersion": 0.01,
        "paired_bookmaker_count": 5,
        "euro_support_ratio": 0.8,
        "euro_oppose_ratio": 0.0,
        "asian_confirm_ratio": 0.0,
        "asian_conflict_ratio": 0.0,
        "total_line_delta_median": 0.0,
        "institution_state_counts": {},
        "leave_one_out_direction_flip": False,
        "leave_one_out_max_probability_shift": 0.01,
        "trajectory": _trajectory(),
    }
    result = assess_europe_features(features, policy, [])
    assert result["proposed_action"] == "caution"
    assert result["rule_evaluations"]["r3_asian_confirmation"]["status"] == "not_evaluable"


def test_hard_data_failure_abstains() -> None:
    policy = load_europe_guardrail_policy(ROOT)
    result = assess_europe_features({"base_probabilities": {}}, policy, ["missing_snapshot:ouzhi"])
    assert result["proposed_action"] == "abstain"
    assert result["proposed_confidence_cap"] == "observation_only"


def test_completed_manifest_counts_europe_assessments(tmp_path: Path) -> None:
    config = ResearchConfig(tmp_path, tmp_path / "data" / "research")
    path = ResearchStore(config).write_completed_shadow_batch(
        run_id="europe",
        records=[
            {"record_type": "ResearchEuropeGuardrailAssessment", "record_id": "assessment", "channel": "channel"}
        ],
        manifest_fields={
            "prediction_count": 0,
            "assessment_count": 0,
            "europe_assessment_count": 1,
            "policy_version": "europe-market-guardrail-shadow-v1",
        },
    )
    verify_shadow_manifest(config.research_dir, path)


def _unavailable_assessment() -> dict:
    policy = load_europe_guardrail_policy(ROOT)
    cutoff = policy.effective_at + timedelta(hours=1)
    record = _unavailable_record(
        channel="research-europe-guardrail-v1",
        fixture_id="europe-fixture",
        competition_id="63",
        target="T-60m",
        cutoff=cutoff,
        assessed_at=cutoff + timedelta(minutes=1),
        policy=policy,
        identity_record_id="identity",
        batch_record_id=None,
        fingerprint={
            "git_commit": "a" * 40,
            "relevant_source_tree_sha256": "b" * 64,
            "relevant_dirty_paths": ["src/football_cups/research/europe_guardrail.py"],
        },
        reason="relevant_source_not_reproducible",
    )
    validate_europe_assessment_record(record)
    return record


def test_europe_assessment_postgres_import_is_idempotent_and_immutable(tmp_path: Path) -> None:
    database_url = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE_URL")
    local_test = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE") == "1"
    if not database_url and not local_test:
        pytest.skip("PostgreSQL integration test is not configured")
    config = ResearchConfig(tmp_path, tmp_path / "data" / "research")
    assessment = _unavailable_assessment()
    first_path = ResearchStore(config).write_completed_shadow_batch(
        run_id="europe-first",
        records=[assessment],
        manifest_fields={
            "prediction_count": 0,
            "assessment_count": 0,
            "europe_assessment_count": 1,
            "policy_version": assessment["policy_version"],
        },
    )
    database_config = DatabaseConfig(tmp_path, tmp_path / "data" / "500", database_url)
    with connect(database_config, autocommit=True) as connection:
        if not connection.info.dbname.endswith("_test"):
            pytest.fail("integration test database name must end with _test")
        connection.execute("DROP SCHEMA IF EXISTS research CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS football CASCADE")
    with connect(database_config) as connection:
        apply_migrations(connection)
        before = connection.execute("SELECT count(*) AS count FROM football.records").fetchone()["count"]
        first = import_research_files(connection, config.normalized_dir)
        second = import_research_files(connection, config.normalized_dir)
        after = connection.execute("SELECT count(*) AS count FROM football.records").fetchone()["count"]
        count = connection.execute(
            "SELECT count(*) AS count FROM research.europe_guardrail_assessments"
        ).fetchone()["count"]
        assert first_path.exists()
        assert first["records_inserted"] == 1
        assert second["records_inserted"] == 0
        assert count == 1
        assert before == after == 0

        conflicting = dict(assessment, reasons=["changed"])
        ResearchStore(config).write_completed_shadow_batch(
            run_id="europe-conflict",
            records=[conflicting],
            manifest_fields={
                "prediction_count": 0,
                "assessment_count": 0,
                "europe_assessment_count": 1,
                "policy_version": assessment["policy_version"],
            },
        )
        with pytest.raises(ResearchImportIntegrityError, match="immutable guardrail assessment payload changed"):
            import_research_files(connection, config.normalized_dir)
    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["europe_assessment_count"] == 1
