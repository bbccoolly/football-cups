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

from . import RESEARCH_FLAGS, SCHEMA_VERSION
from .config import ResearchConfig


SUPPORTED_TYPES = frozenset(
    {
        "ResearchSourceAsset",
        "ResearchFixture",
        "ResearchMarketObservation",
        "ResearchFeatureRow",
        "ResearchQualityEvent",
    }
)


class ResearchImportError(ValueError):
    pass


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
    for key, expected in RESEARCH_FLAGS.items():
        if record.get(key) is not expected:
            raise ResearchImportError(f"{source_file}:{line_number}: invalid research flag {key}")
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


def _insert_record(
    connection: Connection, record: dict[str, Any], source_file: str, line_number: int
) -> bool:
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
        counts = {
            table: int(
                connection.execute(f"SELECT count(*) AS count FROM research.{table}").fetchone()["count"]
            )
            for table in ("source_assets", "fixtures", "market_observations", "feature_rows", "quality_events")
        }
    return {**summary, "migrations_applied": migrations, "counts": counts, "strict_counts": after}
