from __future__ import annotations

import json
import os
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from football_cups.research.k1_guardrail import (
    K1GuardrailError,
    assess_guardrail_features,
    build_guardrail_features,
    load_k1_guardrail_policy,
    verify_shadow_manifest,
    unavailable_assessment,
)
from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import apply_migrations, connect
from football_cups.research.database import import_research_files


ROOT = Path(__file__).resolve().parents[1]


def test_policy_rejects_active_status(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "config" / "research-k1-guardrail.json").read_text(encoding="utf-8"))
    payload["status"] = "active"
    config = tmp_path / "config"
    config.mkdir()
    (config / "research-k1-guardrail.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(K1GuardrailError, match="only accepts status=shadow"):
        load_k1_guardrail_policy(tmp_path)


def test_action_matrix_does_not_add_auxiliary_scores() -> None:
    policy = load_k1_guardrail_policy(ROOT)
    base = {
        "favorite_side": "home",
        "favorite_probability": 0.5,
        "prob_gap": 0.1,
        "current_favorite_line": 0.5,
        "delta_p_favorite_median": 0.0,
        "delta_favorite_odds_median": 0.0,
        "favorite_cooling_support_ratio": 0.0,
        "favorite_strengthening_support_ratio": 0.0,
        "alternative_strengthening_support_ratio": 0.0,
        "delta_favorite_line_median": 0.0,
        "asian_retreat_support_ratio": 0.0,
        "asian_not_strengthening_ratio": 0.0,
        "current_total_line": 2.0,
        "probabilities": {"home": 0.5, "draw": 0.3, "away": 0.2},
        "handicap_index_valid_bookmakers": 3,
        "handicap_index_conflict_support_ratio": 0.7,
        "live_observation_count": 1,
        "live_observation_span_seconds": 0,
        "bookmaker_dispersion": 0.04,
    }
    auxiliary = assess_guardrail_features(base, policy)
    assert auxiliary["proposed_action"] == "caution"
    assert set(auxiliary["rule_flags"]) == {
        "r3_low_total_draw_tail",
        "r4_handicap_cover_conflict",
        "r6_bookmaker_dispersion",
    }
    primary = dict(base)
    primary.update(
        delta_p_favorite_median=-0.02,
        delta_favorite_odds_median=0.04,
        favorite_cooling_support_ratio=2 / 3,
        alternative_strengthening_support_ratio=2 / 3,
    )
    result = assess_guardrail_features(primary, policy)
    assert result["proposed_action"] == "abstain"
    assert "r1_shallow_favorite_cooling" in result["rule_flags"]


def _market_row(name: str, index: int, market: str) -> dict:
    common = {
        "record_id": f"{market}-{index}",
        "source_bookmaker_name": name,
        "source_row_index": index,
    }
    if market == "ouzhi":
        return {
            **common,
            "opening_home": 2.0,
            "opening_draw": 3.4,
            "opening_away": 4.0,
            "current_home": 2.1,
            "current_draw": 3.2,
            "current_away": 3.9,
        }
    if market == "yazhi":
        return {
            **common,
            "opening_home": 0.9,
            "opening_line": -0.5,
            "opening_away": 0.9,
            "current_home": 0.95,
            "current_line": -0.25,
            "current_away": 0.85,
        }
    return {
        **common,
        "opening_over": 0.9,
        "opening_line": 2.5,
        "opening_under": 0.9,
        "current_over": 0.95,
        "current_line": 2.25,
        "current_under": 0.85,
    }


def test_features_pair_same_company_and_detect_conflicting_duplicate() -> None:
    policy = load_k1_guardrail_policy(ROOT)
    rows = {
        market: [_market_row(name, index, market) for index, name in enumerate(("A", "B", "C"), 1)]
        for market in ("ouzhi", "yazhi", "daxiao")
    }
    features, reasons = build_guardrail_features(rows, [], policy)
    assert reasons == []
    assert features["paired_bookmaker_count"] == 3
    assert features["live_observation_count"] == 1
    assert features["handicap_index_valid_bookmakers"] == 0

    duplicate = dict(rows["ouzhi"][0])
    duplicate["record_id"] = "conflict"
    duplicate["current_home"] = 9.0
    rows["ouzhi"].append(duplicate)
    _, reasons = build_guardrail_features(rows, [], policy)
    assert "duplicate_company_conflict:ouzhi:A" in reasons


def test_handicap_cover_mapping_uses_favorite_direction() -> None:
    policy = load_k1_guardrail_policy(ROOT)
    rows = {
        market: [_market_row(name, index, market) for index, name in enumerate(("A", "B", "C"), 1)]
        for market in ("ouzhi", "yazhi", "daxiao")
    }
    handicap = [
        {
            "record_id": f"rangqiu-{index}",
            "source_bookmaker_name": name,
            "source_row_index": index,
            "handicap_line": -1,
            "home_probability": 40,
            "draw_probability": 30,
            "away_probability": 30,
        }
        for index, name in enumerate(("A", "B", "C"), 1)
    ]
    features, reasons = build_guardrail_features(rows, handicap, policy)
    assert reasons == []
    assert features["favorite_side"] == "home"
    assert features["handicap_index_valid_bookmakers"] == 3
    assert features["handicap_index_conflict_support_ratio"] == 1.0


def test_manifest_gate_accepts_legacy_and_rejects_incomplete_guardrail(tmp_path: Path) -> None:
    research = tmp_path / "data" / "research"
    record_dir = research / "normalized" / "shadow-predictions" / "run"
    manifest_dir = research / "manifests" / "run"
    record_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    record_path = record_dir / "shadow-predictions.jsonl"
    prediction = {"record_type": "ResearchShadowPrediction"}
    record_path.write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    manifest = {
        "status": "completed",
        "record_path": record_path.relative_to(research).as_posix(),
    }
    (manifest_dir / "shadow-predictions.json").write_text(json.dumps(manifest), encoding="utf-8")
    verify_shadow_manifest(research, record_path)

    assessment = {"record_type": "ResearchK1GuardrailAssessment"}
    record_path.write_text(json.dumps(prediction) + "\n" + json.dumps(assessment) + "\n", encoding="utf-8")
    with pytest.raises(K1GuardrailError, match="manifest mismatch"):
        verify_shadow_manifest(research, record_path)


def test_manifest_gate_rejects_missing_manifest(tmp_path: Path) -> None:
    research = tmp_path / "data" / "research"
    record_dir = research / "normalized" / "shadow-predictions" / "run"
    record_dir.mkdir(parents=True)
    record_path = record_dir / "shadow-predictions.jsonl"
    record_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(K1GuardrailError, match="lacks completed manifest"):
        verify_shadow_manifest(research, record_path)


def test_k1_assessment_postgres_import_is_idempotent(tmp_path: Path) -> None:
    database_url = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE_URL")
    local_test = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE") == "1"
    if not database_url and not local_test:
        pytest.skip("PostgreSQL integration test is not configured")
    policy = load_k1_guardrail_policy(ROOT)
    prediction = {
        "schema_version": 1,
        "record_type": "ResearchShadowPrediction",
        "record_id": "k1-shadow",
        "research_only": True,
        "research_kind": "shadow_event",
        "backfill": False,
        "strict_backtest_eligible": False,
        "cutoff_eligible": False,
        "channel": "research-shadow-v1",
        "fixture_id": "k1-fixture",
        "target": "T-6h",
        "prediction_cutoff": "2026-07-22T06:00:00Z",
        "published_at": "2026-07-22T06:01:00Z",
        "status": "published",
        "model_key": "devig-consensus-v1",
        "model_version": "test",
        "activation_record_id": None,
        "selected_batch_record_id": "batch",
        "source_snapshot_record_id": "snapshot",
        "market_observed_at": "2026-07-22T05:59:00Z",
        "bookmaker_count": 3,
        "probabilities": {"home": 0.5, "draw": 0.3, "away": 0.2, "sum": 1.0},
        "features": {},
        "abstention_reason": None,
        "competition_id": "16",
        "competition_name": "K1",
        "competition_type": "domestic_league",
        "market_evidence_tier": "B",
        "evaluation_group": "korea-k1",
        "classification_status": "provisional",
        "registry_version": "competition-profile-v1",
        "policy_version": "shadow-confidence-v1",
        "registry_file_sha256": "a" * 64,
        "registry_canonical_sha256": "b" * 64,
        "direction_strength": 0.2,
        "bookmaker_dispersion": 0.01,
        "raw_confidence_label": "medium",
        "competition_confidence_cap": "medium",
        "confidence_label": "medium",
        "confidence_reasons": [],
        "risk_flags": [],
        "identity_record_id": "identity",
        "identity_observed_at": "2026-07-21T00:00:00Z",
        "automatic_verified_fixture_count": 0,
        "evaluation_span_days": 0.0,
        "review_eligible": False,
    }
    assessment = unavailable_assessment(
        prediction,
        policy,
        datetime(2026, 7, 22, 6, 1, tzinfo=UTC),
        "relevant_source_not_reproducible",
        {
            "git_commit": "c" * 40,
            "relevant_source_tree_sha256": "d" * 64,
            "relevant_dirty_paths": ["src/football_cups/research/k1_guardrail.py"],
        },
    )
    research = tmp_path / "data" / "research"
    normalized = research / "normalized" / "shadow-predictions" / "run"
    manifest_dir = research / "manifests" / "run"
    normalized.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    record_path = normalized / "shadow-predictions.jsonl"
    content = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in (prediction, assessment))
    record_path.write_text(content, encoding="utf-8")
    raw = record_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "run_id": "run",
        "status": "completed",
        "record_path": record_path.relative_to(research).as_posix(),
        "record_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "line_count": 2,
        "prediction_count": 1,
        "assessment_count": 1,
        "policy_version": policy.policy_version,
    }
    (manifest_dir / "shadow-predictions.json").write_text(json.dumps(manifest), encoding="utf-8")
    database_config = DatabaseConfig(tmp_path, tmp_path / "data" / "500", database_url)
    with connect(database_config, autocommit=True) as connection:
        if not connection.info.dbname.endswith("_test"):
            pytest.fail("integration test database name must end with _test")
        connection.execute("DROP SCHEMA IF EXISTS research CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS football CASCADE")
    with connect(database_config) as connection:
        apply_migrations(connection)
        first = import_research_files(connection, research / "normalized")
        second = import_research_files(connection, research / "normalized")
        count = connection.execute("SELECT count(*) AS count FROM research.k1_guardrail_assessments").fetchone()["count"]
        football_count = connection.execute("SELECT count(*) AS count FROM football.records").fetchone()["count"]
    assert first["records_inserted"] == 2
    assert second["records_inserted"] == 0
    assert count == 1
    assert football_count == 0
