from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from football_cups.collector.storage import SingleInstanceLock
from .config import ResearchConfig


def json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent, default=str)


def stable_id(kind: str, *parts: object) -> str:
    value = "|".join([kind, *(str(part) for part in parts)])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def research_facts_lock(config: ResearchConfig, *, wait_seconds: float = 0) -> Iterator[None]:
    lock = SingleInstanceLock(config.lock_path)
    if not lock.acquire(wait_seconds=wait_seconds, poll_seconds=5):
        raise TimeoutError(f"research facts lock is busy: {config.lock_path}")
    try:
        yield
    finally:
        lock.release()


class ResearchStore:
    def __init__(self, config: ResearchConfig) -> None:
        self.config = config
        config.ensure_directories()

    @staticmethod
    def atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def store_blob(self, content: bytes, extension: str) -> tuple[str, Path]:
        digest = hashlib.sha256(content).hexdigest()
        path = self.config.research_dir / "raw" / "blobs" / digest[:2] / f"{digest}.{extension}"
        if not path.exists():
            self.atomic_write(path, content)
        return digest, path

    def write_manifest(self, run_id: str, name: str, payload: dict[str, Any]) -> Path:
        path = self.config.research_dir / "manifests" / run_id / f"{name}.json"
        if path.exists():
            raise FileExistsError(path)
        self.atomic_write(path, (json_dumps(payload, indent=2) + "\n").encode("utf-8"))
        return path

    def write_records(
        self, source_id: str, run_id: str, name: str, records: Iterable[dict[str, Any]]
    ) -> Path:
        path = self.config.normalized_dir / source_id / run_id / f"{name}.jsonl"
        content = "".join(json_dumps(record) + "\n" for record in records).encode("utf-8")
        if path.exists():
            if path.read_bytes() != content:
                raise FileExistsError(f"immutable normalized file changed: {path}")
            return path
        self.atomic_write(path, content)
        return path

    def write_report(self, category: str, name: str, payload: dict[str, Any]) -> Path:
        path = self.config.research_dir / "reports" / category / f"{name}.json"
        self.atomic_write(path, (json_dumps(payload, indent=2) + "\n").encode("utf-8"))
        return path
