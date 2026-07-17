from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from football_cups.collector.cli import _health
from football_cups.collector.backup import run_backup, run_oss_backup
from football_cups.collector.config import CollectorConfig
from football_cups.collector.state import StateStore
from football_cups.collector.timeutil import iso_utc
from football_cups.database.config import DatabaseConfig


def config_for(tmp_path, **overrides) -> CollectorConfig:
    values = {
        "workspace": tmp_path,
        "data_dir": tmp_path / "data" / "500",
        "backup_dir": None,
        "oss_backup_dir": None,
        "disk_warning_free_gb": 0,
        "disk_critical_free_gb": 0,
        "disk_warning_free_percent": 0,
        "disk_critical_free_percent": 0,
    }
    values.update(overrides)
    return CollectorConfig(**values)


def set_healthy_timestamps(config: CollectorConfig, now: datetime) -> None:
    with StateStore(config) as state:
        state.set_meta("last_heartbeat_at", iso_utc(now))
        state.set_meta("last_full_discovery_at", iso_utc(now))
        state.set_meta("last_clock_check_at", iso_utc(now))


def test_health_is_warning_before_first_run(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    result = _health(config_for(tmp_path), now=now)

    assert result["status"] == "warning"
    assert {item["code"] for item in result["issues"]} == {
        "last_heartbeat_missing",
        "last_full_discovery_missing",
        "last_clock_check_missing",
    }


def test_health_is_ok_with_recent_runtime_evidence(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    config = config_for(tmp_path)
    set_healthy_timestamps(config, now)

    result = _health(config, now=now)

    assert result["status"] == "ok"
    assert result["state_quick_check"] == "ok"
    assert result["disk_status"] == "ok"
    assert result["issues"] == []


def test_health_fails_when_heartbeat_is_stale(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    config = config_for(tmp_path)
    set_healthy_timestamps(config, now)
    with StateStore(config) as state:
        state.set_meta("last_heartbeat_at", iso_utc(now - timedelta(minutes=11)))

    result = _health(config, now=now)

    assert result["status"] == "failed"
    assert "last_heartbeat_stale" in {item["code"] for item in result["issues"]}


def test_health_fails_until_clock_drift_is_followed_by_full_discovery(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    config = config_for(tmp_path)
    set_healthy_timestamps(config, now - timedelta(minutes=5))
    with StateStore(config) as state:
        state.set_meta("last_clock_drift_at", iso_utc(now - timedelta(minutes=1)))
        state.set_meta("last_clock_drift_seconds", "31.5")

    failed = _health(config, now=now)
    assert failed["status"] == "failed"
    assert failed["last_clock_drift_seconds"] == 31.5
    assert "unresolved_clock_drift" in {item["code"] for item in failed["issues"]}

    with StateStore(config) as state:
        state.set_meta("last_full_discovery_at", iso_utc(now))
        state.set_meta("last_clock_check_at", iso_utc(now))
    recovered = _health(config, now=now)
    assert recovered["status"] == "ok"


def test_health_fails_without_required_mount_and_does_not_create_data_dir(tmp_path) -> None:
    required_mount = tmp_path / "not-mounted"
    data_dir = required_mount / "data" / "500"
    config = config_for(tmp_path, data_dir=data_dir, required_mount=required_mount)

    result = _health(config, now=datetime(2026, 7, 16, 9, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert result["issues"][0]["code"] == "required_mount_unavailable"
    assert not data_dir.exists()


def test_disk_thresholds_combine_absolute_and_percentage_limits(tmp_path) -> None:
    config = config_for(
        tmp_path,
        disk_warning_free_gb=10,
        disk_critical_free_gb=5,
        disk_warning_free_percent=20,
        disk_critical_free_percent=10,
    )

    warning, critical = config.disk_thresholds(40 * 1024**3)

    assert warning == 10 * 1024**3
    assert critical == 5 * 1024**3


def test_database_config_refuses_an_unavailable_required_mount(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FOOTBALL_CUPS_REQUIRED_MOUNT", str(tmp_path / "missing-mount"))
    monkeypatch.setenv("FOOTBALL_CUPS_DATA_DIR", str(tmp_path / "missing-mount" / "data"))

    with pytest.raises(OSError, match="required data mount is unavailable"):
        DatabaseConfig.from_workspace(tmp_path)


def test_health_tracks_completed_backup_ages(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    config = config_for(
        tmp_path,
        backup_dir=tmp_path / "backup",
        oss_backup_dir=tmp_path / "oss",
    )
    config.backup_dir.mkdir()
    config.oss_backup_dir.mkdir()
    set_healthy_timestamps(config, now)

    missing = _health(config, now=now)
    assert missing["status"] == "warning"
    assert missing["backup_status"] == "warning"
    assert missing["oss_backup_status"] == "warning"

    run_backup(config, require_distinct_volume=False, now=now)
    run_oss_backup(config, now=now)
    set_healthy_timestamps(config, now + timedelta(hours=1))
    healthy = _health(config, now=now + timedelta(hours=1))
    assert healthy["status"] == "ok"
    assert healthy["backup_status"] == "ok"
    assert healthy["oss_backup_status"] == "ok"
    assert healthy["backup_drive_free_bytes"] > 0

    set_healthy_timestamps(config, now + timedelta(hours=27))
    warning = _health(config, now=now + timedelta(hours=27))
    assert warning["status"] == "warning"
    assert warning["backup_status"] == "warning"

    set_healthy_timestamps(config, now + timedelta(days=16))
    failed = _health(config, now=now + timedelta(days=16))
    assert failed["status"] == "failed"
    assert failed["backup_status"] == "failed"
    assert failed["oss_backup_status"] == "failed"


def test_health_fails_when_configured_backup_drive_is_unavailable(tmp_path) -> None:
    now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)
    config = config_for(
        tmp_path,
        backup_dir=tmp_path / "missing-backup-drive",
        oss_backup_dir=tmp_path / "missing-oss-drive",
    )
    set_healthy_timestamps(config, now)

    result = _health(config, now=now)

    assert result["status"] == "failed"
    assert result["backup_status"] == "failed"
    assert result["oss_backup_status"] == "failed"
    assert "backup_directory_unavailable" in {item["code"] for item in result["issues"]}
