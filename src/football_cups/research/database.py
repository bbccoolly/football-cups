from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import apply_migrations, connect

from . import SCHEMA_VERSION, research_flags
from .config import ResearchConfig
from .competition_profiles import CONFIDENCE_RANK, valid_sha256
from .k1_guardrail import K1GuardrailError, validate_assessment_record, verify_shadow_manifest


SUPPORTED_TYPES = frozenset(
    {
        "ResearchSourceAsset",
        "ResearchFixture",
        "ResearchMarketObservation",
        "ResearchFeatureRow",
        "ResearchQualityEvent",
        "ResearchModelDataset",
        "ResearchModelVersion",
        "ResearchModelActivation",
        "ResearchShadowPrediction",
        "ResearchK1GuardrailAssessment",
        "ResearchRetrospectiveEvaluation",
        "ResearchShadowEvaluation",
    }
)


class ResearchImportError(ValueError):
    pass


class ResearchImportIntegrityError(RuntimeError):
    pass


def _validate_shadow_policy(record: dict[str, Any], source_file: str, line_number: int) -> None:
    if record.get("record_type") != "ResearchShadowPrediction" or not record.get("policy_version"):
        return
    required = (
        "competition_type",
        "market_evidence_tier",
        "evaluation_group",
        "classification_status",
        "registry_version",
        "registry_file_sha256",
        "registry_canonical_sha256",
        "raw_confidence_label",
        "competition_confidence_cap",
        "confidence_label",
        "confidence_reasons",
        "risk_flags",
    )
    missing = [key for key in required if record.get(key) is None]
    if missing:
        raise ResearchImportIntegrityError(
            f"{source_file}:{line_number}: incomplete shadow policy fields: {', '.join(missing)}"
        )
    if not valid_sha256(record["registry_file_sha256"]) or not valid_sha256(
        record["registry_canonical_sha256"]
    ):
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: invalid registry SHA-256")
    raw = record.get("raw_confidence_label")
    cap = record.get("competition_confidence_cap")
    final = record.get("confidence_label")
    if raw not in CONFIDENCE_RANK or cap not in CONFIDENCE_RANK or final not in CONFIDENCE_RANK:
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: invalid confidence label")
    if CONFIDENCE_RANK[final] > min(CONFIDENCE_RANK[raw], CONFIDENCE_RANK[cap]):
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: confidence exceeds stored cap")
    tier = record.get("market_evidence_tier")
    if tier == "C" and CONFIDENCE_RANK[cap] > CONFIDENCE_RANK["low"]:
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: C tier cap exceeds low")
    if tier == "D" and (
        cap != "observation_only"
        or final != "observation_only"
        or record.get("status") != "abstained"
    ):
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: D tier must abstain")
    if (
        record.get("classification_status") == "provisional"
        and CONFIDENCE_RANK[final] > CONFIDENCE_RANK["medium"]
    ):
        raise ResearchImportIntegrityError(
            f"{source_file}:{line_number}: provisional prediction exceeds medium"
        )
    if record.get("status") == "published" and not record.get("identity_record_id"):
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: published prediction lacks identity")


@contextmanager
def research_import_lock(connection: Connection) -> Iterator[None]:
    lock_key = 2026071704
    acquired = connection.execute(
        "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
    ).fetchone()["acquired"]
    connection.commit()
    if not acquired:
        raise RuntimeError("another research importer is running")
    try:
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        connection.commit()


