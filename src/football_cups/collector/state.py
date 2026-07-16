from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import CUTOFFS, CollectorConfig
from .storage import json_dumps, stable_record_id
from .timeutil import iso_utc, parse_iso, utc_now


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    kickoff_at TEXT,
    buy_end_at TEXT,
    competition TEXT,
    competition_id TEXT,
    competition_format TEXT NOT NULL DEFAULT 'unknown',
    identity_conflict INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    fixture_id TEXT,
    target TEXT,
    priority INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, due_at, priority);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    fixture_id TEXT,
    competition TEXT,
    market TEXT,
    cutoff TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at, event_type);
CREATE TABLE IF NOT EXISTS record_ids (
    record_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, config: CollectorConfig, path: Path | None = None) -> None:
        self.config = config
        self.path = path or config.state_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA_SQL)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def discovery_due(self, now: datetime) -> bool:
        last = self.get_meta("last_full_discovery_at")
        if not last:
            return True
        return now - parse_iso(last) >= timedelta(minutes=self.config.discovery_interval_minutes)

    def start_run(self, run_id: str, run_type: str, started_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO runs(run_id, run_type, started_at, status, details_json) VALUES(?, ?, ?, ?, ?)",
            (run_id, run_type, iso_utc(started_at), "running", "{}"),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, details: dict[str, Any], finished_at: datetime) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at=?, status=?, details_json=? WHERE run_id=?",
            (iso_utc(finished_at), status, json_dumps(details), run_id),
        )
        self.connection.commit()

    def add_event(
        self,
        event_type: str,
        status: str,
        details: dict[str, Any],
        *,
        occurred_at: datetime | None = None,
        fixture_id: str | None = None,
        competition: str | None = None,
        market: str | None = None,
        cutoff: str | None = None,
    ) -> str:
        at = occurred_at or utc_now()
        event_id = stable_record_id(
            "event", event_type, status, fixture_id or "", iso_utc(at), json_dumps(details)
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event_type,
                iso_utc(at),
                fixture_id,
                competition,
                market,
                cutoff,
                status,
                json_dumps(details),
            ),
        )
        self.connection.commit()
        return event_id

    def claim_record(self, record_id: str, record_type: str, at: datetime) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO record_ids(record_id, record_type, created_at) VALUES(?, ?, ?)",
            (record_id, record_type, iso_utc(at)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def upsert_fixture(
        self,
        identity: dict[str, Any],
        observed_at: datetime,
        *,
        identity_conflict: bool,
    ) -> str:
        fixture_id = str(identity["fixture_id"])
        current = self.connection.execute(
            "SELECT * FROM fixtures WHERE fixture_id=?", (fixture_id,)
        ).fetchone()
        kickoff = identity.get("kickoff_at")
        buy_end = identity.get("buy_end_at")
        if current is None:
            self.connection.execute(
                "INSERT INTO fixtures VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)",
                (
                    fixture_id,
                    json_dumps(identity),
                    iso_utc(observed_at),
                    iso_utc(observed_at),
                    kickoff,
                    buy_end,
                    identity.get("competition_name"),
                    identity.get("competition_id"),
                    int(identity_conflict),
                ),
            )
            self.connection.commit()
            return "new"

        old_identity = json.loads(current["identity_json"])
        old_kickoff = current["kickoff_at"]
        status = "unchanged"
        if old_kickoff and kickoff and old_kickoff != kickoff:
            status = "kickoff_changed"
            self.connection.execute(
                "UPDATE jobs SET status='superseded', updated_at=? "
                "WHERE fixture_id=? AND status='pending'",
                (iso_utc(observed_at), fixture_id),
            )
        identity_keys = ("home_team_id", "away_team_id")
        hard_conflict = identity_conflict or any(
            old_identity.get(key)
            and identity.get(key)
            and str(old_identity.get(key)) != str(identity.get(key))
            for key in identity_keys
        )
        if hard_conflict:
            status = "identity_conflict"
        elif status == "unchanged" and old_identity != identity:
            status = "metadata_changed"
        self.connection.execute(
            "UPDATE fixtures SET identity_json=?, last_seen_at=?, kickoff_at=?, buy_end_at=?, "
            "competition=?, competition_id=?, identity_conflict=? WHERE fixture_id=?",
            (
                json_dumps(identity),
                iso_utc(observed_at),
                kickoff,
                buy_end,
                identity.get("competition_name"),
                identity.get("competition_id"),
                int(bool(current["identity_conflict"]) or hard_conflict),
                fixture_id,
            ),
        )
        if status == "metadata_changed":
            pending_rows = self.connection.execute(
                "SELECT job_id, payload_json FROM jobs WHERE fixture_id=? AND status='pending'",
                (fixture_id,),
            ).fetchall()
            for row in pending_rows:
                payload = json.loads(row["payload_json"])
                payload["fixture"] = identity
                payload["kickoff_at"] = kickoff
                self.connection.execute(
                    "UPDATE jobs SET payload_json=?, updated_at=? WHERE job_id=?",
                    (json_dumps(payload), iso_utc(observed_at), row["job_id"]),
                )
        self.connection.commit()
        return status

    def schedule_fixture(self, identity: dict[str, Any], observed_at: datetime, *, is_new: bool) -> None:
        kickoff_text = identity.get("kickoff_at")
        if not kickoff_text:
            return
        fixture_id = str(identity["fixture_id"])
        kickoff = parse_iso(kickoff_text)
        version = kickoff.strftime("%Y%m%dT%H%M%SZ")
        if is_new and kickoff > observed_at:
            self._insert_job(
                job_id=f"market:{fixture_id}:{version}:first_seen",
                job_type="market",
                fixture_id=fixture_id,
                target="first_seen",
                priority=20,
                due_at=observed_at,
                window_start=None,
                window_end=None,
                status="pending",
                payload={"fixture": identity, "kickoff_at": kickoff_text},
                now=observed_at,
            )
        for target, (minutes_before, freshness_minutes) in CUTOFFS.items():
            cutoff = kickoff - timedelta(minutes=minutes_before)
            window_start = cutoff - timedelta(minutes=freshness_minutes)
            status = "missed_before_discovery" if observed_at > cutoff else "pending"
            self._insert_job(
                job_id=f"market:{fixture_id}:{version}:{target}",
                job_type="market",
                fixture_id=fixture_id,
                target=target,
                priority=10,
                due_at=window_start,
                window_start=window_start,
                window_end=cutoff,
                status=status,
                payload={"fixture": identity, "kickoff_at": kickoff_text},
                now=observed_at,
            )
        for hours in (3, 6, 24):
            self._insert_job(
                job_id=f"result:{fixture_id}:{version}:T+{hours}h",
                job_type="result",
                fixture_id=fixture_id,
                target=f"T+{hours}h",
                priority=40,
                due_at=kickoff + timedelta(hours=hours),
                window_start=None,
                window_end=None,
                status="pending",
                payload={"fixture": identity, "kickoff_at": kickoff_text},
                now=observed_at,
            )
        self.connection.commit()

    def _insert_job(
        self,
        *,
        job_id: str,
        job_type: str,
        fixture_id: str,
        target: str,
        priority: int,
        due_at: datetime,
        window_start: datetime | None,
        window_end: datetime | None,
        status: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO jobs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)",
            (
                job_id,
                job_type,
                fixture_id,
                target,
                priority,
                iso_utc(due_at),
                iso_utc(window_start) if window_start else None,
                iso_utc(window_end) if window_end else None,
                status,
                json_dumps(payload),
                iso_utc(now),
                iso_utc(now),
            ),
        )

    def due_jobs(self, now: datetime, *, job_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.connection.execute(
            "UPDATE jobs SET status='late_for_cutoff', updated_at=? "
            "WHERE status='pending' AND job_type='market' AND window_end IS NOT NULL AND window_end < ?",
            (iso_utc(now), iso_utc(now)),
        )
        params: list[Any] = [iso_utc(now)]
        type_clause = ""
        if job_type:
            type_clause = " AND job_type=?"
            params.append(job_type)
        params.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status='pending' AND due_at<=?"
            + type_clause
            + " ORDER BY priority, COALESCE(window_end, due_at), due_at LIMIT ?",
            params,
        ).fetchall()
        self.connection.commit()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def complete_job(self, job_id: str, status: str, now: datetime, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, last_error=?, updated_at=? WHERE job_id=?",
            (status, error, iso_utc(now), job_id),
        )
        self.connection.commit()

    def retry_job(self, job_id: str, now: datetime, error: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET attempts=attempts+1, last_error=?, due_at=?, updated_at=? WHERE job_id=?",
            (error, iso_utc(now + timedelta(minutes=2)), iso_utc(now), job_id),
        )
        self.connection.commit()

    def update_job_payload(self, job_id: str, payload: dict[str, Any], now: datetime) -> None:
        self.connection.execute(
            "UPDATE jobs SET payload_json=?, updated_at=? WHERE job_id=?",
            (json_dumps(payload), iso_utc(now), job_id),
        )
        self.connection.commit()

    def fixtures_by_ids(self, fixture_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in fixture_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM fixtures WHERE fixture_id IN ({placeholders})", ids
        ).fetchall()
        return {
            str(row["fixture_id"]): dict(row) | {"identity": json.loads(row["identity_json"])}
            for row in rows
        }

    def all_fixtures(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM fixtures ORDER BY first_seen_at").fetchall()
        return [dict(row) | {"identity": json.loads(row["identity_json"])} for row in rows]

    def events_for_day(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at",
            (iso_utc(start), iso_utc(end)),
        ).fetchall()
        return [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]

    def runs_for_day(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs WHERE started_at>=? AND started_at<? ORDER BY started_at",
            (iso_utc(start), iso_utc(end)),
        ).fetchall()
        return [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]
