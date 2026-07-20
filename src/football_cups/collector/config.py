from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DISCOVERY_SOURCES: tuple[tuple[str, str], ...] = (
    ("default", "https://trade.500.com/jczq/"),
    ("spf", "https://trade.500.com/jczq/?playid=269&g=2"),
    ("mixed", "https://trade.500.com/jczq/?playid=312&g=2"),
    ("score", "https://trade.500.com/jczq/?playid=271&g=2"),
    ("goals", "https://trade.500.com/jczq/?playid=270&g=2"),
    ("half_full", "https://trade.500.com/jczq/?playid=272&g=2"),
)

MARKETS: tuple[str, ...] = ("ouzhi", "yazhi", "daxiao", "rangqiu")
CORE_MARKETS: frozenset[str] = frozenset({"ouzhi", "yazhi", "daxiao"})

# target -> (minutes before kickoff, freshness window in minutes)
CUTOFFS: dict[str, tuple[int, int]] = {
    "T-48h": (48 * 60, 120),
    "T-24h": (24 * 60, 120),
    "T-12h": (12 * 60, 30),
    "T-6h": (6 * 60, 30),
    "T-3h": (3 * 60, 15),
    "T-60m": (60, 10),
    "T-30m": (30, 5),
    "T-10m": (10, 3),
}


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class CollectorConfig:
    workspace: Path
    data_dir: Path
    backup_dir: Path | None
    oss_backup_dir: Path | None
    required_mount: Path | None = None
    timezone_name: str = "Asia/Shanghai"
    discovery_interval_minutes: int = 30
    request_min_interval_seconds: float = 1.5
    run_time_budget_seconds: int = 100
    clock_drift_limit_seconds: int = 30
    request_timeout_seconds: int = 30
    retry_delays_seconds: tuple[float, ...] = (2.0, 5.0, 15.0)
    disk_warning_free_gb: float = 50.0
    disk_critical_free_gb: float = 20.0
    disk_warning_free_percent: float = 20.0
    disk_critical_free_percent: float = 10.0
    health_heartbeat_max_age_minutes: int = 10
    health_discovery_max_age_minutes: int = 45
    health_clock_max_age_minutes: int = 45
    sporttery_reconcile_enabled: bool = True
    sporttery_reconcile_interval_hours: int = 24
    sporttery_reconcile_minimum_age_hours: int = 24
    sporttery_reconcile_lookback_days: int = 8
    backup_lock_wait_seconds: int = 300
    backup_lock_poll_seconds: float = 5.0
    backup_warning_max_age_hours: float = 26.0
    backup_failed_max_age_hours: float = 48.0
    oss_backup_warning_max_age_days: float = 8.0
    oss_backup_failed_max_age_days: float = 15.0
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.disk_critical_free_gb < 0 or self.disk_warning_free_gb < 0:
            raise ValueError("disk free GB thresholds must be non-negative")
        if not 0 <= self.disk_critical_free_percent <= 100:
            raise ValueError("critical disk free percent must be between 0 and 100")
        if not 0 <= self.disk_warning_free_percent <= 100:
            raise ValueError("warning disk free percent must be between 0 and 100")
        if self.disk_critical_free_gb > self.disk_warning_free_gb:
            raise ValueError("critical disk free GB threshold cannot exceed warning threshold")
        if self.disk_critical_free_percent > self.disk_warning_free_percent:
            raise ValueError("critical disk free percent cannot exceed warning threshold")
        if min(
            self.health_heartbeat_max_age_minutes,
            self.health_discovery_max_age_minutes,
            self.health_clock_max_age_minutes,
        ) <= 0:
            raise ValueError("health age thresholds must be positive")
        if min(
            self.sporttery_reconcile_interval_hours,
            self.sporttery_reconcile_minimum_age_hours,
            self.sporttery_reconcile_lookback_days,
        ) <= 0:
            raise ValueError("Sporttery reconciliation timing values must be positive")
        if self.sporttery_reconcile_lookback_days * 24 <= self.sporttery_reconcile_minimum_age_hours:
            raise ValueError("Sporttery reconciliation lookback must exceed minimum age")
        if self.backup_lock_wait_seconds < 0 or self.backup_lock_poll_seconds <= 0:
            raise ValueError("backup lock timing values are invalid")
        if min(
            self.backup_warning_max_age_hours,
            self.backup_failed_max_age_hours,
            self.oss_backup_warning_max_age_days,
            self.oss_backup_failed_max_age_days,
        ) <= 0:
            raise ValueError("backup age thresholds must be positive")
        if self.backup_warning_max_age_hours > self.backup_failed_max_age_hours:
            raise ValueError("backup warning age cannot exceed failed age")
        if self.oss_backup_warning_max_age_days > self.oss_backup_failed_max_age_days:
            raise ValueError("OSS backup warning age cannot exceed failed age")

    @classmethod
    def from_workspace(cls, workspace: Path) -> "CollectorConfig":
        workspace = workspace.resolve()
        _load_dotenv(workspace / ".env")
        data_value = os.environ.get("FOOTBALL_CUPS_DATA_DIR", "").strip()
        backup_value = os.environ.get("FOOTBALL_CUPS_BACKUP_DIR", "").strip()
        oss_backup_value = os.environ.get("FOOTBALL_CUPS_OSS_BACKUP_DIR", "").strip()
        required_mount_value = os.environ.get("FOOTBALL_CUPS_REQUIRED_MOUNT", "").strip()
        return cls(
            workspace=workspace,
            data_dir=(Path(data_value).expanduser().resolve() if data_value else workspace / "data" / "500"),
            backup_dir=Path(backup_value).expanduser().resolve() if backup_value else None,
            oss_backup_dir=Path(oss_backup_value).expanduser().resolve() if oss_backup_value else None,
            required_mount=(
                Path(required_mount_value).expanduser().resolve() if required_mount_value else None
            ),
            timezone_name=os.environ.get("APP_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai",
            discovery_interval_minutes=_env_int("COLLECTOR_DISCOVERY_INTERVAL_MINUTES", 30),
            request_min_interval_seconds=_env_float("COLLECTOR_REQUEST_MIN_INTERVAL_SECONDS", 1.5),
            run_time_budget_seconds=_env_int("COLLECTOR_RUN_TIME_BUDGET_SECONDS", 100),
            clock_drift_limit_seconds=_env_int("COLLECTOR_CLOCK_DRIFT_LIMIT_SECONDS", 30),
            disk_warning_free_gb=_env_float("COLLECTOR_DISK_WARNING_FREE_GB", 50.0),
            disk_critical_free_gb=_env_float("COLLECTOR_DISK_CRITICAL_FREE_GB", 20.0),
            disk_warning_free_percent=_env_float("COLLECTOR_DISK_WARNING_FREE_PERCENT", 20.0),
            disk_critical_free_percent=_env_float("COLLECTOR_DISK_CRITICAL_FREE_PERCENT", 10.0),
            health_heartbeat_max_age_minutes=_env_int(
                "COLLECTOR_HEALTH_HEARTBEAT_MAX_AGE_MINUTES", 10
            ),
            health_discovery_max_age_minutes=_env_int(
                "COLLECTOR_HEALTH_DISCOVERY_MAX_AGE_MINUTES", 45
            ),
            health_clock_max_age_minutes=_env_int(
                "COLLECTOR_HEALTH_CLOCK_MAX_AGE_MINUTES", 45
            ),
            sporttery_reconcile_enabled=_env_bool(
                "COLLECTOR_SPORTTERY_RECONCILE_ENABLED", True
            ),
            sporttery_reconcile_interval_hours=_env_int(
                "COLLECTOR_SPORTTERY_RECONCILE_INTERVAL_HOURS", 24
            ),
            sporttery_reconcile_minimum_age_hours=_env_int(
                "COLLECTOR_SPORTTERY_RECONCILE_MINIMUM_AGE_HOURS", 24
            ),
            sporttery_reconcile_lookback_days=_env_int(
                "COLLECTOR_SPORTTERY_RECONCILE_LOOKBACK_DAYS", 8
            ),
            backup_lock_wait_seconds=_env_int("COLLECTOR_BACKUP_LOCK_WAIT_SECONDS", 300),
            backup_lock_poll_seconds=_env_float("COLLECTOR_BACKUP_LOCK_POLL_SECONDS", 5.0),
            backup_warning_max_age_hours=_env_float(
                "COLLECTOR_BACKUP_WARNING_MAX_AGE_HOURS", 26.0
            ),
            backup_failed_max_age_hours=_env_float(
                "COLLECTOR_BACKUP_FAILED_MAX_AGE_HOURS", 48.0
            ),
            oss_backup_warning_max_age_days=_env_float(
                "COLLECTOR_OSS_BACKUP_WARNING_MAX_AGE_DAYS", 8.0
            ),
            oss_backup_failed_max_age_days=_env_float(
                "COLLECTOR_OSS_BACKUP_FAILED_MAX_AGE_DAYS", 15.0
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )

    def required_mount_ready(self) -> bool:
        if self.required_mount is None:
            return True
        return self.required_mount.is_dir() and os.path.ismount(self.required_mount)

    def disk_thresholds(self, total_bytes: int) -> tuple[int, int]:
        warning = max(
            int(total_bytes * self.disk_warning_free_percent / 100),
            int(self.disk_warning_free_gb * 1024**3),
        )
        critical = max(
            int(total_bytes * self.disk_critical_free_percent / 100),
            int(self.disk_critical_free_gb * 1024**3),
        )
        return warning, critical

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state" / "collector.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "state" / "collector.lock"

    def ensure_directories(self) -> None:
        if not self.required_mount_ready():
            raise OSError(f"required data mount is unavailable: {self.required_mount}")
        for relative in (
            "raw/blobs",
            "discovery",
            "manifests",
            "normalized",
            "results",
            "reports/daily",
            "quarantine",
            "state",
            "logs",
        ):
            (self.data_dir / relative).mkdir(parents=True, exist_ok=True)