def _validate(record: Any, source_file: str, line_number: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ResearchImportError(f"{source_file}:{line_number}: record must be an object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ResearchImportError(f"{source_file}:{line_number}: unsupported schema version")
    if record.get("record_type") not in SUPPORTED_TYPES:
        raise ResearchImportError(f"{source_file}:{line_number}: unsupported record type")
    if not isinstance(record.get("record_id"), str) or not record["record_id"]:
        raise ResearchImportError(f"{source_file}:{line_number}: invalid record id")
    kind = str(record.get("research_kind") or "historical")
    expected_flags = research_flags(kind)
    for key, expected in expected_flags.items():
        if key == "research_kind" and "research_kind" not in record and kind == "historical":
            continue
        if record.get(key) != expected:
            raise ResearchImportError(f"{source_file}:{line_number}: invalid research flag {key}")
    _validate_shadow_policy(record, source_file, line_number)
    try:
        validate_assessment_record(record)
    except K1GuardrailError as exc:
        raise ResearchImportIntegrityError(f"{source_file}:{line_number}: {exc}") from exc
    return record


def _insert_typed(connection: Connection, record: dict[str, Any]) -> None:
    record_id = record["record_id"]
    record_type = record["record_type"]
    if record_type == "ResearchSourceAsset":
        connection.execute(
            """
            INSERT INTO research.source_assets(
                record_id, source_id, asset_id, url, asset_kind, sha256,
                size_bytes, blob_path, downloaded_at, etag, last_modified,
                metadata_sha256, input_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["source_id"],
                record["asset_id"],
                record.get("url"),
                record["asset_kind"],
                record["sha256"],
                record["size_bytes"],
                record["blob_path"],
                record.get("downloaded_at"),
                record.get("etag"),
                record.get("last_modified"),
                record.get("metadata_sha256"),
                record.get("input_hash"),
            ),
        )
    elif record_type == "ResearchFixture":
        connection.execute(
            """
            INSERT INTO research.fixtures(
                record_id, source_id, source_asset_record_id, source_fixture_key,
                competition, match_date, kickoff_time_raw, home_team, away_team,
                home_goals, away_goals, result_scope, result_eligible, source_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["source_id"],
                record["source_asset_record_id"],
                record["source_fixture_key"],
                record["competition"],
                record["match_date"],
                record.get("kickoff_time_raw"),
                record["home_team"],
                record["away_team"],
                record.get("home_goals"),
                record.get("away_goals"),
                record["result_scope"],
                record["result_eligible"],
                Jsonb(record.get("source_payload") or {}),
            ),
        )
    elif record_type == "ResearchMarketObservation":
        connection.execute(
            """
            INSERT INTO research.market_observations(
                record_id, fixture_record_id, source_id, asset_sha256, cohort,
                market, bookmaker, line, values_json, market_contract
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["fixture_record_id"],
                record["source_id"],
                record["asset_sha256"],
                record["cohort"],
                record["market"],
                record["bookmaker"],
                record.get("line"),
                Jsonb(record["values"]),
                record["market_contract"],
            ),
        )
    elif record_type == "ResearchFeatureRow":
        connection.execute(
            """
            INSERT INTO research.feature_rows(
                record_id, source_id, source_asset_record_id, source_fixture_key,
                competition, match_date, season, cohort, feature_schema,
                market_contract, input_hash, result_scope, result_eligible, features
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["source_id"],
                record["source_asset_record_id"],
                record["source_fixture_key"],
                record["competition"],
                record["match_date"],
                record["season"],
                record["cohort"],
                record["feature_schema"],
                record["market_contract"],
                record["input_hash"],
                record["result_scope"],
                record["result_eligible"],
                Jsonb(record["features"]),
            ),
        )
    elif record_type == "ResearchQualityEvent":
        connection.execute(
            """
            INSERT INTO research.quality_events(record_id, source_id, event_type, status, details)
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["source_id"],
                record["event_type"],
                record["status"],
                Jsonb(record.get("details") or {}),
            ),
        )
    elif record_type == "ResearchModelDataset":
        connection.execute(
            """
            INSERT INTO research.model_datasets(
                record_id, model_key, dataset_hash, training_before_date,
                created_at, source_record_count, fixture_count, feature_schema,
                training_fixture_ids, evaluation_fixture_ids, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["model_key"],
                record["dataset_hash"],
                record["training_before_date"],
                record["created_at"],
                record["source_record_count"],
                record["fixture_count"],
                record["feature_schema"],
                Jsonb(record.get("training_fixture_ids") or []),
                Jsonb(record.get("evaluation_fixture_ids") or []),
                Jsonb(record),
            ),
        )
    elif record_type == "ResearchModelVersion":
        connection.execute(
            """
            INSERT INTO research.model_versions(
                record_id, model_key, model_version, dataset_record_id,
                trained_at, algorithm, artifact_json, metrics
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["model_key"],
                record["model_version"],
                record["dataset_record_id"],
                record["trained_at"],
                record["algorithm"],
                Jsonb(record.get("artifact") or {}),
                Jsonb(record.get("metrics") or {}),
            ),
        )
    elif record_type == "ResearchModelActivation":
        connection.execute(
            """
            INSERT INTO research.model_activations(
                record_id, channel, model_key, model_version, model_record_id,
                activated_at, active_from, active_until, status, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["channel"],
                record["model_key"],
                record["model_version"],
                record["model_record_id"],
                record["activated_at"],
                record.get("active_from"),
                record.get("active_until"),
                record["status"],
                record.get("notes"),
            ),
        )
    elif record_type == "ResearchShadowPrediction":
        connection.execute(
            """
            INSERT INTO research.shadow_predictions(
                record_id, channel, fixture_id, target, prediction_cutoff,
                published_at, status, model_key, model_version,
                activation_record_id, selected_batch_record_id,
                source_snapshot_record_id, market_observed_at, bookmaker_count,
                probabilities, features, abstention_reason,
                competition_id, competition_name, competition_type,
                market_evidence_tier, evaluation_group, classification_status,
                registry_version, policy_version, registry_file_sha256,
                registry_canonical_sha256, direction_strength,
                bookmaker_dispersion, raw_confidence_label,
                competition_confidence_cap, confidence_label,
                confidence_reasons, risk_flags, identity_record_id,
                identity_observed_at, automatic_verified_fixture_count,
                evaluation_span_days, review_eligible
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record["channel"],
                str(record["fixture_id"]),
                record["target"],
                record["prediction_cutoff"],
                record["published_at"],
                record["status"],
                record.get("model_key"),
                record.get("model_version"),
                record.get("activation_record_id"),
                record.get("selected_batch_record_id"),
                record.get("source_snapshot_record_id"),
                record.get("market_observed_at"),
                record.get("bookmaker_count"),
                Jsonb(record.get("probabilities") or {}),
                Jsonb(record.get("features") or {}),
                record.get("abstention_reason"),
                record.get("competition_id"),
                record.get("competition_name"),
                record.get("competition_type"),
                record.get("market_evidence_tier"),
                record.get("evaluation_group"),
                record.get("classification_status"),
                record.get("registry_version"),
                record.get("policy_version"),
                record.get("registry_file_sha256"),
                record.get("registry_canonical_sha256"),
                record.get("direction_strength"),
                record.get("bookmaker_dispersion"),
                record.get("raw_confidence_label"),
                record.get("competition_confidence_cap"),
                record.get("confidence_label"),
                Jsonb(record.get("confidence_reasons") or []),
                Jsonb(record.get("risk_flags") or []),
                record.get("identity_record_id"),
                record.get("identity_observed_at"),
                record.get("automatic_verified_fixture_count"),
                record.get("evaluation_span_days"),
                record.get("review_eligible"),
            ),
        )
    elif record_type == "ResearchK1GuardrailAssessment":
        prediction = connection.execute(
            """
            SELECT channel, fixture_id, competition_id, target, prediction_cutoff, published_at,
                   identity_record_id, selected_batch_record_id
            FROM research.shadow_predictions WHERE record_id=%s
            """,
            (record["prediction_record_id"],),
        ).fetchone()
        if prediction is None:
            raise ResearchImportIntegrityError("K1 assessment references a missing prediction")
        expected = {
            "channel": record["channel"],
            "fixture_id": str(record["fixture_id"]),
            "competition_id": record["competition_id"],
            "target": record["target"],
            "prediction_cutoff": record["prediction_cutoff"],
            "identity_record_id": record.get("identity_record_id"),
            "selected_batch_record_id": record.get("selected_batch_record_id"),
        }
        actual = dict(prediction)
        published_at = actual.pop("published_at")
        actual["fixture_id"] = str(actual["fixture_id"])
        if str(actual["prediction_cutoff"]) != str(expected["prediction_cutoff"]):
            from datetime import datetime as _datetime
            expected_cutoff = _datetime.fromisoformat(str(expected["prediction_cutoff"]).replace("Z", "+00:00"))
            if actual["prediction_cutoff"] != expected_cutoff:
                raise ResearchImportIntegrityError("K1 assessment prediction cutoff mismatch")
            actual["prediction_cutoff"] = expected["prediction_cutoff"]
        if actual != expected:
            raise ResearchImportIntegrityError("K1 assessment prediction reference mismatch")
        from datetime import datetime as _datetime
        assessed_at = _datetime.fromisoformat(str(record["assessed_at"]).replace("Z", "+00:00"))
        if assessed_at < published_at:
            raise ResearchImportIntegrityError("K1 assessment precedes its prediction")
        connection.execute(
            """
            INSERT INTO research.k1_guardrail_assessments(
                record_id, prediction_record_id, channel, fixture_id, competition_id,
                target, prediction_cutoff, assessed_at, policy_version, policy_revision,
                policy_status, policy_snapshot, policy_file_sha256,
                policy_canonical_sha256, historical_dataset_sha256, git_commit,
                relevant_source_tree_sha256, relevant_dirty_paths, identity_record_id,
                selected_batch_record_id, snapshot_record_ids, source_row_record_ids,
                source_hashes, raw_features, rule_evaluations, rule_flags,
                proposed_action, proposed_confidence_cap, reasons, audit_status, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id, record["prediction_record_id"], record["channel"], str(record["fixture_id"]),
                record["competition_id"], record["target"], record["prediction_cutoff"], record["assessed_at"],
                record["policy_version"], record["policy_revision"], record["policy_status"],
                Jsonb(record["policy_snapshot"]), record["policy_file_sha256"], record["policy_canonical_sha256"],
                record["historical_dataset_sha256"], record.get("git_commit"), record["relevant_source_tree_sha256"],
                Jsonb(record.get("relevant_dirty_paths") or []), record.get("identity_record_id"),
                record.get("selected_batch_record_id"), Jsonb(record.get("snapshot_record_ids") or {}),
                Jsonb(record.get("source_row_record_ids") or []), Jsonb(record.get("source_hashes") or {}),
                Jsonb(record.get("raw_features") or {}), Jsonb(record.get("rule_evaluations") or {}),
                Jsonb(record.get("rule_flags") or []), record["proposed_action"],
                record.get("proposed_confidence_cap"), Jsonb(record.get("reasons") or []),
                record["audit_status"], Jsonb(record),
            ),
        )
    elif record_type in {"ResearchRetrospectiveEvaluation", "ResearchShadowEvaluation"}:
        table = (
            "retrospective_evaluations"
            if record_type == "ResearchRetrospectiveEvaluation"
            else "shadow_evaluations"
        )
        connection.execute(
            f"""
            INSERT INTO research.{table}(
                record_id, model_key, model_version, evaluated_at,
                evaluation_kind, dataset_hash, metrics, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                record.get("model_key"),
                record.get("model_version"),
                record["evaluated_at"],
                record["evaluation_kind"],
                record.get("dataset_hash"),
                Jsonb(record.get("metrics") or {}),
                Jsonb(record),
            ),
        )


def _insert_record(
    connection: Connection, record: dict[str, Any], source_file: str, line_number: int
) -> bool:
    if record.get("record_type") == "ResearchK1GuardrailAssessment":
        prior = connection.execute(
            "SELECT payload FROM research.records WHERE record_id=%s", (record["record_id"],)
        ).fetchone()
        if prior is not None and prior["payload"] != record:
            raise ResearchImportIntegrityError(
                f"{source_file}:{line_number}: immutable K1 assessment payload changed"
            )
    inserted = connection.execute(
        """
        INSERT INTO research.records(
            record_id, record_type, schema_version, source_file, source_line, payload
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (record_id) DO NOTHING
        """,
        (
            record["record_id"],
            record["record_type"],
            record["schema_version"],
            source_file,
            line_number,
            Jsonb(record),
        ),
    ).rowcount == 1
    _insert_typed(connection, record)
    return inserted


def import_research_files(connection: Connection, normalized_dir: Path) -> dict[str, Any]:
    run_id = uuid4().hex
    started_at = datetime.now(UTC)
    summary = {"run_id": run_id, "files_seen": 0, "records_inserted": 0, "records_existing": 0}
    connection.execute(
        "INSERT INTO research.import_runs(run_id, started_at, status) VALUES (%s, %s, 'running')",
        (run_id, started_at),
    )
    connection.commit()
    try:
        for path in sorted(normalized_dir.rglob("*.jsonl")) if normalized_dir.is_dir() else []:
            source_file = path.relative_to(normalized_dir.parent).as_posix()
            try:
                verify_shadow_manifest(normalized_dir.parent, path)
            except K1GuardrailError as exc:
                raise ResearchImportIntegrityError(f"{source_file}: {exc}") from exc
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            checkpoint = connection.execute(
                "SELECT sha256 FROM research.import_checkpoints WHERE source_file=%s",
                (source_file,),
            ).fetchone()
            if checkpoint:
                if checkpoint["sha256"] != digest:
                    raise RuntimeError(f"immutable research file changed: {source_file}")
                summary["files_seen"] += 1
                continue
            inserted = existing = line_count = 0
            with connection.transaction():
                for line_count, raw_line in enumerate(content.splitlines(), start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ResearchImportError(f"{source_file}:{line_count}: invalid JSON") from exc
                    record = _validate(value, source_file, line_count)
                    if _insert_record(connection, record, source_file, line_count):
                        inserted += 1
                    else:
                        existing += 1
                connection.execute(
                    "INSERT INTO research.import_checkpoints(source_file, sha256, size_bytes, line_count) "
                    "VALUES (%s, %s, %s, %s)",
                    (source_file, digest, len(content), line_count),
                )
            summary["files_seen"] += 1
            summary["records_inserted"] += inserted
            summary["records_existing"] += existing
        connection.execute(
            """
            UPDATE research.import_runs SET finished_at=%s, status='success', files_seen=%s,
                records_inserted=%s, records_existing=%s WHERE run_id=%s
            """,
            (
                datetime.now(UTC),
                summary["files_seen"],
                summary["records_inserted"],
                summary["records_existing"],
                run_id,
            ),
        )
        connection.commit()
        return summary
    except Exception as exc:
        connection.rollback()
        connection.execute(
            """
            UPDATE research.import_runs SET finished_at=%s, status='failure', files_seen=%s,
                records_inserted=%s, records_existing=%s, error_type=%s, error_message=%s
            WHERE run_id=%s
            """,
            (
                datetime.now(UTC),
                summary["files_seen"],
                summary["records_inserted"],
                summary["records_existing"],
                type(exc).__name__,
                str(exc),
                run_id,
            ),
        )
        connection.commit()
        raise


def run_database_import(config: ResearchConfig) -> dict[str, Any]:
    database_config = DatabaseConfig.from_workspace(config.workspace)
    with connect(database_config) as connection:
        migrations = apply_migrations(connection)
        before = {
            str(row["target"]): int(row["count"])
            for row in connection.execute(
                "SELECT target, count(DISTINCT fixture_id) AS count "
                "FROM football.strict_fixture_results_by_cutoff GROUP BY target ORDER BY target"
            ).fetchall()
        }
        with research_import_lock(connection):
            summary = import_research_files(connection, config.normalized_dir)
        after = {
            str(row["target"]): int(row["count"])
            for row in connection.execute(
                "SELECT target, count(DISTINCT fixture_id) AS count "
                "FROM football.strict_fixture_results_by_cutoff GROUP BY target ORDER BY target"
            ).fetchall()
        }
        if before != after:
            raise RuntimeError("research import changed strict fixture counts")
        count_tables = (
            "source_assets",
            "fixtures",
            "market_observations",
            "feature_rows",
            "quality_events",
            "model_datasets",
            "model_versions",
            "model_activations",
            "shadow_predictions",
            "k1_guardrail_assessments",
            "retrospective_evaluations",
            "shadow_evaluations",
        )
        counts = {
            table: int(
                connection.execute(f"SELECT count(*) AS count FROM research.{table}").fetchone()["count"]
            )
            for table in count_tables
        }
    return {**summary, "migrations_applied": migrations, "counts": counts, "strict_counts": after}
