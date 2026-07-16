from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .config import CollectorConfig
from .storage import DataStore, json_dumps, make_run_id
from .timeutil import iso_utc, utc_now


BACKUP_DIRS = ("raw", "discovery", "manifests", "normalized", "results", "reports")


def _same_volume(source: Path, destination: Path) -> bool:
    source_drive = source.resolve().drive.lower()
    destination_drive = destination.resolve().drive.lower()
    if source_drive or destination_drive:
        return source_drive == destination_drive
    return source.resolve().anchor == destination.resolve().anchor


def _copy_incremental(source: Path, destination: Path) -> tuple[int, int]:
    copied = 0
    skipped = 0
    if not source.exists():
        return copied, skipped
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == path.stat().st_size:
            skipped += 1
            continue
        shutil.copy2(path, target)
        copied += 1
    return copied, skipped


def run_backup(
    config: CollectorConfig,
    *,
    require_distinct_volume: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    if config.backup_dir is None:
        raise ValueError("FOOTBALL_CUPS_BACKUP_DIR is not configured")
    if require_distinct_volume and _same_volume(config.data_dir, config.backup_dir):
        raise ValueError("backup directory must be on another volume")
    at = now or utc_now()
    target_root = config.backup_dir / "data-500"
    target_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for name in BACKUP_DIRS:
        copy_count, skip_count = _copy_incremental(config.data_dir / name, target_root / name)
        copied += copy_count
        skipped += skip_count

    state_target = target_root / "state" / "collector.sqlite3"
    state_target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.state_path) as source_db, sqlite3.connect(state_target) as destination_db:
        source_db.backup(destination_db)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "BackupManifest",
        "run_id": make_run_id(at),
        "created_at": iso_utc(at),
        "source": str(config.data_dir),
        "destination": str(target_root),
        "files_copied": copied,
        "files_skipped": skipped,
        "state_backup": str(state_target),
    }
    manifest_path = config.backup_dir / "manifests" / f"{manifest['run_id']}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json_dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest

