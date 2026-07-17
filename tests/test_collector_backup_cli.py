from __future__ import annotations

from football_cups.collector import backup as backup_module
from football_cups.collector import cli
from football_cups.collector.config import CollectorConfig
from football_cups.collector.state import StateStore
from football_cups.collector.storage import SingleInstanceLock


def test_backup_cli_uses_retryable_exit_code_for_lock_timeout(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data" / "500"
    backup_dir = tmp_path / "backup"
    monkeypatch.setenv("FOOTBALL_CUPS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FOOTBALL_CUPS_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("COLLECTOR_BACKUP_LOCK_WAIT_SECONDS", "0")
    monkeypatch.setenv("COLLECTOR_BACKUP_LOCK_POLL_SECONDS", "0.01")
    monkeypatch.setattr(backup_module, "_same_volume", lambda source, destination: False)
    config = CollectorConfig.from_workspace(tmp_path)
    with StateStore(config):
        pass

    with SingleInstanceLock(config.lock_path) as lock:
        assert lock.acquired
        assert cli.main(["backup", "--workspace", str(tmp_path)]) == 1


def test_backup_cli_uses_configuration_exit_code(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FOOTBALL_CUPS_BACKUP_DIR", raising=False)
    assert cli.main(["backup", "--workspace", str(tmp_path)]) == 2


def test_backup_cli_uses_storage_exit_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FOOTBALL_CUPS_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(cli, "run_backup", lambda config: (_ for _ in ()).throw(OSError("disk")))
    assert cli.main(["backup", "--workspace", str(tmp_path)]) == 3
