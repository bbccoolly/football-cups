from __future__ import annotations

import hashlib
import json
import math
import subprocess
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from football_cups.collector.config import CUTOFFS
from football_cups.collector.storage import make_run_id
from football_cups.collector.timeutil import iso_utc, utc_now
from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import connect

from . import MODEL_ARTIFACT_FLAGS, SHADOW_EVENT_FLAGS
from .competition_profiles import valid_sha256
from .config import ResearchConfig
from .storage import ResearchStore, research_facts_lock, stable_id


TARGETS = ("T-24h", "T-6h", "T-60m", "T-10m")
ACTIONS = frozenset({"keep", "caution", "downgrade", "abstain"})
RULE_STATES = frozenset({"matched", "not_matched", "not_evaluable"})
CHANNEL_DEFAULT = "research-europe-guardrail-v1"
MODEL_KEY = "europe-market-difference-guardrail"
RELEVANT_PATHS = (
    "src/football_cups/research/europe_guardrail.py",
    "src/football_cups/research/storage.py",
    "src/football_cups/research/database.py",
    "src/football_cups/database/migrations/015_research_europe_guardrail_assessments.sql",
    "config/research-europe-guardrail.json",
    "scripts/windows/run_shadow_prediction.ps1",
)


class EuropeGuardrailError(ValueError):
    pass


@dataclass(frozen=True)
class EuropeGuardrailPolicy:
    path: Path
    payload: dict[str, Any]
    policy_version: str
    policy_revision: int
    status: str
    effective_at: datetime
    competition_ids: tuple[str, ...]
    targets: tuple[str, ...]
    thresholds: dict[str, float | int]
    forward_gate: dict[str, int]
    input_policy: dict[str, Any]
    presentation_policy: dict[str, Any]
    file_sha256: str
    canonical_sha256: str


