from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from football_cups.collector.markets import market_row_role

from .competition_profiles import market_statistics, valid_sha256
from .config import ResearchConfig
from .storage import ResearchStore, research_facts_lock, stable_id


K1_DATASET_SHA256 = "e26210d45df9d691bb81b68c078d494705ddb0aadad73ebc1faae4de36b7a931"
K1_METADATA_SHA256 = "6e7452951c098e30afd47ea2cca729c94b9fe4609011e463ff0e5d3add20d710"
K1_INPUT_HASH = "6285cc00625cb1675881c4c8ec41e8d8938ca5402371d95902809bc3b3344455"
TARGETS = frozenset({"T-24h", "T-6h", "T-60m", "T-10m"})
ACTIONS = frozenset({"keep", "caution", "downgrade", "abstain"})
RULE_STATES = frozenset({"matched", "not_matched", "not_evaluable"})
RELEVANT_PATHS = (
    "src/football_cups/research",
    "src/football_cups/database/migrations/014_research_k1_guardrail_assessments.sql",
    "config/research-k1-guardrail.json",
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
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


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
    if favorite_index in {0, 2}:
        for row in handicap_selected.values():
            line = _decimal(row.get("handicap_line"))
            probabilities = [_decimal(row.get(key)) for key in ("home_probability", "draw_probability", "away_probability")]
            if line is None or line == 0 or any(value is None or value < 0 for value in probabilities):
                continue
            if (favorite_index == 0 and line >= 0) or (favorite_index == 2 and line <= 0):
                continue
            total = sum(probabilities)  # type: ignore[arg-type]
            if total <= 0:
                continue
            normalized = [value / total for value in probabilities]  # type: ignore[operator]
            cover = normalized[favorite_index]
            handicap_valid.append(float((Decimal(1) - cover) - cover))
    margin = float(policy.thresholds["handicap_non_cover_margin"])
    features.update({
        "handicap_index_valid_bookmakers": len(handicap_valid),
        "handicap_index_conflicts": handicap_conflicts,
        "handicap_index_conflict_support_ratio": _ratio(sum(value >= margin for value in handicap_valid), len(handicap_valid)),
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
    r5 = r5_evaluable and float(features.get("live_line_range") or math.inf) <= float(t["live_line_range"]) and float(features.get("live_probability_range") or math.inf) <= float(t["live_probability_range"])
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
    manifest_path = research_dir / "manifests" / run_id / "shadow-predictions.json"
    if not manifest_path.is_file():
        raise K1GuardrailError(f"shadow prediction batch lacks completed manifest: {run_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise K1GuardrailError(f"invalid shadow prediction manifest: {run_id}") from exc
    content = record_path.read_bytes()
    records = [json.loads(line) for line in content.splitlines() if line.strip()]
    expected_path = record_path.relative_to(research_dir).as_posix()
    has_guardrail = any(record.get("record_type") == "ResearchK1GuardrailAssessment" for record in records)
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
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise K1GuardrailError(f"shadow prediction manifest mismatch ({run_id}): {', '.join(failed)}")


def collect_k1_guardrail_assessment(
    connection,
    *,
    workspace: Path,
    prediction: Mapping[str, Any],
    batch: Mapping[str, Any],
    policy: K1GuardrailPolicy,
    assessed_at: datetime,
) -> dict[str, Any] | None:
    if str(prediction.get("competition_id") or "") != policy.competition_id:
        return None
    cutoff = _utc(prediction.get("prediction_cutoff"), "prediction_cutoff")
    published = _utc(prediction.get("published_at"), "published_at")
    assessed_at = assessed_at.astimezone(UTC)
    if cutoff < policy.effective_at:
        return None
    fingerprint = relevant_source_fingerprint(workspace)
    if published < cutoff or assessed_at < published:
        return unavailable_assessment(prediction, policy, assessed_at, "invalid_assessment_time_order", fingerprint)
    if fingerprint["relevant_dirty_paths"]:
        return unavailable_assessment(prediction, policy, assessed_at, "relevant_source_not_reproducible", fingerprint)
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
                   source_row_index, source_page_sha256, observed_at
            FROM football.current_bookmaker_market_rows
            WHERE fixture_id=%s AND target=%s AND market=%s
              AND source_snapshot_record_id=%s
              AND event_origin='live' AND normalization_version=2
              AND observed_at <= %s
            ORDER BY source_row_index, record_id
            """,
            (str(prediction["fixture_id"]), prediction["target"], market, snapshot_id, cutoff),
        ).fetchall()
        market_rows[market] = [dict(row) for row in rows]
    handicap_rows: list[Mapping[str, Any]] = []
    if snapshot_ids.get("rangqiu"):
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
    features, feature_reasons = build_guardrail_features(market_rows, handicap_rows, policy)
    hard_reasons.extend(feature_reasons)
    assessment = assess_guardrail_features(features, policy, hard_reasons)
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


def _holm_adjust(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1])
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - index) * value))
        adjusted[key] = running
    return adjusted


def evaluate_k1_guardrail_forward(config: ResearchConfig, *, channel: str, now: datetime | None = None) -> dict[str, Any]:
    from football_cups.database.config import DatabaseConfig
    from football_cups.database.connection import connect

    policy = load_k1_guardrail_policy(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        require_migration(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT assessment.*, prediction.probabilities, prediction.status AS prediction_status,
                       result.home_goals, result.away_goals, result.confirmed_at,
                       result.verification_method
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
                "bootstrap_upper": upper, "one_sided_p": p_value, "metrics": _scoring(points),
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
            group["bootstrap_upper"] is not None and group["bootstrap_upper"] < 0,
            group["holm_adjusted_p"] < 0.05,
        ))
    fixture_ids = sorted({str(row["fixture_id"]) for row in evaluated})
    fixture_set_hash = hashlib.sha256("\n".join(fixture_ids).encode()).hexdigest()
    confirmed = [row["confirmed_at"] for row in evaluated if row.get("confirmed_at")]
    evaluated_through = max(confirmed).astimezone(UTC)
    payload = {
        "evaluation_kind": "k1_guardrail_forward_v1", "channel": channel,
        "policy_version": policy.policy_version, "evaluated_through": evaluated_through.isoformat().replace("+00:00", "Z"),
        "evaluation_fixture_set_hash": fixture_set_hash, "evaluated_fixture_count": len(fixture_ids),
        "automatic_fixture_count": len({str(row["fixture_id"]) for row in evaluated if row.get("verification_method") not in manual_methods}),
        "manual_fixture_count": len({str(row["fixture_id"]) for row in evaluated if row.get("verification_method") in manual_methods}),
        "bootstrap_iterations": iterations, "active_confidence_level": confidence,
        "holm_family": sorted(groups), "groups": groups,
        "review_eligible": any(group["review_eligible"] for group in groups.values()),
    }
    record_id = stable_id("research_k1_guardrail_forward", channel, policy.policy_version, payload["evaluated_through"], fixture_set_hash)
    record = {
        "schema_version": 1, "record_type": "ResearchShadowEvaluation", "record_id": record_id,
        "research_only": True, "backfill": True, "strict_backtest_eligible": False,
        "cutoff_eligible": False, "research_kind": "model_artifact", "model_key": "k1-guardrail",
        "model_version": policy.policy_version, "evaluated_at": payload["evaluated_through"],
        "evaluation_kind": payload["evaluation_kind"], "dataset_hash": fixture_set_hash, "metrics": payload,
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
