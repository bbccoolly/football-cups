from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable


TIERS = frozenset({"A", "B", "C", "D"})
COMPETITION_TYPES = frozenset(
    {
        "domestic_league",
        "lower_evidence_league",
        "continental_competition",
        "international_competition",
        "unknown",
    }
)
CLASSIFICATION_STATUSES = frozenset({"provisional", "reviewed"})
CONFIDENCE_LEVELS = ("observation_only", "low", "medium", "high")
CONFIDENCE_RANK = {value: index for index, value in enumerate(CONFIDENCE_LEVELS)}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CompetitionProfile:
    competition_id: str | None
    canonical_name: str
    competition_type: str
    market_evidence_tier: str
    evaluation_group: str
    classification_status: str
    classification_reason: str
    confidence_cap: str
    conflict: bool = False
    unregistered: bool = False


@dataclass(frozen=True)
class CompetitionRegistry:
    path: Path
    registry_version: str
    policy_version: str
    competitions: dict[str, CompetitionProfile]
    aliases: dict[str, str]
    confidence_policy: dict[str, Any]
    file_sha256: str
    canonical_sha256: str

    def resolve(self, competition_id: Any, competition_name: Any) -> CompetitionProfile:
        raw_id = str(competition_id or "").strip()
        normalized_name = normalize_competition_name(competition_name)
        id_profile = self.competitions.get(raw_id) if raw_id else None
        alias_id = self.aliases.get(normalized_name) if normalized_name else None
        if id_profile is not None:
            if alias_id is not None and alias_id != raw_id:
                return _unknown_profile(conflict=True)
            return id_profile
        if raw_id:
            return _unknown_profile()
        if alias_id is not None:
            return self.competitions[alias_id]
        return _unknown_profile()


