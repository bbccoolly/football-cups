from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection


COUNT_TABLES = (
    "collection_manifests",
    "records",
    "fixture_identities",
    "discovery_observations",
    "sporttery_pool_observations",
    "snapshot_batches",
    "market_snapshots",
    "bookmaker_market_rows",
    "result_candidates",
    "verified_results",
    "quality_events",
)


def database_status(connection: Connection) -> dict[str, Any]:
    migrations = connection.execute(
        "SELECT version, name, sha256, applied_at "
        "FROM football.schema_migrations ORDER BY version"
    ).fetchall()
    counts: dict[str, int] = {}
    for table in COUNT_TABLES:
        row = connection.execute(f"SELECT count(*) AS count FROM football.{table}").fetchone()
        counts[table] = int(row["count"])
    record_types = {
        row["record_type"]: int(row["count"])
        for row in connection.execute(
            "SELECT record_type, count(*) AS count FROM football.records "
            "GROUP BY record_type ORDER BY record_type"
        ).fetchall()
    }
    latest_run = connection.execute(
        """
        SELECT run_id, started_at, finished_at, status, files_seen, lines_seen,
               records_inserted, records_existing, inserted_by_type,
               error_type, error_message
        FROM football.import_runs
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    checkpoint = connection.execute(
        """
        SELECT count(*) AS files,
               coalesce(sum(line_number), 0) AS lines,
               max(updated_at) AS last_updated_at
        FROM football.import_checkpoints
        """
    ).fetchone()
    unsupported = connection.execute(
        "SELECT count(*) AS count FROM football.unsupported_records"
    ).fetchone()
    return {
        "migrations": [dict(row) for row in migrations],
        "counts": counts,
        "record_types": record_types,
        "checkpoints": dict(checkpoint),
        "unsupported_records": int(unsupported["count"]),
        "latest_import_run": dict(latest_run) if latest_run else None,
    }


def market_rows_as_of(
    connection: Connection,
    *,
    fixture_id: str,
    prediction_cutoff: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM football.market_rows_as_of(%s, %s)
        LIMIT %s
        """,
        (fixture_id, prediction_cutoff, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def as_of_audit(
    connection: Connection,
    *,
    fixture_id: str,
    prediction_cutoff: datetime,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            count(*) AS returned_rows,
            max(observed_at) AS max_observed_at,
            count(*) FILTER (WHERE observed_at > %s) AS observed_after_cutoff,
            count(*) FILTER (
                WHERE corrected_at IS NOT NULL AND corrected_at > %s
            ) AS corrected_after_cutoff
        FROM football.market_rows_as_of(%s, %s)
        """,
        (prediction_cutoff, prediction_cutoff, fixture_id, prediction_cutoff),
    ).fetchone()
    return dict(row)
