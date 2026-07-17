from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from . import SCHEMA_VERSION
from .config import CollectorConfig
from .storage import SingleInstanceLock, json_dumps, make_run_id
from .timeutil import iso_utc, parse_iso, utc_now


BACKUP_DIRS = ("raw", "discovery", "manifests", "normalized", "results", "reports")


class BackupLockTimeout(RuntimeError):
    pass


class BackupConsistencyError(OSError):
    pass


@dataclass(frozen=True)
class SnapshotFile:
    source: Path
    relative_path: str
    size_bytes: int
    mtime_ns: int
    staged: bool = False
    always_copy: bool = False


@dataclass(frozen=True)
class BackupSnapshot:
    run_id: str
    started_at: datetime
    snapshot_at: datetime
    staging_root: Path
    files: tuple[SnapshotFile, ...]
    state_quick_check: str


def _same_volume(source: Path, destination: Path) -> bool:
    source_drive = source.resolve().drive.lower()
    destination_drive = destination.resolve().drive.lower()
    if source_drive or destination_drive:
        return source_drive == destination_drive
    return source.resolve().anchor == destination.resolve().anchor


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


def _iter_source_files(config: CollectorConfig) -> list[Path]:
    files: list[Path] = []
    for name in BACKUP_DIRS:
        root = config.data_dir / name
        if root.exists():
            files.extend(path for path in sorted(root.rglob("*")) if path.is_file())
    return files


def _is_mutable_normalized(path: Path, config: CollectorConfig, at: datetime) -> bool:
    relative = path.relative_to(config.data_dir)
    current = Path("normalized") / at.strftime("%Y") / at.strftime("%m") / at.strftime("%d")
    return relative == current or current in relative.parents


def _cleanup_stale_staging(parent: Path, now: datetime) -> None:
    if not parent.is_dir():
        return
    cutoff = now.timestamp() - timedelta(hours=24).total_seconds()
    for child in parent.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
        except OSError:
            continue


