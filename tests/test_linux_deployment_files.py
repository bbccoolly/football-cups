from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "scripts" / "linux"


def test_services_keep_start_limits_in_unit_section() -> None:
    for name in ("football-cups-collector.service", "football-cups-db-import.service"):
        text = (LINUX / name).read_text(encoding="utf-8")
        unit, service = text.split("[Service]", 1)
        assert "StartLimitIntervalSec=1800" in unit
        assert "StartLimitBurst=4" in unit
        assert "StartLimit" not in service


def test_services_require_the_formal_data_mount_and_harden_writes() -> None:
    for name in ("football-cups-collector.service", "football-cups-db-import.service"):
        text = (LINUX / name).read_text(encoding="utf-8")
        assert "RequiresMountsFor=/srv/football-cups" in text
        assert "ConditionPathIsMountPoint=/srv/football-cups" in text
        assert "ProtectSystem=strict" in text
        assert "ReadOnlyPaths=/opt/football-cups" in text
        assert "ReadWritePaths=/srv/football-cups" in text


def test_collector_service_does_not_depend_on_postgresql() -> None:
    text = (LINUX / "football-cups-collector.service").read_text(encoding="utf-8")
    assert "postgresql.service" not in text


def test_smoke_bootstrap_assigns_the_parent_directory_to_the_service_user() -> None:
    text = (LINUX / "bootstrap-smoke.sh").read_text(encoding="utf-8")
    assert (
        "install -d -o football-cups -g football-cups -m 0750 "
        "/var/lib/football-cups-smoke\n"
    ) in text


def test_data_disk_script_requires_explicit_apply_and_confirmation() -> None:
    text = (LINUX / "prepare-data-disk.sh").read_text(encoding="utf-8")
    assert "--confirm-device" in text
    assert "--apply" in text
    assert 'mkfs.ext4 -L football-data "${device}"' in text
    assert "The default mode is read-only" in text
    assert "/srv/football-cups exists and is not empty" in text


def test_oss_roundtrip_uses_fresh_download_and_never_accepts_access_keys() -> None:
    text = (LINUX / "verify-oss-roundtrip.sh").read_text(encoding="utf-8")
    assert 'ossutil sync "${upload_root}/" "${remote_uri%/}/"' in text
    assert 'ossutil sync "${remote_uri%/}/" "${download_root}/"' in text
    assert "verify-oss-backup" in text
    assert "AccessKey values" in text
    assert "--apply" in text


def test_postgresql_installer_forces_the_cluster_onto_the_data_disk() -> None:
    text = (LINUX / "install-postgresql.sh").read_text(encoding="utf-8")
    assert "create_main_cluster = false" in text
    assert "--datadir=\"${data_dir}\"" in text
    assert "--auth-host=scram-sha-256" in text
    assert "listen_addresses 127.0.0.1" in text
    assert "No application role, password, database, or DATABASE_URL is created" in text