def _utc(value: Any, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EuropeGuardrailError(f"{label} must be RFC3339") from exc
    if result.tzinfo is None:
        raise EuropeGuardrailError(f"{label} must include timezone")
    return result.astimezone(UTC)


def _number(
    block: Mapping[str, Any], name: str, *, minimum: float, maximum: float | None = None
) -> float:
    value = block.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EuropeGuardrailError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise EuropeGuardrailError(f"{name} is outside its allowed range")
    return result


def load_europe_guardrail_policy(workspace: Path) -> EuropeGuardrailPolicy:
    path = workspace.resolve() / "config" / "research-europe-guardrail.json"
    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EuropeGuardrailError(f"invalid Europe guardrail policy: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise EuropeGuardrailError("unsupported Europe guardrail policy schema")
    version = str(payload.get("policy_version") or "").strip()
    revision = payload.get("policy_revision")
    status = str(payload.get("status") or "").strip()
    competition_ids = payload.get("competition_ids")
    targets = payload.get("targets")
    if not version or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise EuropeGuardrailError("policy version and positive revision are required")
    if status != "shadow":
        raise EuropeGuardrailError("Europe guardrail only accepts status=shadow")
    if competition_ids != ["63", "101"]:
        raise EuropeGuardrailError("Europe guardrail competition_ids must be 63 and 101")
    if not isinstance(targets, list) or tuple(targets) != TARGETS:
        raise EuropeGuardrailError("Europe guardrail targets must match product cutoffs")
    expected_input = {
        "opening_source": "provider_declared_opening_from_selected_v2_row",
        "close_source": "selected_v2_row_current",
        "close_semantics": "as_of_cutoff_current",
        "batch_selection": "latest_model_eligible_batch_at_or_before_cutoff",
        "cross_target_mixing": False,
        "cross_batch_market_mixing": False,
        "bookmaker_identity": "source_id_then_exact_normalized_name",
    }
    if payload.get("input_policy") != expected_input:
        raise EuropeGuardrailError("invalid Europe guardrail input policy")
    presentation = payload.get("presentation_policy")
    expected_presentation = {
        "version": "europe-guardrail-presentation-v1",
        "keep": {"label": "保持", "confidence_action": "unchanged"},
        "caution": {"label": "谨慎", "confidence_action": "unchanged"},
        "downgrade": {"label": "降置信", "confidence_cap": "low"},
        "abstain": {"label": "回避", "direction_action": "suppress"},
    }
    if presentation != expected_presentation:
        raise EuropeGuardrailError("invalid Europe guardrail presentation policy")
    thresholds = payload.get("thresholds")
    gate = payload.get("forward_gate")
    if not isinstance(thresholds, dict) or not isinstance(gate, dict):
        raise EuropeGuardrailError("thresholds and forward_gate are required")
    checked: dict[str, float | int] = {}
    integer_minimums = {
        "minimum_bookmakers_per_market": 3,
        "minimum_institution_analysis_bookmakers": 3,
        "trajectory_observation_count": 3,
        "trajectory_span_seconds": 1,
    }
    for name, minimum in integer_minimums.items():
        value = thresholds.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise EuropeGuardrailError(f"{name} must be an integer of at least {minimum}")
        checked[name] = value
    for name in (
        "weak_direction_gap",
        "material_probability_move",
        "signal_support_ratio",
        "high_dispersion",
        "absolute_anomaly_floor",
        "leave_one_out_probability_shift",
        "unchanged_probability_tolerance",
    ):
        checked[name] = _number(thresholds, name, minimum=0, maximum=1)
    for name in (
        "material_asian_line_move",
        "material_total_line_move",
        "robust_z_threshold",
        "source_margin_minimum",
        "source_margin_maximum",
    ):
        checked[name] = _number(thresholds, name, minimum=0)
    if checked["signal_support_ratio"] <= 0 or checked["source_margin_minimum"] >= checked["source_margin_maximum"]:
        raise EuropeGuardrailError("invalid support ratio or source margin range")
    checked_gate: dict[str, int] = {}
    for name in ("minimum_automatic_fixtures", "minimum_span_days", "minimum_rule_hits"):
        value = gate.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EuropeGuardrailError(f"{name} must be a positive integer")
        checked_gate[name] = value
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return EuropeGuardrailPolicy(
        path=path,
        payload=payload,
        policy_version=version,
        policy_revision=revision,
        status=status,
        effective_at=_utc(payload.get("effective_at"), "effective_at"),
        competition_ids=tuple(competition_ids),
        targets=tuple(targets),
        thresholds=checked,
        forward_gate=checked_gate,
        input_policy=dict(expected_input),
        presentation_policy=dict(presentation),
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _median(values: Iterable[float]) -> float | None:
    selected = [value for value in values if math.isfinite(value)]
    return float(median(selected)) if selected else None


def _mad(values: Iterable[float], center: float | None = None) -> float | None:
    selected = [value for value in values if math.isfinite(value)]
    if not selected:
        return None
    middle = float(median(selected)) if center is None else center
    return float(median(abs(value - middle) for value in selected))


def _normalize(values: Iterable[float]) -> tuple[float, ...] | None:
    selected = tuple(values)
    total = sum(selected)
    if not selected or total <= 0 or any(not math.isfinite(value) or value < 0 for value in selected):
        return None
    return tuple(value / total for value in selected)


def _devig_three(values: tuple[Any, Any, Any]) -> tuple[tuple[float, float, float], float] | None:
    odds = tuple(_float(value) for value in values)
    if any(value is None or value <= 1 for value in odds):
        return None
    inverse = tuple(1 / value for value in odds)  # type: ignore[arg-type]
    normalized = _normalize(inverse)
    return (normalized, sum(inverse)) if normalized else None  # type: ignore[return-value]


def _devig_hk_pair(first: Any, second: Any) -> tuple[tuple[float, float], float] | None:
    prices = (_float(first), _float(second))
    if any(value is None or value <= 0 for value in prices):
        return None
    inverse = tuple(1 / (1 + value) for value in prices)  # type: ignore[arg-type]
    normalized = _normalize(inverse)
    return (normalized, sum(inverse)) if normalized else None  # type: ignore[return-value]


def _consensus(probabilities: Iterable[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    rows = list(probabilities)
    if not rows:
        return None
    result = _normalize(median(row[index] for row in rows) for index in range(3))
    return result if result is None else (result[0], result[1], result[2])


def _direction(probabilities: tuple[float, float, float]) -> tuple[str, float]:
    labels = ("home", "draw", "away")
    ordered = sorted(enumerate(probabilities), key=lambda item: item[1], reverse=True)
    return labels[ordered[0][0]], ordered[0][1] - ordered[1][1]


def _dispersion(probabilities: Iterable[tuple[float, float, float]]) -> float:
    rows = list(probabilities)
    consensus = _consensus(rows)
    if not rows or consensus is None:
        return 0.0
    return float(median(max(abs(row[index] - consensus[index]) for index in range(3)) for row in rows))


def normalize_bookmaker_name(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).strip().split())


def _bookmaker_keys(rows_by_market: Mapping[str, list[dict[str, Any]]]) -> tuple[dict[int, str], list[str]]:
    name_to_ids: dict[str, set[str]] = defaultdict(set)
    id_to_names: dict[str, set[str]] = defaultdict(set)
    for rows in rows_by_market.values():
        for row in rows:
            name = normalize_bookmaker_name(row.get("source_bookmaker_name"))
            source_id = str(row.get("source_bookmaker_id") or "").strip()
            if name and source_id:
                name_to_ids[name].add(source_id)
                id_to_names[source_id].add(name)
    conflicting_names = {name for name, ids in name_to_ids.items() if len(ids) > 1}
    conflicting_ids = {source_id for source_id, names in id_to_names.items() if len(names) > 1}
    # An ID/name contradiction invalidates every row in the affected mapping,
    # including a name-only row that could otherwise fall back to that ID.
    excluded_names = conflicting_names | {
        name for source_id in conflicting_ids for name in id_to_names[source_id]
    }
    conflicts = [f"bookmaker_name_maps_multiple_ids:{name}" for name in conflicting_names]
    conflicts.extend(f"bookmaker_id_maps_multiple_names:{source_id}" for source_id in conflicting_ids)
    keys: dict[int, str] = {}
    for rows in rows_by_market.values():
        for row in rows:
            name = normalize_bookmaker_name(row.get("source_bookmaker_name"))
            source_id = str(row.get("source_bookmaker_id") or "").strip()
            if source_id and source_id not in conflicting_ids and name not in excluded_names:
                key = f"id:{source_id}"
            elif name and name not in excluded_names and len(name_to_ids.get(name, set())) == 1:
                key = f"id:{next(iter(name_to_ids[name]))}"
            elif name and name not in excluded_names:
                key = f"name:{name}" if name else ""
            else:
                key = ""
            if key:
                keys[id(row)] = key
    return keys, sorted(conflicts)


def _deduplicate_market(
    rows: list[dict[str, Any]], keys: Mapping[int, str], required: tuple[str, ...]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = keys.get(id(row))
        if key and all(_float(row.get(field)) is not None for field in required):
            grouped[key].append(row)
    selected: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for key, company_rows in grouped.items():
        signatures = {tuple(str(row.get(field)) for field in required) for row in company_rows}
        if len(signatures) != 1:
            conflicts.append(f"duplicate_company_conflict:{key}")
            continue
        selected[key] = min(
            company_rows,
            key=lambda row: (int(row.get("source_row_index") or 0), str(row.get("record_id") or "")),
        )
    return selected, sorted(conflicts)


def prediction_cutoff(kickoff_at: datetime, target: str) -> datetime:
    if target not in TARGETS:
        raise EuropeGuardrailError(f"unsupported Europe target: {target}")
    return kickoff_at.astimezone(UTC) - timedelta(minutes=CUTOFFS[target][0])


def publication_deadline(kickoff_at: datetime, cutoff: datetime) -> datetime:
    return min(cutoff + timedelta(minutes=10), kickoff_at.astimezone(UTC) - timedelta(minutes=1))


def select_europe_batch_as_of(
    connection,
    *,
    fixture_id: str,
    target: str,
    prediction_cutoff: datetime,
    available_at: datetime,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT batch.*
        FROM football.model_eligible_snapshot_batches_v2 AS batch
        WHERE batch.fixture_id=%s AND batch.target=%s
          AND batch.model_strict_eligible=true
          AND batch.core_observed_at <= %s AND batch.completed_at <= %s
          AND NOT EXISTS (
              SELECT 1 FROM football.current_invalid_fixtures invalid
              WHERE invalid.fixture_id=batch.fixture_id
          )
        ORDER BY batch.core_observed_at DESC, batch.completed_at DESC, batch.record_id DESC
        LIMIT 1
        """,
        (fixture_id, target, prediction_cutoff.astimezone(UTC), available_at.astimezone(UTC)),
    ).fetchone()
    return dict(row) if row else None


def _identity_as_of(connection, fixture_id: str, cutoff: datetime) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT record_id, fixture_id, observed_at, kickoff_at, competition_id,
               competition_name, home_team_name, away_team_name, identity_status
        FROM football.fixture_identities
        WHERE fixture_id=%s AND kickoff_at IS NOT NULL AND observed_at <= %s
        ORDER BY observed_at DESC, record_id DESC LIMIT 1
        """,
        (fixture_id, cutoff),
    ).fetchone()
    return dict(row) if row else None


def relevant_source_fingerprint(workspace: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files: list[Path] = []
    for value in RELEVANT_PATHS:
        path = workspace / value
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    for path in sorted(set(files)):
        relative = path.relative_to(workspace).as_posix()
        digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *RELEVANT_PATHS],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        dirty = sorted(line[3:].replace("\\", "/") for line in status if len(line) > 3)
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, ["git_unavailable"]
    return {
        "git_commit": commit,
        "relevant_source_tree_sha256": digest.hexdigest(),
        "relevant_dirty_paths": dirty,
    }


def _load_selected_rows(
    connection,
    *,
    fixture_id: str,
    target: str,
    cutoff: datetime,
    batch: Mapping[str, Any],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, str], list[str]]:
    results = batch.get("market_results") or {}
    raw_rows: dict[str, list[dict[str, Any]]] = {}
    source_hashes: dict[str, str] = {}
    reasons: list[str] = []
    fields = {
        "ouzhi": (
            "opening_home", "opening_draw", "opening_away",
            "current_home", "current_draw", "current_away",
        ),
        "yazhi": (
            "opening_home", "opening_line", "opening_away",
            "current_home", "current_line", "current_away",
        ),
        "daxiao": (
            "opening_over", "opening_line", "opening_under",
            "current_over", "current_line", "current_under",
        ),
    }
    for market in fields:
        snapshot_id = str(((results.get(market) or {}).get("snapshot_record_id") or ""))
        if not snapshot_id:
            reasons.append(f"missing_snapshot:{market}")
            raw_rows[market] = []
            continue
        snapshot = connection.execute(
            """SELECT record_id, fixture_id, market, target, observed_at, raw_sha256
               FROM football.market_snapshots WHERE record_id=%s""",
            (snapshot_id,),
        ).fetchone()
        if not snapshot:
            reasons.append(f"missing_snapshot_record:{market}")
            raw_rows[market] = []
            continue
        snapshot = dict(snapshot)
        if (
            str(snapshot["fixture_id"]) != fixture_id
            or snapshot["market"] != market
            or snapshot["target"] != target
            or snapshot["observed_at"].astimezone(UTC) > cutoff
        ):
            reasons.append(f"snapshot_reference_conflict:{market}")
        source_hashes[market] = str(snapshot.get("raw_sha256") or "")
        raw_rows[market] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT record_id, source_bookmaker_id, source_bookmaker_name, row_role,
                       opening_home, opening_draw, opening_away, opening_line,
                       opening_over, opening_under, current_home, current_draw,
                       current_away, current_line, current_over, current_under,
                       source_row_index, source_page_sha256, source_workbook_sha256,
                       observed_at
                FROM football.current_bookmaker_market_rows
                WHERE fixture_id=%s AND target=%s AND market=%s
                  AND source_snapshot_record_id=%s AND event_origin='live'
                  AND normalization_version=2 AND row_role='bookmaker'
                  AND observed_at <= %s
                ORDER BY source_row_index, record_id
                """,
                (fixture_id, target, market, snapshot_id, cutoff),
            ).fetchall()
        ]
    keys, identity_conflicts = _bookmaker_keys(raw_rows)
    reasons.extend(identity_conflicts)
    selected: dict[str, dict[str, dict[str, Any]]] = {}
    for market, required in fields.items():
        market_rows, duplicate_conflicts = _deduplicate_market(raw_rows[market], keys, required)
        selected[market] = market_rows
        reasons.extend(f"{reason}:{market}" for reason in duplicate_conflicts)
    return selected, source_hashes, sorted(set(reasons))


def _selection_hard_reasons(reasons: Iterable[str]) -> list[str]:
    return sorted(
        reason
        for reason in reasons
        if not reason.startswith("duplicate_company_conflict:")
        and not reason.startswith("bookmaker_name_maps_multiple_ids:")
        and not reason.startswith("bookmaker_id_maps_multiple_names:")
    )


def _batch_trajectory(
    connection,
    *,
    fixture_id: str,
    target: str,
    cutoff: datetime,
    available_at: datetime,
    policy: EuropeGuardrailPolicy,
) -> dict[str, Any]:
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
    observations: dict[str, dict[str, Any]] = {}
    for raw_batch in batches:
        batch = dict(raw_batch)
        results = batch.get("market_results") or {}
        ids = {market: str(((results.get(market) or {}).get("snapshot_record_id") or "")) for market in ("ouzhi", "yazhi", "daxiao")}
        if any(not value for value in ids.values()):
            continue
        hashes = {
            str(row["record_id"]): str(row.get("raw_sha256") or "")
            for row in connection.execute(
                "SELECT record_id, raw_sha256 FROM football.market_snapshots WHERE record_id=ANY(%s)",
                (list(ids.values()),),
            ).fetchall()
        }
        if len(hashes) != 3 or any(not hashes.get(value) for value in ids.values()):
            continue
        response_hash = hashlib.sha256(
            json.dumps([hashes[ids[market]] for market in ("ouzhi", "yazhi", "daxiao")], separators=(",", ":")).encode()
        ).hexdigest()
        if response_hash in observations:
            continue
        selected, _, reasons = _load_selected_rows(
            connection, fixture_id=fixture_id, target=target, cutoff=cutoff, batch=batch
        )
        if _selection_hard_reasons(reasons):
            continue
        probabilities: dict[str, tuple[float, float, float]] = {}
        for key, row in selected["ouzhi"].items():
            result = _devig_three((row["current_home"], row["current_draw"], row["current_away"]))
            if result:
                probabilities[key] = result[0]
        consensus = _consensus(probabilities.values())
        if consensus is None or len(probabilities) < int(policy.thresholds["minimum_bookmakers_per_market"]):
            continue
        direction, _ = _direction(consensus)
        favorite_index = {"home": 0, "draw": 1, "away": 2}[direction]
        observations[response_hash] = {
            "observed_at": batch["core_observed_at"].astimezone(UTC),
            "consensus": consensus,
            "direction": direction,
            "company_probabilities": probabilities,
        }
    ordered = sorted(observations.items(), key=lambda item: item[1]["observed_at"])
    company_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for response_hash, observation in ordered:
        direction = observation["direction"]
        index = {"home": 0, "draw": 1, "away": 2}[direction]
        for key, probability in observation["company_probabilities"].items():
            company_series[key].append(
                {
                    "response_hash": response_hash,
                    "observed_at": iso_utc(observation["observed_at"]),
                    "direction": direction,
                    "direction_probability": probability[index],
                    "consensus_probability": observation["consensus"][index],
                    "deviation": probability[index] - observation["consensus"][index],
                }
            )
    minimum_count = int(policy.thresholds["trajectory_observation_count"])
    minimum_span = int(policy.thresholds["trajectory_span_seconds"])
    tolerance = float(policy.thresholds["unchanged_probability_tolerance"])
    floor = float(policy.thresholds["absolute_anomaly_floor"])
    company_states: dict[str, list[str]] = {}
    for key, series in company_series.items():
        states: list[str] = []
        times = [_utc(item["observed_at"], "trajectory observed_at") for item in series]
        span = int((max(times) - min(times)).total_seconds()) if len(times) > 1 else 0
        values = [float(item["direction_probability"]) for item in series]
        deviations = [float(item["deviation"]) for item in series]
        if len(series) >= minimum_count and span >= minimum_span:
            if max(values) - min(values) <= tolerance:
                states.append("unchanged_confirmed")
            if all(abs(value) >= floor for value in deviations) and len({value > 0 for value in deviations}) == 1:
                states.append("persistent_dissent")
            if len(values) >= 3 and abs(values[0] - values[-1]) <= tolerance:
                middle = max(abs(values[index] - (values[index - 1] + values[index + 1]) / 2) for index in range(1, len(values) - 1))
                if middle >= floor:
                    states.append("one_off_spike")
        company_states[key] = states
    return {
        "observation_count": len(ordered),
        "observation_span_seconds": (
            int((ordered[-1][1]["observed_at"] - ordered[0][1]["observed_at"]).total_seconds())
            if len(ordered) > 1
            else 0
        ),
        "response_hashes": [item[0] for item in ordered],
        "company_states": company_states,
    }


def build_europe_features(
    selected: Mapping[str, Mapping[str, Mapping[str, Any]]],
    trajectory: Mapping[str, Any],
    policy: EuropeGuardrailPolicy,
    pairing_warnings: Iterable[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    t = policy.thresholds
    hard_reasons: list[str] = []
    minimum = int(t["minimum_bookmakers_per_market"])
    for market in ("ouzhi", "yazhi", "daxiao"):
        if len(selected.get(market, {})) < minimum:
            hard_reasons.append(f"insufficient_bookmakers:{market}")
    euro: dict[str, dict[str, Any]] = {}
    margin_anomalies: list[str] = []
    for key, row in selected.get("ouzhi", {}).items():
        opening = _devig_three((row.get("opening_home"), row.get("opening_draw"), row.get("opening_away")))
        current = _devig_three((row.get("current_home"), row.get("current_draw"), row.get("current_away")))
        if not opening or not current:
            continue
        euro[key] = {
            "opening": opening[0],
            "current": current[0],
            "delta": tuple(current[0][index] - opening[0][index] for index in range(3)),
            "opening_margin": opening[1],
            "current_margin": current[1],
            "row_record_id": str(row.get("record_id") or ""),
            "name": normalize_bookmaker_name(row.get("source_bookmaker_name")),
        }
        if not (
            float(t["source_margin_minimum"]) <= current[1] <= float(t["source_margin_maximum"])
        ):
            margin_anomalies.append(key)
    if len(euro) < minimum:
        hard_reasons.append("insufficient_valid_1x2_bookmakers")
    consensus = _consensus(value["current"] for value in euro.values())
    if consensus is None:
        hard_reasons.append("missing_base_consensus")
        return {
            "base_probabilities": {},
            "institution_details": {},
            "bookmaker_count_by_market": {market: len(selected.get(market, {})) for market in ("ouzhi", "yazhi", "daxiao")},
            "source_row_record_ids": sorted(str(row.get("record_id") or "") for rows in selected.values() for row in rows.values()),
            "company_pairing_warnings": sorted(set(pairing_warnings)),
        }, sorted(set(hard_reasons))
    base_direction, gap = _direction(consensus)
    direction_index = {"home": 0, "draw": 1, "away": 2}[base_direction]
    deviations_by_component = [[value["current"][index] - consensus[index] for value in euro.values()] for index in range(3)]
    component_mads = [_mad(values, 0.0) or 0.0 for values in deviations_by_component]
    dispersion = _dispersion(value["current"] for value in euro.values())
    institutions: dict[str, dict[str, Any]] = {}
    material_probability = float(t["material_probability_move"])
    asian_material = float(t["material_asian_line_move"])
    total_material = float(t["material_total_line_move"])
    tolerance = float(t["unchanged_probability_tolerance"])
    for key, value in sorted(euro.items()):
        current = value["current"]
        delta = value["delta"]
        deviation = tuple(current[index] - consensus[index] for index in range(3))
        robust_z = tuple(
            0.6745 * deviation[index] / component_mads[index] if component_mads[index] > 0 else None
            for index in range(3)
        )
        max_deviation = max(abs(item) for item in deviation)
        robust_outlier = any(
            item is not None and abs(item) >= float(t["robust_z_threshold"])
            for item in robust_z
        )
        # With a zero MAD, the robust Z score is undefined.  Retain the
        # company and fall back to the frozen absolute-deviation floor.
        zero_mad_absolute_outlier = any(
            component_mads[index] == 0.0
            and abs(deviation[index]) >= float(t["absolute_anomaly_floor"])
            for index in range(3)
        )
        anomaly = (
            len(euro) >= int(t["minimum_institution_analysis_bookmakers"])
            and max_deviation >= float(t["absolute_anomaly_floor"])
            and (robust_outlier or zero_mad_absolute_outlier)
        )
        euro_signal = "support" if delta[direction_index] >= material_probability else (
            "oppose" if delta[direction_index] <= -material_probability else "unchanged"
        )
        asian: dict[str, Any] = {"status": "not_available"}
        asian_row = selected.get("yazhi", {}).get(key)
        asian_signal = "not_evaluable"
        if asian_row and base_direction in {"home", "away"}:
            opening_line = float(asian_row["opening_line"])
            current_line = float(asian_row["current_line"])
            favorite_opening_line = -opening_line if base_direction == "home" else opening_line
            favorite_current_line = -current_line if base_direction == "home" else current_line
            line_delta = favorite_current_line - favorite_opening_line
            opening_pair = _devig_hk_pair(asian_row.get("opening_home"), asian_row.get("opening_away"))
            current_pair = _devig_hk_pair(asian_row.get("current_home"), asian_row.get("current_away"))
            favorite_pair_index = 0 if base_direction == "home" else 1
            price_probability_delta = (
                current_pair[0][favorite_pair_index] - opening_pair[0][favorite_pair_index]
                if opening_pair and current_pair
                else None
            )
            if line_delta >= asian_material or (abs(line_delta) < 1e-12 and price_probability_delta is not None and price_probability_delta >= material_probability):
                asian_signal = "confirm"
            elif line_delta <= -asian_material or (abs(line_delta) < 1e-12 and price_probability_delta is not None and price_probability_delta <= -material_probability):
                asian_signal = "conflict"
            else:
                asian_signal = "flat"
            asian = {
                "status": asian_signal,
                "opening_line": opening_line,
                "current_line": current_line,
                "favorite_line_delta": line_delta,
                "favorite_price_probability_delta": price_probability_delta,
                "row_record_id": str(asian_row.get("record_id") or ""),
            }
        total: dict[str, Any] = {"status": "not_available"}
        total_row = selected.get("daxiao", {}).get(key)
        if total_row:
            opening_line = float(total_row["opening_line"])
            current_line = float(total_row["current_line"])
            opening_pair = _devig_hk_pair(total_row.get("opening_over"), total_row.get("opening_under"))
            current_pair = _devig_hk_pair(total_row.get("current_over"), total_row.get("current_under"))
            total = {
                "status": "material" if abs(current_line - opening_line) >= total_material else "flat",
                "opening_line": opening_line,
                "current_line": current_line,
                "line_delta": current_line - opening_line,
                "over_probability_delta": current_pair[0][0] - opening_pair[0][0] if opening_pair and current_pair else None,
                "row_record_id": str(total_row.get("record_id") or ""),
            }
        states = list((trajectory.get("company_states") or {}).get(key, []))
        if anomaly:
            states.append("isolated_move" if euro_signal != "unchanged" else "cross_sectional_dissent")
        internal_conflict = euro_signal in {"support", "oppose"} and asian_signal in {"confirm", "conflict"} and (
            (euro_signal == "support" and asian_signal == "conflict")
            or (euro_signal == "oppose" and asian_signal == "confirm")
        )
        if internal_conflict:
            states.append("internal_cross_market_conflict")
        if not states and max_deviation < float(t["absolute_anomaly_floor"]):
            states.append("consensus_following")
        institutions[key] = {
            "name": value["name"],
            "opening_probabilities": {name: value["opening"][index] for index, name in enumerate(("home", "draw", "away"))},
            "current_probabilities": {name: current[index] for index, name in enumerate(("home", "draw", "away"))},
            "probability_delta": {name: delta[index] for index, name in enumerate(("home", "draw", "away"))},
            "consensus_deviation": {name: deviation[index] for index, name in enumerate(("home", "draw", "away"))},
            "robust_z": {name: robust_z[index] for index, name in enumerate(("home", "draw", "away"))},
            "maximum_consensus_deviation": max_deviation,
            "euro_signal": euro_signal,
            "asian": asian,
            "total": total,
            "states": sorted(set(states)),
            "source_margin_anomaly": key in margin_anomalies,
            "source_row_record_ids": sorted(filter(None, (value["row_record_id"], asian.get("row_record_id"), total.get("row_record_id")))),
        }
    leave_one_out: dict[str, dict[str, Any]] = {}
    max_shift = 0.0
    direction_flip = False
    for key in sorted(euro):
        reduced = _consensus(value["current"] for company, value in euro.items() if company != key)
        if reduced is None:
            continue
        reduced_direction, _ = _direction(reduced)
        shift = max(abs(reduced[index] - consensus[index]) for index in range(3))
        max_shift = max(max_shift, shift)
        direction_flip = direction_flip or reduced_direction != base_direction
        leave_one_out[key] = {
            "probabilities": {name: reduced[index] for index, name in enumerate(("home", "draw", "away"))},
            "direction": reduced_direction,
            "direction_changed": reduced_direction != base_direction,
            "maximum_probability_shift": shift,
        }
    support_values = [item["euro_signal"] for item in institutions.values()]
    asian_values = [item["asian"]["status"] for item in institutions.values() if item["asian"]["status"] != "not_available"]
    total_deltas = [float(item["total"]["line_delta"]) for item in institutions.values() if item["total"].get("line_delta") is not None]
    features = {
        "base_probabilities": {name: consensus[index] for index, name in enumerate(("home", "draw", "away"))},
        "base_direction": base_direction,
        "direction_gap": gap,
        "bookmaker_dispersion": dispersion,
        "bookmaker_count_by_market": {market: len(selected.get(market, {})) for market in ("ouzhi", "yazhi", "daxiao")},
        "paired_bookmaker_count": len(euro),
        "euro_support_ratio": support_values.count("support") / len(support_values) if support_values else 0.0,
        "euro_oppose_ratio": support_values.count("oppose") / len(support_values) if support_values else 0.0,
        "euro_unchanged_ratio": support_values.count("unchanged") / len(support_values) if support_values else 0.0,
        "asian_confirm_ratio": asian_values.count("confirm") / len(asian_values) if asian_values else 0.0,
        "asian_conflict_ratio": asian_values.count("conflict") / len(asian_values) if asian_values else 0.0,
        "total_line_delta_median": _median(total_deltas),
        "institution_details": institutions,
        "institution_state_counts": dict(sorted(Counter(state for item in institutions.values() for state in item["states"]).items())),
        "source_margin_anomaly_bookmakers": sorted(margin_anomalies),
        "leave_one_out": leave_one_out,
        "leave_one_out_direction_flip": direction_flip,
        "leave_one_out_max_probability_shift": max_shift,
        "trajectory": dict(trajectory),
        "source_row_record_ids": sorted(str(row.get("record_id") or "") for rows in selected.values() for row in rows.values()),
        "company_pairing_warnings": sorted(set(pairing_warnings)),
    }
    return features, sorted(set(hard_reasons))


def assess_europe_features(
    features: Mapping[str, Any], policy: EuropeGuardrailPolicy, hard_reasons: Iterable[str]
) -> dict[str, Any]:
    t = policy.thresholds
    hard = sorted(set(hard_reasons))
    probabilities = features.get("base_probabilities") or {}
    rules: dict[str, dict[str, Any]] = {}
    rules["r0_data_integrity"] = {"status": "matched" if hard else "not_matched", "reasons": hard}
    if hard or not probabilities:
        for name in (
            "r1_base_direction", "r2_euro_movement", "r3_asian_confirmation",
            "r4_total_context", "r5_company_support", "r6_dispersion",
            "r7_same_target_trajectory", "r8_institution_anomaly",
            "r9_leave_one_out", "r10_unchanged_evidence",
        ):
            rules[name] = {"status": "not_evaluable"}
        return {
            "rule_evaluations": rules,
            "rule_flags": ["r0_data_integrity"],
            "proposed_action": "abstain",
            "proposed_confidence_cap": "observation_only",
            "reasons": hard or ["missing_base_consensus"],
        }
    direction = str(features["base_direction"])
    weak = float(features["direction_gap"]) < float(t["weak_direction_gap"])
    support = float(features["euro_support_ratio"]) >= float(t["signal_support_ratio"])
    asian_confirm = float(features["asian_confirm_ratio"]) >= float(t["signal_support_ratio"])
    asian_conflict = float(features["asian_conflict_ratio"]) >= float(t["signal_support_ratio"])
    total_delta = features.get("total_line_delta_median")
    high_dispersion = float(features["bookmaker_dispersion"]) > float(t["high_dispersion"])
    trajectory = features.get("trajectory") or {}
    trajectory_ready = (
        int(trajectory.get("observation_count") or 0) >= int(t["trajectory_observation_count"])
        and int(trajectory.get("observation_span_seconds") or 0) >= int(t["trajectory_span_seconds"])
    )
    state_counts = features.get("institution_state_counts") or {}
    anomaly_count = sum(int(state_counts.get(name, 0)) for name in (
        "isolated_move", "cross_sectional_dissent", "persistent_dissent", "one_off_spike",
        "internal_cross_market_conflict",
    ))
    internal_conflicts = int(state_counts.get("internal_cross_market_conflict", 0))
    persistent_dissent = int(state_counts.get("persistent_dissent", 0))
    loo_flip = bool(features.get("leave_one_out_direction_flip"))
    loo_shift = float(features.get("leave_one_out_max_probability_shift") or 0) >= float(t["leave_one_out_probability_shift"])
    unchanged = int(state_counts.get("unchanged_confirmed", 0))
    pairing_warnings = list(features.get("company_pairing_warnings") or [])
    rules.update({
        "r1_base_direction": {"status": "matched" if weak else "not_matched", "direction": direction, "direction_gap": features["direction_gap"], "threshold": t["weak_direction_gap"]},
        "r2_euro_movement": {"status": "matched" if support else "not_matched", "support_ratio": features["euro_support_ratio"], "oppose_ratio": features["euro_oppose_ratio"], "threshold": t["signal_support_ratio"]},
        "r3_asian_confirmation": ({"status": "not_evaluable", "reason": "draw_has_no_direct_asian_confirmation"} if direction == "draw" else {"status": "matched" if asian_confirm else "not_matched", "confirm_ratio": features["asian_confirm_ratio"], "conflict_ratio": features["asian_conflict_ratio"], "threshold": t["signal_support_ratio"]}),
        "r4_total_context": {"status": "matched" if total_delta is not None and abs(float(total_delta)) >= float(t["material_total_line_move"]) else "not_matched", "median_line_delta": total_delta, "threshold": t["material_total_line_move"]},
        "r5_company_support": {"status": "matched" if support else "not_matched", "support_ratio": features["euro_support_ratio"], "bookmakers": features["paired_bookmaker_count"]},
        "r6_dispersion": {"status": "matched" if high_dispersion else "not_matched", "dispersion": features["bookmaker_dispersion"], "threshold": t["high_dispersion"]},
        "r7_same_target_trajectory": {"status": "matched" if trajectory_ready else "not_evaluable", "observation_count": trajectory.get("observation_count", 0), "span_seconds": trajectory.get("observation_span_seconds", 0)},
        "r8_institution_anomaly": {"status": "matched" if anomaly_count or pairing_warnings else "not_matched", "anomaly_count": anomaly_count, "internal_conflict_count": internal_conflicts, "persistent_dissent_count": persistent_dissent, "pairing_warnings": pairing_warnings, "state_counts": state_counts},
        "r9_leave_one_out": {"status": "matched" if loo_flip or loo_shift else "not_matched", "direction_flip": loo_flip, "maximum_probability_shift": features.get("leave_one_out_max_probability_shift"), "threshold": t["leave_one_out_probability_shift"]},
        "r10_unchanged_evidence": {"status": "matched" if unchanged else "not_matched", "unchanged_company_count": unchanged},
    })
    primary: list[str] = []
    if asian_conflict or internal_conflicts:
        primary.append("cross_market_conflict")
    if high_dispersion:
        primary.append("high_bookmaker_dispersion")
    if any(
        int(state_counts.get(name, 0))
        for name in ("isolated_move", "cross_sectional_dissent", "one_off_spike")
    ):
        primary.append("single_institution_anomaly_or_one_off_spike")
    if loo_flip or loo_shift:
        primary.append("single_bookmaker_concentration_risk")
    auxiliary: list[str] = []
    if weak:
        auxiliary.append("weak_direction_gap")
    if int(features["paired_bookmaker_count"]) < int(t["minimum_institution_analysis_bookmakers"]):
        auxiliary.append("limited_institution_sample")
    if not trajectory_ready:
        auxiliary.append("insufficient_same_target_trajectory")
    if persistent_dissent:
        auxiliary.append("persistent_institution_dissent")
    if unchanged:
        auxiliary.append("unchanged_institution_evidence")
    if pairing_warnings:
        auxiliary.append("institution_pairing_warning")
    if direction == "draw":
        auxiliary.append("draw_direction_not_directly_asian_confirmable")
    if loo_flip and int(features["paired_bookmaker_count"]) < int(t["minimum_institution_analysis_bookmakers"]):
        action = "abstain"
    elif len(set(primary)) >= 2:
        action = "abstain"
    elif primary:
        action = "downgrade"
    elif auxiliary or not support or (direction != "draw" and not asian_confirm):
        action = "caution"
    else:
        action = "keep"
    cap = "observation_only" if action == "abstain" else ("low" if action == "downgrade" else (
        "medium" if int(features["paired_bookmaker_count"]) >= int(t["minimum_institution_analysis_bookmakers"]) and not weak and not high_dispersion else "low"
    ))
    flags = sorted(set(primary + auxiliary))
    return {
        "rule_evaluations": rules,
        "rule_flags": flags,
        "proposed_action": action,
        "proposed_confidence_cap": cap,
        "reasons": [],
    }


def _base_record_id(
    channel: str, fixture_id: str, target: str, cutoff: datetime, policy: EuropeGuardrailPolicy
) -> str:
    return stable_id(
        "research_europe_guardrail_assessment",
        channel,
        fixture_id,
        target,
        iso_utc(cutoff),
        policy.policy_version,
    )


def _unavailable_record(
    *,
    channel: str,
    fixture_id: str,
    competition_id: str,
    target: str,
    cutoff: datetime,
    assessed_at: datetime,
    policy: EuropeGuardrailPolicy,
    identity_record_id: str | None,
    batch_record_id: str | None,
    fingerprint: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "ResearchEuropeGuardrailAssessment",
        "record_id": _base_record_id(channel, fixture_id, target, cutoff, policy),
        **SHADOW_EVENT_FLAGS,
        "channel": channel,
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "target": target,
        "prediction_cutoff": iso_utc(cutoff),
        "assessed_at": iso_utc(assessed_at),
        "policy_version": policy.policy_version,
        "policy_revision": policy.policy_revision,
        "policy_status": policy.status,
        "policy_snapshot": policy.payload,
        "policy_file_sha256": policy.file_sha256,
        "policy_canonical_sha256": policy.canonical_sha256,
        **fingerprint,
        "identity_record_id": identity_record_id,
        "selected_batch_record_id": batch_record_id,
        "snapshot_record_ids": {},
        "source_row_record_ids": [],
        "source_hashes": {},
        "base_probabilities": {},
        "base_direction": None,
        "institution_details": {},
        "trajectory": {},
        "raw_features": {},
        "rule_evaluations": {"r0_data_integrity": {"status": "matched", "reasons": [reason]}},
        "rule_flags": [reason],
        "proposed_action": "abstain",
        "proposed_confidence_cap": "observation_only",
        "reasons": [reason],
        "audit_status": "unavailable",
    }


def build_europe_assessment(
    connection,
    *,
    workspace: Path,
    channel: str,
    fixture_id: str,
    target: str,
    cutoff: datetime,
    available_at: datetime,
    assessed_at: datetime,
    policy: EuropeGuardrailPolicy,
    identity: Mapping[str, Any] | None,
    batch: Mapping[str, Any] | None,
    enforce_effective_at: bool,
    enforce_reproducible_source: bool,
) -> dict[str, Any] | None:
    cutoff = cutoff.astimezone(UTC)
    available_at = available_at.astimezone(UTC)
    assessed_at = assessed_at.astimezone(UTC)
    if enforce_effective_at and cutoff < policy.effective_at:
        return None
    competition_id = str((identity or {}).get("competition_id") or "")
    fingerprint = relevant_source_fingerprint(workspace)
    identity_record_id = str((identity or {}).get("record_id") or "") or None
    batch_record_id = str((batch or {}).get("record_id") or "") or None
    if competition_id not in policy.competition_ids:
        return _unavailable_record(
            channel=channel, fixture_id=fixture_id, competition_id=competition_id or policy.competition_ids[0],
            target=target, cutoff=cutoff, assessed_at=assessed_at, policy=policy,
            identity_record_id=identity_record_id, batch_record_id=batch_record_id,
            fingerprint=fingerprint, reason="unsupported_or_missing_competition_identity",
        )
    if enforce_reproducible_source and fingerprint["relevant_dirty_paths"]:
        return _unavailable_record(
            channel=channel, fixture_id=fixture_id, competition_id=competition_id,
            target=target, cutoff=cutoff, assessed_at=assessed_at, policy=policy,
            identity_record_id=identity_record_id, batch_record_id=batch_record_id,
            fingerprint=fingerprint, reason="relevant_source_not_reproducible",
        )
    if identity is None or identity.get("identity_status") == "conflict":
        return _unavailable_record(
            channel=channel, fixture_id=fixture_id, competition_id=competition_id,
            target=target, cutoff=cutoff, assessed_at=assessed_at, policy=policy,
            identity_record_id=identity_record_id, batch_record_id=batch_record_id,
            fingerprint=fingerprint, reason="missing_or_conflicting_identity_as_of_cutoff",
        )
    if batch is None:
        return _unavailable_record(
            channel=channel, fixture_id=fixture_id, competition_id=competition_id,
            target=target, cutoff=cutoff, assessed_at=assessed_at, policy=policy,
            identity_record_id=identity_record_id, batch_record_id=None,
            fingerprint=fingerprint, reason="missing_model_eligible_batch_as_of_cutoff",
        )
    if batch.get("completed_at") and batch["completed_at"].astimezone(UTC) > available_at:
        return _unavailable_record(
            channel=channel, fixture_id=fixture_id, competition_id=competition_id,
            target=target, cutoff=cutoff, assessed_at=assessed_at, policy=policy,
            identity_record_id=identity_record_id, batch_record_id=batch_record_id,
            fingerprint=fingerprint, reason="batch_not_available_at_assessment",
        )
    selected, source_hashes, selection_reasons = _load_selected_rows(
        connection, fixture_id=fixture_id, target=target, cutoff=cutoff, batch=batch
    )
    trajectory = _batch_trajectory(
        connection, fixture_id=fixture_id, target=target, cutoff=cutoff,
        available_at=available_at, policy=policy,
    )
    pairing_warnings = [
        reason for reason in selection_reasons if reason not in _selection_hard_reasons(selection_reasons)
    ]
    features, feature_reasons = build_europe_features(selected, trajectory, policy, pairing_warnings)
    assessment = assess_europe_features(
        features, policy, [*_selection_hard_reasons(selection_reasons), *feature_reasons]
    )
    results = batch.get("market_results") or {}
    snapshot_ids = {
        market: str(((results.get(market) or {}).get("snapshot_record_id") or ""))
        for market in ("ouzhi", "yazhi", "daxiao")
        if str(((results.get(market) or {}).get("snapshot_record_id") or ""))
    }
    record = {
        "schema_version": 1,
        "record_type": "ResearchEuropeGuardrailAssessment",
        "record_id": _base_record_id(channel, fixture_id, target, cutoff, policy),
        **SHADOW_EVENT_FLAGS,
        "channel": channel,
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "target": target,
        "prediction_cutoff": iso_utc(cutoff),
        "assessed_at": iso_utc(assessed_at),
        "policy_version": policy.policy_version,
        "policy_revision": policy.policy_revision,
        "policy_status": policy.status,
        "policy_snapshot": policy.payload,
        "policy_file_sha256": policy.file_sha256,
        "policy_canonical_sha256": policy.canonical_sha256,
        **fingerprint,
        "identity_record_id": identity_record_id,
        "selected_batch_record_id": batch_record_id,
        "snapshot_record_ids": snapshot_ids,
        "source_row_record_ids": features.get("source_row_record_ids") or [],
        "source_hashes": source_hashes,
        "base_probabilities": features.get("base_probabilities") or {},
        "base_direction": features.get("base_direction"),
        "institution_details": features.get("institution_details") or {},
        "trajectory": trajectory,
        "raw_features": features,
        **assessment,
        "audit_status": "eligible",
    }
    return record


def validate_europe_assessment_record(record: Mapping[str, Any]) -> None:
    if record.get("record_type") != "ResearchEuropeGuardrailAssessment":
        return
    if record.get("competition_id") not in {"63", "101"} or record.get("target") not in TARGETS:
        raise EuropeGuardrailError("Europe assessment has invalid competition or target")
    if record.get("policy_status") != "shadow" or record.get("audit_status") not in {"eligible", "unavailable"}:
        raise EuropeGuardrailError("Europe assessment has invalid policy or audit status")
    if record.get("proposed_action") not in ACTIONS:
        raise EuropeGuardrailError("Europe assessment has invalid action")
    for name in ("policy_file_sha256", "policy_canonical_sha256", "relevant_source_tree_sha256"):
        if not valid_sha256(record.get(name)):
            raise EuropeGuardrailError(f"Europe assessment has invalid {name}")
    snapshot = record.get("policy_snapshot")
    if not isinstance(snapshot, dict):
        raise EuropeGuardrailError("Europe assessment lacks policy snapshot")
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != record.get("policy_canonical_sha256"):
        raise EuropeGuardrailError("Europe assessment policy snapshot hash mismatch")
    cutoff = _utc(record.get("prediction_cutoff"), "prediction_cutoff")
    effective = _utc(snapshot.get("effective_at"), "effective_at")
    assessed = _utc(record.get("assessed_at"), "assessed_at")
    if cutoff < effective or assessed < cutoff:
        raise EuropeGuardrailError("Europe assessment violates policy time boundary")
    expected_id = stable_id(
        "research_europe_guardrail_assessment",
        record.get("channel"), record.get("fixture_id"), record.get("target"),
        iso_utc(cutoff), record.get("policy_version"),
    )
    if record.get("record_id") != expected_id:
        raise EuropeGuardrailError("Europe assessment stable id mismatch")
    if record.get("audit_status") == "eligible":
        embedded = load_policy_snapshot(snapshot, record)
        expected = assess_europe_features(
            record.get("raw_features") or {}, embedded,
            (record.get("rule_evaluations") or {}).get("r0_data_integrity", {}).get("reasons") or [],
        )
        for key in ("rule_evaluations", "rule_flags", "proposed_action", "proposed_confidence_cap", "reasons"):
            if record.get(key) != expected[key]:
                raise EuropeGuardrailError(f"Europe assessment derived field mismatch: {key}")


def load_policy_snapshot(snapshot: Mapping[str, Any], record: Mapping[str, Any]) -> EuropeGuardrailPolicy:
    return EuropeGuardrailPolicy(
        path=Path("<embedded>"), payload=dict(snapshot),
        policy_version=str(snapshot["policy_version"]), policy_revision=int(snapshot["policy_revision"]),
        status=str(snapshot["status"]), effective_at=_utc(snapshot["effective_at"], "effective_at"),
        competition_ids=tuple(snapshot["competition_ids"]), targets=tuple(snapshot["targets"]),
        thresholds=dict(snapshot["thresholds"]), forward_gate=dict(snapshot["forward_gate"]),
        input_policy=dict(snapshot["input_policy"]), presentation_policy=dict(snapshot["presentation_policy"]),
        file_sha256=str(record["policy_file_sha256"]), canonical_sha256=str(record["policy_canonical_sha256"]),
    )


def require_migration(connection, version: str = "015") -> None:
    row = connection.execute(
        "SELECT version FROM football.schema_migrations WHERE version=%s", (version,)
    ).fetchone()
    if row is None:
        raise EuropeGuardrailError(f"database migration {version} is required; run db-import first")


def _existing_assessment_ids(config: ResearchConfig, channel: str) -> set[str]:
    result: set[str] = set()
    root = config.normalized_dir / "shadow-predictions"
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("record_type") == "ResearchEuropeGuardrailAssessment" and record.get("channel") == channel:
                result.add(str(record.get("record_id")))
    return result


def publish_europe_guardrail_shadow(
    config: ResearchConfig,
    *,
    channel: str = CHANNEL_DEFAULT,
    targets: list[str] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    lookahead_hours: int = 48,
    lookback_hours: int = 2,
) -> dict[str, Any]:
    assessed_at = (now or utc_now()).astimezone(UTC)
    policy = load_europe_guardrail_policy(config.workspace)
    selected_targets = sorted(set(targets or TARGETS))
    if any(target not in TARGETS for target in selected_targets):
        raise EuropeGuardrailError("Europe shadow received unsupported target")
    existing = _existing_assessment_ids(config, channel)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    records: list[dict[str, Any]] = []
    skipped_existing = 0
    with connect(database_config) as connection:
        require_migration(connection)
        lower = assessed_at - timedelta(hours=lookback_hours + 48)
        upper = assessed_at + timedelta(hours=lookahead_hours + 1)
        fixtures = [
            dict(row)
            for row in connection.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (fixture_id)
                        fixture_id, kickoff_at, competition_id
                    FROM football.fixture_identities
                    WHERE kickoff_at IS NOT NULL
                    ORDER BY fixture_id, observed_at DESC, record_id DESC
                )
                SELECT * FROM latest
                WHERE competition_id=ANY(%s) AND kickoff_at BETWEEN %s AND %s
                ORDER BY kickoff_at, fixture_id
                """,
                (list(policy.competition_ids), lower, upper),
            ).fetchall()
        ]
        for fixture in fixtures:
            fixture_id = str(fixture["fixture_id"])
            latest_kickoff = fixture["kickoff_at"].astimezone(UTC)
            for target in selected_targets:
                candidate_cutoff = prediction_cutoff(latest_kickoff, target)
                candidate_deadline = publication_deadline(latest_kickoff, candidate_cutoff)
                if not (candidate_cutoff <= assessed_at <= candidate_deadline):
                    continue
                identity = _identity_as_of(connection, fixture_id, candidate_cutoff)
                kickoff = identity["kickoff_at"].astimezone(UTC) if identity else latest_kickoff
                cutoff = prediction_cutoff(kickoff, target)
                deadline = publication_deadline(kickoff, cutoff)
                if not (cutoff <= assessed_at <= deadline) or cutoff < policy.effective_at:
                    continue
                record_id = _base_record_id(channel, fixture_id, target, cutoff, policy)
                if record_id in existing:
                    skipped_existing += 1
                    continue
                batch = select_europe_batch_as_of(
                    connection, fixture_id=fixture_id, target=target,
                    prediction_cutoff=cutoff, available_at=assessed_at,
                )
                record = build_europe_assessment(
                    connection, workspace=config.workspace, channel=channel,
                    fixture_id=fixture_id, target=target, cutoff=cutoff,
                    available_at=assessed_at, assessed_at=assessed_at, policy=policy,
                    identity=identity, batch=batch, enforce_effective_at=True,
                    enforce_reproducible_source=True,
                )
                if record:
                    records.append(record)
    if dry_run or not records:
        return {
            "status": "dry_run" if dry_run else "unchanged",
            "channel": channel,
            "candidate_records": len(records),
            "skipped_existing": skipped_existing,
            "records": records,
        }
    with research_facts_lock(config):
        run_id = make_run_id(assessed_at)
        path = ResearchStore(config).write_completed_shadow_batch(
            run_id=run_id,
            records=records,
            manifest_fields={
                "prediction_count": 0,
                "assessment_count": 0,
                "europe_assessment_count": len(records),
                "policy_version": policy.policy_version,
            },
        )
    return {
        "status": "published",
        "channel": channel,
        "run_id": run_id,
        "path": str(path),
        "published": len(records),
        "skipped_existing": skipped_existing,
    }


def _history_context(connection, fixture_id: str, competition_id: str, cutoff: datetime) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT result.fixture_id, result.home_goals, result.away_goals,
               result.confirmed_at AS label_available_at, result.verification_method
        FROM football.current_verified_results result
        JOIN football.latest_fixture_identities identity USING (fixture_id)
        WHERE identity.competition_id=%s AND result.fixture_id<>%s
          AND result.confirmed_at <= %s
        ORDER BY result.confirmed_at, result.fixture_id
        """,
        (competition_id, fixture_id, cutoff),
    ).fetchall()
    return {
        "context_only": True,
        "fixture_count": len(rows),
        "automatic_fixture_count": sum(
            row["verification_method"] not in {"manual", "manual-import", "project-owner-manual-declaration"}
            for row in rows
        ),
        "self_attestation_fixture_count": sum(
            row["verification_method"] == "project-owner-manual-declaration" for row in rows
        ),
        "cases": [dict(row) for row in rows],
    }


def analyze_europe(
    config: ResearchConfig,
    *,
    fixture_id: str,
    target: str | None,
    latest_available_target: bool,
    audit: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed = (now or utc_now()).astimezone(UTC)
    policy = load_europe_guardrail_policy(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        require_migration(connection)
        latest = connection.execute(
            """SELECT * FROM football.latest_fixture_identities
               WHERE fixture_id=%s AND competition_id=ANY(%s)""",
            (fixture_id, list(policy.competition_ids)),
        ).fetchone()
        if not latest:
            raise EuropeGuardrailError("fixture is not a registered Europe fixture")
        kickoff = latest["kickoff_at"].astimezone(UTC)
        if latest_available_target:
            candidates = [value for value in TARGETS if prediction_cutoff(kickoff, value) <= observed]
            if not candidates:
                raise EuropeGuardrailError("no Europe target is available yet")
            target = max(candidates, key=lambda value: prediction_cutoff(kickoff, value))
        if target not in TARGETS:
            raise EuropeGuardrailError("analyze-europe requires a supported target")
        cutoff = prediction_cutoff(kickoff, target)
        stored = connection.execute(
            """SELECT payload FROM research.current_europe_guardrail_assessments
               WHERE fixture_id=%s AND target=%s AND prediction_cutoff=%s
               ORDER BY assessed_at DESC LIMIT 1""",
            (fixture_id, target, cutoff),
        ).fetchone()
        if stored:
            assessment = dict(stored["payload"])
            source = "stored_natural_assessment"
        else:
            identity = _identity_as_of(connection, fixture_id, cutoff)
            available_at = min(observed, publication_deadline(kickoff, cutoff))
            if available_at < cutoff:
                raise EuropeGuardrailError("requested target is not available as of now")
            batch = select_europe_batch_as_of(
                connection, fixture_id=fixture_id, target=target,
                prediction_cutoff=cutoff, available_at=available_at,
            )
            assessment = build_europe_assessment(
                connection, workspace=config.workspace, channel=CHANNEL_DEFAULT,
                fixture_id=fixture_id, target=target, cutoff=cutoff,
                available_at=available_at, assessed_at=max(cutoff, available_at), policy=policy,
                identity=identity, batch=batch, enforce_effective_at=False,
                enforce_reproducible_source=False,
            )
            if assessment is None:
                raise EuropeGuardrailError("Europe analysis was not produced")
            source = "ephemeral_as_of_replay"
        assessment["analysis_source"] = source
        assessment["persisted"] = source == "stored_natural_assessment"
        assessment["forward_gate_eligible"] = source == "stored_natural_assessment"
        assessment["fixture"] = {
            "fixture_id": fixture_id,
            "competition_id": str(latest["competition_id"]),
            "competition_name": latest["competition_name"],
            "home_team_name": latest["home_team_name"],
            "away_team_name": latest["away_team_name"],
            "kickoff_at": iso_utc(kickoff),
        }
        assessment["historical_context"] = _history_context(
            connection, fixture_id, str(latest["competition_id"]), cutoff
        )
        if not audit:
            assessment.pop("policy_snapshot", None)
            assessment.pop("relevant_source_tree_sha256", None)
        return assessment


def render_europe_analysis(result: Mapping[str, Any], *, summary: bool, audit: bool) -> str:
    fixture = result.get("fixture") or {}
    probabilities = result.get("base_probabilities") or {}
    lines = [
        f"欧战盘口差异分析：{fixture.get('home_team_name')} vs {fixture.get('away_team_name')}",
        f"赛事/切点：{fixture.get('competition_name')} / {result.get('target')}",
        f"截止时间：{result.get('prediction_cutoff')}（仅90分钟及补时）",
        "",
        "基础概率",
        f"主胜 {float(probabilities.get('home', 0)):.2%} | 平局 {float(probabilities.get('draw', 0)):.2%} | 客胜 {float(probabilities.get('away', 0)):.2%}",
        f"基础方向：{result.get('base_direction') or '无'}；基础概率未被规则修改。",
        "",
        f"规则动作：{result.get('proposed_action')} / 拟议置信上限：{result.get('proposed_confidence_cap')}",
        f"风险：{', '.join(result.get('rule_flags') or []) or '无'}",
    ]
    if summary:
        return "\n".join(lines) + "\n"
    lines.extend(["", "机构逐行变化"])
    for key, item in sorted((result.get("institution_details") or {}).items()):
        delta = item.get("probability_delta") or {}
        lines.append(
            f"- {item.get('name') or key}: 欧赔={item.get('euro_signal')} "
            f"Δ主/平/客={float(delta.get('home', 0)):+.2%}/{float(delta.get('draw', 0)):+.2%}/{float(delta.get('away', 0)):+.2%}; "
            f"亚盘={item.get('asian', {}).get('status')}; 状态={','.join(item.get('states') or [])}"
        )
    lines.extend(["", "留一机构敏感性"])
    for key, item in sorted(((result.get("raw_features") or {}).get("leave_one_out") or {}).items()):
        lines.append(
            f"- 去除 {key}: 方向={item.get('direction')}，方向变化={item.get('direction_changed')}，最大概率变化={float(item.get('maximum_probability_shift', 0)):.2%}"
        )
    lines.extend(["", "R0-R10"])
    for name, value in sorted((result.get("rule_evaluations") or {}).items()):
        lines.append(f"- {name}: {value.get('status')}")
    context = result.get("historical_context") or {}
    lines.extend([
        "",
        "历史标签上下文（仅展示，不调整概率或动作）",
        f"截止前可用：{context.get('fixture_count', 0)}场；自动={context.get('automatic_fixture_count', 0)}，负责人声明={context.get('self_attestation_fixture_count', 0)}。",
    ])
    if audit:
        lines.extend([
            "",
            "审计",
            f"记录={result.get('record_id')}；身份={result.get('identity_record_id')}；批次={result.get('selected_batch_record_id')}",
            f"策略={result.get('policy_version')}；文件哈希={result.get('policy_file_sha256')}；canonical哈希={result.get('policy_canonical_sha256')}",
        ])
    return "\n".join(lines) + "\n"


def _score_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {"fixtures": 0, "log_loss": None, "brier": None, "rps": None, "direction_accuracy": None}
    log_loss = brier = rps = hits = 0.0
    for point in points:
        probabilities = point["probabilities"]
        actual = int(point["actual"])
        log_loss -= math.log(max(1e-15, probabilities[actual]))
        brier += sum((probabilities[index] - (1 if index == actual else 0)) ** 2 for index in range(3))
        rps += (probabilities[0] - (1 if actual == 0 else 0)) ** 2
        rps += ((probabilities[0] + probabilities[1]) - (1 if actual <= 1 else 0)) ** 2
        hits += int(max(range(3), key=lambda index: probabilities[index]) == actual)
    count = len(points)
    return {
        "fixtures": count,
        "log_loss": log_loss / count,
        "brier": brier / count,
        "rps": rps / (2 * count),
        "direction_accuracy": hits / count,
    }


def replay_europe_guardrail(
    config: ResearchConfig,
    *,
    fixture_ids: list[str] | None,
    since: datetime | None,
    until: datetime | None,
    targets: list[str] | None,
    reveal_result: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed = (now or utc_now()).astimezone(UTC)
    policy = load_europe_guardrail_policy(config.workspace)
    selected_targets = sorted(set(targets or TARGETS))
    database_config = DatabaseConfig.from_workspace(config.workspace)
    replays: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with connect(database_config) as connection:
        require_migration(connection)
        if fixture_ids:
            identities = connection.execute(
                """SELECT * FROM football.latest_fixture_identities
                   WHERE fixture_id=ANY(%s) AND competition_id=ANY(%s)
                   ORDER BY kickoff_at, fixture_id""",
                (list(dict.fromkeys(fixture_ids)), list(policy.competition_ids)),
            ).fetchall()
        else:
            if since is None or until is None:
                raise EuropeGuardrailError("replay requires fixture ids or complete since/until range")
            identities = connection.execute(
                """SELECT * FROM football.latest_fixture_identities
                   WHERE competition_id=ANY(%s) AND kickoff_at BETWEEN %s AND %s
                   ORDER BY kickoff_at, fixture_id""",
                (list(policy.competition_ids), since.astimezone(UTC), until.astimezone(UTC)),
            ).fetchall()
        for raw_identity in identities:
            latest = dict(raw_identity)
            fixture_id = str(latest["fixture_id"])
            kickoff = latest["kickoff_at"].astimezone(UTC)
            for target in selected_targets:
                cutoff = prediction_cutoff(kickoff, target)
                if cutoff > observed:
                    continue
                available = publication_deadline(kickoff, cutoff)
                identity = _identity_as_of(connection, fixture_id, cutoff)
                batch = select_europe_batch_as_of(
                    connection, fixture_id=fixture_id, target=target,
                    prediction_cutoff=cutoff, available_at=available,
                )
                try:
                    record = build_europe_assessment(
                        connection, workspace=config.workspace, channel=CHANNEL_DEFAULT,
                        fixture_id=fixture_id, target=target, cutoff=cutoff,
                        available_at=available, assessed_at=max(cutoff, available), policy=policy,
                        identity=identity, batch=batch, enforce_effective_at=False,
                        enforce_reproducible_source=False,
                    )
                    if not record:
                        continue
                    item = {
                        "fixture_id": fixture_id,
                        "competition_id": str(latest["competition_id"]),
                        "target": target,
                        "prediction_cutoff": iso_utc(cutoff),
                        "base_probabilities": record["base_probabilities"],
                        "base_direction": record["base_direction"],
                        "proposed_action": record["proposed_action"],
                        "rule_flags": record["rule_flags"],
                        "audit_status": record["audit_status"],
                    }
                    if reveal_result:
                        result = connection.execute(
                            """SELECT home_goals, away_goals, verification_method, confirmed_at
                               FROM football.current_verified_results WHERE fixture_id=%s""",
                            (fixture_id,),
                        ).fetchone()
                        item["revealed_result"] = dict(result) if result else None
                    replays.append(item)
                except EuropeGuardrailError as exc:
                    errors.append({"fixture_id": fixture_id, "target": target, "error": str(exc)})
    points: list[dict[str, Any]] = []
    if reveal_result:
        for item in replays:
            result = item.get("revealed_result")
            probabilities = item.get("base_probabilities") or {}
            if not result or len(probabilities) != 3:
                continue
            actual = 0 if result["home_goals"] > result["away_goals"] else (1 if result["home_goals"] == result["away_goals"] else 2)
            points.append({"actual": actual, "probabilities": [probabilities[name] for name in ("home", "draw", "away")]})
    return {
        "status": "completed",
        "evaluation_mode": "retrospective_as_of_replay",
        "persisted": False,
        "forward_gate_eligible": False,
        "result_revealed": reveal_result,
        "fixture_count": len({item["fixture_id"] for item in replays}),
        "replay_count": len(replays),
        "metrics": _score_points(points),
        "action_counts": dict(sorted(Counter(item["proposed_action"] for item in replays).items())),
        "replays": replays,
        "errors": errors,
    }


def evaluate_europe_guardrail_forward(
    config: ResearchConfig, *, channel: str = CHANNEL_DEFAULT, now: datetime | None = None
) -> dict[str, Any]:
    evaluated_at = (now or utc_now()).astimezone(UTC)
    policy = load_europe_guardrail_policy(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        require_migration(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT assessment.*, result.home_goals, result.away_goals,
                       result.verification_method, result.confirmed_at
                FROM research.europe_guardrail_assessments assessment
                LEFT JOIN football.current_verified_results result USING (fixture_id)
                WHERE assessment.channel=%s AND assessment.policy_version=%s
                ORDER BY assessment.prediction_cutoff, assessment.fixture_id, assessment.target
                """,
                (channel, policy.policy_version),
            ).fetchall()
        ]
    evaluated = [row for row in rows if row.get("home_goals") is not None and row.get("audit_status") == "eligible"]
    manual = {"manual", "manual-import", "project-owner-manual-declaration"}
    def points(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for value in values:
            probabilities = value.get("base_probabilities") or {}
            if not all(name in probabilities for name in ("home", "draw", "away")):
                continue
            actual = 0 if value["home_goals"] > value["away_goals"] else (1 if value["home_goals"] == value["away_goals"] else 2)
            result.append({"actual": actual, "probabilities": [float(probabilities[name]) for name in ("home", "draw", "away")]})
        return result

    groups: dict[str, dict[str, Any]] = {}
    for competition_id in policy.competition_ids:
        for target in policy.targets:
            group = [row for row in evaluated if row["competition_id"] == competition_id and row["target"] == target]
            automatic = [row for row in group if row.get("verification_method") not in manual]
            cutoffs = [row["prediction_cutoff"] for row in automatic]
            span = (max(cutoffs) - min(cutoffs)).total_seconds() / 86400 if len(cutoffs) > 1 else 0.0
            rule_hits = Counter(
                name
                for row in automatic
                for name, value in (row.get("rule_evaluations") or {}).items()
                if value.get("status") == "matched"
            )
            review = (
                len({row["fixture_id"] for row in automatic}) >= policy.forward_gate["minimum_automatic_fixtures"]
                and span >= policy.forward_gate["minimum_span_days"]
                and any(value >= policy.forward_gate["minimum_rule_hits"] for value in rule_hits.values())
            )
            by_action = {
                action: _score_points(points([row for row in automatic if row["proposed_action"] == action]))
                for action in sorted({str(row["proposed_action"]) for row in automatic})
            }
            groups[f"{competition_id}|{target}"] = {
                "all_valid_fixture_count": len({row["fixture_id"] for row in group}),
                "automatic_fixture_count": len({row["fixture_id"] for row in automatic}),
                "self_attestation_fixture_count": len({row["fixture_id"] for row in group if row.get("verification_method") == "project-owner-manual-declaration"}),
                "evaluation_span_days": span,
                "action_counts": dict(sorted(Counter(row["proposed_action"] for row in automatic).items())),
                "metrics": {
                    "all_valid": _score_points(points(group)),
                    "automatic": _score_points(points(automatic)),
                    "automatic_by_action": by_action,
                },
                "rule_hit_counts": dict(sorted(rule_hits.items())),
                "review_eligible": review,
            }
    payload = {
        "status": "completed" if evaluated else "unchanged",
        "channel": channel,
        "policy_version": policy.policy_version,
        "evaluation_kind": "research_europe_guardrail_forward_not_formal_backtest",
        "evaluated_at": iso_utc(evaluated_at),
        "evaluated_fixture_count": len({row["fixture_id"] for row in evaluated}),
        "review_eligible": any(group["review_eligible"] for group in groups.values()),
        "groups": groups,
    }
    if not evaluated:
        return payload
    fixture_hash = hashlib.sha256(
        json.dumps(
            [(str(row["record_id"]), str(row["fixture_id"])) for row in evaluated],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    record_id = stable_id(
        "research_europe_guardrail_forward", channel, policy.policy_version,
        iso_utc(evaluated_at), fixture_hash,
    )
    record = {
        "schema_version": 1,
        "record_type": "ResearchShadowEvaluation",
        "record_id": record_id,
        **MODEL_ARTIFACT_FLAGS,
        "model_key": MODEL_KEY,
        "model_version": policy.policy_version,
        "evaluated_at": iso_utc(evaluated_at),
        "evaluation_kind": payload["evaluation_kind"],
        "dataset_hash": fixture_hash,
        "metrics": payload,
    }
    with research_facts_lock(config):
        run_id = make_run_id(evaluated_at)
        store = ResearchStore(config)
        path = store.write_records("europe-guardrail-evaluations", run_id, "forward-evaluation", [record])
        store.write_manifest(
            run_id,
            "europe-guardrail-forward-evaluation",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "record_path": path.relative_to(config.research_dir).as_posix(),
                "record_id": record_id,
                "policy_version": policy.policy_version,
            },
        )
    return payload | {"run_id": run_id, "path": str(path), "record_id": record_id}
