from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from football_cups.collector.markets import market_row_role
from football_cups.collector.config import CUTOFFS
from football_cups.collector.results import load_competition_formats

from .competition_profiles import (
    confidence_assessment,
    load_competition_registry,
    market_statistics,
    valid_sha256,
)
from .config import ResearchConfig
from .k1_analysis_workflow import (
    PREVIOUS_TARGET,
    UPDATE_COMPONENTS,
    K1AnalysisWorkflow,
    canonical_market_fingerprint,
    classify_market_update,
    direction_strength_label,
    load_k1_analysis_workflow,
    probability_delta,
    sample_maturity,
)
from .storage import ResearchStore, research_facts_lock, stable_id


K1_DATASET_SHA256 = "e26210d45df9d691bb81b68c078d494705ddb0aadad73ebc1faae4de36b7a931"
K1_METADATA_SHA256 = "6e7452951c098e30afd47ea2cca729c94b9fe4609011e463ff0e5d3add20d710"
K1_INPUT_HASH = "6285cc00625cb1675881c4c8ec41e8d8938ca5402371d95902809bc3b3344455"
TARGETS = frozenset({"T-24h", "T-6h", "T-60m", "T-10m"})
ACTIONS = frozenset({"keep", "caution", "downgrade", "abstain"})
RULE_STATES = frozenset({"matched", "not_matched", "not_evaluable"})
RELEVANT_PATHS = (
    "src/football_cups/research/k1_guardrail.py",
    "src/football_cups/research/k1_analysis_workflow.py",
    "src/football_cups/research/modeling.py",
    "src/football_cups/research/competition_profiles.py",
    "src/football_cups/database/migrations/014_research_k1_guardrail_assessments.sql",
    "config/research-k1-guardrail.json",
    "config/research-k1-analysis-workflow.json",
)


class K1GuardrailError(ValueError):
    pass


@dataclass(frozen=True)
class K1GuardrailPolicy:
    path: Path
    payload: dict[str, Any]
    policy_version: str
    policy_revision: int
    status: str
    effective_at: datetime
    competition_id: str
    targets: tuple[str, ...]
    thresholds: dict[str, float | int]
    forward_gate: dict[str, float | int]
    input_policy: dict[str, Any]
    presentation_policy: dict[str, Any]
    file_sha256: str
    canonical_sha256: str


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise K1GuardrailError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise K1GuardrailError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _number(block: Mapping[str, Any], name: str, *, minimum: float, maximum: float | None = None) -> float:
    value = block.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise K1GuardrailError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise K1GuardrailError(f"{name} is outside its allowed range")
    return result


