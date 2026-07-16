from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from football_cups.collector.config import CollectorConfig


@dataclass(frozen=True)
class DatabaseConfig:
    workspace: Path
    data_dir: Path
    database_url: str | None

    @classmethod
    def from_workspace(cls, workspace: Path) -> "DatabaseConfig":
        collector = CollectorConfig.from_workspace(workspace)
        if not collector.required_mount_ready():
            raise OSError(f"required data mount is unavailable: {collector.required_mount}")
        database_url = os.environ.get("DATABASE_URL", "").strip() or None
        return cls(
            workspace=collector.workspace,
            data_dir=collector.data_dir,
            database_url=database_url,
        )

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def local_postgres_data_dir(self) -> Path:
        return self.data_dir.parent / "postgresql" / "17-main"

    @property
    def local_postgres_available(self) -> bool:
        return (self.local_postgres_data_dir / "PG_VERSION").is_file()
