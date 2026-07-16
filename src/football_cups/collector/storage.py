from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import SCHEMA_VERSION
from .config import CollectorConfig
from .http import ObservedResponse
from .timeutil import iso_utc, utc_now

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is declared, fallback keeps CLI importable.
    psutil = None


def make_run_id(now: datetime | None = None) -> str:
    value = now or utc_now()
    return value.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]


def stable_record_id(kind: str, *parts: object) -> str:
    payload = "|".join([kind, *(str(part) for part in parts)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent, default=str)


def _safe_extension(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.lower())
    return cleaned[:10] or "bin"


def extension_for_response(response: ObservedResponse, default: str = "bin") -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return "html"
    if "json" in content_type:
        return "json"
    if "excel" in content_type or "spreadsheet" in content_type:
        return "xls"
    return _safe_extension(default)


@dataclass
class SingleInstanceLock:
    path: Path
    stale_after: timedelta = timedelta(minutes=30)
    acquired: bool = False

    def _current_payload(self, now: datetime) -> dict[str, Any]:
        create_time: float | None = None
        boot_time: float | None = None
        if psutil is not None:
            process = psutil.Process(os.getpid())
            create_time = process.create_time()
            boot_time = psutil.boot_time()
        return {
            "hostname": socket.gethostname(),
            "boot_time": boot_time,
            "pid": os.getpid(),
            "process_create_time": create_time,
            "acquired_at": iso_utc(now),
        }

    def _active_owner_exists(self, payload: dict[str, Any]) -> bool:
        if psutil is None or payload.get("hostname") != socket.gethostname():
            return False
        boot_time = payload.get("boot_time")
        if boot_time is not None and abs(float(boot_time) - psutil.boot_time()) > 1:
            return False
        pid = payload.get("pid")
        create_time = payload.get("process_create_time")
        if not isinstance(pid, int) or not psutil.pid_exists(pid):
            return False
        if create_time is None:
            return True
        try:
            return abs(psutil.Process(pid).create_time() - float(create_time)) < 0.01
        except (psutil.Error, ValueError, TypeError):
            return False

    def _existing_lock_is_protected(self, now: datetime) -> bool:
        try:
            raw_payload = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw_payload)
        except (OSError, json.JSONDecodeError):
            payload = {}
        if self._active_owner_exists(payload):
            return True
        try:
            age = now - datetime.fromtimestamp(self.path.stat().st_mtime, tz=now.tzinfo)
        except OSError:
            return True
        return age <= self.stale_after

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        payload = self._current_payload(now)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            if self._existing_lock_is_protected(now):
                return False
            try:
                self.path.unlink()
            except OSError:
                return False
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json_dumps(payload))
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class DataStore:
    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.config.ensure_directories()

    def _atomic_write(self, path: Path, content: bytes) -> None:
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

    def store_response(self, response: ObservedResponse, *, default_extension: str) -> dict[str, Any]:
        digest = hashlib.sha256(response.content).hexdigest()
        extension = extension_for_response(response, default_extension)
        path = self.config.data_dir / "raw" / "blobs" / digest[:2] / f"{digest}.{extension}"
        if not path.exists():
            self._atomic_write(path, response.content)
        return {
            "schema_version": SCHEMA_VERSION,
            "record_type": "RawBlob",
            "record_id": stable_record_id("raw_blob_observation", response.url, iso_utc(response.response_received_at), digest),
            "method": response.method,
            "url": response.url,
            "http_status": response.status_code,
            "headers": response.headers,
            "request_started_at": iso_utc(response.request_started_at),
            "observed_at": iso_utc(response.response_received_at),
            "source_encoding": response.source_encoding,
            "sha256": digest,
            "size_bytes": len(response.content),
            "path": path.relative_to(self.config.data_dir).as_posix(),
        }

    def write_manifest(self, category: str, run_id: str, payload: dict[str, Any], at: datetime) -> Path:
        path = (
            self.config.data_dir
            / "manifests"
            / at.strftime("%Y")
            / at.strftime("%m")
            / at.strftime("%d")
            / f"{run_id}-{category}.json"
        )
        if path.exists():
            raise FileExistsError(f"manifest already exists: {path}")
        self._atomic_write(path, (json_dumps(payload, indent=2) + "\n").encode("utf-8"))
        return path

    def write_discovery_summary(self, run_id: str, payload: dict[str, Any], at: datetime) -> Path:
        path = (
            self.config.data_dir
            / "discovery"
            / at.strftime("%Y")
            / at.strftime("%m")
            / at.strftime("%d")
            / f"{run_id}.json"
        )
        self._atomic_write(path, (json_dumps(payload, indent=2) + "\n").encode("utf-8"))
        return path

    def append_normalized(self, stream: str, record: dict[str, Any], at: datetime) -> Path:
        path = (
            self.config.data_dir
            / "normalized"
            / at.strftime("%Y")
            / at.strftime("%m")
            / at.strftime("%d")
            / f"{stream}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json_dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def write_result(self, kind: str, record: dict[str, Any], at: datetime) -> Path:
        path = (
            self.config.data_dir
            / "results"
            / at.strftime("%Y")
            / at.strftime("%m")
            / kind
            / f"{record['record_id']}.json"
        )
        if not path.exists():
            self._atomic_write(path, (json_dumps(record, indent=2) + "\n").encode("utf-8"))
        return path
