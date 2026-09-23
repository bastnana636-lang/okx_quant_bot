from pathlib import Path

import pytest

import native.launcher as launcher
from native.launcher import LauncherError, _is_protected, sync_payload


def test_runtime_and_credentials_are_protected():
    assert _is_protected(Path(".compose.env"))
    assert _is_protected(Path("conf/connectors/okx_perpetual.yml"))
    assert _is_protected(Path("conf/controllers/conf_okx_pmm_btc.yml"))
    assert _is_protected(Path("conf/.password_verification"))
    assert _is_protected(Path("logs/bot.log"))
    assert not _is_protected(Path("dashboard/dashboard.py"))


def test_sync_payload_updates_code_and_preserves_user_data(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "dashboard").mkdir(parents=True)
    (source / "conf" / "controllers").mkdir(parents=True)
    (source / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (source / "dashboard" / "dashboard.py").write_text("new code\n", encoding="utf-8")
    (source / "conf" / "controllers" / "bot.yml").write_text("new default\n", encoding="utf-8")
    (destination / "dashboard").mkdir(parents=True)
    (destination / "conf" / "controllers").mkdir(parents=True)
    (destination / "dashboard" / "dashboard.py").write_text("old code\n", encoding="utf-8")
    (destination / "conf" / "controllers" / "bot.yml").write_text("user config\n", encoding="utf-8")

    sync_payload(source, destination, "1.1.0")

    assert (destination / "dashboard" / "dashboard.py").read_text() == "new code\n"
    assert (destination / "conf" / "controllers" / "bot.yml").read_text() == "user config\n"
    assert (destination / ".native-version").read_text() == "1.1.0\n"


def test_start_bot_opens_dashboard_before_waiting_for_docker(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(launcher, "prepare", lambda: tmp_path)
    monkeypatch.setattr(launcher, "start_dashboard", lambda root: calls.append(("dashboard", root)))

    def unavailable_docker():
        calls.append(("docker", None))
        raise LauncherError("Docker unavailable")

    monkeypatch.setattr(launcher, "ensure_docker", unavailable_docker)

    with pytest.raises(LauncherError, match="Docker unavailable"):
        launcher.start_bot()

    assert calls == [("dashboard", tmp_path), ("docker", None)]
