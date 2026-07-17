from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ResearchState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY,
                host TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                bytes_received INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS requests_host_time_idx
                ON requests(host, requested_at);
            CREATE TABLE IF NOT EXISTS host_state (
                host TEXT PRIMARY KEY,
                last_request_at TEXT,
                circuit_until TEXT,
                circuit_reason TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                robots_body TEXT,
                robots_sha256 TEXT,
                robots_checked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS asset_cache (
                asset_id TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                sha256 TEXT,
                blob_path TEXT,
                observed_at TEXT
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def host_snapshot(self, host: str, now: datetime) -> dict[str, Any]:
        cutoff = iso_utc(now - timedelta(hours=24))
        usage = self.connection.execute(
            "SELECT count(*) AS requests, coalesce(sum(bytes_received), 0) AS bytes "
            "FROM requests WHERE host=? AND requested_at>=?",
            (host, cutoff),
        ).fetchone()
        row = self.connection.execute(
            "SELECT * FROM host_state WHERE host=?", (host,)
        ).fetchone()
        return {
            "requests": int(usage["requests"]),
            "bytes": int(usage["bytes"]),
            **(dict(row) if row else {}),
        }

    def record_request(self, host: str, at: datetime, size: int) -> None:
        value = iso_utc(at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO requests(host, requested_at, bytes_received) VALUES (?, ?, ?)",
                (host, value, size),
            )
            self.connection.execute(
                "INSERT INTO host_state(host, last_request_at) VALUES (?, ?) "
                "ON CONFLICT(host) DO UPDATE SET last_request_at=excluded.last_request_at",
                (host, value),
            )

    def record_success(self, host: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO host_state(host, consecutive_failures) VALUES (?, 0) "
                "ON CONFLICT(host) DO UPDATE SET consecutive_failures=0",
                (host,),
            )

    def record_failure(self, host: str, *, now: datetime) -> int:
        with self.connection:
            self.connection.execute(
                "INSERT INTO host_state(host, consecutive_failures) VALUES (?, 1) "
                "ON CONFLICT(host) DO UPDATE SET consecutive_failures=consecutive_failures+1",
                (host,),
            )
        row = self.connection.execute(
            "SELECT consecutive_failures FROM host_state WHERE host=?", (host,)
        ).fetchone()
        return int(row[0])

    def open_circuit(self, host: str, until: datetime, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO host_state(host, circuit_until, circuit_reason) VALUES (?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET circuit_until=excluded.circuit_until, "
                "circuit_reason=excluded.circuit_reason",
                (host, iso_utc(until), reason),
            )

    def save_robots(
        self, host: str, *, body: str, sha256: str, checked_at: datetime
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO host_state(host, robots_body, robots_sha256, robots_checked_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(host) DO UPDATE SET "
                "robots_body=excluded.robots_body, robots_sha256=excluded.robots_sha256, "
                "robots_checked_at=excluded.robots_checked_at",
                (host, body, sha256, iso_utc(checked_at)),
            )

    def asset_cache(self, asset_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM asset_cache WHERE asset_id=?", (asset_id,)
        ).fetchone()
        return dict(row) if row else None

    def all_assets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM asset_cache ORDER BY asset_id"
        ).fetchall()]

    def save_asset(
        self,
        asset_id: str,
        *,
        etag: str | None,
        last_modified: str | None,
        sha256: str,
        blob_path: str,
        observed_at: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO asset_cache(asset_id, etag, last_modified, sha256, blob_path, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
                "etag=excluded.etag, last_modified=excluded.last_modified, sha256=excluded.sha256, "
                "blob_path=excluded.blob_path, observed_at=excluded.observed_at",
                (
                    asset_id,
                    etag,
                    last_modified,
                    sha256,
                    blob_path,
                    iso_utc(observed_at),
                ),
            )