def _create_snapshot(config: CollectorConfig, *, now: datetime | None = None) -> BackupSnapshot:
    started_at = now or utc_now()
    run_id = make_run_id(started_at)
    staging_parent = config.data_dir / "state" / "backup-staging"
    _cleanup_stale_staging(staging_parent, started_at)
    staging_root = staging_parent / run_id
    staging_root.mkdir(parents=True, exist_ok=False)
    lock = SingleInstanceLock(config.lock_path)
    if not lock.acquire(
        wait_seconds=config.backup_lock_wait_seconds,
        poll_seconds=config.backup_lock_poll_seconds,
    ):
        shutil.rmtree(staging_root, ignore_errors=True)
        raise BackupLockTimeout(
            f"collector lock was not available within {config.backup_lock_wait_seconds} seconds"
        )

    files: list[SnapshotFile] = []
    try:
        snapshot_reference = now or utc_now()
        for source in _iter_source_files(config):
            relative = source.relative_to(config.data_dir).as_posix()
            staged = _is_mutable_normalized(source, config, snapshot_reference)
            selected = source
            if staged:
                selected = staging_root / "files" / Path(*PurePosixPath(relative).parts)
                selected.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, selected)
            stat = selected.stat()
            files.append(
                SnapshotFile(
                    source=selected,
                    relative_path=relative,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    staged=staged,
                )
            )

        if not config.state_path.is_file():
            raise FileNotFoundError(f"collector state database is missing: {config.state_path}")
        state_snapshot = staging_root / "files" / "state" / "collector.sqlite3"
        state_snapshot.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(config.state_path)) as source_db, closing(
            sqlite3.connect(state_snapshot)
        ) as destination_db:
            source_db.backup(destination_db)
            quick_check = str(destination_db.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise BackupConsistencyError(f"SQLite snapshot quick_check returned {quick_check}")
        state_stat = state_snapshot.stat()
        files.append(
            SnapshotFile(
                source=state_snapshot,
                relative_path="state/collector.sqlite3",
                size_bytes=state_stat.st_size,
                mtime_ns=state_stat.st_mtime_ns,
                staged=True,
                always_copy=True,
            )
        )
        return BackupSnapshot(
            run_id=run_id,
            started_at=started_at,
            snapshot_at=now or utc_now(),
            staging_root=staging_root,
            files=tuple(files),
            state_quick_check=quick_check,
        )
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        lock.release()


def _validate_direct_source(item: SnapshotFile) -> None:
    if item.staged:
        return
    try:
        stat = item.source.stat()
    except OSError as exc:
        raise BackupConsistencyError(
            f"snapshot source disappeared: {item.relative_path}"
        ) from exc
    if stat.st_size != item.size_bytes or stat.st_mtime_ns != item.mtime_ns:
        raise BackupConsistencyError(f"snapshot source changed: {item.relative_path}")


def _copy_snapshot_file(item: SnapshotFile, target: Path) -> bool:
    _validate_direct_source(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not item.always_copy and target.exists() and target.stat().st_size == item.size_bytes:
        _validate_direct_source(item)
        return False
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(item.source, temp_name)
        _validate_direct_source(item)
        if Path(temp_name).stat().st_size != item.size_bytes:
            raise BackupConsistencyError(f"backup target size mismatch: {item.relative_path}")
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return True


def _store_object(item: SnapshotFile, objects_root: Path, digest: str) -> bool:
    _validate_direct_source(item)
    target = objects_root / "sha256" / digest[:2] / digest
    if target.exists():
        if _sha256_file(target) != digest:
            raise BackupConsistencyError(f"existing backup object is corrupt: {digest}")
        _validate_direct_source(item)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle, item.source.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_direct_source(item)
        if _sha256_file(Path(temp_name)) != digest:
            raise BackupConsistencyError(f"backup object digest mismatch: {item.relative_path}")
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
    snapshot = _create_snapshot(config, now=now)
    try:
        target_root = config.backup_dir / "data-500"
        target_root.mkdir(parents=True, exist_ok=True)
        copied = 0
        skipped = 0
        for item in snapshot.files:
            target = target_root / Path(*PurePosixPath(item.relative_path).parts)
            if _copy_snapshot_file(item, target):
                copied += 1
            else:
                skipped += 1

        completed_at = now or utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "BackupManifest",
            "run_id": snapshot.run_id,
            "backup_kind": "incremental",
            "snapshot_at": iso_utc(snapshot.snapshot_at),
            "started_at": iso_utc(snapshot.started_at),
            "completed_at": iso_utc(completed_at),
            "status": "completed",
            "source_file_count": len(snapshot.files),
            "files_copied": copied,
            "files_skipped": skipped,
            "state_quick_check": snapshot.state_quick_check,
            "integrity_level": "operational_mirror",
            "source": str(config.data_dir),
            "destination": str(target_root),
            "state_backup": str(target_root / "state" / "collector.sqlite3"),
        }
        manifest_path = config.backup_dir / "manifests" / f"{snapshot.run_id}.json"
        _atomic_write(manifest_path, (json_dumps(manifest, indent=2) + "\n").encode("utf-8"))
        return manifest
    finally:
        shutil.rmtree(snapshot.staging_root, ignore_errors=True)


def run_oss_backup(config: CollectorConfig, *, now: datetime | None = None) -> dict[str, Any]:
    if config.oss_backup_dir is None:
        raise ValueError("FOOTBALL_CUPS_OSS_BACKUP_DIR is not configured")
    snapshot = _create_snapshot(config, now=now)
    try:
        root = config.oss_backup_dir
        objects_root = root / "objects"
        run_root = root / "runs" / snapshot.run_id
        entries: list[dict[str, Any]] = []
        objects_written = 0
        objects_reused = 0
        for item in snapshot.files:
            _validate_direct_source(item)
            digest = _sha256_file(item.source)
            _validate_direct_source(item)
            if _store_object(item, objects_root, digest):
                objects_written += 1
            else:
                objects_reused += 1
            entries.append(
                {
                    "path": item.relative_path,
                    "sha256": digest,
                    "size_bytes": item.size_bytes,
                }
            )

        completed_at = now or utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "OssBackupManifest",
            "run_id": snapshot.run_id,
            "backup_kind": "content_addressed",
            "snapshot_at": iso_utc(snapshot.snapshot_at),
            "started_at": iso_utc(snapshot.started_at),
            "completed_at": iso_utc(completed_at),
            "status": "completed",
            "source_file_count": len(snapshot.files),
            "files_copied": objects_written,
            "files_skipped": objects_reused,
            "state_quick_check": snapshot.state_quick_check,
            "integrity_level": "sha256_verified",
            "source": str(config.data_dir),
            "destination": str(root),
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
            "run_id": snapshot.run_id,
            "status": "completed",
            "completed_at": iso_utc(completed_at),
            "manifest_sha256": manifest_digest,
            "file_count": len(entries),
        }
        _atomic_write(
            run_root / "complete.json",
            (json_dumps(complete, indent=2) + "\n").encode("utf-8"),
        )
        return {
            "status": "completed",
            "run_id": snapshot.run_id,
            "root": str(root),
            "file_count": len(entries),
            "objects_written": objects_written,
            "objects_reused": objects_reused,
            "manifest_sha256": manifest_digest,
            "completed_at": iso_utc(completed_at),
        }
    finally:
        shutil.rmtree(snapshot.staging_root, ignore_errors=True)


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
    if manifest.get("status") != "completed" or complete.get("status") != "completed":
        raise ValueError("backup run is not completed")
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


