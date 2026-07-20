from __future__ import annotations

import json
import hashlib
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb


SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_RECORD_TYPES = frozenset(
    {
        "FixtureIdentity",
        "DiscoveryObservation",
        "SportteryPoolObservation",
        "SnapshotBatch",
        "MarketSnapshot",
        "BookmakerMarketRow",
        "MarketNormalization",
        "SnapshotEligibilityAssessment",
        "HandicapIndexRow",
        "ResultCandidate",
        "VerifiedResult",
        "QualityEvent",
    }
)
SUMMARY_BOOKMAKERS = frozenset({"最高值", "最低值", "平均值", "离散值"})


class ImportContractError(ValueError):
    pass


class AppendOnlyViolation(RuntimeError):
    pass


class ImportAlreadyRunning(RuntimeError):
    pass


@dataclass
class ImportSummary:
    run_id: str
    status: str = "running"
    files_seen: int = 0
    lines_seen: int = 0
    records_inserted: int = 0
    records_existing: int = 0
    inserted_by_type: Counter[str] = field(default_factory=Counter)
    unsupported_by_type: Counter[str] = field(default_factory=Counter)

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["inserted_by_type"] = dict(sorted(self.inserted_by_type.items()))
        payload["unsupported_by_type"] = dict(sorted(self.unsupported_by_type.items()))
        return payload


@dataclass
class ManifestImportSummary:
    files_seen: int = 0
    manifests_inserted: int = 0
    manifests_existing: int = 0



