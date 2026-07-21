from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from football_cups.research.competition_profiles import (
    confidence_assessment,
    load_competition_registry,
    market_statistics,
)
from football_cups.research.database import ResearchImportIntegrityError, _validate
from football_cups.research.modeling import _identity_as_of


ROOT = Path(__file__).resolve().parents[1]


def _workspace_with_registry(tmp_path: Path, payload: dict, *, compact: bool = False) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    text = (
        json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
        if compact
        else json.dumps(payload, ensure_ascii=False, indent=2)
    )
    (config_dir / "research-competition-profiles.json").write_text(text, encoding="utf-8")
    return tmp_path


def _registry_payload() -> dict:
    return json.loads((ROOT / "config" / "research-competition-profiles.json").read_text(encoding="utf-8"))


def test_registry_resolves_id_alias_conflict_and_unknown() -> None:
    registry = load_competition_registry(ROOT)
    assert registry.resolve("5", "芬兰超级联赛").market_evidence_tier == "C"
    assert registry.resolve(None, "  芬超  ").competition_id == "5"
    assert registry.resolve("5", "瑞超").conflict is True
    assert registry.resolve("999", "芬超").unregistered is True


def test_canonical_hash_ignores_json_formatting(tmp_path: Path) -> None:
    payload = _registry_payload()
    first = load_competition_registry(_workspace_with_registry(tmp_path / "first", payload))
    second = load_competition_registry(
        _workspace_with_registry(tmp_path / "second", payload, compact=True)
    )
    assert first.file_sha256 != second.file_sha256
    assert first.canonical_sha256 == second.canonical_sha256


def test_market_statistics_uses_component_median_and_mad() -> None:
    probabilities, margin, dispersion = market_statistics(
        [(0.6, 0.25, 0.15), (0.58, 0.27, 0.15), (0.62, 0.23, 0.15)]
    )
    assert probabilities == pytest.approx((0.6, 0.25, 0.15))
    assert margin == pytest.approx(0.35)
    assert dispersion == pytest.approx(0.02)


def test_c_tier_preserves_probabilities_and_caps_confidence() -> None:
    registry = load_competition_registry(ROOT)
    profile = registry.resolve("5", "芬兰超级联赛")
    probabilities = (0.62, 0.2, 0.18)
    result = confidence_assessment(
        registry,
        profile,
        probabilities,
        bookmaker_count=12,
        direction_strength=0.42,
        bookmaker_dispersion=0.01,
        automatic_verified_fixtures=500,
        evaluation_span_days=180,
        competition_format="regular_time_only",
    )
    assert probabilities == (0.62, 0.2, 0.18)
    assert result["raw_confidence_label"] == "high"
    assert result["confidence_label"] == "low"
    assert "low_market_evidence_tier" in result["risk_flags"]
    assert "strong_favorite_draw_tail" in result["risk_flags"]


def test_provisional_and_sample_gate_prevent_high_confidence() -> None:
    registry = load_competition_registry(ROOT)
    profile = registry.resolve("101", "欧冠")
    result = confidence_assessment(
        registry,
        profile,
        (0.7, 0.18, 0.12),
        bookmaker_count=10,
        direction_strength=0.52,
        bookmaker_dispersion=0.01,
        automatic_verified_fixtures=0,
        evaluation_span_days=0,
        competition_format="may_have_extra_time",
    )
    assert result["raw_confidence_label"] == "high"
    assert result["confidence_label"] == "medium"
    assert "small_competition_evaluation_sample" in result["risk_flags"]
    assert "result_scope_verification_risk" in result["risk_flags"]


def test_reviewed_a_tier_can_reach_high_after_same_target_gate() -> None:
    registry = load_competition_registry(ROOT)
    profile = replace(registry.resolve("101", "欧冠"), classification_status="reviewed")
    result = confidence_assessment(
        registry,
        profile,
        (0.72, 0.16, 0.12),
        bookmaker_count=10,
        direction_strength=0.56,
        bookmaker_dispersion=0.01,
        automatic_verified_fixtures=200,
        evaluation_span_days=90,
        competition_format="may_have_extra_time",
    )
    assert result["confidence_label"] == "high"
    assert result["review_eligible"] is True


def test_new_shadow_record_rejects_invalid_registry_hash() -> None:
    record = {
        "schema_version": 1,
        "record_type": "ResearchShadowPrediction",
        "record_id": "shadow-record",
        "research_only": True,
        "backfill": False,
        "strict_backtest_eligible": False,
        "cutoff_eligible": False,
        "research_kind": "shadow_event",
        "policy_version": "shadow-confidence-v1",
        "competition_type": "domestic_league",
        "market_evidence_tier": "B",
        "evaluation_group": "test",
        "classification_status": "provisional",
        "registry_version": "competition-profile-v1",
        "registry_file_sha256": "bad",
        "registry_canonical_sha256": "a" * 64,
        "raw_confidence_label": "medium",
        "competition_confidence_cap": "medium",
        "confidence_label": "medium",
        "confidence_reasons": [],
        "risk_flags": [],
        "status": "published",
        "identity_record_id": "identity",
    }
    with pytest.raises(ResearchImportIntegrityError, match="SHA-256"):
        _validate(record, "records.jsonl", 1)


def test_identity_query_uses_identity_own_prediction_cutoff() -> None:
    class Result:
        @staticmethod
        def fetchone():
            return None

    class Connection:
        @staticmethod
        def execute(sql, params):
            assert "observed_at <= kickoff_at - %s" in sql
            assert params == ("fixture", timedelta(hours=6))
            return Result()

    assert _identity_as_of(Connection(), "fixture", "T-6h") is None
