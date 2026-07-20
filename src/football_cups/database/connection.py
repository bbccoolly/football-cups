from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .config import DatabaseConfig


BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS football;
CREATE TABLE IF NOT EXISTS football.schema_migrations (
    version text PRIMARY KEY,
    name text NOT NULL,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


def connect(config: DatabaseConfig, *, autocommit: bool = False) -> Connection:
    kwargs = {"autocommit": autocommit, "row_factory": dict_row, "connect_timeout": 5}
    if config.database_url:
        return psycopg.connect(config.database_url, **kwargs)
    if not os.environ.get("PGHOST") and config.local_postgres_available:
        return psycopg.connect(
            host="127.0.0.1",
            port=55432,
            dbname="football_cups",
            user="football_cups",
            **kwargs,
        )
    return psycopg.connect("", **kwargs)


def migration_files() -> Iterator[Path]:
    folder = Path(__file__).with_name("migrations")
    yield from sorted(folder.glob("*.sql"))


def apply_migrations(connection: Connection, *, target_version: str | None = None) -> list[str]:
    applied_now: list[str] = []
    files = list(migration_files())
    versions = [path.stem.partition("_")[0] for path in files]
    if target_version is not None and target_version not in versions:
        raise ValueError(f"unknown migration target: {target_version}")
    with connection.transaction():
        connection.execute(BOOTSTRAP_SQL, prepare=False)
        existing = {
            row["version"]: row
            for row in connection.execute(
                "SELECT version, name, sha256 FROM football.schema_migrations"
            ).fetchall()
        }
        if target_version is not None and any(
            version > target_version for version in existing
        ):
            raise ValueError(
                f"database is already newer than requested target {target_version}"
            )
        for path in files:
            version, _, name = path.stem.partition("_")
            if target_version is not None and version > target_version:
                break
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            prior = existing.get(version)
            if prior:
                if prior["sha256"] != digest:
                    raise RuntimeError(
                        f"applied migration {version} has changed: {path.name}"
                    )
                continue
            connection.execute(sql, prepare=False)
            connection.execute(
                "INSERT INTO football.schema_migrations(version, name, sha256) "
                "VALUES (%s, %s, %s)",
                (version, name, digest),
            )
            applied_now.append(version)
    return applied_now