def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def import_lock(connection: Connection):
    lock_key = 2026071603
    row = connection.execute(
        "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
    ).fetchone()
    connection.commit()
    if not row["acquired"]:
        raise ImportAlreadyRunning("another database import is already running")
    try:
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        connection.commit()


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("parsed")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ImportContractError(f"invalid timestamp value: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportContractError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ImportContractError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(timezone.utc)


def parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("decimal")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ImportContractError(f"invalid decimal value: {value!r}") from exc


def nested_decimal(record: dict[str, Any], section: str, key: str) -> Decimal | None:
    container = record.get(section)
    return parse_decimal(container.get(key)) if isinstance(container, dict) else None


def record_event_at(record: dict[str, Any]) -> datetime | None:
    for key in (
        "observed_at",
        "occurred_at",
        "confirmed_at",
        "completed_at",
        "normalized_at",
        "assessed_at",
    ):
        if record.get(key):
            return parse_time(record[key])
    return None


def bookmaker_role(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    normalized = name.strip()
    if normalized in SUMMARY_BOOKMAKERS:
        return "summary"
    if normalized == "竞彩官方":
        return "official"
    return "bookmaker"


def validate_record(record: Any, *, source_file: str, source_line: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ImportContractError(f"{source_file}:{source_line}: JSON value must be an object")
    missing = [key for key in ("record_id", "record_type", "schema_version") if key not in record]
    if missing:
        raise ImportContractError(
            f"{source_file}:{source_line}: missing fields: {', '.join(missing)}"
        )
    if not isinstance(record["record_id"], str) or not record["record_id"]:
        raise ImportContractError(f"{source_file}:{source_line}: invalid record_id")
    if not isinstance(record["record_type"], str) or not record["record_type"]:
        raise ImportContractError(f"{source_file}:{source_line}: invalid record_type")
    if record["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise ImportContractError(
            f"{source_file}:{source_line}: unsupported schema_version "
            f"{record['schema_version']!r}"
        )
    return record


def _insert_typed(connection: Connection, record: dict[str, Any]) -> None:
    record_type = record["record_type"]
    record_id = record["record_id"]
    fixture_id = str(record["fixture_id"]) if record.get("fixture_id") is not None else None

    if record_type == "FixtureIdentity":
        connection.execute(
            """
            INSERT INTO football.fixture_identities (
                record_id, fixture_id, observed_at, kickoff_at, buy_end_at,
                competition_id, competition_name, season_id, match_number,
                home_team_id, home_team_name, away_team_id, away_team_name,
                identity_status
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                parse_time(record.get("observed_at")),
                parse_time(record.get("kickoff_at")),
                parse_time(record.get("buy_end_at")),
                record.get("competition_id"),
                record.get("competition_name"),
                record.get("season_id"),
                record.get("match_number"),
                record.get("home_team_id"),
                record.get("home_team_name"),
                record.get("away_team_id"),
                record.get("away_team_name"),
                record.get("identity_status"),
            ),
        )
        return

    if record_type == "DiscoveryObservation":
        connection.execute(
            """
            INSERT INTO football.discovery_observations (
                record_id, fixture_id, observed_at, kickoff_at, buy_end_at,
                source_name, source_url, competition_id, competition_name,
                season_id, match_number, home_team_id, home_team_name,
                away_team_id, away_team_name, official_handicap_raw,
                is_show_raw, is_active_raw, is_end_raw, row_sha256
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                parse_time(record.get("observed_at")),
                parse_time(record.get("kickoff_at")),
                parse_time(record.get("buy_end_at")),
                record.get("source_name"),
                record.get("source_url"),
                record.get("competition_id"),
                record.get("competition_name"),
                record.get("season_id"),
                record.get("match_number"),
                record.get("home_team_id"),
                record.get("home_team_name"),
                record.get("away_team_id"),
                record.get("away_team_name"),
                record.get("official_handicap_raw"),
                record.get("is_show_raw"),
                record.get("is_active_raw"),
                record.get("is_end_raw"),
                record.get("row_sha256"),
            ),
        )
        return

    if record_type == "SportteryPoolObservation":
        connection.execute(
            """
            INSERT INTO football.sporttery_pool_observations (
                record_id, fixture_id, observed_at, source_name, source_url,
                pool_type, option_value, handicap_raw, sp_raw, sp_decimal
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                parse_time(record.get("observed_at")),
                record.get("source_name"),
                record.get("source_url"),
                record.get("pool_type"),
                str(record.get("option_value")),
                record.get("handicap_raw"),
                record.get("sp_raw"),
                parse_decimal(record.get("sp_raw")),
            ),
        )
        return

    if record_type == "SnapshotBatch":
        connection.execute(
            """
            INSERT INTO football.snapshot_batches (
                record_id, fixture_id, target, job_id, window_start, window_end,
                completed_at, core_market_complete, strict_eligible, market_results
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("target"),
                record.get("job_id"),
                parse_time(record.get("window_start")),
                parse_time(record.get("window_end")),
                parse_time(record.get("completed_at")),
                bool(record.get("core_market_complete")),
                bool(record.get("strict_eligible")),
                Jsonb(record.get("market_results") or {}),
            ),
        )
        return

    if record_type == "MarketSnapshot":
        connection.execute(
            """
            INSERT INTO football.market_snapshots (
                record_id, fixture_id, market, target, observed_at, ingested_at,
                corrected_at, source_event_time, source_url, raw_sha256,
                parser_version, parse_status, source_market_available, clock_ok,
                bookmaker_count, row_count
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("market"),
                record.get("target"),
                parse_time(record.get("observed_at")),
                parse_time(record.get("ingested_at")),
                parse_time(record.get("corrected_at")),
                parse_time(record.get("source_event_time")),
                record.get("source_url"),
                record.get("raw_sha256"),
                record.get("parser_version"),
                record.get("parse_status"),
                record.get("source_market_available"),
                record.get("clock_ok"),
                record.get("bookmaker_count"),
                record.get("row_count"),
            ),
        )
        return

    if record_type == "BookmakerMarketRow":
        opening = record.get("opening")
        current = record.get("current")
        name = record.get("source_bookmaker_name")
        connection.execute(
            """
            INSERT INTO football.bookmaker_market_rows (
                record_id, fixture_id, market, target, observed_at, corrected_at,
                source_event_time, opening_source_event_time, source_bookmaker_id,
                source_bookmaker_name, row_role, opening, current,
                opening_home, opening_draw, opening_away, opening_line,
                opening_over, opening_under, current_home, current_draw,
                current_away, current_line, current_over, current_under,
                parser_version, normalization_version, normalized_at,
                source_snapshot_record_id, normalization_record_id,
                source_page_sha256, source_workbook_sha256,
                source_page_observed_at, snapshot_observed_at, source_row_index,
                line_movement, reprocessed, event_origin
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("market"),
                record.get("target"),
                parse_time(record.get("observed_at")),
                parse_time(record.get("corrected_at")),
                parse_time(record.get("source_event_time")),
                parse_time(record.get("opening_source_event_time")),
                record.get("source_bookmaker_id"),
                name,
                bookmaker_role(name),
                Jsonb(opening) if opening is not None else None,
                Jsonb(current) if current is not None else None,
                nested_decimal(record, "opening", "home"),
                nested_decimal(record, "opening", "draw"),
                nested_decimal(record, "opening", "away"),
                nested_decimal(record, "opening", "line"),
                nested_decimal(record, "opening", "over"),
                nested_decimal(record, "opening", "under"),
                nested_decimal(record, "current", "home"),
                nested_decimal(record, "current", "draw"),
                nested_decimal(record, "current", "away"),
                nested_decimal(record, "current", "line"),
                nested_decimal(record, "current", "over"),
                nested_decimal(record, "current", "under"),
                record.get("parser_version"),
                record.get("normalization_version"),
                parse_time(record.get("normalized_at")),
                record.get("source_snapshot_record_id"),
                record.get("normalization_record_id"),
                record.get("source_page_sha256"),
                record.get("source_workbook_sha256"),
                parse_time(record.get("source_page_observed_at")),
                parse_time(record.get("snapshot_observed_at")),
                record.get("source_row_index"),
                Jsonb(record.get("line_movement")) if record.get("line_movement") else None,
                bool(record.get("reprocessed")),
                record.get("event_origin") or "live",
            ),
        )
        return

    if record_type == "MarketNormalization":
        connection.execute(
            """
            INSERT INTO football.market_normalizations (
                record_id, fixture_id, snapshot_record_id, market, target,
                normalization_version, parser_version, normalized_at, status,
                valid_bookmaker_rows, line_parse_failure_count,
                source_page_sha256, source_workbook_sha256,
                source_page_observed_at, snapshot_observed_at,
                quality_reasons, decoding, reprocessed, event_origin
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("snapshot_record_id"),
                record.get("market"),
                record.get("target"),
                record.get("normalization_version"),
                record.get("parser_version"),
                parse_time(record.get("normalized_at")),
                record.get("status"),
                record.get("valid_bookmaker_rows"),
                record.get("line_parse_failure_count"),
                record.get("source_page_sha256"),
                record.get("source_workbook_sha256"),
                parse_time(record.get("source_page_observed_at")),
                parse_time(record.get("snapshot_observed_at")),
                Jsonb(record.get("quality_reasons") or []),
                Jsonb(record.get("decoding") or {}),
                bool(record.get("reprocessed")),
                record.get("event_origin") or "live",
            ),
        )
        return

    if record_type == "SnapshotEligibilityAssessment":
        connection.execute(
            """
            INSERT INTO football.snapshot_eligibility_assessments (
                record_id, fixture_id, snapshot_batch_record_id, target,
                assessment_version, assessed_at, collection_eligible,
                data_complete, model_strict_eligible, market_stats,
                ineligibility_reasons, event_origin
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("snapshot_batch_record_id"),
                record.get("target"),
                record.get("assessment_version"),
                parse_time(record.get("assessed_at")),
                bool(record.get("collection_eligible")),
                bool(record.get("data_complete")),
                bool(record.get("model_strict_eligible")),
                Jsonb(record.get("market_stats") or {}),
                Jsonb(record.get("ineligibility_reasons") or []),
                record.get("event_origin") or "live",
            ),
        )
        return

    if record_type == "HandicapIndexRow":
        connection.execute(
            """
            INSERT INTO football.handicap_index_rows (
                record_id, fixture_id, target, observed_at, source_bookmaker_name,
                handicap_line, home_index, draw_index, away_index,
                home_probability, draw_probability, away_probability, return_rate,
                home_kelly, draw_kelly, away_kelly, raw_cells, parser_version,
                normalization_version, normalized_at, source_snapshot_record_id,
                normalization_record_id, source_page_sha256,
                source_page_observed_at, snapshot_observed_at, source_row_index,
                reprocessed, event_origin
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                record.get("target"),
                parse_time(record.get("observed_at")),
                record.get("source_bookmaker_name"),
                parse_decimal(record.get("handicap_line")),
                parse_decimal(record.get("home_index")),
                parse_decimal(record.get("draw_index")),
                parse_decimal(record.get("away_index")),
                parse_decimal(record.get("home_probability")),
                parse_decimal(record.get("draw_probability")),
                parse_decimal(record.get("away_probability")),
                parse_decimal(record.get("return_rate")),
                parse_decimal(record.get("home_kelly")),
                parse_decimal(record.get("draw_kelly")),
                parse_decimal(record.get("away_kelly")),
                Jsonb(record.get("raw_cells") or []),
                record.get("parser_version"),
                record.get("normalization_version"),
                parse_time(record.get("normalized_at")),
                record.get("source_snapshot_record_id"),
                record.get("normalization_record_id"),
                record.get("source_page_sha256"),
                parse_time(record.get("source_page_observed_at")),
                parse_time(record.get("snapshot_observed_at")),
                record.get("source_row_index"),
                bool(record.get("reprocessed")),
                record.get("event_origin") or "live",
            ),
        )
        return

    if record_type == "ResultCandidate":
        connection.execute(
            """
            INSERT INTO football.result_candidates (
                record_id, fixture_id, observed_at, kickoff_at, home_goals, away_goals,
                half_time_score_raw, status_raw, status_code, scope,
                completed_page_sha256, live_page_sha256, analysis_page_sha256,
                analysis_consistency, source_urls
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                parse_time(record.get("observed_at")),
                parse_time(record.get("kickoff_at")),
                record.get("home_goals"),
                record.get("away_goals"),
                record.get("half_time_score_raw"),
                record.get("status_raw"),
                record.get("status_code"),
                record.get("scope"),
                record.get("completed_page_sha256"),
                record.get("live_page_sha256"),
                record.get("analysis_page_sha256"),
                record.get("analysis_consistency"),
                Jsonb(record.get("source_urls") or []),
            ),
        )
        return

    if record_type == "VerifiedResult":
        connection.execute(
            """
            INSERT INTO football.verified_results (
                record_id, fixture_id, confirmed_at, home_goals, away_goals,
                scope, source_url, verification_method, verification_status,
                notes, candidate_id, supersedes_record_id, correction_reason
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                fixture_id,
                parse_time(record.get("confirmed_at")),
                record.get("home_goals"),
                record.get("away_goals"),
                record.get("scope"),
                record.get("source_url"),
                record.get("verification_method"),
                record.get("verification_status") or "accepted",
                record.get("notes"),
                record.get("candidate_id"),
                record.get("supersedes_record_id"),
                record.get("correction_reason"),
            ),
        )
        return

    if record_type == "QualityEvent":
        connection.execute(
            """
            INSERT INTO football.quality_events (
                record_id, occurred_at, event_type, status, fixture_id,
                competition, market, cutoff, details
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (record_id) DO NOTHING
            """,
            (
                record_id,
                parse_time(record.get("occurred_at")),
                record.get("event_type"),
                record.get("status"),
                fixture_id,
                record.get("competition"),
                record.get("market"),
                record.get("cutoff"),
                Jsonb(record.get("details") or {}),
            ),
        )


def insert_record(
    connection: Connection,
    record: dict[str, Any],
    *,
    source_file: str,
    source_line: int,
) -> bool:
    fixture_id = str(record["fixture_id"]) if record.get("fixture_id") is not None else None
    cursor = connection.execute(
        """
        INSERT INTO football.records (
            record_id, record_type, schema_version, fixture_id, event_at,
            payload, source_file, source_line
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (record_id) DO NOTHING
        """,
        (
            record["record_id"],
            record["record_type"],
            record["schema_version"],
            fixture_id,
            record_event_at(record),
            Jsonb(record),
            source_file,
            source_line,
        ),
    )
    _insert_typed(connection, record)
    return cursor.rowcount == 1


def _last_record_id(handle: BinaryIO, byte_offset: int) -> str | None:
    if byte_offset == 0:
        return None
    handle.seek(byte_offset - 1)
    if handle.read(1) != b"\n":
        raise AppendOnlyViolation("checkpoint does not end at a complete JSONL line")
    end = byte_offset - 1
    position = end
    chunk_size = 8192
    line_start = 0
    while position > 0:
        read_size = min(chunk_size, position)
        position -= read_size
        handle.seek(position)
        chunk = handle.read(read_size)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            line_start = position + newline + 1
            break
    handle.seek(line_start)
    raw_line = handle.read(end - line_start)
    try:
        payload = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppendOnlyViolation("checkpoint tail is not valid JSON") from exc
    record_id = payload.get("record_id") if isinstance(payload, dict) else None
    return str(record_id) if record_id else None


def import_manifests(connection: Connection, data_dir: Path) -> ManifestImportSummary:
    summary = ManifestImportSummary()
    manifest_dir = data_dir / "manifests"
    files = sorted(manifest_dir.rglob("*.json")) if manifest_dir.is_dir() else []
    with connection.transaction():
        for path in files:
            summary.files_seen += 1
            relative = path.relative_to(data_dir).as_posix()
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            existing = connection.execute(
                "SELECT sha256 FROM football.collection_manifests WHERE source_file = %s",
                (relative,),
            ).fetchone()
            if existing:
                if existing["sha256"] != digest:
                    raise AppendOnlyViolation(
                        f"{relative}: immutable manifest content changed"
                    )
                summary.manifests_existing += 1
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImportContractError(f"{relative}: invalid UTF-8 JSON manifest") from exc
            if not isinstance(payload, dict):
                raise ImportContractError(f"{relative}: manifest must be a JSON object")
            schema_version = payload.get("schema_version")
            record_type = payload.get("record_type")
            if schema_version != SUPPORTED_SCHEMA_VERSION or not isinstance(record_type, str):
                raise ImportContractError(
                    f"{relative}: unsupported or incomplete manifest contract"
                )
            job = payload.get("job")
            fixture_id = None
            if isinstance(job, dict) and job.get("fixture_id") is not None:
                fixture_id = str(job["fixture_id"])
            connection.execute(
                """
                INSERT INTO football.collection_manifests (
                    source_file, sha256, size_bytes, schema_version, record_type,
                    run_id, status, fixture_id, started_at, finished_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    relative,
                    digest,
                    len(raw),
                    schema_version,
                    record_type,
                    payload.get("run_id"),
                    payload.get("status"),
                    fixture_id,
                    parse_time(payload.get("started_at")),
                    parse_time(payload.get("finished_at")),
                    Jsonb(payload),
                ),
            )
            summary.manifests_inserted += 1
    return summary


def _jsonl_sort_key(path: Path, normalized_dir: Path) -> tuple[str, int, str]:
    relative = path.relative_to(normalized_dir).as_posix()
    priority = {
        "market_snapshots.jsonl": 0,
        "snapshot_batches.jsonl": 0,
        "market_normalizations.jsonl": 1,
        "bookmaker_market_rows.jsonl": 2,
        "handicap_index_rows.jsonl": 2,
        "snapshot_eligibility_assessments.jsonl": 3,
    }.get(path.name, 1)
    return (path.parent.relative_to(normalized_dir).as_posix(), priority, relative)


def _validate_repair_run(run_dir: Path) -> None:
    manifest_path = run_dir / "manifest.json"
    complete_path = run_dir / "complete.json"
    if not manifest_path.is_file() or not complete_path.is_file():
        raise ImportContractError(f"{run_dir}: repair run is missing completion evidence")
    manifest_raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportContractError(f"{run_dir}: repair metadata is invalid") from exc
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    if complete.get("status") != "completed" or complete.get("manifest_sha256") != manifest_sha:
        raise AppendOnlyViolation(f"{run_dir}: repair completion marker is invalid")
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        raise ImportContractError(f"{run_dir}: repair manifest has no file inventory")
    for item in expected_files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ImportContractError(f"{run_dir}: invalid repair file inventory")
        path = run_dir / item["name"]
        if not path.is_file():
            raise AppendOnlyViolation(f"{run_dir}: repair file is missing: {item['name']}")
        raw = path.read_bytes()
        if len(raw) != item.get("size_bytes") or hashlib.sha256(raw).hexdigest() != item.get(
            "sha256"
        ):
            raise AppendOnlyViolation(f"{run_dir}: repair file hash mismatch: {item['name']}")


def import_jsonl_tree(connection: Connection, normalized_dir: Path) -> ImportSummary:
    run_id = uuid4().hex
    summary = ImportSummary(run_id=run_id)
    started_at = utc_now()
    connection.execute(
        "INSERT INTO football.import_runs(run_id, started_at, status) VALUES (%s, %s, 'running')",
        (run_id, started_at),
    )
    connection.commit()

    try:
        files = (
            sorted(normalized_dir.rglob("*.jsonl"), key=lambda path: _jsonl_sort_key(path, normalized_dir))
            if normalized_dir.is_dir()
            else []
        )
        validated_repairs: set[Path] = set()
        for path in files:
            try:
                repair_relative = path.relative_to(normalized_dir / "repairs")
            except ValueError:
                repair_relative = None
            if repair_relative is not None:
                run_dir = normalized_dir / "repairs" / repair_relative.parts[0]
                if run_dir not in validated_repairs:
                    _validate_repair_run(run_dir)
                    validated_repairs.add(run_dir)
            relative = path.relative_to(normalized_dir.parent).as_posix()
            stat = path.stat()
            checkpoint = connection.execute(
                "SELECT * FROM football.import_checkpoints WHERE source_file = %s",
                (relative,),
            ).fetchone()
            byte_offset = int(checkpoint["byte_offset"]) if checkpoint else 0
            line_number = int(checkpoint["line_number"]) if checkpoint else 0
            last_record_id = checkpoint["last_record_id"] if checkpoint else None
            if stat.st_size < byte_offset:
                raise AppendOnlyViolation(
                    f"{relative}: file shrank from checkpoint {byte_offset} to {stat.st_size} bytes"
                )

            summary.files_seen += 1
            file_inserted = 0
            file_existing = 0
            file_inserted_by_type: Counter[str] = Counter()
            file_unsupported_by_type: Counter[str] = Counter()
            with connection.transaction(), path.open("rb") as handle:
                actual_last = _last_record_id(handle, byte_offset)
                if actual_last != last_record_id:
                    raise AppendOnlyViolation(
                        f"{relative}: checkpoint tail changed; expected {last_record_id!r}, "
                        f"found {actual_last!r}"
                    )
                handle.seek(byte_offset)
                snapshot_size = stat.st_size
                while handle.tell() < snapshot_size:
                    line_start = handle.tell()
                    remaining = snapshot_size - handle.tell()
                    raw_line = handle.readline(remaining)
                    if not raw_line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    line_number += 1
                    summary.lines_seen += 1
                    try:
                        decoded = raw_line.decode("utf-8")
                        raw_record = json.loads(decoded)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ImportContractError(
                            f"{relative}:{line_number}: invalid UTF-8 JSON"
                        ) from exc
                    record = validate_record(
                        raw_record, source_file=relative, source_line=line_number
                    )
                    inserted = insert_record(
                        connection,
                        record,
                        source_file=relative,
                        source_line=line_number,
                    )
                    last_record_id = record["record_id"]
                    if inserted:
                        file_inserted += 1
                        file_inserted_by_type[record["record_type"]] += 1
                        if record["record_type"] not in SUPPORTED_RECORD_TYPES:
                            file_unsupported_by_type[record["record_type"]] += 1
                    else:
                        file_existing += 1
                new_offset = handle.tell()
                connection.execute(
                    """
                    INSERT INTO football.import_checkpoints (
                        source_file, byte_offset, line_number, file_size,
                        file_mtime_ns, last_record_id, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())
                    ON CONFLICT (source_file) DO UPDATE SET
                        byte_offset = EXCLUDED.byte_offset,
                        line_number = EXCLUDED.line_number,
                        file_size = EXCLUDED.file_size,
                        file_mtime_ns = EXCLUDED.file_mtime_ns,
                        last_record_id = EXCLUDED.last_record_id,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        relative,
                        new_offset,
                        line_number,
                        snapshot_size,
                        stat.st_mtime_ns,
                        last_record_id,
                    ),
                )
            summary.records_inserted += file_inserted
            summary.records_existing += file_existing
            summary.inserted_by_type.update(file_inserted_by_type)
            summary.unsupported_by_type.update(file_unsupported_by_type)

        summary.status = "success"
        connection.execute(
            """
            UPDATE football.import_runs SET
                finished_at = %s,
                status = 'success',
                files_seen = %s,
                lines_seen = %s,
                records_inserted = %s,
                records_existing = %s,
                inserted_by_type = %s
            WHERE run_id = %s
            """,
            (
                utc_now(),
                summary.files_seen,
                summary.lines_seen,
                summary.records_inserted,
                summary.records_existing,
                Jsonb(dict(summary.inserted_by_type)),
                run_id,
            ),
        )
        connection.commit()
        return summary
    except Exception as exc:
        connection.rollback()
        summary.status = "failure"
        connection.execute(
            """
            UPDATE football.import_runs SET
                finished_at = %s,
                status = 'failure',
                files_seen = %s,
                lines_seen = %s,
                records_inserted = %s,
                records_existing = %s,
                inserted_by_type = %s,
                error_type = %s,
                error_message = %s
            WHERE run_id = %s
            """,
            (
                utc_now(),
                summary.files_seen,
                summary.lines_seen,
                summary.records_inserted,
                summary.records_existing,
                Jsonb(dict(summary.inserted_by_type)),
                type(exc).__name__,
                str(exc),
                run_id,
            ),
        )
        connection.commit()
        raise
