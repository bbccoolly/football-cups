from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from football_cups.collector.config import _load_dotenv


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class ResearchConfig:
    workspace: Path
    research_dir: Path
    min_interval_seconds: float = 10.0
    requests_per_24h: int = 60
    bytes_per_24h: int = 200 * 1024 * 1024
    max_file_bytes: int = 50 * 1024 * 1024
    max_xlsx_uncompressed_bytes: int = 200 * 1024 * 1024
    request_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 10:
            raise ValueError("research request interval cannot be lower than 10 seconds")
        if not 1 <= self.requests_per_24h <= 60:
            raise ValueError("research request budget must be between 1 and 60")
        if self.bytes_per_24h <= 0 or self.bytes_per_24h > 200 * 1024 * 1024:
            raise ValueError("research byte budget cannot exceed 200 MiB per 24 hours")
        if self.max_file_bytes <= 0 or self.max_file_bytes > 50 * 1024 * 1024:
            raise ValueError("research file limit cannot exceed 50 MiB")
        if self.max_xlsx_uncompressed_bytes <= 0:
            raise ValueError("XLSX uncompressed limit must be positive")

    @classmethod
    def from_workspace(cls, workspace: Path) -> "ResearchConfig":
        workspace = workspace.resolve()
        _load_dotenv(workspace / ".env")
        configured = os.environ.get("FOOTBALL_CUPS_RESEARCH_DIR", "").strip()
        return cls(
            workspace=workspace,
            research_dir=(
                Path(configured).expanduser().resolve()
                if configured
                else workspace / "data" / "research"
            ),
            min_interval_seconds=_env_float("RESEARCH_REQUEST_MIN_INTERVAL_SECONDS", 10),
            requests_per_24h=_env_int("RESEARCH_REQUESTS_PER_24H", 60),
            bytes_per_24h=_env_int("RESEARCH_BYTES_PER_24H", 200 * 1024 * 1024),
            max_file_bytes=_env_int("RESEARCH_MAX_FILE_BYTES", 50 * 1024 * 1024),
            max_xlsx_uncompressed_bytes=_env_int(
                "RESEARCH_MAX_XLSX_UNCOMPRESSED_BYTES", 200 * 1024 * 1024
            ),
        )

    @property
    def state_path(self) -> Path:
        return self.research_dir / "state" / "research.sqlite3"

    @property
    def normalized_dir(self) -> Path:
        return self.research_dir / "normalized"

    def ensure_directories(self) -> None:
        for relative in (
            "raw/blobs",
            "manifests",
            "normalized",
            "reports/coverage",
            "reports/evaluation",
            "quarantine",
            "state",
        ):
            (self.research_dir / relative).mkdir(parents=True, exist_ok=True)
