from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any, Iterable

from football_cups.collector.config import CUTOFFS
from football_cups.collector.results import load_competition_formats
from football_cups.collector.storage import make_run_id
from football_cups.collector.timeutil import iso_utc, utc_now
from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import connect

from . import SCHEMA_VERSION, research_flags
from .config import ResearchConfig
from .competition_profiles import (
    CompetitionProfile,
    confidence_assessment,
    load_competition_registry,
    market_statistics,
)
from .k1_analysis_workflow import canonical_market_fingerprint
from .k1_guardrail import (
    collect_k1_guardrail_assessment,
    load_k1_guardrail_policy,
    require_migration,
    select_k1_batch_as_of,
    verify_shadow_manifest,
)
from .reporting import OUTCOMES, _metric_rows, load_records
from .storage import ResearchStore, json_dumps, research_facts_lock, stable_id


MODEL_KEY = "devig-consensus-v1"
FEATURE_SCHEMA = "closing-1x2-devig-consensus-v1"
CHANNEL_DEFAULT = "research-shadow-v1"
SUMMARY_BOOKMAKERS = {"Avg", "Max", "Min", "Average", "Highest", "Lowest"}


class ResearchModelError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetPoint:
    fixture_record_id: str
    source_id: str
    competition: str
    match_date: str
    actual: int
    probabilities: tuple[float, float, float]
    bookmaker_count: int
    bookmakers: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "fixture_record_id": self.fixture_record_id,
            "source_id": self.source_id,
            "competition": self.competition,
            "match_date": self.match_date,
            "actual": self.actual,
            "probabilities": {
                "home": self.probabilities[0],
                "draw": self.probabilities[1],
                "away": self.probabilities[2],
            },
            "bookmaker_count": self.bookmaker_count,
            "bookmakers": list(self.bookmakers),
            "features": {
                "log_home_draw": math.log(self.probabilities[0] / self.probabilities[1]),
                "log_away_draw": math.log(self.probabilities[2] / self.probabilities[1]),
            },
        }


def _base_record(record_type: str, record_id: str, kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "record_id": record_id,
        **research_flags(kind),
    }


def _outcome(home_goals: int, away_goals: int) -> int:
    return 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2


def _devig_odds(values: dict[str, Any]) -> tuple[float, float, float] | None:
    try:
        inverse = tuple(1.0 / float(values[key]) for key in OUTCOMES)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    total = sum(inverse)
    if total <= 0 or not all(math.isfinite(value) and value > 0 for value in inverse):
        return None
    return tuple(value / total for value in inverse)  # type: ignore[return-value]