def _latest_incremental(config: CollectorConfig) -> tuple[datetime | None, str | None]:
    if config.backup_dir is None:
        return None, None
    manifest_root = config.backup_dir / "manifests"
    manifests = sorted(manifest_root.glob("*.json")) if manifest_root.is_dir() else []
    if not manifests:
        return None, None
    path = manifests[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "completed" or payload.get("state_quick_check") != "ok":
            return None, "latest incremental backup manifest is not completed"
        state_path = Path(str(payload["state_backup"]))
        if not state_path.is_file():
            return None, "latest incremental backup state database is missing"
        with closing(sqlite3.connect(f"file:{state_path.as_posix()}?mode=ro", uri=True)) as db:
            if str(db.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                return None, "latest incremental backup state database failed quick_check"
        return parse_iso(str(payload["completed_at"])), None
    except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
        return None, f"latest incremental backup manifest is invalid: {exc}"


def _latest_oss(config: CollectorConfig) -> tuple[datetime | None, str | None]:
    if config.oss_backup_dir is None:
        return None, None
    runs_root = config.oss_backup_dir / "runs"
    runs = sorted(path for path in runs_root.iterdir() if path.is_dir()) if runs_root.is_dir() else []
    if not runs:
        return None, None
    run_root = runs[-1]
    try:
        complete = json.loads((run_root / "complete.json").read_text(encoding="utf-8"))
        manifest_bytes = (run_root / "manifest.json").read_bytes()
        if complete.get("status") != "completed":
            return None, "latest content-addressed backup is not completed"
        if complete.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
            return None, "latest content-addressed backup manifest digest does not match"
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("status") != "completed":
            return None, "latest content-addressed backup manifest is not completed"
        return parse_iso(str(complete["completed_at"])), None
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return None, f"latest content-addressed backup is invalid: {exc}"


def backup_health(
    config: CollectorConfig, *, now: datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    checked_at = now or utc_now()
    issues: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "backup_status": "unconfigured",
        "latest_backup_at": None,
        "backup_age_hours": None,
        "oss_backup_status": "unconfigured",
        "latest_oss_backup_at": None,
        "oss_backup_age_days": None,
        "backup_drive_free_bytes": None,
    }

    configured_roots = [path for path in (config.backup_dir, config.oss_backup_dir) if path]
    for root in configured_roots:
        if not root.is_dir():
            issues.append(
                {
                    "code": "backup_directory_unavailable",
                    "severity": "failed",
                    "message": f"configured backup directory is unavailable: {root}",
                }
            )
            if root == config.backup_dir:
                result["backup_status"] = "failed"
            if root == config.oss_backup_dir:
                result["oss_backup_status"] = "failed"
    if configured_roots and all(path.is_dir() for path in configured_roots):
        result["backup_drive_free_bytes"] = min(
            shutil.disk_usage(path).free for path in configured_roots
        )

    if config.backup_dir is not None and config.backup_dir.is_dir():
        latest, error = _latest_incremental(config)
        if error:
            result["backup_status"] = "failed"
            issues.append({"code": "backup_invalid", "severity": "failed", "message": error})
        elif latest is None:
            result["backup_status"] = "warning"
            issues.append(
                {
                    "code": "backup_missing",
                    "severity": "warning",
                    "message": "no completed incremental backup exists",
                }
            )
        else:
            age_hours = (checked_at - latest).total_seconds() / 3600
            result["latest_backup_at"] = iso_utc(latest)
            result["backup_age_hours"] = round(age_hours, 3)
            if age_hours > config.backup_failed_max_age_hours:
                result["backup_status"] = "failed"
                issues.append(
                    {"code": "backup_stale", "severity": "failed", "message": "incremental backup is stale"}
                )
            elif age_hours > config.backup_warning_max_age_hours:
                result["backup_status"] = "warning"
                issues.append(
                    {"code": "backup_stale", "severity": "warning", "message": "incremental backup is stale"}
                )
            else:
                result["backup_status"] = "ok"

    if config.oss_backup_dir is not None and config.oss_backup_dir.is_dir():
        latest, error = _latest_oss(config)
        if error:
            result["oss_backup_status"] = "failed"
            issues.append({"code": "oss_backup_invalid", "severity": "failed", "message": error})
        elif latest is None:
            result["oss_backup_status"] = "warning"
            issues.append(
                {
                    "code": "oss_backup_missing",
                    "severity": "warning",
                    "message": "no completed content-addressed backup exists",
                }
            )
        else:
            age_days = (checked_at - latest).total_seconds() / 86400
            result["latest_oss_backup_at"] = iso_utc(latest)
            result["oss_backup_age_days"] = round(age_days, 3)
            if age_days > config.oss_backup_failed_max_age_days:
                result["oss_backup_status"] = "failed"
                issues.append(
                    {"code": "oss_backup_stale", "severity": "failed", "message": "content-addressed backup is stale"}
                )
            elif age_days > config.oss_backup_warning_max_age_days:
                result["oss_backup_status"] = "warning"
                issues.append(
                    {"code": "oss_backup_stale", "severity": "warning", "message": "content-addressed backup is stale"}
                )
            else:
                result["oss_backup_status"] = "ok"
    return result, issues
