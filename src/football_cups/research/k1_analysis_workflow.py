from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WORKFLOW_VERSION = "k1-analysis-flow-v2"
UPDATE_COMPONENTS = (
    "base_ouzhi",
    "guardrail_ouzhi",
    "guardrail_yazhi",
    "guardrail_daxiao",
)
PREVIOUS_TARGET = {
    "T-24h": None,
    "T-6h": "T-24h",
    "T-60m": "T-6h",
    "T-10m": "T-60m",
}


class K1AnalysisWorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class K1AnalysisWorkflow:
    path: Path
    payload: dict[str, Any]
    workflow_version: str
    effective_at: datetime
    diagnostic_minimum_automatic_fixtures: int
    daily_evaluation: dict[str, Any]
    file_sha256: str
    canonical_sha256: str


def _utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise K1AnalysisWorkflowError("workflow effective_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise K1AnalysisWorkflowError("workflow effective_at must include a timezone")
    return parsed.astimezone(UTC)


def load_k1_analysis_workflow(workspace: Path) -> K1AnalysisWorkflow:
    path = workspace.resolve() / "config" / "research-k1-analysis-workflow.json"
    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise K1AnalysisWorkflowError(f"invalid K1 analysis workflow: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise K1AnalysisWorkflowError("unsupported K1 analysis workflow schema")
    if payload.get("workflow_version") != WORKFLOW_VERSION:
        raise K1AnalysisWorkflowError("unsupported K1 analysis workflow version")
    diagnostic = payload.get("diagnostic_minimum_automatic_fixtures")
    if isinstance(diagnostic, bool) or not isinstance(diagnostic, int) or diagnostic < 1:
        raise K1AnalysisWorkflowError("diagnostic fixture minimum must be a positive integer")
    schedule = payload.get("daily_evaluation")
    if not isinstance(schedule, dict) or not isinstance(schedule.get("enabled"), bool):
        raise K1AnalysisWorkflowError("daily_evaluation must define enabled")
    try:
        ZoneInfo(str(schedule.get("timezone") or ""))
    except ZoneInfoNotFoundError as exc:
        raise K1AnalysisWorkflowError("daily evaluation timezone is invalid") from exc
    local_time = str(schedule.get("local_time") or "")
    try:
        parsed_time = datetime.strptime(local_time, "%H:%M")
    except ValueError as exc:
        raise K1AnalysisWorkflowError("daily evaluation local_time must be HH:MM") from exc
    if parsed_time.strftime("%H:%M") != local_time:
        raise K1AnalysisWorkflowError("daily evaluation local_time must be zero-padded HH:MM")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return K1AnalysisWorkflow(
        path=path,
        payload=payload,
        workflow_version=WORKFLOW_VERSION,
        effective_at=_utc(payload.get("effective_at")),
        diagnostic_minimum_automatic_fixtures=diagnostic,
        daily_evaluation=dict(schedule),
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def canonical_decimal(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def canonical_market_fingerprint(
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    normalized = []
    for row in rows:
        name = str(row.get("source_bookmaker_name") or "").strip()
        if not name:
            continue
        values = {
            field: (
                str(row.get(field) or "none").strip().lower()
                if field == "line_movement"
                else canonical_decimal(row.get(field))
            )
            for field in fields
        }
        if any(value is None for value in values.values()):
            continue
        normalized.append({"source_bookmaker_name": name, **values})
    normalized.sort(key=lambda row: (row["source_bookmaker_name"], *(row[field] for field in fields)))
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bookmaker_count": len(normalized),
    }


def classify_market_update(
    current: Mapping[str, str],
    previous: Mapping[str, str] | None,
) -> tuple[str, list[str]]:
    if previous is None or any(not previous.get(component) for component in UPDATE_COMPONENTS):
        return "not_available", []
    changed = [component for component in UPDATE_COMPONENTS if current.get(component) != previous.get(component)]
    if not changed:
        return "unchanged", []
    if len(changed) == len(UPDATE_COMPONENTS):
        return "full_update", changed
    return "partial_update", changed


def direction_strength_label(direction_strength: float, confidence_policy: Mapping[str, Any]) -> str:
    if not math.isfinite(direction_strength) or direction_strength < 0:
        raise K1AnalysisWorkflowError("direction strength must be a finite non-negative number")
    if direction_strength >= float(confidence_policy["high"]["minimum_direction_margin"]):
        return "strong"
    if direction_strength >= float(confidence_policy["medium"]["minimum_direction_margin"]):
        return "moderate"
    return "weak"


def sample_maturity(
    *,
    automatic_fixture_count: int,
    span_days: float,
    diagnostic_minimum: int,
    sample_gate_minimum: int,
    sample_gate_span_days: float,
) -> str:
    if automatic_fixture_count < diagnostic_minimum:
        return "unvalidated"
    if automatic_fixture_count < sample_gate_minimum or span_days < sample_gate_span_days:
        return "provisional"
    return "sample_gate_met"


def probability_delta(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, float] | None:
    if previous is None:
        return None
    return {name: float(current[name]) - float(previous[name]) for name in ("home", "draw", "away")}
