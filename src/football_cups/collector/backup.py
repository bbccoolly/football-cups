from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath
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


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_data_files(config: CollectorConfig) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for name in BACKUP_DIRS:
        root = config.data_dir / name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append((path, path.relative_to(config.data_dir).as_posix()))
    return files


def _store_object(source: Path, objects_root: Path, digest: str) -> bool:
    target = objects_root / "sha256" / digest[:2] / digest
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return True


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
    with closing(sqlite3.connect(config.state_path)) as source_db, closing(
        sqlite3.connect(state_target)
    ) as destination_db:
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


def run_oss_backup(config: CollectorConfig, *, now: datetime | None = None) -> dict[str, Any]:
    if config.oss_backup_dir is None:
        raise ValueError("FOOTBALL_CUPS_OSS_BACKUP_DIR is not configured")
    at = now or utc_now()
    run_id = make_run_id(at)
    root = config.oss_backup_dir
    objects_root = root / "objects"
    run_root = root / "runs" / run_id
    entries: list[dict[str, Any]] = []
    objects_written = 0
    objects_reused = 0

    with tempfile.TemporaryDirectory(prefix="football-cups-oss-backup-") as temp_dir:
        temp_root = Path(temp_dir)
        files = _iter_data_files(config)
        if config.state_path.exists():
            state_snapshot = temp_root / "collector.sqlite3"
            with closing(sqlite3.connect(config.state_path)) as source_db, closing(
                sqlite3.connect(state_snapshot)
            ) as destination_db:
                source_db.backup(destination_db)
            files.append((state_snapshot, "state/collector.sqlite3"))

        for source, relative_path in files:
            digest = _sha256_file(source)
            if _store_object(source, objects_root, digest):
                objects_written += 1
            else:
                objects_reused += 1
            entries.append(
                {
                    "path": relative_path,
                    "sha256": digest,
                    "size_bytes": source.stat().st_size,
                }
            )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "OssBackupManifest",
        "run_id": run_id,
        "created_at": iso_utc(at),
        "source": str(config.data_dir),
        "object_layout": "objects/sha256/<first-two>/<sha256>",
        "entries": entries,
        "objects_written": objects_written,
        "objects_reused": objects_reused,
    }
    manifest_bytes = (json_dumps(manifest, indent=2) + "\n").encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    _atomic_write(run_root / "manifest.json", manifest_bytes)
    complete = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "OssBackupComplete",
        "run_id": run_id,
        "completed_at": iso_utc(utc_now()),
        "manifest_sha256": manifest_digest,
        "file_count": len(entries),
    }
    _atomic_write(run_root / "complete.json", (json_dumps(complete, indent=2) + "\n").encode("utf-8"))
    return {
        "status": "completed",
        "run_id": run_id,
        "root": str(root),
        "file_count": len(entries),
        "objects_written": objects_written,
        "objects_reused": objects_reused,
        "manifest_sha256": manifest_digest,
    }


def verify_oss_backup(config: CollectorConfig, *, run_id: str, target: Path) -> dict[str, Any]:
    if config.oss_backup_dir is None:
        raise ValueError("FOOTBALL_CUPS_OSS_BACKUP_DIR is not configured")
    if target.exists() and any(target.iterdir()):
        raise ValueError("restore target must be empty")
    root = config.oss_backup_dir
    run_root = root / "runs" / run_id
    complete_path = run_root / "complete.json"
    manifest_path = run_root / "manifest.json"
    if not complete_path.is_file():
        raise ValueError(f"backup run is incomplete: {run_id}")
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    manifest_bytes = manifest_path.read_bytes()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if complete.get("manifest_sha256") != manifest_digest:
        raise ValueError("backup manifest digest does not match complete marker")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    restored = 0
    for entry in manifest.get("entries", []):
        digest = str(entry["sha256"])
        object_path = root / "objects" / "sha256" / digest[:2] / digest
        if not object_path.is_file():
            raise ValueError(f"missing backup object: {digest}")
        if _sha256_file(object_path) != digest:
            raise ValueError(f"backup object digest mismatch: {digest}")
        relative = PurePosixPath(str(entry["path"]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe restore path: {entry['path']}")
        destination = target / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(object_path, destination)
        if _sha256_file(destination) != digest:
            raise ValueError(f"restored file digest mismatch: {entry['path']}")
        restored += 1
    return {
        "status": "verified",
        "run_id": run_id,
        "target": str(target),
        "file_count": restored,
    }
