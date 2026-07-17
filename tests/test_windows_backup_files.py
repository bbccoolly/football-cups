from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"


def test_backup_task_installer_has_stable_s4u_schedules() -> None:
    text = (WINDOWS / "install_backup_tasks.ps1").read_text(encoding="utf-8")
    assert "FootballCups-Daily-Backup" in text
    assert "FootballCups-Weekly-Verified-Backup" in text
    assert '-LogonType $logonType -RunLevel Limited' in text
    assert 'New-ScheduledTaskTrigger -Daily -At "03:30"' in text
    assert "-DaysOfWeek Sunday" in text
    assert '-At "04:30"' in text
    assert "$Interactive" in text
    assert "$Uninstall" in text


def test_database_task_supports_a_dedicated_non_admin_password_user() -> None:
    installer = (WINDOWS / "install_database_import_task.ps1").read_text(encoding="utf-8")
    configurator = (WINDOWS / "configure_database_task_user.ps1").read_text(encoding="utf-8")
    assert '[string]$UserId = ""' in installer
    assert "[Security.SecureString]$Password" in installer
    assert "$PasswordLogon" in installer
    assert "-UserId $UserId -LogonType $logonType -RunLevel Limited" in installer
    assert "-Password $passwordText" in installer
    assert 'New-LocalUser `' in configurator
    assert "-NoPassword" in configurator
    assert 'Get-LocalGroup -SID "S-1-5-32-544"' in configurator
    assert 'Get-LocalGroup -SID "S-1-5-32-545"' in configurator
    assert '".venv\\pyvenv.cfg"' in configurator
    assert "RandomNumberGenerator" in configurator
    assert "-PasswordLogon `" in configurator
    assert "FileSystemAccessRule" in configurator


def test_backup_configuration_preserves_other_environment_lines() -> None:
    text = (WINDOWS / "configure_local_backup.ps1").read_text(encoding="utf-8")
    assert "Get-Content -LiteralPath $envPath" in text
    assert "$existing.RemoveAt($index)" in text
    assert "[System.IO.File]::Replace" in text
    assert "Get-Partition -DriveLetter" in text
    assert "different physical disk" in text


def test_backup_runner_propagates_exit_code_and_writes_jsonl() -> None:
    text = (WINDOWS / "run_backup_task.ps1").read_text(encoding="utf-8")
    assert '"backup-task.jsonl"' in text
    assert "$LASTEXITCODE" in text
    assert "exit $exitCode" in text
    assert "ConvertTo-Json -Compress" in text


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_windows_backup_scripts_parse() -> None:
    for name in (
        "configure_local_backup.ps1",
        "run_backup_task.ps1",
        "install_backup_tasks.ps1",
        "configure_database_task_user.ps1",
        "install_database_import_task.ps1",
    ):
        path = WINDOWS / name
        command = (
            "$errors=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',"
            "[ref]$null,[ref]$errors); "
            "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