def _median_consensus(probabilities: Iterable[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    rows = list(probabilities)
    if not rows:
        return None
    values = tuple(median(row[index] for row in rows) for index in range(3))
    total = sum(values)
    if total <= 0:
        return None
    return tuple(value / total for value in values)  # type: ignore[return-value]


def _dataset_hash(points: list[DatasetPoint]) -> str:
    payload = [point.public_dict() for point in sorted(points, key=lambda item: item.fixture_record_id)]
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def build_closing_1x2_dataset(config: ResearchConfig) -> tuple[list[DatasetPoint], dict[str, dict[str, Any]]]:
    records = load_records(config)
    fixtures = {
        record_id: record
        for record_id, record in records.items()
        if record.get("record_type") == "ResearchFixture"
        and record.get("result_eligible") is True
        and record.get("home_goals") is not None
        and record.get("away_goals") is not None
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records.values():
        if (
            record.get("record_type") == "ResearchMarketObservation"
            and record.get("cohort") == "closing"
            and record.get("market") == "1x2"
            and record.get("bookmaker") not in SUMMARY_BOOKMAKERS
        ):
            grouped[str(record["fixture_record_id"])].append(record)

    points: list[DatasetPoint] = []
    for fixture_id, rows in grouped.items():
        fixture = fixtures.get(fixture_id)
        if not fixture:
            continue
        by_bookmaker: dict[str, tuple[float, float, float]] = {}
        for row in rows:
            bookmaker = str(row.get("bookmaker") or "").strip()
            probability = _devig_odds(row.get("values") or {})
            if bookmaker and probability is not None:
                by_bookmaker[bookmaker] = probability
        if len(by_bookmaker) < 3:
            continue
        consensus = _median_consensus(by_bookmaker.values())
        if consensus is None:
            continue
        points.append(
            DatasetPoint(
                fixture_record_id=fixture_id,
                source_id=str(fixture["source_id"]),
                competition=str(fixture["competition"]),
                match_date=str(fixture["match_date"]),
                actual=_outcome(int(fixture["home_goals"]), int(fixture["away_goals"])),
                probabilities=consensus,
                bookmaker_count=len(by_bookmaker),
                bookmakers=tuple(sorted(by_bookmaker)),
            )
        )
    return sorted(points, key=lambda item: (item.match_date, item.fixture_record_id)), records


def _existing_record(records: dict[str, dict[str, Any]], record_type: str, **criteria: Any) -> dict[str, Any] | None:
    for record in records.values():
        if record.get("record_type") != record_type:
            continue
        if all(record.get(key) == value for key, value in criteria.items()):
            return record
    return None


def write_model_dataset(
    config: ResearchConfig,
    *,
    training_before_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    points, records = build_closing_1x2_dataset(config)
    if not points:
        raise ResearchModelError("no eligible historical closing 1x2 dataset rows found")
    digest = _dataset_hash(points)
    existing = _existing_record(
        records,
        "ResearchModelDataset",
        model_key=MODEL_KEY,
        dataset_hash=digest,
        training_before_date=training_before_date.isoformat(),
    )
    if existing:
        return {"status": "unchanged", "dataset_record_id": existing["record_id"], "dataset_hash": digest}

    train_ids = sorted(
        point.fixture_record_id for point in points if date.fromisoformat(point.match_date) < training_before_date
    )
    evaluation_ids = sorted(
        point.fixture_record_id for point in points if date.fromisoformat(point.match_date) >= training_before_date
    )
    if not train_ids:
        raise ResearchModelError("training set is empty")
    if set(train_ids) & set(evaluation_ids):
        raise ResearchModelError("training and evaluation fixtures overlap")
    created_at = now or utc_now()
    record_id = stable_id("research_model_dataset", MODEL_KEY, digest, training_before_date.isoformat())
    record = {
        **_base_record("ResearchModelDataset", record_id, "model_artifact"),
        "model_key": MODEL_KEY,
        "dataset_hash": digest,
        "training_before_date": training_before_date.isoformat(),
        "created_at": iso_utc(created_at),
        "source_record_count": len(records),
        "fixture_count": len(points),
        "feature_schema": FEATURE_SCHEMA,
        "training_fixture_ids": train_ids,
        "evaluation_fixture_ids": evaluation_ids,
        "rows": [point.public_dict() for point in points],
    }
    run_id = make_run_id(created_at)
    with research_facts_lock(config):
        store = ResearchStore(config)
        path = store.write_records("model-artifacts", run_id, "model-dataset", [record])
        store.write_manifest(
            run_id,
            "model-dataset",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "model_key": MODEL_KEY,
                "dataset_record_id": record_id,
                "dataset_hash": digest,
                "record_path": path.relative_to(config.research_dir).as_posix(),
            },
        )
    return {
        "status": "created",
        "run_id": run_id,
        "dataset_record_id": record_id,
        "dataset_hash": digest,
        "fixture_count": len(points),
        "training_fixtures": len(train_ids),
        "evaluation_fixtures": len(evaluation_ids),
    }


def _metrics_for_points(points: list[DatasetPoint]) -> dict[str, Any]:
    metric_points = [
        {"actual": point.actual, "market": point.probabilities}
        for point in points
    ]
    return _metric_rows(metric_points, "market")


def train_devig_consensus_model(
    config: ResearchConfig,
    *,
    training_before_date: date,
    activate: bool,
    channel: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    trained_at = now or utc_now()
    dataset_result = write_model_dataset(config, training_before_date=training_before_date, now=trained_at)
    records = load_records(config)
    dataset = records.get(str(dataset_result["dataset_record_id"]))
    if dataset is None:
        records = load_records(config)
        dataset = records.get(str(dataset_result["dataset_record_id"]))
    if dataset is None:
        raise ResearchModelError("dataset record was not found after creation")
    points = [
        DatasetPoint(
            fixture_record_id=row["fixture_record_id"],
            source_id=row["source_id"],
            competition=row["competition"],
            match_date=row["match_date"],
            actual=int(row["actual"]),
            probabilities=(row["probabilities"]["home"], row["probabilities"]["draw"], row["probabilities"]["away"]),
            bookmaker_count=int(row["bookmaker_count"]),
            bookmakers=tuple(row["bookmakers"]),
        )
        for row in dataset.get("rows", [])
    ]
    training_points = [
        point for point in points if date.fromisoformat(point.match_date) < training_before_date
    ]
    evaluation_points = [
        point for point in points if date.fromisoformat(point.match_date) >= training_before_date
    ]
    model_version = f"{MODEL_KEY}-{dataset['dataset_hash'][:12]}-{trained_at.strftime('%Y%m%dT%H%M%SZ')}"
    model_record_id = stable_id("research_model_version", MODEL_KEY, model_version)
    artifact = {
        "model_key": MODEL_KEY,
        "model_version": model_version,
        "probability_source": "component_median_of_individual_devig_1x2",
        "calibrator": "identity",
        "feature_schema": FEATURE_SCHEMA,
        "minimum_bookmakers": 3,
        "outputs": ["home", "draw", "away"],
    }
    model_record = {
        **_base_record("ResearchModelVersion", model_record_id, "model_artifact"),
        "model_key": MODEL_KEY,
        "model_version": model_version,
        "dataset_record_id": dataset["record_id"],
        "trained_at": iso_utc(trained_at),
        "algorithm": "devig-consensus-baseline",
        "artifact": artifact,
        "metrics": {
            "training": _metrics_for_points(training_points),
            "time_separated_evaluation": _metrics_for_points(evaluation_points),
        },
    }
    records_to_write: list[dict[str, Any]] = [model_record]
    activation_record_id = None
    if activate:
        activation_record_id = stable_id("research_model_activation", channel, model_version, iso_utc(trained_at))
        records_to_write.append(
            {
                **_base_record("ResearchModelActivation", activation_record_id, "model_artifact"),
                "channel": channel,
                "model_key": MODEL_KEY,
                "model_version": model_version,
                "model_record_id": model_record_id,
                "activated_at": iso_utc(trained_at),
                "active_from": iso_utc(trained_at),
                "active_until": None,
                "status": "active",
                "notes": "research-only shadow predictions; not a formal product model",
            }
        )
    run_id = make_run_id(trained_at)
    with research_facts_lock(config):
        store = ResearchStore(config)
        path = store.write_records("model-artifacts", run_id, "model-version", records_to_write)
        store.write_manifest(
            run_id,
            "model-version",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "model_key": MODEL_KEY,
                "model_version": model_version,
                "model_record_id": model_record_id,
                "activation_record_id": activation_record_id,
                "record_path": path.relative_to(config.research_dir).as_posix(),
            },
        )
    return {
        "status": "created",
        "run_id": run_id,
        "dataset": dataset_result,
        "model_record_id": model_record_id,
        "model_version": model_version,
        "activation_record_id": activation_record_id,
        "metrics": model_record["metrics"],
    }


def _prediction_cutoff(kickoff_at: datetime, target: str) -> datetime:
    if target not in CUTOFFS:
        raise ResearchModelError(f"unsupported shadow target: {target}")
    minutes, _ = CUTOFFS[target]
    return kickoff_at - timedelta(minutes=minutes)


def _deadline(kickoff_at: datetime, cutoff: datetime) -> datetime:
    return min(cutoff + timedelta(minutes=10), kickoff_at - timedelta(minutes=1))


def _published_prediction_ids(config: ResearchConfig, channel: str) -> set[str]:
    result: set[str] = set()
    root = config.normalized_dir / "shadow-predictions"
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else []:
        try:
            verify_shadow_manifest(config.research_dir, path)
        except Exception:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("record_type") == "ResearchShadowPrediction" and record.get("channel") == channel:
                result.add(str(record["record_id"]))
    return result


def _live_batch_rows(connection, targets: list[str], now: datetime, lookahead_hours: int, lookback_hours: int) -> list[dict[str, Any]]:
    lower = now - timedelta(hours=lookback_hours + 48)
    upper = now + timedelta(hours=lookahead_hours + 1)
    return [
        dict(row)
        for row in connection.execute(
            """
            WITH latest_identity AS (
                SELECT DISTINCT ON (fixture_id)
                    fixture_id, kickoff_at, competition_id, competition_name,
                    home_team_name, away_team_name
                FROM football.fixture_identities
                WHERE kickoff_at IS NOT NULL
                ORDER BY fixture_id, observed_at DESC, record_id DESC
            )
            SELECT
                batch.record_id AS snapshot_batch_record_id,
                batch.fixture_id,
                batch.target,
                batch.completed_at,
                batch.core_observed_at,
                batch.market_results,
                latest_identity.kickoff_at,
                latest_identity.competition_id,
                latest_identity.competition_name,
                latest_identity.home_team_name,
                latest_identity.away_team_name
            FROM football.current_model_eligible_snapshot_batches AS batch
            JOIN latest_identity USING (fixture_id)
            WHERE batch.target = ANY(%s)
              AND latest_identity.kickoff_at BETWEEN %s AND %s
            ORDER BY latest_identity.kickoff_at, batch.fixture_id, batch.target
            """,
            (targets, lower, upper),
        ).fetchall()
    ]


def _identity_as_of(connection, fixture_id: str, target: str) -> dict[str, Any] | None:
    if target not in CUTOFFS:
        raise ResearchModelError(f"unsupported shadow target: {target}")
    minutes, _ = CUTOFFS[target]
    row = connection.execute(
        """
        SELECT record_id, fixture_id, observed_at, kickoff_at, competition_id,
               competition_name, home_team_name, away_team_name, identity_status
        FROM football.fixture_identities
        WHERE fixture_id = %s
          AND kickoff_at IS NOT NULL
          AND observed_at <= kickoff_at - %s
        ORDER BY observed_at DESC, record_id DESC
        LIMIT 1
        """,
        (fixture_id, timedelta(minutes=minutes)),
    ).fetchone()
    return dict(row) if row is not None else None


def _automatic_evaluation_stats(
    connection,
    channel: str,
    available_at: datetime | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT prediction.competition_id, prediction.target,
               count(DISTINCT prediction.fixture_id) AS fixture_count,
               min(prediction.prediction_cutoff) AS first_cutoff,
               max(prediction.prediction_cutoff) AS last_cutoff
        FROM research.shadow_predictions AS prediction
        JOIN football.current_verified_results AS result
          ON result.fixture_id = prediction.fixture_id
        WHERE prediction.channel = %s
          AND prediction.status = 'published'
          AND prediction.competition_id IS NOT NULL
          AND result.verification_method NOT IN (
              'manual', 'manual-import', 'project-owner-manual-declaration'
          )
          AND (%s IS NULL OR result.confirmed_at <= %s)
        GROUP BY prediction.competition_id, prediction.target
        """,
        (channel, available_at, available_at),
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        span = row["last_cutoff"] - row["first_cutoff"]
        result[(str(row["competition_id"]), str(row["target"]))] = {
            "fixture_count": int(row["fixture_count"]),
            "span_days": span.total_seconds() / 86400,
        }
    return result


def _live_1x2_consensus(connection, fixture_id: str, target: str, snapshot_record_id: str, cutoff: datetime) -> tuple[tuple[float, float, float] | None, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT source_bookmaker_name, current_home, current_draw, current_away,
               observed_at, source_snapshot_record_id, record_id
        FROM football.current_bookmaker_market_rows
        WHERE fixture_id = %s
          AND target = %s
          AND market = 'ouzhi'
          AND row_role = 'bookmaker'
          AND event_origin = 'live'
          AND source_snapshot_record_id = %s
          AND observed_at <= %s
        ORDER BY source_bookmaker_name, record_id
        """,
        (fixture_id, target, snapshot_record_id, cutoff),
    ).fetchall()
    probabilities: dict[str, tuple[float, float, float]] = {}
    selected_rows: dict[str, dict[str, Any]] = {}
    max_observed_at = None
    for row in rows:
        bookmaker = str(row["source_bookmaker_name"] or "").strip()
        probability = _devig_odds(
            {"home": row["current_home"], "draw": row["current_draw"], "away": row["current_away"]}
        )
        if bookmaker and probability is not None:
            probabilities[bookmaker] = probability
            selected_rows[bookmaker] = dict(row)
            if max_observed_at is None or row["observed_at"] > max_observed_at:
                max_observed_at = row["observed_at"]
    consensus = None
    direction_strength = None
    bookmaker_dispersion = None
    if len(probabilities) >= 3:
        consensus, direction_strength, bookmaker_dispersion = market_statistics(probabilities.values())
    features = {
        "bookmaker_count": len(probabilities),
        "bookmakers": sorted(probabilities),
        "source_snapshot_record_id": snapshot_record_id,
        "market_observed_at": iso_utc(max_observed_at) if max_observed_at else None,
        "direction_strength": direction_strength,
        "bookmaker_dispersion": bookmaker_dispersion,
        "base_1x2_input_fingerprint": canonical_market_fingerprint(
            selected_rows.values(),
            fields=("current_home", "current_draw", "current_away"),
        ),
    }
    if consensus:
        features["log_home_draw"] = math.log(consensus[0] / consensus[1])
        features["log_away_draw"] = math.log(consensus[2] / consensus[1])
    return consensus, features


def _profile_fields(registry, profile: CompetitionProfile, identity: dict[str, Any] | None) -> dict[str, Any]:
    resolved_competition_id = None
    if identity:
        resolved_competition_id = str(
            identity.get("competition_id") or profile.competition_id or ""
        ) or None
    return {
        "competition_id": resolved_competition_id,
        "competition_name": identity.get("competition_name") if identity else None,
        "competition_type": profile.competition_type,
        "market_evidence_tier": profile.market_evidence_tier,
        "evaluation_group": profile.evaluation_group,
        "classification_status": profile.classification_status,
        "registry_version": registry.registry_version,
        "policy_version": registry.policy_version,
        "registry_file_sha256": registry.file_sha256,
        "registry_canonical_sha256": registry.canonical_sha256,
        "identity_record_id": identity.get("record_id") if identity else None,
        "identity_observed_at": iso_utc(identity["observed_at"]) if identity else None,
    }


def publish_shadow_predictions(
    config: ResearchConfig,
    *,
    channel: str,
    targets: list[str],
    dry_run: bool = False,
    now: datetime | None = None,
    lookahead_hours: int = 48,
    lookback_hours: int = 2,
) -> dict[str, Any]:
    observed_now = now or utc_now()
    registry = load_competition_registry(config.workspace)
    guardrail_policy = load_k1_guardrail_policy(config.workspace)
    competition_formats = load_competition_formats(
        config.workspace / "config" / "competition-formats.json"
    )
    targets = sorted(set(targets))
    for target in targets:
        if target not in {"T-24h", "T-6h", "T-60m", "T-10m"}:
            raise ResearchModelError(f"shadow predictions only support product cutoffs: {target}")
    existing_ids = _published_prediction_ids(config, channel)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    records: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    skipped_existing = 0
    with connect(database_config) as connection:
        require_migration(connection)
        activation = connection.execute(
            "SELECT * FROM research.current_model_activations WHERE channel = %s",
            (channel,),
        ).fetchone()
        if activation is None:
            raise ResearchModelError(f"no active research model activation for channel {channel}")
        evaluation_stats = _automatic_evaluation_stats(connection, channel, observed_now)
        batches = _live_batch_rows(connection, targets, observed_now, lookahead_hours, lookback_hours)
        for batch in batches:
            target = str(batch["target"])
            candidate_kickoff_at = batch["kickoff_at"].astimezone(UTC)
            candidate_cutoff = _prediction_cutoff(candidate_kickoff_at, target)
            candidate_deadline = _deadline(candidate_kickoff_at, candidate_cutoff)
            if observed_now < candidate_cutoff or observed_now > candidate_deadline:
                continue
            identity = _identity_as_of(connection, str(batch["fixture_id"]), target)
            if identity is None:
                kickoff_at = candidate_kickoff_at
                cutoff = candidate_cutoff
                deadline = candidate_deadline
            else:
                kickoff_at = identity["kickoff_at"].astimezone(UTC)
                cutoff = _prediction_cutoff(kickoff_at, target)
                deadline = _deadline(kickoff_at, cutoff)
            if observed_now < cutoff or observed_now > deadline:
                continue
            selected_batch = select_k1_batch_as_of(
                connection,
                fixture_id=str(batch["fixture_id"]),
                target=target,
                prediction_cutoff=cutoff,
                available_at=observed_now,
            )
            if selected_batch is None:
                continue
            selected_batch["snapshot_batch_record_id"] = selected_batch["record_id"]
            batch = selected_batch
            record_id = stable_id(
                "research_shadow_prediction",
                channel,
                batch["fixture_id"],
                target,
                iso_utc(cutoff),
            )
            if record_id in existing_ids:
                skipped_existing += 1
                continue
            profile = registry.resolve(
                identity.get("competition_id") if identity else None,
                identity.get("competition_name") if identity else None,
            )
            profile_fields = _profile_fields(registry, profile, identity)
            competition_id = str(identity.get("competition_id") or "") if identity else ""
            competition_format = competition_formats.get(
                f"id:{competition_id}",
                competition_formats.get(str(identity.get("competition_name") or ""), "unknown")
                if identity
                else "unknown",
            )
            sample = evaluation_stats.get((competition_id, target), {})
            market_results = batch.get("market_results") or {}
            ouzhi = market_results.get("ouzhi") if isinstance(market_results, dict) else None
            snapshot_record_id = str((ouzhi or {}).get("snapshot_record_id") or "")
            probabilities = None
            features: dict[str, Any] = {}
            abstention_reason = None
            if identity is None:
                abstention_reason = "missing_identity_as_of_cutoff"
            elif profile.conflict:
                abstention_reason = "competition_profile_conflict"
            elif profile.unregistered:
                abstention_reason = "unregistered_competition"
            elif not snapshot_record_id:
                abstention_reason = "missing_selected_1x2_snapshot"
            else:
                probabilities, features = _live_1x2_consensus(
                    connection,
                    str(batch["fixture_id"]),
                    str(batch["target"]),
                    snapshot_record_id,
                    cutoff,
                )
                if probabilities is None:
                    abstention_reason = "insufficient_live_1x2_bookmakers"
            if probabilities is None or profile.market_evidence_tier == "D":
                status = "abstained"
                probability_payload: dict[str, Any] = {}
                assessment = {
                    "raw_confidence_label": "observation_only",
                    "competition_confidence_cap": profile.confidence_cap,
                    "confidence_label": "observation_only",
                    "confidence_reasons": [abstention_reason or "prediction_abstained"],
                    "risk_flags": sorted(
                        flag
                        for flag, enabled in (
                            ("competition_profile_conflict", profile.conflict),
                            ("unregistered_competition", profile.unregistered),
                            ("result_scope_verification_risk", competition_format != "regular_time_only"),
                        )
                        if enabled
                    ),
                    "automatic_verified_fixture_count": int(sample.get("fixture_count", 0)),
                    "evaluation_span_days": float(sample.get("span_days", 0.0)),
                    "review_eligible": False,
                }
            else:
                status = "published"
                probability_payload = {
                    "home": probabilities[0],
                    "draw": probabilities[1],
                    "away": probabilities[2],
                    "sum": sum(probabilities),
                    "method": "individual-devig-component-median",
                }
                assessment = confidence_assessment(
                    registry,
                    profile,
                    probabilities,
                    bookmaker_count=int(features["bookmaker_count"]),
                    direction_strength=float(features["direction_strength"]),
                    bookmaker_dispersion=float(features["bookmaker_dispersion"]),
                    automatic_verified_fixtures=int(sample.get("fixture_count", 0)),
                    evaluation_span_days=float(sample.get("span_days", 0.0)),
                    competition_format=competition_format,
                )
            prediction = {
                    **_base_record("ResearchShadowPrediction", record_id, "shadow_event"),
                    "channel": channel,
                    "fixture_id": str(batch["fixture_id"]),
                    "target": target,
                    "prediction_cutoff": iso_utc(cutoff),
                    "published_at": iso_utc(observed_now),
                    "status": status,
                    "model_key": activation["model_key"] if status == "published" else None,
                    "model_version": activation["model_version"] if status == "published" else None,
                    "activation_record_id": activation["record_id"] if status == "published" else None,
                    "selected_batch_record_id": batch["snapshot_batch_record_id"],
                    "source_snapshot_record_id": snapshot_record_id or None,
                    "market_observed_at": features.get("market_observed_at"),
                    "bookmaker_count": features.get("bookmaker_count", 0),
                    "probabilities": probability_payload,
                    **profile_fields,
                    "direction_strength": features.get("direction_strength"),
                    "bookmaker_dispersion": features.get("bookmaker_dispersion"),
                    **assessment,
                    "features": {
                        **features,
                        "competition_format": competition_format,
                        "home_team_name": identity.get("home_team_name") if identity else None,
                        "away_team_name": identity.get("away_team_name") if identity else None,
                    },
                    "abstention_reason": abstention_reason,
                }
            records.append(prediction)
            assessment = collect_k1_guardrail_assessment(
                connection,
                workspace=config.workspace,
                prediction=prediction,
                batch=batch,
                policy=guardrail_policy,
                assessed_at=observed_now,
            )
            if assessment is not None:
                records.append(assessment)
                assessments.append(assessment)
    if dry_run or not records:
        prediction_records = [record for record in records if record["record_type"] == "ResearchShadowPrediction"]
        return {
            "status": "dry_run" if dry_run else "unchanged",
            "channel": channel,
            "candidate_records": len(prediction_records),
            "skipped_existing": skipped_existing,
            "records": prediction_records,
            "guardrail_assessments": assessments,
        }
    with research_facts_lock(config):
        store = ResearchStore(config)
        run_id = make_run_id(observed_now)
        path = store.write_completed_shadow_batch(
            run_id=run_id,
            records=records,
            manifest_fields={
                "prediction_count": sum(record["record_type"] == "ResearchShadowPrediction" for record in records),
                "assessment_count": len(assessments),
                "policy_version": guardrail_policy.policy_version if assessments else None,
                "channel": channel,
                "targets": targets,
            },
        )
    return {
        "status": "completed",
        "run_id": run_id,
        "channel": channel,
        "records_written": sum(record["record_type"] == "ResearchShadowPrediction" for record in records),
        "total_records_written": len(records),
        "published": sum(1 for record in records if record["record_type"] == "ResearchShadowPrediction" and record["status"] == "published"),
        "abstained": sum(1 for record in records if record["record_type"] == "ResearchShadowPrediction" and record["status"] == "abstained"),
        "assessments_written": len(assessments),
        "guardrail_unavailable": sum(assessment["audit_status"] == "unavailable" for assessment in assessments),
        "skipped_existing": skipped_existing,
    }


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "minimum": min(values),
        "median": median(values),
        "maximum": max(values),
    }


def _shadow_group_summary(rows: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    published = [row for row in rows if row["status"] == "published"]
    evaluated = [row for row in published if row.get("home_goals") is not None]
    automatic = [
        row
        for row in evaluated
        if row.get("verification_method")
        not in {"manual", "manual-import", "project-owner-manual-declaration"}
    ]

    def points(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "actual": _outcome(int(row["home_goals"]), int(row["away_goals"])),
                "market": tuple(float(row["probabilities"][key]) for key in OUTCOMES),
            }
            for row in selected
        ]

    def direction_accuracy(selected: list[dict[str, Any]]) -> float | None:
        values = points(selected)
        if not values:
            return None
        correct = sum(
            max(range(3), key=lambda index: point["market"][index]) == point["actual"]
            for point in values
        )
        return correct / len(values)

    risk_counts = defaultdict(int)
    for row in rows:
        for risk in row.get("risk_flags") or []:
            risk_counts[str(risk)] += 1
    tail_rows = [row for row in evaluated if "strong_favorite_draw_tail" in (row.get("risk_flags") or [])]
    automatic_fixtures = {str(row["fixture_id"]) for row in automatic}
    automatic_cutoffs = [row["prediction_cutoff"] for row in automatic]
    span_days = (
        (max(automatic_cutoffs) - min(automatic_cutoffs)).total_seconds() / 86400
        if automatic_cutoffs
        else 0.0
    )
    return {
        "fixture_count": len({str(row["fixture_id"]) for row in rows}),
        "published": len(published),
        "abstained": sum(row["status"] == "abstained" for row in rows),
        "evaluated": len(evaluated),
        "manual_declared_results": sum(
            row.get("verification_method") == "project-owner-manual-declaration" for row in evaluated
        ),
        "all_valid_results": {
            **_metric_rows(points(evaluated), "market"),
            "direction_accuracy": direction_accuracy(evaluated),
        },
        "automatic_results": {
            **_metric_rows(points(automatic), "market"),
            "direction_accuracy": direction_accuracy(automatic),
        },
        "bookmaker_count": _distribution([float(row["bookmaker_count"]) for row in published]),
        "direction_strength": _distribution(
            [float(row["direction_strength"]) for row in published if row.get("direction_strength") is not None]
        ),
        "bookmaker_dispersion": _distribution(
            [float(row["bookmaker_dispersion"]) for row in published if row.get("bookmaker_dispersion") is not None]
        ),
        "risk_flags": dict(sorted(risk_counts.items())),
        "strong_favorite_draw_tail": {
            "evaluated": len(tail_rows),
            "draws": sum(int(row["home_goals"]) == int(row["away_goals"]) for row in tail_rows),
            "draw_rate": (
                sum(int(row["home_goals"]) == int(row["away_goals"]) for row in tail_rows)
                / len(tail_rows)
                if tail_rows
                else None
            ),
        },
        "automatic_verified_fixture_count": len(automatic_fixtures),
        "evaluation_span_days": span_days,
        "review_eligible": (
            len(automatic_fixtures) >= gate["minimum_automatic_verified_fixtures"]
            and span_days >= gate["minimum_span_days"]
        ),
    }


def evaluate_shadow_predictions(config: ResearchConfig, *, channel: str, now: datetime | None = None) -> dict[str, Any]:
    evaluated_at = now or utc_now()
    registry = load_competition_registry(config.workspace)
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        require_migration(connection)
        rows = connection.execute(
            """
            SELECT prediction.*, result.home_goals, result.away_goals,
                   result.verification_method
            FROM research.shadow_predictions AS prediction
            LEFT JOIN football.current_verified_results AS result
              ON result.fixture_id = prediction.fixture_id
            WHERE prediction.channel = %s
            ORDER BY prediction.prediction_cutoff, prediction.fixture_id
            """,
            (channel,),
        ).fetchall()
    values = [dict(row) for row in rows]
    legacy_points = []
    for row in values:
        if row["status"] == "published" and row.get("home_goals") is not None:
            probabilities = row["probabilities"]
            legacy_points.append(
                {
                    "actual": _outcome(int(row["home_goals"]), int(row["away_goals"])),
                    "market": tuple(float(probabilities[key]) for key in OUTCOMES),
                }
            )
        if not row.get("market_evidence_tier"):
            row["market_evidence_tier"] = "legacy_unclassified"
        if not row.get("competition_type"):
            row["competition_type"] = "legacy_unclassified"
        if not row.get("evaluation_group"):
            row["evaluation_group"] = "legacy_unclassified"
        if not row.get("confidence_label"):
            row["confidence_label"] = "legacy_unclassified"
    gate = registry.confidence_policy["high_confidence_gate"]

    def grouped(field: str) -> dict[str, Any]:
        selected: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in values:
            selected[str(row.get(field) or "unknown")].append(row)
        return {
            key: _shadow_group_summary(group, gate) for key, group in sorted(selected.items())
        }

    competition_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in values:
        key = f"{row.get('competition_id') or 'unknown'}|{row['target']}"
        competition_target[key].append(row)
    metrics = {
        "all": _metric_rows(legacy_points, "market"),
        "summary": _shadow_group_summary(values, gate),
        "by_target": grouped("target"),
        "by_competition_id": grouped("competition_id"),
        "by_competition_type": grouped("competition_type"),
        "by_market_evidence_tier": grouped("market_evidence_tier"),
        "by_evaluation_group": grouped("evaluation_group"),
        "by_confidence_label": grouped("confidence_label"),
        "by_result_method": grouped("verification_method"),
        "by_competition_target": {
            key: _shadow_group_summary(group, gate)
            for key, group in sorted(competition_target.items())
        },
    }
    record_id = stable_id("research_shadow_evaluation", channel, iso_utc(evaluated_at))
    record = {
        **_base_record("ResearchShadowEvaluation", record_id, "model_artifact"),
        "model_key": MODEL_KEY,
        "model_version": None,
        "evaluated_at": iso_utc(evaluated_at),
        "evaluation_kind": "research_shadow_predictions_not_formal_backtest",
        "dataset_hash": None,
        "channel": channel,
        "metrics": metrics,
    }
    run_id = make_run_id(evaluated_at)
    with research_facts_lock(config):
        store = ResearchStore(config)
        path = store.write_records("model-artifacts", run_id, "shadow-evaluation", [record])
        store.write_manifest(
            run_id,
            "shadow-evaluation",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "channel": channel,
                "record_path": path.relative_to(config.research_dir).as_posix(),
            },
        )
    return {"status": "completed", "run_id": run_id, "path": str(path), "metrics": metrics}