def load_k1_guardrail_policy(workspace: Path) -> K1GuardrailPolicy:
    path = workspace.resolve() / "config" / "research-k1-guardrail.json"
    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise K1GuardrailError(f"invalid K1 guardrail policy: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise K1GuardrailError("unsupported K1 guardrail policy schema")
    version = str(payload.get("policy_version") or "").strip()
    revision = payload.get("policy_revision")
    status = str(payload.get("status") or "").strip()
    competition_id = str(payload.get("competition_id") or "").strip()
    targets = payload.get("targets")
    if not version or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise K1GuardrailError("policy version and positive integer revision are required")
    if status != "shadow":
        raise K1GuardrailError("K1 guardrail v1 only accepts status=shadow")
    if competition_id != "16":
        raise K1GuardrailError("K1 guardrail competition_id must be 16")
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)):
        raise K1GuardrailError("guardrail targets must be a non-empty unique list")
    if any(target not in TARGETS for target in targets):
        raise K1GuardrailError("guardrail targets contain an unsupported cutoff")
    thresholds = payload.get("thresholds")
    gate = payload.get("forward_gate")
    if not isinstance(thresholds, dict) or not isinstance(gate, dict):
        raise K1GuardrailError("thresholds and forward_gate are required")
    input_policy = payload.get("input_policy")
    presentation = payload.get("presentation_policy")
    expected_input = {
        "opening_source": "provider_declared_opening_from_selected_v2_row",
        "close_source": "selected_v2_row_current",
        "close_semantics": "as_of_cutoff_current",
        "batch_selection": "latest_model_eligible_batch_at_or_before_cutoff",
        "cross_target_mixing": False,
        "cross_batch_market_mixing": False,
    }
    if input_policy != expected_input:
        raise K1GuardrailError("invalid K1 guardrail input policy")
    if not isinstance(presentation, dict) or presentation.get("version") != "k1-guardrail-presentation-v1":
        raise K1GuardrailError("invalid K1 guardrail presentation policy")
    expected_presentation = {
        "keep": {"label": "保持", "confidence_action": "unchanged"},
        "caution": {"label": "谨慎", "confidence_action": "unchanged"},
        "downgrade": {"label": "降置信", "confidence_cap": "low"},
        "abstain": {"label": "回避", "direction_action": "suppress"},
    }
    for action, expected in expected_presentation.items():
        if presentation.get(action) != expected:
            raise K1GuardrailError(f"invalid presentation policy for {action}")
    integer_thresholds = {
        "minimum_bookmakers_per_market": 3,
        "minimum_paired_bookmakers": 3,
        "live_observation_count": 1,
        "live_observation_span_seconds": 0,
    }
    checked_thresholds: dict[str, float | int] = {}
    for name, minimum in integer_thresholds.items():
        value = thresholds.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise K1GuardrailError(f"{name} must be an integer of at least {minimum}")
        checked_thresholds[name] = value
    for name in (
        "favorite_probability_drop", "favorite_probability_strengthening",
        "alternative_probability_rise", "signal_support_ratio", "clear_favorite_probability",
        "clear_direction_gap", "handicap_non_cover_margin", "high_dispersion",
        "live_probability_range",
    ):
        checked_thresholds[name] = _number(thresholds, name, minimum=0, maximum=1)
    for name in (
        "favorite_odds_rise", "asian_material_move", "low_total_line", "draw_tail_probability",
        "live_line_range",
    ):
        checked_thresholds[name] = _number(thresholds, name, minimum=0)
    if checked_thresholds["signal_support_ratio"] <= 0:
        raise K1GuardrailError("signal_support_ratio must be greater than zero")
    checked_gate: dict[str, float | int] = {}
    for name in (
        "minimum_automatic_fixtures", "minimum_span_days", "minimum_rule_hits",
        "minimum_batch_hits", "bootstrap_iterations",
    ):
        value = gate.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise K1GuardrailError(f"{name} must be a positive integer")
        checked_gate[name] = value
    for name in ("shadow_confidence_level", "active_confidence_level"):
        checked_gate[name] = _number(gate, name, minimum=0, maximum=1)
    for name in ("calibration_residual_maximum", "relative_residual_maximum"):
        checked_gate[name] = _number(gate, name, minimum=-1, maximum=0)
    if checked_gate["active_confidence_level"] <= checked_gate["shadow_confidence_level"]:
        raise K1GuardrailError("active confidence level must exceed shadow confidence level")
    if checked_gate["active_confidence_level"] != 0.95:
        raise K1GuardrailError("active confidence level must be the registered one-sided 0.95")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return K1GuardrailPolicy(
        path=path,
        payload=payload,
        policy_version=version,
        policy_revision=revision,
        status=status,
        effective_at=_utc(payload.get("effective_at"), "effective_at"),
        competition_id=competition_id,
        targets=tuple(targets),
        thresholds=checked_thresholds,
        forward_gate=checked_gate,
        input_policy=dict(input_policy),
        presentation_policy=dict(presentation),
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def select_k1_batch_as_of(
    connection,
    *,
    fixture_id: str,
    target: str,
    prediction_cutoff: datetime,
    available_at: datetime,
) -> dict[str, Any] | None:
    """Select the latest complete model-eligible batch that was operationally available."""
    if target not in TARGETS:
        raise K1GuardrailError(f"unsupported K1 guardrail target: {target}")
    cutoff = prediction_cutoff.astimezone(UTC)
    available = available_at.astimezone(UTC)
    row = connection.execute(
        """
        SELECT batch.*
        FROM football.model_eligible_snapshot_batches_v2 AS batch
        WHERE batch.fixture_id=%s AND batch.target=%s
          AND batch.model_strict_eligible=true
          AND batch.core_observed_at <= %s
          AND batch.completed_at <= %s
          AND NOT EXISTS (
              SELECT 1 FROM football.current_invalid_fixtures AS invalid
              WHERE invalid.fixture_id=batch.fixture_id
          )
        ORDER BY batch.core_observed_at DESC, batch.completed_at DESC, batch.record_id DESC
        LIMIT 1
        """,
        (fixture_id, target, cutoff, available),
    ).fetchone()
    return dict(row) if row is not None else None


def guarded_presentation(
    *,
    probabilities: Mapping[str, Any],
    base_confidence: str,
    action: str,
    policy: K1GuardrailPolicy,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise K1GuardrailError(f"unsupported guardrail action: {action}")
    labels = policy.presentation_policy[action]
    direction = None
    if probabilities and action != "abstain":
        direction = max(("home", "draw", "away"), key=lambda name: float(probabilities.get(name, 0)))
    confidence = base_confidence
    if action == "downgrade":
        rank = {"observation_only": 0, "low": 1, "medium": 2, "high": 3}
        confidence = min((base_confidence, "low"), key=lambda value: rank.get(value, 0))
    elif action == "abstain":
        confidence = "observation_only"
    return {
        "action_code": action,
        "action_label": labels["label"],
        "direction": direction,
        "confidence_label": confidence,
        "presentation_policy_version": policy.presentation_policy["version"],
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _median(values: Iterable[Decimal]) -> Decimal | None:
    selected = sorted(values)
    if not selected:
        return None
    middle = len(selected) // 2
    if len(selected) % 2:
        return selected[middle]
    return (selected[middle - 1] + selected[middle]) / Decimal(2)


def _ratio(matches: int, total: int) -> float:
    return matches / total if total else 0.0


def _devig(values: tuple[Any, Any, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
    odds = tuple(_decimal(value) for value in values)
    if any(value is None or value <= 1 for value in odds):
        return None
    inverse = tuple(Decimal(1) / value for value in odds)  # type: ignore[arg-type]
    total = sum(inverse)
    return tuple(value / total for value in inverse)  # type: ignore[return-value]


def _deduplicate(rows: Iterable[Mapping[str, Any]], fields: tuple[str, ...]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        name = str(row.get("source_bookmaker_name") or "").strip()
        if not name or market_row_role(name) != "bookmaker":
            continue
        grouped.setdefault(name, []).append(row)
    selected: dict[str, Mapping[str, Any]] = {}
    conflicts: list[str] = []
    for name, company_rows in grouped.items():
        signatures = {tuple(str(row.get(field)) for field in fields) for row in company_rows}
        if len(signatures) > 1:
            conflicts.append(name)
            continue
        selected[name] = min(company_rows, key=lambda row: (int(row.get("source_row_index") or 0), str(row.get("record_id") or "")))
    return selected, sorted(conflicts)


def build_guardrail_features(
    market_rows: Mapping[str, list[Mapping[str, Any]]],
    handicap_rows: list[Mapping[str, Any]],
    policy: K1GuardrailPolicy,
) -> tuple[dict[str, Any], list[str]]:
    fields = {
        "ouzhi": ("opening_home", "opening_draw", "opening_away", "current_home", "current_draw", "current_away"),
        "yazhi": ("opening_home", "opening_line", "opening_away", "current_home", "current_line", "current_away"),
        "daxiao": ("opening_over", "opening_line", "opening_under", "current_over", "current_line", "current_under"),
    }
    markets: dict[str, dict[str, Mapping[str, Any]]] = {}
    conflicts: list[str] = []
    for market, required in fields.items():
        complete_rows = [
            row for row in market_rows.get(market, [])
            if all(_decimal(row.get(field)) is not None for field in required)
        ]
        selected, duplicate_conflicts = _deduplicate(complete_rows, required)
        markets[market] = selected
        conflicts.extend(f"duplicate_company_conflict:{market}:{name}" for name in duplicate_conflicts)
    minimum = int(policy.thresholds["minimum_bookmakers_per_market"])
    reasons = list(conflicts)
    for market in ("ouzhi", "yazhi", "daxiao"):
        if len(markets[market]) < minimum:
            reasons.append(f"insufficient_bookmakers:{market}")
    company_probabilities: dict[str, dict[str, tuple[Decimal, Decimal, Decimal]]] = {}
    for name, row in markets["ouzhi"].items():
        opening = _devig((row.get("opening_home"), row.get("opening_draw"), row.get("opening_away")))
        current = _devig((row.get("current_home"), row.get("current_draw"), row.get("current_away")))
        if opening and current:
            company_probabilities[name] = {"opening": opening, "current": current}
    paired_minimum = int(policy.thresholds["minimum_paired_bookmakers"])
    if len(company_probabilities) < paired_minimum:
        reasons.append("insufficient_paired_bookmakers:ouzhi")
    current_probabilities = [tuple(float(value) for value in values["current"]) for values in company_probabilities.values()]
    consensus = direction = dispersion = None
    if current_probabilities:
        consensus, direction, dispersion = market_statistics(current_probabilities)
    favorite_index = None
    if consensus is not None:
        favorite_index = max(range(3), key=lambda index: consensus[index])
    favorite_side = ("home", "draw", "away")[favorite_index] if favorite_index is not None else None
    features: dict[str, Any] = {
        "bookmaker_count_by_market": {market: len(rows) for market, rows in markets.items()},
        "paired_bookmaker_count": len(company_probabilities),
        "duplicate_conflicts": conflicts,
        "probabilities": ({"home": consensus[0], "draw": consensus[1], "away": consensus[2]} if consensus else {}),
        "favorite_side": favorite_side,
        "favorite_probability": consensus[favorite_index] if consensus is not None and favorite_index is not None else None,
        "prob_gap": direction,
        "bookmaker_dispersion": dispersion,
        "source_row_record_ids": sorted(str(row.get("record_id")) for rows in markets.values() for row in rows.values()),
        "guardrail_input_fingerprints": {
            market: canonical_market_fingerprint(
                (
                    {**dict(row), "line_movement": row.get("line_movement") or "none"}
                    for row in selected.values()
                ),
                fields={
                    "ouzhi": (
                        "opening_home", "opening_draw", "opening_away",
                        "current_home", "current_draw", "current_away",
                    ),
                    "yazhi": (
                        "opening_home", "opening_line", "opening_away",
                        "current_home", "current_line", "current_away", "line_movement",
                    ),
                    "daxiao": (
                        "opening_over", "opening_line", "opening_under",
                        "current_over", "current_line", "current_under", "line_movement",
                    ),
                }[market],
            )
            for market, selected in markets.items()
        },
    }
    if favorite_index in {0, 2}:
        opponent_index = 2 if favorite_index == 0 else 0
        deltas = {name: tuple(values["current"][i] - values["opening"][i] for i in range(3)) for name, values in company_probabilities.items()}
        favorite_deltas = [values[favorite_index] for values in deltas.values()]
        odds_deltas = []
        for row in markets["ouzhi"].values():
            opening = _decimal(row.get("opening_home" if favorite_index == 0 else "opening_away"))
            current = _decimal(row.get("current_home" if favorite_index == 0 else "current_away"))
            if opening is not None and current is not None:
                odds_deltas.append(current - opening)
        drop = Decimal(str(policy.thresholds["favorite_probability_drop"]))
        strengthen = Decimal(str(policy.thresholds["favorite_probability_strengthening"]))
        alternative = Decimal(str(policy.thresholds["alternative_probability_rise"]))
        features.update({
            "delta_p_favorite_median": float(_median(favorite_deltas) or 0),
            "delta_favorite_odds_median": float(_median(odds_deltas) or 0),
            "favorite_cooling_support_ratio": _ratio(sum(value <= -drop for value in favorite_deltas), len(favorite_deltas)),
            "favorite_strengthening_support_ratio": _ratio(sum(value >= strengthen for value in favorite_deltas), len(favorite_deltas)),
            "alternative_strengthening_support_ratio": _ratio(sum(values[1] >= alternative or values[opponent_index] >= alternative for values in deltas.values()), len(deltas)),
        })
        asian_deltas: list[Decimal] = []
        asian_current: list[Decimal] = []
        material = Decimal(str(policy.thresholds["asian_material_move"]))
        for row in markets["yazhi"].values():
            opening = _decimal(row.get("opening_line"))
            current = _decimal(row.get("current_line"))
            if opening is None or current is None:
                continue
            opening_favorite = -opening if favorite_index == 0 else opening
            current_favorite = -current if favorite_index == 0 else current
            asian_current.append(current_favorite)
            asian_deltas.append(current_favorite - opening_favorite)
        if len(asian_deltas) < paired_minimum:
            reasons.append("insufficient_paired_bookmakers:yazhi")
        features.update({
            "current_favorite_line": float(_median(asian_current) or 0),
            "delta_favorite_line_median": float(_median(asian_deltas) or 0),
            "asian_retreat_support_ratio": _ratio(sum(value <= -material for value in asian_deltas), len(asian_deltas)),
            "asian_not_strengthening_ratio": _ratio(sum(value <= 0 for value in asian_deltas), len(asian_deltas)),
        })
    total_lines = [_decimal(row.get("current_line")) for row in markets["daxiao"].values()]
    total_values = [value for value in total_lines if value is not None]
    features["current_total_line"] = float(_median(total_values)) if total_values else None
    handicap_valid = []
    handicap_selected, handicap_conflicts = _deduplicate(
        handicap_rows,
        ("handicap_line", "home_probability", "draw_probability", "away_probability"),
    )
    zero_line_count = 0
    direction_mismatch_count = 0
    invalid_probability_count = 0
    if favorite_index in {0, 2}:
        for row in handicap_selected.values():
            line = _decimal(row.get("handicap_line"))
            probabilities = [_decimal(row.get(key)) for key in ("home_probability", "draw_probability", "away_probability")]
            if line is None or any(value is None or value < 0 for value in probabilities):
                invalid_probability_count += 1
                continue
            if line == 0:
                zero_line_count += 1
                continue
            if (favorite_index == 0 and line >= 0) or (favorite_index == 2 and line <= 0):
                direction_mismatch_count += 1
                continue
            total = sum(probabilities)  # type: ignore[arg-type]
            if total <= 0:
                invalid_probability_count += 1
                continue
            normalized = [value / total for value in probabilities]  # type: ignore[operator]
            cover = normalized[favorite_index]
            handicap_valid.append(float((Decimal(1) - cover) - cover))
    margin = float(policy.thresholds["handicap_non_cover_margin"])
    features.update({
        "handicap_index_valid_bookmakers": len(handicap_valid),
        "handicap_index_conflicts": handicap_conflicts,
        "handicap_index_conflict_support_ratio": _ratio(sum(value >= margin for value in handicap_valid), len(handicap_valid)),
        "r4_raw_row_count": len(handicap_rows),
        "r4_bookmaker_row_count": len(handicap_selected),
        "r4_zero_line_count": zero_line_count,
        "r4_direction_mismatch_count": direction_mismatch_count,
        "r4_invalid_probability_count": invalid_probability_count,
        "r4_valid_bookmaker_count": len(handicap_valid),
        "live_observation_count": 1,
        "live_observation_span_seconds": 0,
        "live_line_range": None,
        "live_probability_range": None,
    })
    return features, sorted(set(reasons))


def assess_guardrail_features(features: Mapping[str, Any], policy: K1GuardrailPolicy, hard_reasons: Iterable[str] = ()) -> dict[str, Any]:
    reasons = sorted(set(str(reason) for reason in hard_reasons))
    rules: dict[str, dict[str, Any]] = {}
    if reasons:
        rules["r0_data_integrity"] = {"status": "matched", "reasons": reasons}
        for name in ("r1_shallow_favorite_cooling", "r2_asian_retreat", "r2_euro_strong_asian_flat", "r3_low_total_draw_tail", "r4_handicap_cover_conflict", "r5_live_market_stability", "r6_bookmaker_dispersion"):
            rules[name] = {"status": "not_evaluable", "reasons": ["r0_data_integrity"]}
        return {"rule_evaluations": rules, "rule_flags": ["r0_data_integrity"], "proposed_action": "abstain", "proposed_confidence_cap": "observation_only", "reasons": reasons}
    t = policy.thresholds
    favorite_side = features.get("favorite_side")
    directional = favorite_side in {"home", "away"}
    support = float(t["signal_support_ratio"])
    current_line = features.get("current_favorite_line")
    r1 = directional and abs(float(current_line)) in {0.25, 0.5} and float(features.get("delta_p_favorite_median", 0)) <= -float(t["favorite_probability_drop"]) and float(features.get("delta_favorite_odds_median", 0)) >= float(t["favorite_odds_rise"]) and float(features.get("favorite_cooling_support_ratio", 0)) >= support and float(features.get("alternative_strengthening_support_ratio", 0)) >= support
    clear = directional and float(features.get("favorite_probability") or 0) >= float(t["clear_favorite_probability"]) and float(features.get("prob_gap") or 0) >= float(t["clear_direction_gap"])
    r2_retreat = clear and float(features.get("delta_favorite_line_median", 0)) <= -float(t["asian_material_move"]) and float(features.get("asian_retreat_support_ratio", 0)) >= support
    r2_flat = clear and float(features.get("delta_p_favorite_median", 0)) >= float(t["favorite_probability_strengthening"]) and float(features.get("favorite_strengthening_support_ratio", 0)) >= support and float(current_line or 0) == 0 and float(features.get("asian_not_strengthening_ratio", 0)) >= support
    total_line = features.get("current_total_line")
    probabilities = features.get("probabilities") or {}
    r3 = total_line is not None and float(total_line) <= float(t["low_total_line"]) and float(probabilities.get("draw", 0)) >= float(t["draw_tail_probability"]) and abs(float(current_line or 0)) <= 0.5
    handicap_count = int(features.get("handicap_index_valid_bookmakers", 0))
    r4_status = "not_evaluable" if handicap_count < int(t["minimum_bookmakers_per_market"]) else ("matched" if float(features.get("handicap_index_conflict_support_ratio", 0)) >= support else "not_matched")
    observation_count = int(features.get("live_observation_count", 0))
    observation_span = int(features.get("live_observation_span_seconds", 0))
    r5_evaluable = observation_count >= int(t["live_observation_count"]) and observation_span >= int(t["live_observation_span_seconds"])
    line_range = features.get("live_line_range")
    probability_range = features.get("live_probability_range")
    r5 = (
        r5_evaluable
        and line_range is not None
        and probability_range is not None
        and float(line_range) <= float(t["live_line_range"])
        and float(probability_range) <= float(t["live_probability_range"])
    )
    r6 = float(features.get("bookmaker_dispersion") or 0) > float(t["high_dispersion"])
    values = {
        "r0_data_integrity": "not_matched",
        "r1_shallow_favorite_cooling": "matched" if r1 else ("not_matched" if directional else "not_evaluable"),
        "r2_asian_retreat": "matched" if r2_retreat else ("not_matched" if directional else "not_evaluable"),
        "r2_euro_strong_asian_flat": "matched" if r2_flat else ("not_matched" if directional else "not_evaluable"),
        "r3_low_total_draw_tail": "matched" if r3 else "not_matched",
        "r4_handicap_cover_conflict": r4_status,
        "r5_live_market_stability": ("matched" if r5 else "not_matched") if r5_evaluable else "not_evaluable",
        "r6_bookmaker_dispersion": "matched" if r6 else "not_matched",
    }
    rules = {name: {"status": status, "reasons": []} for name, status in values.items()}
    if r4_status == "not_evaluable":
        r4_reasons = []
        if not directional:
            r4_reasons.append("favorite_not_directional")
        if int(features.get("r4_invalid_probability_count", 0)):
            r4_reasons.append("invalid_handicap_probability")
        if int(features.get("r4_direction_mismatch_count", 0)):
            r4_reasons.append("unsupported_handicap_direction")
        if handicap_count < int(t["minimum_bookmakers_per_market"]):
            r4_reasons.append("insufficient_handicap_index_bookmakers")
        rules["r4_handicap_cover_conflict"]["reasons"] = sorted(set(r4_reasons))
    if not r5_evaluable:
        r5_reasons = []
        if not directional:
            r5_reasons.append("favorite_not_directional")
        if observation_count < int(t["live_observation_count"]):
            r5_reasons.append("insufficient_distinct_responses")
        if observation_span < int(t["live_observation_span_seconds"]):
            r5_reasons.append("insufficient_observation_span")
        rules["r5_live_market_stability"]["reasons"] = sorted(set(r5_reasons))
    flags = sorted(name for name, status in values.items() if status == "matched")
    primary = r1 or r2_retreat
    independent = r3 or r4_status == "matched" or r6
    if primary and independent:
        action, cap = "abstain", "observation_only"
    elif primary:
        action, cap = "downgrade", "low"
    elif r2_flat or independent or r5:
        action, cap = "caution", None
    else:
        action, cap = "keep", None
    return {"rule_evaluations": rules, "rule_flags": flags, "proposed_action": action, "proposed_confidence_cap": cap, "reasons": []}


def relevant_source_fingerprint(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    files: list[Path] = []
    for relative in RELEVANT_PATHS:
        path = workspace / relative
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*") if item.is_file() and "__pycache__" not in item.parts))
        elif path.is_file():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        relative = path.relative_to(workspace).as_posix()
        digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--", *RELEVANT_PATHS], cwd=workspace, check=True, capture_output=True, text=True).stdout.splitlines()
        dirty = sorted(line[3:].replace("\\", "/") for line in status if len(line) > 3)
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, ["git_unavailable"]
    return {"git_commit": commit, "relevant_source_tree_sha256": digest.hexdigest(), "relevant_dirty_paths": dirty}


def unavailable_assessment(prediction: Mapping[str, Any], policy: K1GuardrailPolicy, assessed_at: datetime, reason: str, fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    record_id = stable_id("research_k1_guardrail_assessment", prediction["record_id"], policy.policy_version)
    return {
        "schema_version": 1, "record_type": "ResearchK1GuardrailAssessment", "record_id": record_id,
        "research_only": True, "backfill": False, "strict_backtest_eligible": False,
        "cutoff_eligible": False, "research_kind": "shadow_event",
        "prediction_record_id": prediction["record_id"], "channel": prediction["channel"],
        "fixture_id": str(prediction["fixture_id"]), "competition_id": "16", "target": prediction["target"],
        "prediction_cutoff": prediction["prediction_cutoff"], "assessed_at": assessed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "policy_version": policy.policy_version, "policy_revision": policy.policy_revision,
        "policy_status": policy.status, "policy_snapshot": policy.payload,
        "policy_file_sha256": policy.file_sha256, "policy_canonical_sha256": policy.canonical_sha256,
        "historical_dataset_sha256": K1_DATASET_SHA256, **fingerprint,
        "identity_record_id": prediction.get("identity_record_id"), "selected_batch_record_id": prediction.get("selected_batch_record_id"),
        "snapshot_record_ids": {}, "source_row_record_ids": [], "source_hashes": {}, "raw_features": {},
        "rule_evaluations": {}, "rule_flags": [reason], "proposed_action": "abstain",
        "proposed_confidence_cap": "observation_only", "reasons": [reason], "audit_status": "unavailable",
    }


def validate_assessment_record(record: Mapping[str, Any]) -> None:
    if record.get("record_type") != "ResearchK1GuardrailAssessment":
        return
    if record.get("competition_id") != "16" or record.get("target") not in TARGETS:
        raise K1GuardrailError("K1 assessment has invalid competition or target")
    if record.get("policy_status") != "shadow" or record.get("audit_status") not in {"eligible", "unavailable"}:
        raise K1GuardrailError("K1 assessment has invalid policy or audit status")
    if record.get("proposed_action") not in ACTIONS:
        raise K1GuardrailError("K1 assessment has invalid action")
    for name in ("policy_file_sha256", "policy_canonical_sha256", "historical_dataset_sha256", "relevant_source_tree_sha256"):
        if not valid_sha256(record.get(name)):
            raise K1GuardrailError(f"K1 assessment has invalid {name}")
    snapshot = record.get("policy_snapshot")
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != record.get("policy_canonical_sha256"):
        raise K1GuardrailError("K1 assessment policy snapshot hash mismatch")
    expected_id = stable_id(
        "research_k1_guardrail_assessment",
        record.get("prediction_record_id"),
        record.get("policy_version"),
    )
    if record.get("record_id") != expected_id:
        raise K1GuardrailError("K1 assessment stable id mismatch")
    cutoff = _utc(record.get("prediction_cutoff"), "prediction_cutoff")
    effective = _utc(snapshot.get("effective_at"), "effective_at") if isinstance(snapshot, dict) else None
    assessed_at = _utc(record.get("assessed_at"), "assessed_at")
    if effective is None or cutoff < effective or assessed_at < cutoff:
        raise K1GuardrailError("K1 assessment violates policy time boundary")
    raw_features = record.get("raw_features") or {}
    workflow = raw_features.get("analysis_workflow") or {}
    if workflow:
        required_workflow = {
            "workflow_version", "workflow_effective_at", "workflow_file_sha256",
            "workflow_canonical_sha256", "active_at_cutoff",
        }
        if not required_workflow.issubset(workflow):
            raise K1GuardrailError("K1 assessment has incomplete analysis workflow")
        if workflow.get("workflow_version") != "k1-analysis-flow-v2":
            raise K1GuardrailError("K1 assessment has unsupported analysis workflow")
        if not valid_sha256(workflow.get("workflow_file_sha256")) or not valid_sha256(workflow.get("workflow_canonical_sha256")):
            raise K1GuardrailError("K1 assessment has invalid analysis workflow hash")
        if not isinstance(raw_features.get("base_input_fingerprint"), dict) or not isinstance(raw_features.get("guardrail_input_fingerprints"), dict):
            raise K1GuardrailError("K1 assessment has incomplete workflow fingerprints")
        if not isinstance(raw_features.get("market_update"), dict) or raw_features["market_update"].get("status") not in {"not_available", "unchanged", "partial_update", "full_update"}:
            raise K1GuardrailError("K1 assessment has invalid market update status")
    if record.get("audit_status") == "eligible":
        policy = K1GuardrailPolicy(
            path=Path("<embedded>"),
            payload=dict(snapshot),
            policy_version=str(snapshot.get("policy_version")),
            policy_revision=int(snapshot.get("policy_revision")),
            status=str(snapshot.get("status")),
            effective_at=effective,
            competition_id=str(snapshot.get("competition_id")),
            targets=tuple(snapshot.get("targets") or []),
            thresholds=dict(snapshot.get("thresholds") or {}),
            forward_gate=dict(snapshot.get("forward_gate") or {}),
            input_policy=dict(snapshot.get("input_policy") or {}),
            presentation_policy=dict(snapshot.get("presentation_policy") or {}),
            file_sha256=str(record.get("policy_file_sha256")),
            canonical_sha256=str(record.get("policy_canonical_sha256")),
        )
        hard_reasons = record.get("reasons") if (record.get("rule_evaluations") or {}).get("r0_data_integrity", {}).get("status") == "matched" else []
        expected = assess_guardrail_features(record.get("raw_features") or {}, policy, hard_reasons)
        for key in ("rule_evaluations", "rule_flags", "proposed_action", "proposed_confidence_cap", "reasons"):
            if record.get(key) != expected[key]:
                raise K1GuardrailError(f"K1 assessment derived field mismatch: {key}")


def completed_manifest(record_path: Path, *, run_id: str, prediction_count: int, assessment_count: int, policy_version: str | None) -> dict[str, Any]:
    content = record_path.read_bytes()
    return {
        "schema_version": 1, "run_id": run_id, "status": "completed",
        "record_path": record_path.as_posix(), "record_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content), "line_count": len(content.splitlines()),
        "prediction_count": prediction_count, "assessment_count": assessment_count,
        "policy_version": policy_version,
    }


def require_migration(connection, version: str = "014") -> None:
    try:
        row = connection.execute(
            "SELECT version FROM football.schema_migrations WHERE version=%s", (version,)
        ).fetchone()
    except Exception as exc:
        raise K1GuardrailError(f"database migration {version} is required") from exc
    if row is None:
        raise K1GuardrailError(f"database migration {version} is required; run db-import first")


def verify_shadow_manifest(research_dir: Path, record_path: Path) -> None:
    try:
        relative = record_path.relative_to(research_dir / "normalized" / "shadow-predictions")
    except ValueError:
        return
    if len(relative.parts) < 2:
        raise K1GuardrailError(f"invalid shadow prediction path: {record_path}")
    run_id = relative.parts[0]
    colocated = record_path.parent / "manifest.json"
    manifest_path = colocated if colocated.is_file() else research_dir / "manifests" / run_id / "shadow-predictions.json"
    if not manifest_path.is_file():
        raise K1GuardrailError(f"shadow prediction batch lacks completed manifest: {run_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise K1GuardrailError(f"invalid shadow prediction manifest: {run_id}") from exc
    content = record_path.read_bytes()
    records = [json.loads(line) for line in content.splitlines() if line.strip()]
    expected_path = record_path.relative_to(research_dir).as_posix()
    has_guardrail = any(
        record.get("record_type") in {
            "ResearchK1GuardrailAssessment",
            "ResearchEuropeGuardrailAssessment",
        }
        for record in records
    )
    if not has_guardrail and "record_sha256" not in manifest:
        if manifest.get("status") != "completed" or manifest.get("record_path") != expected_path:
            raise K1GuardrailError(f"legacy shadow prediction manifest mismatch: {run_id}")
        return
    checks = {
        "status": manifest.get("status") == "completed",
        "record_path": manifest.get("record_path") == expected_path,
        "record_sha256": manifest.get("record_sha256") == hashlib.sha256(content).hexdigest(),
        "size_bytes": manifest.get("size_bytes") == len(content),
        "line_count": manifest.get("line_count") == len(records),
        "prediction_count": manifest.get("prediction_count") == sum(record.get("record_type") == "ResearchShadowPrediction" for record in records),
        "assessment_count": manifest.get("assessment_count") == sum(record.get("record_type") == "ResearchK1GuardrailAssessment" for record in records),
    }
    if "europe_assessment_count" in manifest or any(
        record.get("record_type") == "ResearchEuropeGuardrailAssessment" for record in records
    ):
        checks["europe_assessment_count"] = manifest.get("europe_assessment_count") == sum(
            record.get("record_type") == "ResearchEuropeGuardrailAssessment" for record in records
        )
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise K1GuardrailError(f"shadow prediction manifest mismatch ({run_id}): {', '.join(failed)}")


def _opening_fields(market: str) -> tuple[str, ...]:
    return {
        "ouzhi": ("opening_home", "opening_draw", "opening_away"),
        "yazhi": ("opening_home", "opening_line", "opening_away"),
        "daxiao": ("opening_over", "opening_line", "opening_under"),
    }[market]


def _unstable_opening_companies(
    connection,
    *,
    fixture_id: str,
    target: str,
    market: str,
    cutoff: datetime,
    available_at: datetime,
) -> set[str]:
    rows = connection.execute(
        """
        SELECT row.source_bookmaker_name, row.opening_home, row.opening_draw,
               row.opening_away, row.opening_line, row.opening_over, row.opening_under
        FROM football.model_eligible_snapshot_batches_v2 AS batch
        JOIN football.current_bookmaker_market_rows AS row
          ON row.source_snapshot_record_id=(batch.market_results->%s->>'snapshot_record_id')
        WHERE batch.fixture_id=%s AND batch.target=%s
          AND batch.model_strict_eligible=true
          AND batch.core_observed_at <= %s AND batch.completed_at <= %s
          AND row.market=%s AND row.event_origin='live' AND row.normalization_version=2
          AND row.row_role='bookmaker' AND row.observed_at <= %s
        """,
        (market, fixture_id, target, cutoff, available_at, market, cutoff),
    ).fetchall()
    fields = _opening_fields(market)
    signatures: dict[str, set[tuple[str, ...]]] = {}
    for raw in rows:
        row = dict(raw)
        name = str(row.get("source_bookmaker_name") or "").strip()
        if name:
            signatures.setdefault(name, set()).add(tuple(str(row.get(field)) for field in fields))
    return {name for name, values in signatures.items() if len(values) > 1}


def _trajectory_features(
    connection,
    *,
    fixture_id: str,
    target: str,
    cutoff: datetime,
    available_at: datetime,
    favorite_side: str | None,
) -> dict[str, Any]:
    if favorite_side not in {"home", "away"}:
        return {"live_observation_count": 0, "live_observation_span_seconds": 0,
                "live_line_range": None, "live_probability_range": None,
                "live_response_set_hashes": [], "r5_raw_response_count": 0,
                "r5_distinct_response_count": 0, "r5_duplicate_response_count": 0}
    batches = connection.execute(
        """
        SELECT record_id, completed_at, core_observed_at, market_results
        FROM football.model_eligible_snapshot_batches_v2
        WHERE fixture_id=%s AND target=%s AND model_strict_eligible=true
          AND core_observed_at <= %s AND completed_at <= %s
        ORDER BY core_observed_at, completed_at, record_id
        """,
        (fixture_id, target, cutoff, available_at),
    ).fetchall()
    observations: dict[str, tuple[datetime, float, float]] = {}
    duplicate_response_count = 0
    for raw_batch in batches:
        batch = dict(raw_batch)
        results = batch.get("market_results") or {}
        ids = [str((results.get(market) or {}).get("snapshot_record_id") or "") for market in ("ouzhi", "yazhi", "daxiao")]
        if any(not value for value in ids):
            continue
        hash_rows = connection.execute(
            "SELECT record_id, raw_sha256 FROM football.market_snapshots WHERE record_id=ANY(%s)",
            (ids,),
        ).fetchall()
        hashes = {str(row["record_id"]): str(row["raw_sha256"] or "") for row in hash_rows}
        if len(hashes) != 3 or any(not hashes.get(value) for value in ids):
            continue
        response_hash = hashlib.sha256(json.dumps([hashes[value] for value in ids], separators=(",", ":")).encode()).hexdigest()
        if response_hash in observations:
            duplicate_response_count += 1
            continue
        euro_rows = [dict(row) for row in connection.execute(
            """SELECT record_id, source_bookmaker_name, current_home, current_draw, current_away, source_row_index
               FROM football.current_bookmaker_market_rows
               WHERE source_snapshot_record_id=%s AND market='ouzhi' AND row_role='bookmaker'
                 AND event_origin='live' AND normalization_version=2 AND observed_at <= %s""",
            (ids[0], cutoff),
        ).fetchall()]
        euro, conflicts = _deduplicate(euro_rows, ("current_home", "current_draw", "current_away"))
        if conflicts:
            continue
        probs = []
        for row in euro.values():
            value = _devig((row.get("current_home"), row.get("current_draw"), row.get("current_away")))
            if value:
                probs.append(tuple(float(part) for part in value))
        if len(probs) < 3:
            continue
        consensus, _, _ = market_statistics(probs)
        asian_rows = [dict(row) for row in connection.execute(
            """SELECT record_id, source_bookmaker_name, current_line, source_row_index
               FROM football.current_bookmaker_market_rows
               WHERE source_snapshot_record_id=%s AND market='yazhi' AND row_role='bookmaker'
                 AND event_origin='live' AND normalization_version=2 AND observed_at <= %s""",
            (ids[1], cutoff),
        ).fetchall()]
        asian, conflicts = _deduplicate(asian_rows, ("current_line",))
        lines = [_decimal(row.get("current_line")) for row in asian.values()]
        lines = [value for value in lines if value is not None]
        if conflicts or len(lines) < 3:
            continue
        line = _median((-value if favorite_side == "home" else value for value in lines))
        if line is None:
            continue
        probability = consensus[0 if favorite_side == "home" else 2]
        observations[response_hash] = (batch["core_observed_at"].astimezone(UTC), float(line), float(probability))
    ordered = sorted(observations.values())
    if not ordered:
        return {"live_observation_count": 0, "live_observation_span_seconds": 0,
                "live_line_range": None, "live_probability_range": None,
                "live_response_set_hashes": [], "r5_raw_response_count": len(batches),
                "r5_distinct_response_count": 0, "r5_duplicate_response_count": duplicate_response_count}
    return {
        "live_observation_count": len(ordered),
        "live_observation_span_seconds": int((ordered[-1][0] - ordered[0][0]).total_seconds()),
        "live_line_range": max(value[1] for value in ordered) - min(value[1] for value in ordered),
        "live_probability_range": max(value[2] for value in ordered) - min(value[2] for value in ordered),
        "live_response_set_hashes": sorted(observations),
        "r5_raw_response_count": len(batches),
        "r5_distinct_response_count": len(ordered),
        "r5_duplicate_response_count": duplicate_response_count,
    }


def _workflow_snapshot(workflow: K1AnalysisWorkflow, cutoff: datetime) -> dict[str, Any]:
    return {
        "workflow_version": workflow.workflow_version,
        "workflow_effective_at": workflow.effective_at.isoformat().replace("+00:00", "Z"),
        "workflow_file_sha256": workflow.file_sha256,
        "workflow_canonical_sha256": workflow.canonical_sha256,
        "active_at_cutoff": cutoff >= workflow.effective_at,
    }


def _component_hashes(base_fingerprint: Mapping[str, Any], guardrail_fingerprints: Mapping[str, Any]) -> dict[str, str]:
    return {
        "base_ouzhi": str(base_fingerprint.get("sha256") or ""),
        "guardrail_ouzhi": str((guardrail_fingerprints.get("ouzhi") or {}).get("sha256") or ""),
        "guardrail_yazhi": str((guardrail_fingerprints.get("yazhi") or {}).get("sha256") or ""),
        "guardrail_daxiao": str((guardrail_fingerprints.get("daxiao") or {}).get("sha256") or ""),
    }


def _market_update_payload(
    *,
    current_components: Mapping[str, str],
    current_probabilities: Mapping[str, Any],
    current_features: Mapping[str, Any],
    previous_target: str | None,
    previous_components: Mapping[str, str] | None,
    previous_probabilities: Mapping[str, Any] | None,
    previous_features: Mapping[str, Any] | None,
    comparison_time_source: str,
) -> dict[str, Any]:
    status, changed = classify_market_update(current_components, previous_components)
    current_side = current_features.get("favorite_side")
    previous_side = previous_features.get("favorite_side") if previous_features else None
    asian_delta = None
    comparison_reason = None
    if previous_features is not None:
        if current_side == previous_side and current_side in {"home", "away"}:
            current_line = current_features.get("current_favorite_line")
            previous_line = previous_features.get("current_favorite_line")
            if current_line is not None and previous_line is not None:
                asian_delta = float(current_line) - float(previous_line)
        elif current_side != previous_side:
            comparison_reason = "favorite_direction_changed"
    total_delta = None
    if previous_features is not None:
        current_total = current_features.get("current_total_line")
        previous_total = previous_features.get("current_total_line")
        if current_total is not None and previous_total is not None:
            total_delta = float(current_total) - float(previous_total)
    return {
        "status": status,
        "previous_target": previous_target,
        "comparison_time_source": comparison_time_source,
        "base_hash_current": current_components.get("base_ouzhi"),
        "base_hash_previous": previous_components.get("base_ouzhi") if previous_components else None,
        "guardrail_hashes_current": {
            market: current_components.get(f"guardrail_{market}") for market in ("ouzhi", "yazhi", "daxiao")
        },
        "guardrail_hashes_previous": (
            {market: previous_components.get(f"guardrail_{market}") for market in ("ouzhi", "yazhi", "daxiao")}
            if previous_components else None
        ),
        "changed_components": changed,
        "probability_delta": probability_delta(current_probabilities, previous_probabilities),
        "asian_line_delta": asian_delta,
        "total_line_delta": total_delta,
        "comparison_reason": comparison_reason,
    }


def _stored_previous_workflow_context(
    connection,
    *,
    prediction: Mapping[str, Any],
    policy: K1GuardrailPolicy,
    workflow: K1AnalysisWorkflow,
) -> tuple[dict[str, str] | None, dict[str, Any] | None, dict[str, Any] | None, str]:
    previous_target = PREVIOUS_TARGET.get(str(prediction.get("target")))
    if previous_target is None:
        return None, None, None, "not_available"
    cutoff = _utc(prediction.get("prediction_cutoff"), "prediction_cutoff")
    row = connection.execute(
        """
        SELECT prediction.probabilities, assessment.raw_features,
               prediction.published_at, prediction.prediction_cutoff
        FROM research.shadow_predictions AS prediction
        JOIN research.current_k1_guardrail_assessments AS assessment
          ON assessment.prediction_record_id=prediction.record_id
        WHERE prediction.fixture_id=%s AND prediction.target=%s
          AND prediction.status='published' AND prediction.prediction_cutoff < %s
          AND assessment.policy_version=%s
        ORDER BY prediction.prediction_cutoff DESC, prediction.record_id DESC
        LIMIT 1
        """,
        (str(prediction.get("fixture_id")), previous_target, cutoff, policy.policy_version),
    ).fetchone()
    if row is None:
        return None, None, None, "not_available"
    row = dict(row)
    raw_features = dict(row.get("raw_features") or {})
    workflow_block = raw_features.get("analysis_workflow") or {}
    if workflow_block.get("workflow_version") != workflow.workflow_version:
        return None, None, None, "not_available"
    base = raw_features.get("base_input_fingerprint") or {}
    guardrail = raw_features.get("guardrail_input_fingerprints") or {}
    components = _component_hashes(base, guardrail)
    if any(not components.get(name) for name in UPDATE_COMPONENTS):
        return None, None, None, "not_available"
    return components, dict(row.get("probabilities") or {}), raw_features, "stored_prediction"


def _workflow_enrichment(
    connection,
    *,
    workspace: Path,
    prediction: Mapping[str, Any],
    policy: K1GuardrailPolicy,
    workflow: K1AnalysisWorkflow,
    features: Mapping[str, Any],
    assessment: Mapping[str, Any],
    hard_reasons: Iterable[str],
) -> dict[str, Any]:
    cutoff = _utc(prediction.get("prediction_cutoff"), "prediction_cutoff")
    registry = load_competition_registry(workspace)
    gate = registry.confidence_policy["high_confidence_gate"]
    automatic_count = int(prediction.get("automatic_verified_fixture_count") or 0)
    span_days = float(prediction.get("evaluation_span_days") or 0.0)
    maturity = sample_maturity(
        automatic_fixture_count=automatic_count,
        span_days=span_days,
        diagnostic_minimum=workflow.diagnostic_minimum_automatic_fixtures,
        sample_gate_minimum=int(gate["minimum_automatic_verified_fixtures"]),
        sample_gate_span_days=float(gate["minimum_span_days"]),
    )
    base_fingerprint = dict((prediction.get("features") or {}).get("base_1x2_input_fingerprint") or {})
    guardrail_fingerprints = dict(features.get("guardrail_input_fingerprints") or {})
    current_components = _component_hashes(base_fingerprint, guardrail_fingerprints)
    previous_components, previous_probabilities, previous_features, comparison_source = _stored_previous_workflow_context(
        connection, prediction=prediction, policy=policy, workflow=workflow,
    )
    market_update = _market_update_payload(
        current_components=current_components,
        current_probabilities=prediction.get("probabilities") or features.get("probabilities") or {},
        current_features=features,
        previous_target=PREVIOUS_TARGET.get(str(prediction.get("target"))),
        previous_components=previous_components,
        previous_probabilities=previous_probabilities,
        previous_features=previous_features,
        comparison_time_source=comparison_source,
    )
    rule_evaluations = assessment.get("rule_evaluations") or {}
    not_evaluable = {
        name: list(value.get("reasons") or [])
        for name, value in rule_evaluations.items()
        if value.get("status") == "not_evaluable"
    }
    core_complete = not list(hard_reasons) and (rule_evaluations.get("r0_data_integrity") or {}).get("status") != "matched"
    auxiliary_complete = all(
        (rule_evaluations.get(rule) or {}).get("status") != "not_evaluable"
        for rule in ("r4_handicap_cover_conflict", "r5_live_market_stability")
    )
    direction = float(prediction.get("direction_strength") if prediction.get("direction_strength") is not None else features.get("prob_gap") or 0.0)
    return {
        "analysis_workflow": _workflow_snapshot(workflow, cutoff),
        "base_input_fingerprint": base_fingerprint,
        "guardrail_input_fingerprints": guardrail_fingerprints,
        "market_update": market_update,
        "confidence_interpretation": {
            "raw_confidence_label": prediction.get("raw_confidence_label"),
            "competition_confidence_cap": prediction.get("competition_confidence_cap"),
            "final_confidence_label": prediction.get("confidence_label"),
            "confidence_reasons": list(prediction.get("confidence_reasons") or []),
            "direction_strength": direction,
            "direction_strength_label": direction_strength_label(direction, registry.confidence_policy),
            "bookmaker_count": int(prediction.get("bookmaker_count") or features.get("paired_bookmaker_count") or 0),
            "bookmaker_dispersion": prediction.get("bookmaker_dispersion", features.get("bookmaker_dispersion")),
        },
        "sample_maturity": {
            "automatic_verified_fixture_count_as_of": automatic_count,
            "automatic_evaluation_span_days_as_of": span_days,
            "status": maturity,
        },
        "input_quality": {
            "core_input_quality": "complete" if core_complete else "invalid",
            "auxiliary_rule_coverage": "complete" if auxiliary_complete else "limited",
        },
        "rule_evaluability": {
            "evaluable_rule_count": sum(value.get("status") != "not_evaluable" for value in rule_evaluations.values()),
            "not_evaluable_rule_count": len(not_evaluable),
            "not_evaluable_reasons": not_evaluable,
            "r4_raw_row_count": int(features.get("r4_raw_row_count", 0)),
            "r4_bookmaker_row_count": int(features.get("r4_bookmaker_row_count", 0)),
            "r4_zero_line_count": int(features.get("r4_zero_line_count", 0)),
            "r4_direction_mismatch_count": int(features.get("r4_direction_mismatch_count", 0)),
            "r4_invalid_probability_count": int(features.get("r4_invalid_probability_count", 0)),
            "r4_valid_bookmaker_count": int(features.get("r4_valid_bookmaker_count", 0)),
            "r5_raw_response_count": int(features.get("r5_raw_response_count", 0)),
            "r5_distinct_response_count": int(features.get("r5_distinct_response_count", 0)),
            "r5_observation_span_seconds": int(features.get("live_observation_span_seconds", 0)),
            "r5_duplicate_response_count": int(features.get("r5_duplicate_response_count", 0)),
            "opening_stability_excluded_by_market": dict(features.get("opening_stability_excluded_by_market") or {}),
        },
    }


def collect_k1_guardrail_assessment(
    connection,
    *,
    workspace: Path,
    prediction: Mapping[str, Any],
    batch: Mapping[str, Any],
    policy: K1GuardrailPolicy,
    assessed_at: datetime,
    enforce_effective_at: bool = True,
    enforce_reproducible_source: bool = True,
) -> dict[str, Any] | None:
    if str(prediction.get("competition_id") or "") != policy.competition_id:
        return None
    workflow = load_k1_analysis_workflow(workspace)
    cutoff = _utc(prediction.get("prediction_cutoff"), "prediction_cutoff")
    published = _utc(prediction.get("published_at"), "published_at")
    assessed_at = assessed_at.astimezone(UTC)
    if enforce_effective_at and cutoff < policy.effective_at:
        return None
    fingerprint = relevant_source_fingerprint(workspace)
    if published < cutoff or assessed_at < published:
        return unavailable_assessment(prediction, policy, assessed_at, "invalid_assessment_time_order", fingerprint)
    if enforce_reproducible_source and fingerprint["relevant_dirty_paths"]:
        return unavailable_assessment(prediction, policy, assessed_at, "relevant_source_not_reproducible", fingerprint)
    if str(batch.get("record_id") or batch.get("snapshot_batch_record_id") or "") != str(prediction.get("selected_batch_record_id") or ""):
        return unavailable_assessment(prediction, policy, assessed_at, "selected_batch_reference_conflict", fingerprint)
    if batch.get("completed_at") and batch["completed_at"].astimezone(UTC) > published:
        return unavailable_assessment(prediction, policy, assessed_at, "batch_not_available_at_publication", fingerprint)
    market_results = batch.get("market_results") or {}
    snapshot_ids: dict[str, str] = {}
    hard_reasons: list[str] = []
    for market in ("ouzhi", "yazhi", "daxiao", "rangqiu"):
        result = market_results.get(market) if isinstance(market_results, dict) else None
        snapshot_id = str((result or {}).get("snapshot_record_id") or "")
        if snapshot_id:
            snapshot_ids[market] = snapshot_id
        elif market != "rangqiu":
            hard_reasons.append(f"missing_snapshot:{market}")
    market_rows: dict[str, list[Mapping[str, Any]]] = {}
    source_hashes: dict[str, str] = {}
    opening_warnings: list[str] = []
    for market in ("ouzhi", "yazhi", "daxiao"):
        snapshot_id = snapshot_ids.get(market)
        if not snapshot_id:
            market_rows[market] = []
            continue
        snapshot = connection.execute(
            """
            SELECT record_id, fixture_id, market, target, observed_at, raw_sha256
            FROM football.market_snapshots
            WHERE record_id=%s
            """,
            (snapshot_id,),
        ).fetchone()
        if snapshot is None:
            hard_reasons.append(f"missing_snapshot_record:{market}")
            market_rows[market] = []
            continue
        snapshot = dict(snapshot)
        if str(snapshot["fixture_id"]) != str(prediction["fixture_id"]) or snapshot["market"] != market or snapshot["target"] != prediction["target"] or snapshot["observed_at"].astimezone(UTC) > cutoff:
            hard_reasons.append(f"snapshot_reference_conflict:{market}")
        source_hashes[market] = str(snapshot.get("raw_sha256") or "")
        rows = connection.execute(
            """
            SELECT record_id, source_bookmaker_name, row_role,
                   opening_home, opening_draw, opening_away, opening_line,
                   opening_over, opening_under, current_home, current_draw,
                   current_away, current_line, current_over, current_under,
                   source_row_index, source_page_sha256, source_workbook_sha256,
                   line_movement, observed_at
            FROM football.current_bookmaker_market_rows
            WHERE fixture_id=%s AND target=%s AND market=%s
              AND source_snapshot_record_id=%s
              AND event_origin='live' AND normalization_version=2
              AND observed_at <= %s
            ORDER BY source_row_index, record_id
            """,
            (str(prediction["fixture_id"]), prediction["target"], market, snapshot_id, cutoff),
        ).fetchall()
        selected_rows = [dict(row) for row in rows]
        unstable = _unstable_opening_companies(
            connection, fixture_id=str(prediction["fixture_id"]), target=str(prediction["target"]),
            market=market, cutoff=cutoff, available_at=published,
        )
        if unstable:
            opening_warnings.extend(f"provider_opening_changed:{market}:{name}" for name in sorted(unstable))
            selected_rows = [row for row in selected_rows if str(row.get("source_bookmaker_name") or "").strip() not in unstable]
        expected_hash_field = "source_workbook_sha256" if market == "ouzhi" else "source_page_sha256"
        row_hashes = {str(row.get(expected_hash_field) or "") for row in selected_rows}
        if row_hashes != {source_hashes[market]}:
            hard_reasons.append(f"source_hash_mismatch:{market}")
        market_rows[market] = selected_rows
    handicap_rows: list[Mapping[str, Any]] = []
    if snapshot_ids.get("rangqiu"):
        snapshot = connection.execute(
            "SELECT record_id, fixture_id, market, target, observed_at, raw_sha256 FROM football.market_snapshots WHERE record_id=%s",
            (snapshot_ids["rangqiu"],),
        ).fetchone()
        if snapshot is None or str(snapshot["fixture_id"]) != str(prediction["fixture_id"]) or snapshot["market"] != "rangqiu" or snapshot["target"] != prediction["target"] or snapshot["observed_at"].astimezone(UTC) > cutoff:
            hard_reasons.append("snapshot_reference_conflict:rangqiu")
        rows = connection.execute(
            """
            SELECT row.record_id, row.source_bookmaker_name, row.handicap_line,
                   row.home_probability, row.draw_probability, row.away_probability,
                   row.source_row_index, row.source_page_sha256, row.observed_at
            FROM football.handicap_index_rows AS row
            JOIN football.market_normalizations AS normalization
              ON normalization.record_id=row.normalization_record_id
            WHERE row.fixture_id=%s AND row.target=%s
              AND row.source_snapshot_record_id=%s
              AND row.event_origin='live' AND row.normalization_version=2
              AND normalization.status='accepted' AND row.observed_at <= %s
            ORDER BY row.source_row_index, row.record_id
            """,
            (str(prediction["fixture_id"]), prediction["target"], snapshot_ids["rangqiu"], cutoff),
        ).fetchall()
        handicap_rows = [dict(row) for row in rows]
        if handicap_rows:
            source_hashes["rangqiu"] = str(handicap_rows[0].get("source_page_sha256") or "")
            if snapshot is not None and source_hashes["rangqiu"] != str(snapshot["raw_sha256"] or ""):
                hard_reasons.append("source_hash_mismatch:rangqiu")
    features, feature_reasons = build_guardrail_features(market_rows, handicap_rows, policy)
    features["opening_stability_warnings"] = opening_warnings
    excluded_by_market: dict[str, list[str]] = {}
    for warning in opening_warnings:
        _, market, company = warning.split(":", 2)
        excluded_by_market.setdefault(market, []).append(company)
    features["opening_stability_excluded_by_market"] = {
        market: sorted(companies) for market, companies in sorted(excluded_by_market.items())
    }
    features["source_row_record_ids"] = sorted(set(features.get("source_row_record_ids", [])) | {str(row.get("record_id")) for row in handicap_rows})
    features.update(_trajectory_features(
        connection, fixture_id=str(prediction["fixture_id"]), target=str(prediction["target"]),
        cutoff=cutoff, available_at=published, favorite_side=features.get("favorite_side"),
    ))
    hard_reasons.extend(feature_reasons)
    assessment = assess_guardrail_features(features, policy, hard_reasons)
    if cutoff >= workflow.effective_at or not enforce_effective_at:
        features.update(_workflow_enrichment(
            connection,
            workspace=workspace,
            prediction=prediction,
            policy=policy,
            workflow=workflow,
            features=features,
            assessment=assessment,
            hard_reasons=hard_reasons,
        ))
    record_id = stable_id("research_k1_guardrail_assessment", prediction["record_id"], policy.policy_version)
    return {
        "schema_version": 1,
        "record_type": "ResearchK1GuardrailAssessment",
        "record_id": record_id,
        "research_only": True,
        "backfill": False,
        "strict_backtest_eligible": False,
        "cutoff_eligible": False,
        "research_kind": "shadow_event",
        "prediction_record_id": prediction["record_id"],
        "channel": prediction["channel"],
        "fixture_id": str(prediction["fixture_id"]),
        "competition_id": policy.competition_id,
        "target": prediction["target"],
        "prediction_cutoff": prediction["prediction_cutoff"],
        "assessed_at": assessed_at.isoformat().replace("+00:00", "Z"),
        "policy_version": policy.policy_version,
        "policy_revision": policy.policy_revision,
        "policy_status": policy.status,
        "policy_snapshot": policy.payload,
        "policy_file_sha256": policy.file_sha256,
        "policy_canonical_sha256": policy.canonical_sha256,
        "historical_dataset_sha256": K1_DATASET_SHA256,
        **fingerprint,
        "identity_record_id": prediction.get("identity_record_id"),
        "selected_batch_record_id": prediction.get("selected_batch_record_id"),
        "snapshot_record_ids": snapshot_ids,
        "source_row_record_ids": features.get("source_row_record_ids", []),
        "source_hashes": source_hashes,
        "raw_features": features,
        **assessment,
        "audit_status": "eligible",
    }


def _scoring(points: list[tuple[tuple[float, float, float], int]]) -> dict[str, Any]:
    if not points:
        return {"count": 0}
    losses: list[float] = []
    briers: list[float] = []
    rps_values: list[float] = []
    confidence: list[tuple[float, int]] = []
    for probabilities, actual in points:
        losses.append(-math.log(max(probabilities[actual], 1e-15)))
        briers.append(sum((probabilities[index] - (1 if index == actual else 0)) ** 2 for index in range(3)))
        predicted_cumulative = (probabilities[0], probabilities[0] + probabilities[1])
        actual_cumulative = (1.0 if actual == 0 else 0.0, 1.0 if actual <= 1 else 0.0)
        rps_values.append(sum((left - right) ** 2 for left, right in zip(predicted_cumulative, actual_cumulative)) / 2)
        top = max(range(3), key=lambda index: probabilities[index])
        confidence.append((probabilities[top], 1 if top == actual else 0))
    ordered = sorted(confidence)
    ece = 0.0
    bins = min(10, len(ordered))
    for index in range(bins):
        bucket = ordered[index * len(ordered) // bins : (index + 1) * len(ordered) // bins]
        if bucket:
            ece += len(bucket) / len(ordered) * abs(sum(value for value, _ in bucket) / len(bucket) - sum(hit for _, hit in bucket) / len(bucket))
    return {
        "count": len(points),
        "log_loss": sum(losses) / len(losses),
        "brier": sum(briers) / len(briers),
        "rps": sum(rps_values) / len(rps_values),
        "ece": ece,
        "ece_bins": bins,
    }


def _block_bootstrap(values_by_week: Mapping[str, list[float]], *, iterations: int, confidence: float, seed: int) -> dict[str, Any]:
    weeks = sorted(values_by_week)
    if not weeks:
        return {"iterations": iterations, "count": 0}
    generator = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        values: list[float] = []
        for _ in weeks:
            values.extend(values_by_week[generator.choice(weeks)])
        samples.append(sum(values) / len(values))
    samples.sort()
    alpha = (1 - confidence) / 2
    lower = samples[max(0, int(alpha * len(samples)))]
    upper = samples[min(len(samples) - 1, int((1 - alpha) * len(samples)))]
    return {"iterations": iterations, "count": sum(map(len, values_by_week.values())), "confidence": confidence, "lower": lower, "upper": upper}


def _bootstrap_samples(values_by_week: Mapping[str, list[float]], *, iterations: int, seed: int) -> list[float]:
    weeks = sorted(values_by_week)
    if not weeks:
        return []
    generator = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        values: list[float] = []
        for _ in weeks:
            values.extend(values_by_week[generator.choice(weeks)])
        samples.append(sum(values) / len(values))
    return samples


def dry_run_k1_guardrail(config: ResearchConfig, *, fixture_id: str, target: str, now: datetime | None = None) -> dict[str, Any]:
    from football_cups.database.config import DatabaseConfig
    from football_cups.database.connection import connect

    if target not in TARGETS:
        raise K1GuardrailError(f"unsupported K1 guardrail target: {target}")
    policy = load_k1_guardrail_policy(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    assessed_at = (now or datetime.now(UTC)).astimezone(UTC)
    with connect(database_config) as connection:
        require_migration(connection)
        prediction_row = connection.execute(
            """
            SELECT * FROM research.shadow_predictions
            WHERE fixture_id=%s AND target=%s AND competition_id='16'
            ORDER BY prediction_cutoff DESC, record_id DESC LIMIT 1
            """,
            (fixture_id, target),
        ).fetchone()
        if prediction_row is None:
            raise K1GuardrailError("no K1 shadow prediction exists for fixture and target")
        prediction = dict(prediction_row)
        prediction["prediction_cutoff"] = prediction["prediction_cutoff"].astimezone(UTC).isoformat().replace("+00:00", "Z")
        prediction["published_at"] = prediction["published_at"].astimezone(UTC).isoformat().replace("+00:00", "Z")
        batch_row = connection.execute(
            "SELECT * FROM football.snapshot_batches WHERE record_id=%s",
            (prediction.get("selected_batch_record_id"),),
        ).fetchone()
        if batch_row is None:
            raise K1GuardrailError("shadow prediction references a missing snapshot batch")
        assessment = collect_k1_guardrail_assessment(
            connection,
            workspace=config.workspace,
            prediction=prediction,
            batch=dict(batch_row),
            policy=policy,
            assessed_at=max(assessed_at, _utc(prediction["published_at"], "published_at")),
        )
    return {"status": "dry_run", "fixture_id": fixture_id, "target": target, "assessment": assessment}


def _replay_one(
    connection,
    *,
    config: ResearchConfig,
    fixture_id: str,
    target: str,
    policy: K1GuardrailPolicy,
    now: datetime,
    prefer_stored_prediction: bool,
    include_previous_simulation: bool = True,
) -> dict[str, Any]:
    from .modeling import (
        _automatic_evaluation_stats,
        _deadline,
        _identity_as_of,
        _live_1x2_consensus,
        _prediction_cutoff,
        _profile_fields,
    )

    latest = connection.execute(
        """SELECT fixture_id, kickoff_at FROM football.fixture_identities
           WHERE fixture_id=%s AND kickoff_at IS NOT NULL
           ORDER BY observed_at DESC, record_id DESC LIMIT 1""",
        (fixture_id,),
    ).fetchone()
    if latest is None:
        raise K1GuardrailError(f"fixture identity is missing: {fixture_id}")
    identity = _identity_as_of(connection, fixture_id, target)
    if identity is None or str(identity.get("competition_id") or "") != "16" or identity.get("identity_status") == "conflict":
        raise K1GuardrailError(f"fixture is not an unambiguous K1 identity as of cutoff: {fixture_id}")
    kickoff = identity["kickoff_at"].astimezone(UTC)
    cutoff = _prediction_cutoff(kickoff, target)
    deadline = _deadline(kickoff, cutoff)
    stored = None
    if prefer_stored_prediction:
        stored = connection.execute(
            """SELECT * FROM research.shadow_predictions
               WHERE fixture_id=%s AND target=%s AND prediction_cutoff=%s
               ORDER BY published_at, record_id LIMIT 1""",
            (fixture_id, target, cutoff),
        ).fetchone()
    if stored is not None:
        prediction = dict(stored)
        available_at = prediction["published_at"].astimezone(UTC)
        batch = connection.execute(
            "SELECT * FROM football.model_eligible_snapshot_batches_v2 WHERE record_id=%s",
            (prediction.get("selected_batch_record_id"),),
        ).fetchone()
        if batch is None:
            raise K1GuardrailError("stored prediction references a missing eligible batch")
        selected = select_k1_batch_as_of(
            connection, fixture_id=fixture_id, target=target,
            prediction_cutoff=cutoff, available_at=available_at,
        )
        if selected is None or selected["record_id"] != batch["record_id"]:
            raise K1GuardrailError("stored prediction batch is not the deterministic as-of batch")
        batch = dict(batch)
        publication_source = "stored_prediction"
        probabilities = dict(prediction.get("probabilities") or {})
        base_confidence = str(prediction.get("confidence_label") or "observation_only")
        market_results = batch.get("market_results") or {}
        snapshot_id = str(prediction.get("source_snapshot_record_id") or (market_results.get("ouzhi") or {}).get("snapshot_record_id") or "")
        if snapshot_id:
            _, replay_features = _live_1x2_consensus(connection, fixture_id, target, snapshot_id, cutoff)
            prediction["features"] = {**dict(prediction.get("features") or {}), **replay_features}
        prediction["prediction_cutoff"] = cutoff.isoformat().replace("+00:00", "Z")
        prediction["published_at"] = available_at.isoformat().replace("+00:00", "Z")
    else:
        available_at = min(deadline, now) if now < kickoff else deadline
        batch = select_k1_batch_as_of(
            connection, fixture_id=fixture_id, target=target,
            prediction_cutoff=cutoff, available_at=available_at,
        )
        if batch is None:
            raise K1GuardrailError(f"no model-eligible K1 batch exists as of {target}: {fixture_id}")
        results = batch.get("market_results") or {}
        snapshot_id = str((results.get("ouzhi") or {}).get("snapshot_record_id") or "")
        consensus, features = _live_1x2_consensus(connection, fixture_id, target, snapshot_id, cutoff)
        if consensus is None:
            raise K1GuardrailError("selected K1 batch lacks a valid 1X2 consensus")
        probabilities = {"home": consensus[0], "draw": consensus[1], "away": consensus[2], "sum": sum(consensus)}
        registry = load_competition_registry(config.workspace)
        profile = registry.resolve(identity.get("competition_id"), identity.get("competition_name"))
        formats = load_competition_formats(config.workspace / "config" / "competition-formats.json")
        competition_format = formats.get(
            f"id:{identity.get('competition_id')}",
            formats.get(str(identity.get("competition_name") or ""), "unknown"),
        )
        sample = _automatic_evaluation_stats(connection, "research-shadow-v1", available_at).get(("16", target), {})
        confidence = confidence_assessment(
            registry,
            profile,
            consensus,
            bookmaker_count=int(features["bookmaker_count"]),
            direction_strength=float(features["direction_strength"]),
            bookmaker_dispersion=float(features["bookmaker_dispersion"]),
            automatic_verified_fixtures=int(sample.get("fixture_count", 0)),
            evaluation_span_days=float(sample.get("span_days", 0.0)),
            competition_format=competition_format,
        )
        base_confidence = str(confidence["confidence_label"])
        prediction = {
            "record_id": stable_id("ephemeral_k1_prediction", fixture_id, target, cutoff.isoformat()),
            "channel": "research-shadow-v1", "fixture_id": fixture_id, "target": target,
            "prediction_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "published_at": available_at.isoformat().replace("+00:00", "Z"),
            "competition_id": "16", "identity_record_id": identity["record_id"],
            "selected_batch_record_id": batch["record_id"], "probabilities": probabilities,
            "bookmaker_count": features.get("bookmaker_count"),
            "direction_strength": features.get("direction_strength"),
            "bookmaker_dispersion": features.get("bookmaker_dispersion"),
            "features": {**features, "competition_format": competition_format},
            **_profile_fields(registry, profile, identity),
            **confidence,
        }
        publication_source = "simulated_deadline" if available_at == deadline else "analysis_time"
    assessment = collect_k1_guardrail_assessment(
        connection, workspace=config.workspace, prediction=prediction, batch=batch,
        policy=policy, assessed_at=max(now, available_at), enforce_effective_at=False,
        enforce_reproducible_source=False,
    )
    if assessment is None:
        raise K1GuardrailError("K1 guardrail assessment was not produced")
    raw_features = assessment["raw_features"]
    market_update = raw_features.get("market_update") or {}
    previous_target = PREVIOUS_TARGET.get(target)
    if include_previous_simulation and previous_target and market_update.get("status") == "not_available":
        try:
            previous = _replay_one(
                connection,
                config=config,
                fixture_id=fixture_id,
                target=previous_target,
                policy=policy,
                now=now,
                prefer_stored_prediction=True,
                include_previous_simulation=False,
            )
            previous_features = previous["guardrail"]["raw_features"]
            current_components = _component_hashes(
                raw_features.get("base_input_fingerprint") or {},
                raw_features.get("guardrail_input_fingerprints") or {},
            )
            previous_components = _component_hashes(
                previous_features.get("base_input_fingerprint") or {},
                previous_features.get("guardrail_input_fingerprints") or {},
            )
            raw_features["market_update"] = _market_update_payload(
                current_components=current_components,
                current_probabilities=probabilities,
                current_features=raw_features,
                previous_target=previous_target,
                previous_components=previous_components,
                previous_probabilities=previous["base_probabilities"],
                previous_features=previous_features,
                comparison_time_source=(
                    "stored_prediction" if previous["publication_time_source"] == "stored_prediction"
                    else "simulated_previous_target"
                ),
            )
        except K1GuardrailError:
            pass
    guarded = guarded_presentation(
        probabilities=probabilities, base_confidence=base_confidence,
        action=str(assessment["proposed_action"]), policy=policy,
    )
    display_features = dict(raw_features)
    source_rows = display_features.pop("source_row_record_ids", [])
    display_features["source_row_record_count"] = len(source_rows)
    return {
        "fixture_id": fixture_id, "competition_id": "16", "target": target,
        "kickoff_at": kickoff.isoformat().replace("+00:00", "Z"),
        "prediction_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "available_at": available_at.isoformat().replace("+00:00", "Z"),
        "publication_time_source": publication_source,
        "identity_record_id": identity["record_id"], "selected_batch_record_id": batch["record_id"],
        "home_team_name": identity.get("home_team_name"), "away_team_name": identity.get("away_team_name"),
        "base_probabilities": probabilities, "base_confidence_label": base_confidence,
        "guardrail": {
            "policy_version": policy.policy_version,
            "policy_canonical_sha256": policy.canonical_sha256,
            "thresholds": policy.thresholds,
            "policy_was_active_at_cutoff": cutoff >= policy.effective_at,
            "action_code": assessment["proposed_action"],
            "action_label": guarded["action_label"],
            "guarded_direction": guarded["direction"],
            "guarded_confidence_label": guarded["confidence_label"],
            "rule_evaluations": assessment["rule_evaluations"],
            "rule_flags": assessment["rule_flags"],
            "reasons": assessment["reasons"],
            "raw_features": display_features,
        },
    }


def analyze_k1(
    config: ResearchConfig,
    *,
    fixture_id: str,
    target: str | None,
    latest_available_target: bool,
    audit: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    from football_cups.database.config import DatabaseConfig
    from football_cups.database.connection import connect

    observed = (now or datetime.now(UTC)).astimezone(UTC)
    policy = load_k1_guardrail_policy(config.workspace)
    with connect(DatabaseConfig.from_workspace(config.workspace)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        require_migration(connection)
        targets = [target] if target else sorted(policy.targets, key=lambda value: CUTOFFS[value][0], reverse=True)
        errors = []
        for candidate in targets:
            try:
                item = _replay_one(
                    connection, config=config, fixture_id=fixture_id, target=str(candidate),
                    policy=policy, now=observed, prefer_stored_prediction=True,
                )
                kickoff = _utc(item["kickoff_at"], "kickoff_at")
                cutoff = _utc(item["prediction_cutoff"], "prediction_cutoff")
                if latest_available_target and not (cutoff <= observed < kickoff):
                    continue
                from .k1_history_context import build_k1_historical_context

                probabilities = item["base_probabilities"]
                direction = max(("home", "draw", "away"), key=lambda name: float(probabilities.get(name, 0)))
                try:
                    history = build_k1_historical_context(
                        connection, workspace=config.workspace, analysis=item,
                    )
                except K1GuardrailError as exc:
                    history = {
                        "status": "unavailable", "reason": str(exc),
                        "comparison_scope": "final_closing_vs_as_of_cutoff_current",
                        "context_only": True, "probability_adjustment": False,
                        "guardrail_action_adjustment": False,
                    }
                action = str(item["guardrail"]["action_code"])
                workflow_features = item["guardrail"]["raw_features"]
                summaries = {
                    "keep": "护栏保持基础方向和置信。",
                    "caution": "护栏保留基础方向和置信，但要求突出已触发的结构风险。",
                    "downgrade": "护栏保留基础概率和方向，并将派生置信限制为low。",
                    "abstain": "护栏保留基础概率用于审计，但不输出护栏后方向。",
                }
                audit_summary = {"included": bool(audit)}
                if audit:
                    audit_summary.update({
                        "identity_record_id": item["identity_record_id"],
                        "selected_batch_record_id": item["selected_batch_record_id"],
                        "policy_canonical_sha256": item["guardrail"]["policy_canonical_sha256"],
                        "source_row_record_count": item["guardrail"]["raw_features"].get("source_row_record_count"),
                        "live_response_set_hashes": item["guardrail"]["raw_features"].get("live_response_set_hashes", []),
                        "analysis_workflow": workflow_features.get("analysis_workflow"),
                        "base_input_fingerprint": workflow_features.get("base_input_fingerprint"),
                        "guardrail_input_fingerprints": workflow_features.get("guardrail_input_fingerprints"),
                    })
                return {
                    "status": "dry_run", "persisted": False,
                    "analysis_context": {
                        "fixture_id": item["fixture_id"], "competition_id": "16",
                        "home_team_name": item["home_team_name"], "away_team_name": item["away_team_name"],
                        "kickoff_at": item["kickoff_at"], "target": item["target"],
                        "prediction_cutoff": item["prediction_cutoff"], "available_at": item["available_at"],
                        "publication_time_source": item["publication_time_source"],
                        "market_semantics": "as_of_cutoff_current",
                        "market_update": workflow_features.get("market_update", {"status": "not_available"}),
                        "analysis_workflow": workflow_features.get("analysis_workflow", {"status": "legacy_analysis_flow"}),
                    },
                    "base_prediction": {
                        "probabilities": probabilities, "direction": direction,
                        "confidence_label": item["base_confidence_label"],
                        "confidence_interpretation": workflow_features.get("confidence_interpretation", {}),
                        "sample_maturity": workflow_features.get("sample_maturity", {"status": "unvalidated"}),
                        "input_fingerprint": workflow_features.get("base_input_fingerprint", {}),
                    },
                    "historical_context": history,
                    "guardrail_assessment": {
                        "policy_version": item["guardrail"]["policy_version"],
                        "policy_canonical_sha256": item["guardrail"]["policy_canonical_sha256"],
                        "policy_was_active_at_cutoff": item["guardrail"]["policy_was_active_at_cutoff"],
                        "action_code": action, "rule_evaluations": item["guardrail"]["rule_evaluations"],
                        "rule_flags": item["guardrail"]["rule_flags"], "reasons": item["guardrail"]["reasons"],
                        "features": item["guardrail"]["raw_features"], "thresholds": item["guardrail"]["thresholds"],
                        "input_quality": workflow_features.get("input_quality", {"core_input_quality": "invalid", "auxiliary_rule_coverage": "limited"}),
                        "rule_evaluability": workflow_features.get("rule_evaluability", {}),
                        "input_fingerprints": workflow_features.get("guardrail_input_fingerprints", {}),
                    },
                    "guarded_output": {
                        "action_code": action, "action_label": item["guardrail"]["action_label"],
                        "direction": item["guardrail"]["guarded_direction"],
                        "confidence_label": item["guardrail"]["guarded_confidence_label"],
                        "summary": summaries[action],
                    },
                    "audit_summary": audit_summary,
                }
            except K1GuardrailError as exc:
                errors.append(f"{candidate}:{exc}")
        raise K1GuardrailError("no available K1 analysis target: " + "; ".join(errors))


def blind_test_k1_guardrail(
    config: ResearchConfig,
    *,
    fixture_ids: list[str] | None,
    since: datetime | None,
    until: datetime | None,
    targets: list[str],
    reveal_result: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    from football_cups.database.config import DatabaseConfig
    from football_cups.database.connection import connect

    observed = (now or datetime.now(UTC)).astimezone(UTC)
    policy = load_k1_guardrail_policy(config.workspace)
    with connect(DatabaseConfig.from_workspace(config.workspace)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        require_migration(connection)
        ids = list(dict.fromkeys(fixture_ids or []))
        if not ids:
            if since is None or until is None or since >= until:
                raise K1GuardrailError("a valid since/until range is required")
            rows = connection.execute(
                """SELECT DISTINCT fixture_id FROM football.fixture_identities
                   WHERE competition_id='16' AND kickoff_at >= %s AND kickoff_at < %s
                   ORDER BY fixture_id LIMIT 501""",
                (since, until),
            ).fetchall()
            ids = [str(row["fixture_id"]) for row in rows]
        if len(ids) > 500:
            raise K1GuardrailError("blind replay is limited to 500 fixtures")
        frozen = []
        errors = []
        for fixture_id in ids:
            for target in targets:
                try:
                    frozen.append(_replay_one(
                        connection, config=config, fixture_id=fixture_id, target=target,
                        policy=policy, now=observed, prefer_stored_prediction=True,
                    ))
                except K1GuardrailError as exc:
                    errors.append({"fixture_id": fixture_id, "target": target, "error": str(exc)})
        if reveal_result:
            result_rows = connection.execute(
                """SELECT fixture_id, home_goals, away_goals, record_id, verification_method
                   FROM football.current_verified_results WHERE fixture_id=ANY(%s)""",
                (ids,),
            ).fetchall()
            results = {str(row["fixture_id"]): dict(row) for row in result_rows}
            for item in frozen:
                item["revealed_result"] = results.get(item["fixture_id"])
    return {
        "status": "completed", "evaluation_mode": "retrospective_as_of_replay",
        "persisted": False, "forward_gate_eligible": False,
        "result_revealed": reveal_result, "fixture_count": len(ids),
        "replay_count": len(frozen), "replays": frozen, "errors": errors,
    }


def _holm_adjust(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1])
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - index) * value))
        adjusted[key] = running
    return adjusted


def _forward_descriptive_metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    points: list[tuple[tuple[float, float, float], int]] = []
    top_probabilities: list[float] = []
    hits = 0
    uniform_losses: list[float] = []
    uniform_briers: list[float] = []
    uniform_rps: list[float] = []
    for row in rows:
        payload = row.get("probabilities") or {}
        probabilities = tuple(float(payload.get(name, 0)) for name in ("home", "draw", "away"))
        if any(value <= 0 or value >= 1 for value in probabilities) or abs(sum(probabilities) - 1) > 1e-6:
            continue
        actual = 0 if row["home_goals"] > row["away_goals"] else 2 if row["home_goals"] < row["away_goals"] else 1
        top = max(range(3), key=lambda index: probabilities[index])
        hits += int(top == actual)
        top_probabilities.append(probabilities[top])
        points.append((probabilities, actual))
        uniform_losses.append(math.log(3))
        uniform_briers.append(2 / 3)
        actual_cumulative = (1.0 if actual == 0 else 0.0, 1.0 if actual <= 1 else 0.0)
        uniform_rps.append(sum((value - actual_value) ** 2 for value, actual_value in zip((1 / 3, 2 / 3), actual_cumulative)) / 2)
    scoring = _scoring(points)
    count = len(points)
    if not count:
        return {"fixture_count": 0}
    uniform = {
        "log_loss": sum(uniform_losses) / count,
        "brier": sum(uniform_briers) / count,
        "rps": sum(uniform_rps) / count,
    }
    return {
        "fixture_count": count,
        "direction_hit_count": hits,
        "direction_hit_rate": hits / count,
        "average_top_probability": sum(top_probabilities) / count,
        "calibration_residual": hits / count - sum(top_probabilities) / count,
        "log_loss": scoring["log_loss"],
        "brier": scoring["brier"],
        "rps": scoring["rps"],
        "ece": (
            {"status": "available", "value": scoring["ece"], "bins": scoring["ece_bins"]}
            if count >= 100 else {"status": "insufficient_sample", "minimum_size": 100}
        ),
        "uniform_baseline": uniform,
        "delta_vs_uniform": {
            "log_loss": scoring["log_loss"] - uniform["log_loss"],
            "brier": scoring["brier"] - uniform["brier"],
            "rps": scoring["rps"] - uniform["rps"],
        },
    }


def _guardrail_risk_metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    wrong = flagged = flagged_wrong = false_warning = keep = keep_wrong = 0
    abstain = abstain_avoided = downgrade_wrong = 0
    for row in rows:
        probabilities = row.get("probabilities") or {}
        predicted = max(("home", "draw", "away"), key=lambda name: float(probabilities.get(name, 0)))
        actual = "home" if row["home_goals"] > row["away_goals"] else "away" if row["home_goals"] < row["away_goals"] else "draw"
        is_wrong = predicted != actual
        wrong += int(is_wrong)
        action = str(row.get("proposed_action") or "keep")
        is_flagged = action in {"caution", "downgrade", "abstain"}
        flagged += int(is_flagged)
        flagged_wrong += int(is_flagged and is_wrong)
        false_warning += int(is_flagged and not is_wrong)
        keep += int(action == "keep")
        keep_wrong += int(action == "keep" and is_wrong)
        abstain += int(action == "abstain")
        abstain_avoided += int(action == "abstain" and is_wrong)
        downgrade_wrong += int(action == "downgrade" and is_wrong)
    flagged_rate = flagged_wrong / flagged if flagged else None
    keep_rate = keep_wrong / keep if keep else None
    return {
        "base_wrong_direction_count": wrong,
        "flagged_fixture_count": flagged,
        "flagged_wrong_direction_count": flagged_wrong,
        "error_capture_rate": flagged_wrong / wrong if wrong else None,
        "flagged_error_rate": flagged_rate,
        "keep_error_rate": keep_rate,
        "risk_lift_vs_keep": flagged_rate - keep_rate if flagged_rate is not None and keep_rate is not None else None,
        "false_warning_count": false_warning,
        "abstain_count": abstain,
        "abstain_avoided_wrong_count": abstain_avoided,
        "downgrade_wrong_count": downgrade_wrong,
    }


def _group_forward_metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "by_target": {}, "by_market_update_status": {}, "by_action": {}, "by_sample_maturity": {}, "by_rule": {},
    }
    for row in rows:
        raw = row.get("raw_features") or {}
        values = {
            "by_target": str(row.get("target")),
            "by_market_update_status": str((raw.get("market_update") or {}).get("status") or "legacy_analysis_flow"),
            "by_action": str(row.get("proposed_action")),
            "by_sample_maturity": str((raw.get("sample_maturity") or {}).get("status") or "legacy_analysis_flow"),
        }
        for dimension, value in values.items():
            dimensions[dimension].setdefault(value, []).append(row)
        for rule, evaluation in (row.get("rule_evaluations") or {}).items():
            dimensions["by_rule"].setdefault(f"{rule}|{evaluation.get('status')}", []).append(row)
    result = {
        dimension: {key: _forward_descriptive_metrics(group) for key, group in sorted(groups.items())}
        for dimension, groups in dimensions.items()
    }
    result["overall"] = _forward_descriptive_metrics(rows)
    result["guardrail_risk"] = _guardrail_risk_metrics(rows)
    return result


def evaluate_k1_guardrail_forward(config: ResearchConfig, *, channel: str, now: datetime | None = None) -> dict[str, Any]:
    from football_cups.database.config import DatabaseConfig
    from football_cups.database.connection import connect

    policy = load_k1_guardrail_policy(config.workspace)
    workflow = load_k1_analysis_workflow(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        require_migration(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT assessment.*, prediction.probabilities, prediction.status AS prediction_status,
                       result.home_goals, result.away_goals, result.confirmed_at,
                       result.record_id AS result_record_id, result.verification_method,
                       result.verification_status AS current_result_status
                FROM research.k1_guardrail_assessments AS assessment
                JOIN research.shadow_predictions AS prediction
                  ON prediction.record_id=assessment.prediction_record_id
                LEFT JOIN football.current_verified_results AS result
                  ON result.fixture_id=assessment.fixture_id
                WHERE assessment.channel=%s AND assessment.policy_version=%s
                ORDER BY assessment.prediction_cutoff, assessment.fixture_id, assessment.target
                """,
                (channel, policy.policy_version),
            ).fetchall()
        ]
    evaluated = [row for row in rows if row.get("home_goals") is not None and row.get("prediction_status") == "published" and row.get("audit_status") == "eligible"]
    if not evaluated:
        return {"status": "unchanged", "channel": channel, "policy_version": policy.policy_version, "evaluated_fixtures": 0}
    manual_methods = {"manual", "manual-import", "project-owner-manual-declaration"}
    automatic_rows = [row for row in evaluated if row.get("verification_method") not in manual_methods]
    if not automatic_rows:
        return {"status": "unchanged", "channel": channel, "policy_version": policy.policy_version, "evaluated_fixtures": 0}
    automatic_members = sorted(
        "|".join(str(row.get(name) or "") for name in (
            "record_id", "prediction_record_id", "fixture_id", "target", "result_record_id",
            "verification_method", "home_goals", "away_goals", "current_result_status",
        ))
        for row in automatic_rows
    )
    automatic_evidence_set_hash = hashlib.sha256("\n".join(automatic_members).encode()).hexdigest()
    all_members = sorted(
        "|".join(str(row.get(name) or "") for name in (
            "record_id", "prediction_record_id", "fixture_id", "target", "result_record_id",
            "verification_method", "home_goals", "away_goals", "current_result_status",
        ))
        for row in evaluated
    )
    all_result_sensitivity_hash = hashlib.sha256("\n".join(all_members).encode()).hexdigest()
    with connect(database_config) as connection:
        existing = connection.execute(
            """
            SELECT record_id FROM research.shadow_evaluations
            WHERE evaluation_kind='k1_guardrail_forward_v2'
              AND metrics->>'automatic_evidence_set_hash'=%s
            ORDER BY evaluated_at DESC, record_id DESC LIMIT 1
            """,
            (automatic_evidence_set_hash,),
        ).fetchone()
    if existing is not None:
        return {
            "status": "unchanged", "channel": channel, "policy_version": policy.policy_version,
            "automatic_evidence_set_hash": automatic_evidence_set_hash,
            "evaluation_record_id": existing["record_id"],
        }
    primary_rules = ("r1_shallow_favorite_cooling", "r2_asian_retreat")
    groups: dict[str, dict[str, Any]] = {}
    p_values: dict[str, float] = {}
    iterations = int(policy.forward_gate["bootstrap_iterations"])
    confidence = float(policy.forward_gate["active_confidence_level"])
    for target in policy.targets:
        target_rows = [row for row in evaluated if row["target"] == target and row.get("verification_method") not in manual_methods]
        if not target_rows:
            continue
        target_fixture_count = len({str(row["fixture_id"]) for row in target_rows})
        cutoffs = sorted(row["prediction_cutoff"] for row in target_rows)
        span_days = (cutoffs[-1] - cutoffs[0]).total_seconds() / 86400 if len(cutoffs) > 1 else 0.0
        for rule in primary_rules:
            hit_rows = [row for row in target_rows if (row.get("rule_evaluations") or {}).get(rule, {}).get("status") == "matched"]
            nonhit_rows = [row for row in target_rows if (row.get("rule_evaluations") or {}).get(rule, {}).get("status") == "not_matched"]
            residuals_by_week: dict[str, list[float]] = {}
            hit_residuals: list[float] = []
            points: list[tuple[tuple[float, float, float], int]] = []
            for row in hit_rows:
                probabilities = row.get("probabilities") or {}
                triplet = tuple(float(probabilities.get(name, 0)) for name in ("home", "draw", "away"))
                actual = 0 if row["home_goals"] > row["away_goals"] else 2 if row["home_goals"] < row["away_goals"] else 1
                favorite = 0 if triplet[0] >= triplet[2] else 2
                residual = (1.0 if actual == favorite else 0.0) - triplet[favorite]
                hit_residuals.append(residual)
                cutoff = row["prediction_cutoff"]
                week = f"{cutoff.isocalendar().year}-W{cutoff.isocalendar().week:02d}"
                residuals_by_week.setdefault(week, []).append(residual)
                points.append((triplet, actual))
            nonhit_residuals = []
            for row in nonhit_rows:
                probabilities = row.get("probabilities") or {}
                triplet = tuple(float(probabilities.get(name, 0)) for name in ("home", "draw", "away"))
                actual = 0 if row["home_goals"] > row["away_goals"] else 2 if row["home_goals"] < row["away_goals"] else 1
                favorite = 0 if triplet[0] >= triplet[2] else 2
                nonhit_residuals.append((1.0 if actual == favorite else 0.0) - triplet[favorite])
            key = f"{target}|{rule}"
            samples = _bootstrap_samples(residuals_by_week, iterations=iterations, seed=int(hashlib.sha256(f"{policy.canonical_sha256}|{key}".encode()).hexdigest()[:16], 16))
            samples.sort()
            upper = samples[min(len(samples) - 1, int(confidence * len(samples)))] if samples else None
            p_value = (1 + sum(value >= 0 for value in samples)) / (1 + len(samples)) if samples else 1.0
            p_values[key] = p_value
            residual = sum(hit_residuals) / len(hit_residuals) if hit_residuals else None
            nonhit = sum(nonhit_residuals) / len(nonhit_residuals) if nonhit_residuals else None
            relative = residual - nonhit if residual is not None and nonhit is not None else None
            ordered_residuals = [value for _, value in sorted(zip((row["prediction_cutoff"] for row in hit_rows), hit_residuals))]
            middle = len(ordered_residuals) // 2
            batch_one = ordered_residuals[:middle]
            batch_two = ordered_residuals[middle:]
            groups[key] = {
                "target": target, "rule_id": rule, "automatic_fixture_count": target_fixture_count,
                "evaluation_span_days": span_days, "rule_hit_count": len(hit_rows), "nonhit_count": len(nonhit_rows),
                "calibration_residual": residual, "relative_calibration_residual": relative,
                "batch_one_count": len(batch_one), "batch_two_count": len(batch_two),
                "batch_one_residual": sum(batch_one) / len(batch_one) if batch_one else None,
                "batch_two_residual": sum(batch_two) / len(batch_two) if batch_two else None,
                "one_sided_95_upper_bound": upper, "one_sided_p": p_value, "metrics": _scoring(points),
            }
    adjusted = _holm_adjust(p_values)
    gate = policy.forward_gate
    for key, group in groups.items():
        group["holm_adjusted_p"] = adjusted[key]
        group["review_eligible"] = all((
            group["automatic_fixture_count"] >= int(gate["minimum_automatic_fixtures"]),
            group["evaluation_span_days"] >= int(gate["minimum_span_days"]),
            group["rule_hit_count"] >= int(gate["minimum_rule_hits"]),
            group["batch_one_count"] >= int(gate["minimum_batch_hits"]),
            group["batch_two_count"] >= int(gate["minimum_batch_hits"]),
            group["batch_one_residual"] is not None and group["batch_one_residual"] < 0,
            group["batch_two_residual"] is not None and group["batch_two_residual"] < 0,
            group["calibration_residual"] is not None and group["calibration_residual"] <= float(gate["calibration_residual_maximum"]),
            group["relative_calibration_residual"] is not None and group["relative_calibration_residual"] <= float(gate["relative_residual_maximum"]),
            group["one_sided_95_upper_bound"] is not None and group["one_sided_95_upper_bound"] < 0,
            group["holm_adjusted_p"] < 0.05,
        ))
    fixture_ids = sorted({str(row["fixture_id"]) for row in evaluated})
    confirmed = [row["confirmed_at"] for row in automatic_rows if row.get("confirmed_at")]
    evaluated_through = max(confirmed).astimezone(UTC)
    payload = {
        "evaluation_kind": "k1_guardrail_forward_v2", "channel": channel,
        "policy_version": policy.policy_version, "evaluated_through": evaluated_through.isoformat().replace("+00:00", "Z"),
        "workflow_version": workflow.workflow_version,
        "automatic_evidence_set_hash": automatic_evidence_set_hash,
        "all_result_sensitivity_hash": all_result_sensitivity_hash,
        "evaluated_fixture_count": len(fixture_ids),
        "automatic_fixture_count": len({str(row["fixture_id"]) for row in evaluated if row.get("verification_method") not in manual_methods}),
        "manual_fixture_count": len({str(row["fixture_id"]) for row in evaluated if row.get("verification_method") in manual_methods}),
        "bootstrap_iterations": iterations, "active_confidence_level": confidence,
        "holm_family": sorted(groups), "groups": groups,
        "descriptive_metrics": {
            "automatic_results": _group_forward_metrics(automatic_rows),
            "all_valid_results": _group_forward_metrics(evaluated),
        },
        "review_eligible": any(group["review_eligible"] for group in groups.values()),
    }
    record_id = stable_id(
        "research_k1_guardrail_forward_v2", channel, policy.policy_version, workflow.workflow_version,
        automatic_evidence_set_hash, all_result_sensitivity_hash,
    )
    record = {
        "schema_version": 1, "record_type": "ResearchShadowEvaluation", "record_id": record_id,
        "research_only": True, "backfill": True, "strict_backtest_eligible": False,
        "cutoff_eligible": False, "research_kind": "model_artifact", "model_key": "k1-guardrail",
        "model_version": policy.policy_version, "evaluated_at": payload["evaluated_through"],
        "evaluation_kind": payload["evaluation_kind"], "dataset_hash": automatic_evidence_set_hash, "metrics": payload,
    }
    existing = None
    for path in (config.normalized_dir / "model-artifacts").rglob("*.jsonl") if (config.normalized_dir / "model-artifacts").is_dir() else []:
        if record_id in path.read_text(encoding="utf-8"):
            existing = path
            break
    if existing:
        return {"status": "unchanged", **payload, "record_path": str(existing)}
    run_id = evaluated_through.strftime("%Y%m%dT%H%M%S%fZ") + "-k1forward"
    with research_facts_lock(config):
        store = ResearchStore(config)
        report_path = store.write_report("k1-guardrail/forward", run_id, payload)
        record_path = store.write_records("model-artifacts", run_id, "k1-guardrail-forward", [record])
        store.write_manifest(run_id, "k1-guardrail-forward", {"schema_version": 1, "run_id": run_id, "status": "completed", "record_path": record_path.relative_to(config.research_dir).as_posix(), "report_path": report_path.relative_to(config.research_dir).as_posix()})
    return {"status": "completed", "run_id": run_id, "record_path": str(record_path), "report_path": str(report_path), **payload}


def evaluate_k1_guardrail_history(config: ResearchConfig, *, now: datetime | None = None) -> dict[str, Any]:
    policy = load_k1_guardrail_policy(config.workspace)
    csv_path = config.research_dir / "raw" / "blobs" / "e2" / f"{K1_DATASET_SHA256}.csv"
    metadata_path = config.research_dir / "raw" / "blobs" / "6e" / f"{K1_METADATA_SHA256}.json"
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != K1_DATASET_SHA256:
        raise K1GuardrailError("K1 CSV SHA-256 mismatch")
    if hashlib.sha256(metadata_path.read_bytes()).hexdigest() != K1_METADATA_SHA256:
        raise K1GuardrailError("K1 metadata SHA-256 mismatch")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if str(metadata.get("inputHash") or metadata.get("input_hash") or "").lower() != K1_INPUT_HASH:
        raise K1GuardrailError("K1 metadata input hash mismatch")
    feature_files = sorted((config.normalized_dir / "k1-derived-core3").rglob("k1-core3-features.jsonl"))
    if not feature_files:
        raise K1GuardrailError("normalized K1 feature rows are missing")
    rows = []
    for line in feature_files[-1].read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("record_type") == "ResearchFeatureRow":
            rows.append(record)
    seasons: dict[str, int] = {}
    fixture_ids = set()
    points: list[tuple[tuple[float, float, float], int]] = []
    residuals_by_week: dict[str, list[float]] = {}
    kickoffs: list[datetime] = []
    for row in rows:
        features = row["features"]
        season = str(row["season"])
        seasons[season] = seasons.get(season, 0) + 1
        fixture_ids.add(str(row["source_fixture_key"]))
        kickoff = _utc(features["kickoff"], "K1 kickoff")
        kickoffs.append(kickoff)
        probabilities = tuple(float(features[key]) for key in ("implied_home", "implied_draw", "implied_away"))
        total = sum(probabilities)
        probabilities = tuple(value / total for value in probabilities)
        outcome = {"homeWin": 0, "draw": 1, "awayWin": 2}[features["actual_direction"]]
        points.append((probabilities, outcome))
        favorite = 0 if probabilities[0] >= probabilities[2] else 2
        residual = (1.0 if outcome == favorite else 0.0) - probabilities[favorite]
        week = f"{kickoff.isocalendar().year}-W{kickoff.isocalendar().week:02d}"
        residuals_by_week.setdefault(week, []).append(residual)
    if len(rows) != 330 or len(fixture_ids) != 330 or seasons != {"2025": 228, "2026": 102}:
        raise K1GuardrailError("K1 historical row contract mismatch")
    if min(kickoffs).isoformat() != "2025-02-15T04:00:00+00:00" or max(kickoffs).isoformat() != "2026-07-12T10:30:00+00:00":
        raise K1GuardrailError("K1 historical kickoff range mismatch")
    seed = int(K1_DATASET_SHA256[:16], 16)
    payload = {
        "schema_version": 1,
        "evaluation_kind": "k1_guardrail_history_retrospective_proxy_v1",
        "dataset_sha256": K1_DATASET_SHA256,
        "metadata_sha256": K1_METADATA_SHA256,
        "input_hash": K1_INPUT_HASH,
        "row_count": len(rows),
        "unique_fixture_count": len(fixture_ids),
        "seasons": seasons,
        "kickoff_min": min(kickoffs).isoformat(),
        "kickoff_max": max(kickoffs).isoformat(),
        "historical_exact_evaluable": False,
        "not_available_in_dataset": ["paired_bookmaker_count", "company_support_ratio", "exact_r1", "exact_r2", "r4_handicap_cover_conflict", "r5_live_market_stability"],
        "metrics": _scoring(points),
        "favorite_calibration_residual": sum(value for values in residuals_by_week.values() for value in values) / len(rows),
        "block_bootstrap": _block_bootstrap(residuals_by_week, iterations=int(policy.forward_gate["bootstrap_iterations"]), confidence=float(policy.forward_gate["shadow_confidence_level"]), seed=seed),
        "review_eligible": False,
    }
    evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
    record_id = stable_id("research_k1_guardrail_history", policy.policy_version, K1_DATASET_SHA256, evaluated_at.isoformat())
    record = {
        "schema_version": 1, "record_type": "ResearchRetrospectiveEvaluation", "record_id": record_id,
        "research_only": True, "backfill": True, "strict_backtest_eligible": False,
        "cutoff_eligible": False, "research_kind": "model_artifact", "model_key": "k1-guardrail",
        "model_version": policy.policy_version, "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "evaluation_kind": payload["evaluation_kind"], "dataset_hash": K1_DATASET_SHA256,
        "metrics": payload,
    }
    run_id = evaluated_at.strftime("%Y%m%dT%H%M%S%fZ") + "-k1guard"
    with research_facts_lock(config):
        store = ResearchStore(config)
        report_path = store.write_report("k1-guardrail/history", run_id, payload)
        record_path = store.write_records("model-artifacts", run_id, "k1-guardrail-history", [record])
        store.write_manifest(run_id, "k1-guardrail-history", {"schema_version": 1, "run_id": run_id, "status": "completed", "record_path": record_path.relative_to(config.research_dir).as_posix(), "report_path": report_path.relative_to(config.research_dir).as_posix()})
    return {"status": "completed", "run_id": run_id, "report_path": str(report_path), **payload}