def normalize_competition_name(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    return " ".join(text.split())


def _unknown_profile(*, conflict: bool = False) -> CompetitionProfile:
    return CompetitionProfile(
        competition_id=None,
        canonical_name="unknown",
        competition_type="unknown",
        market_evidence_tier="D",
        evaluation_group="unknown",
        classification_status="provisional",
        classification_reason=("Competition identity conflicts with registry" if conflict else "Competition is not registered"),
        confidence_cap="observation_only",
        conflict=conflict,
        unregistered=not conflict,
    )


def _require_number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return result


def _validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise ValueError("confidence_policy must be an object")
    minimum = policy.get("minimum_bookmakers")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 3:
        raise ValueError("minimum_bookmakers must be an integer of at least 3")
    for level in ("high", "medium"):
        block = policy.get(level)
        if not isinstance(block, dict):
            raise ValueError(f"confidence_policy.{level} must be an object")
        count = block.get("minimum_bookmakers")
        if isinstance(count, bool) or not isinstance(count, int) or count < minimum:
            raise ValueError(f"confidence_policy.{level}.minimum_bookmakers is invalid")
        direction = _require_number(
            block.get("minimum_direction_margin"), f"{level}.minimum_direction_margin"
        )
        dispersion = _require_number(block.get("maximum_dispersion"), f"{level}.maximum_dispersion")
        if direction > 1 or dispersion > 1:
            raise ValueError(f"confidence_policy.{level} probability thresholds cannot exceed 1")
    tail = policy.get("strong_favorite_draw_tail")
    gate = policy.get("high_confidence_gate")
    if not isinstance(tail, dict) or not isinstance(gate, dict):
        raise ValueError("confidence policy tail and high-confidence gate are required")
    favorite = _require_number(
        tail.get("minimum_favorite_probability"), "minimum_favorite_probability"
    )
    draw = _require_number(tail.get("minimum_draw_probability"), "minimum_draw_probability")
    if favorite > 1 or draw > 1:
        raise ValueError("strong favorite probability thresholds cannot exceed 1")
    fixture_count = gate.get("minimum_automatic_verified_fixtures")
    span_days = gate.get("minimum_span_days")
    if isinstance(fixture_count, bool) or not isinstance(fixture_count, int) or fixture_count < 1:
        raise ValueError("minimum_automatic_verified_fixtures must be a positive integer")
    if isinstance(span_days, bool) or not isinstance(span_days, int) or span_days < 1:
        raise ValueError("minimum_span_days must be a positive integer")
    if policy["high"]["minimum_bookmakers"] < policy["medium"]["minimum_bookmakers"]:
        raise ValueError("high confidence bookmaker threshold cannot be lower than medium")
    if policy["high"]["minimum_direction_margin"] < policy["medium"]["minimum_direction_margin"]:
        raise ValueError("high confidence direction threshold cannot be lower than medium")
    if policy["high"]["maximum_dispersion"] > policy["medium"]["maximum_dispersion"]:
        raise ValueError("high confidence dispersion threshold cannot exceed medium")
    return policy


def load_competition_registry(workspace: Path) -> CompetitionRegistry:
    path = workspace.resolve() / "config" / "research-competition-profiles.json"
    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid competition profile registry: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported competition profile registry schema")
    registry_version = str(payload.get("registry_version") or "").strip()
    policy_version = str(payload.get("policy_version") or "").strip()
    if not registry_version or not policy_version:
        raise ValueError("registry_version and policy_version are required")
    raw_competitions = payload.get("competitions")
    if not isinstance(raw_competitions, dict):
        raise ValueError("competitions must be an object")
    competitions: dict[str, CompetitionProfile] = {}
    aliases: dict[str, str] = {}
    for competition_id, raw in raw_competitions.items():
        if not isinstance(competition_id, str) or not competition_id.isdigit():
            raise ValueError("competition ids must be digit strings")
        if not isinstance(raw, dict):
            raise ValueError(f"competition {competition_id} must be an object")
        competition_type = str(raw.get("competition_type") or "")
        tier = str(raw.get("market_evidence_tier") or "")
        status = str(raw.get("classification_status") or "")
        cap = str(raw.get("confidence_cap") or "")
        canonical_name = normalize_competition_name(raw.get("canonical_name"))
        evaluation_group = str(raw.get("evaluation_group") or "").strip()
        reason = str(raw.get("classification_reason") or "").strip()
        if competition_type not in COMPETITION_TYPES - {"unknown"}:
            raise ValueError(f"competition {competition_id} has invalid competition_type")
        if tier not in TIERS - {"D"}:
            raise ValueError(f"competition {competition_id} has invalid market_evidence_tier")
        if status not in CLASSIFICATION_STATUSES or cap not in CONFIDENCE_RANK:
            raise ValueError(f"competition {competition_id} has invalid classification or confidence")
        if not canonical_name or not evaluation_group or not reason:
            raise ValueError(f"competition {competition_id} is missing profile metadata")
        if tier == "C" and CONFIDENCE_RANK[cap] > CONFIDENCE_RANK["low"]:
            raise ValueError(f"competition {competition_id} C tier cap cannot exceed low")
        profile = CompetitionProfile(
            competition_id=competition_id,
            canonical_name=canonical_name,
            competition_type=competition_type,
            market_evidence_tier=tier,
            evaluation_group=evaluation_group,
            classification_status=status,
            classification_reason=reason,
            confidence_cap=cap,
        )
        competitions[competition_id] = profile
        raw_aliases = raw.get("name_aliases")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise ValueError(f"competition {competition_id} must have name_aliases")
        for alias in [canonical_name, *raw_aliases]:
            normalized = normalize_competition_name(alias)
            if not normalized:
                raise ValueError(f"competition {competition_id} has an empty alias")
            prior = aliases.get(normalized)
            if prior is not None and prior != competition_id:
                raise ValueError(f"competition alias maps to multiple ids: {normalized}")
            aliases[normalized] = competition_id
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CompetitionRegistry(
        path=path,
        registry_version=registry_version,
        policy_version=policy_version,
        competitions=competitions,
        aliases=aliases,
        confidence_policy=_validate_policy(payload.get("confidence_policy")),
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def market_statistics(
    bookmaker_probabilities: Iterable[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], float, float]:
    rows = list(bookmaker_probabilities)
    if not rows:
        raise ValueError("bookmaker probabilities are required")
    components = tuple(tuple(row[index] for row in rows) for index in range(3))
    medians = tuple(median(component) for component in components)
    total = sum(medians)
    if total <= 0:
        raise ValueError("probability consensus is invalid")
    consensus = tuple(value / total for value in medians)
    ordered = sorted(consensus, reverse=True)
    direction_strength = ordered[0] - ordered[1]
    component_mads = [median(abs(value - median(component)) for value in component) for component in components]
    return consensus, direction_strength, max(component_mads)


def confidence_assessment(
    registry: CompetitionRegistry,
    profile: CompetitionProfile,
    probabilities: tuple[float, float, float],
    *,
    bookmaker_count: int,
    direction_strength: float,
    bookmaker_dispersion: float,
    automatic_verified_fixtures: int,
    evaluation_span_days: float,
    competition_format: str,
) -> dict[str, Any]:
    policy = registry.confidence_policy
    raw = "low"
    for label in ("high", "medium"):
        threshold = policy[label]
        if (
            bookmaker_count >= threshold["minimum_bookmakers"]
            and direction_strength >= threshold["minimum_direction_margin"]
            and bookmaker_dispersion <= threshold["maximum_dispersion"]
        ):
            raw = label
            break
    reasons: list[str] = []
    caps = [profile.confidence_cap]
    if profile.classification_status == "provisional":
        caps.append("medium")
        reasons.append("provisional_classification")
    gate = policy["high_confidence_gate"]
    sample_gate_passed = (
        automatic_verified_fixtures >= gate["minimum_automatic_verified_fixtures"]
        and evaluation_span_days >= gate["minimum_span_days"]
    )
    if not sample_gate_passed:
        caps.append("medium")
        reasons.append("insufficient_automatic_evaluation_sample")
    final = min([raw, *caps], key=lambda value: CONFIDENCE_RANK[value])
    if CONFIDENCE_RANK[final] < CONFIDENCE_RANK[raw]:
        reasons.append("competition_confidence_cap")
    risks: list[str] = []
    if profile.market_evidence_tier == "C":
        risks.append("low_market_evidence_tier")
    if profile.unregistered:
        risks.append("unregistered_competition")
    if profile.conflict:
        risks.append("competition_profile_conflict")
    if direction_strength < policy["medium"]["minimum_direction_margin"]:
        risks.append("weak_direction_margin")
    if bookmaker_dispersion > policy["medium"]["maximum_dispersion"]:
        risks.append("high_bookmaker_dispersion")
    if not sample_gate_passed:
        risks.append("small_competition_evaluation_sample")
    tail = policy["strong_favorite_draw_tail"]
    top_index = max(range(3), key=lambda index: probabilities[index])
    if (
        top_index in {0, 2}
        and probabilities[top_index] >= tail["minimum_favorite_probability"]
        and probabilities[1] >= tail["minimum_draw_probability"]
    ):
        risks.append("strong_favorite_draw_tail")
    if competition_format != "regular_time_only":
        risks.append("result_scope_verification_risk")
    return {
        "raw_confidence_label": raw,
        "competition_confidence_cap": profile.confidence_cap,
        "confidence_label": final,
        "confidence_reasons": sorted(set(reasons)),
        "risk_flags": sorted(set(risks)),
        "automatic_verified_fixture_count": automatic_verified_fixtures,
        "evaluation_span_days": evaluation_span_days,
        "review_eligible": sample_gate_passed,
    }


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None
