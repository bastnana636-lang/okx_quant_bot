"""Exercise lifecycle commands with fake Docker; never start a trading process."""
import copy
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import wait_for_bot_ready as readiness

ROOT = Path(__file__).resolve().parents[2]
CONFIG = "conf_okx_multi.yml"


@pytest.fixture
def fake_docker(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as log:
    safe_args = ["HBOT_PASSWORD=<redacted>" if arg.startswith("HBOT_PASSWORD=") else arg for arg in args]
    log.write(json.dumps(safe_args) + "\\n")
if args[:2] == ["container", "ls"]:
    print(os.environ.get("CONTAINER_STATE", "running"))
    sys.exit(int(os.environ.get("LIST_EXIT", "0")))
if args[-2:] == ["hbot", "stop"]:
    print("stop result")
    sys.exit(int(os.environ.get("STOP_EXIT", "0")))
if "scripts.validate_mean_reversion" in args:
    sys.exit(int(os.environ.get("VALIDATE_EXIT", "0")))
if args[:2] == ["exec", "-i"]:
    sys.exit(int(os.environ.get("WAIT_EXIT", "0")))
''')
    docker.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", DOCKER_CALLS=str(calls))

    def run(target, **updates):
        # Include the outer Makefile when present, as in the reported command.
        cwd = ROOT.parent if (ROOT.parent / "Makefile").exists() else ROOT
        result = subprocess.run(["make", target], cwd=cwd, env=dict(env, **updates),
                                capture_output=True, text=True, timeout=10)
        return result, [json.loads(line) for line in calls.read_text().splitlines()]

    return run


@pytest.mark.parametrize("state", ["", "created", "exited", "dead"])
@pytest.mark.parametrize("target", ["stop", "status"])
def test_stopped_container_is_success_without_exec(fake_docker, state, target):
    result, calls = fake_docker(target, CONTAINER_STATE=state)
    assert result.returncode == 0
    assert "stopped" in result.stdout
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["0", "2", "3"])
def test_running_container_stop_is_idempotent(fake_docker, code):
    result, calls = fake_docker("stop", STOP_EXIT=code)
    assert result.returncode == 0
    assert calls[-1] == ["exec", "hummingbot", "hbot", "stop"]


@pytest.mark.parametrize("options", [{"LIST_EXIT": "1"}, {"STOP_EXIT": "5"},
                                    {"STOP_EXIT": "1"}, {"CONTAINER_STATE": "paused"}])
def test_real_failures_are_not_suppressed(fake_docker, options):
    result, _ = fake_docker("stop", **options)
    assert result.returncode != 0


def test_start_waits_for_readiness_and_propagates_timeout(fake_docker):
    result, calls = fake_docker("start", WAIT_EXIT="5", SKIP_SETUP="1", HBOT_PASSWORD="test")
    assert result.returncode != 0
    assert calls[0] == ["compose", "--env-file", ".compose.env", "up", "-d", "hummingbot"]
    assert "scripts.validate_mean_reversion" in calls[1]
    assert calls[2][-5:] == ["hbot", "start", CONFIG, "--v2-script", "--replace"]
    assert calls[3] == ["exec", "-i", "hummingbot", "python", "-", "--config", CONFIG, "--timeout", "120"]


def test_failed_validation_does_not_start_bot(fake_docker):
    result, calls = fake_docker("start", VALIDATE_EXIT="1", SKIP_SETUP="1", HBOT_PASSWORD="test")
    assert result.returncode != 0
    assert len(calls) == 2


def ready_snapshot():
    return {"pid": 22, "running": True, "updated_at": 100,
            "engine": {"strategy_running": True, "clock_running": True,
                       "connectors": {"okx_perpetual": {"ready": True, "trading_pairs": ["BTC-USDT"]}}},
            "format_status": "Controller: btc\nMean reversion | z=1 | outside_entry_band"}


def run_wait(monkeypatch, snapshots, timeout=5, alive=True, config=CONFIG):
    state = {"snapshot": ready_snapshot(), "now": 0}
    iterator = iter(snapshots)

    def refresh(timeout):
        state["snapshot"] = next(iterator, state["snapshot"])

    def sleep(seconds):
        state["now"] += seconds

    monkeypatch.setattr(readiness.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(readiness.time, "sleep", sleep)
    bot = SimpleNamespace(read_pid=lambda: 22, is_engine_pid=lambda pid: alive,
                          read_meta=lambda: {"file": config, "started_at": 10},
                          read_status=lambda: state["snapshot"])
    return readiness.wait_for_ready(bot, refresh, CONFIG, timeout)


def test_waits_through_connector_and_candle_warmup(monkeypatch, capsys):
    connector = ready_snapshot()
    connector["updated_at"] = 101
    connector["engine"]["connectors"]["okx_perpetual"]["ready"] = False
    connector["format_status"] = "Market connectors are not ready."
    candles = ready_snapshot()
    candles["updated_at"] = 102
    candles["format_status"] = "Controller: btc\nMean reversion | z=n/a | waiting_for_data: candles=12/50"
    ready = ready_snapshot()
    ready["updated_at"] = 103
    assert run_wait(monkeypatch, [connector, candles, ready]) == 0
    output = capsys.readouterr().out
    assert "market connector initializing" in output
    assert "candles=12/50" in output
    assert "Ready:" in output


@pytest.mark.parametrize("change", [{}, {"pid": 21, "updated_at": 101},
                                  {"running": False, "updated_at": 101}, {"updated_at": 9}])
def test_stale_or_previous_run_snapshot_never_reports_ready(monkeypatch, change):
    snapshot = ready_snapshot()
    snapshot.update(change)
    assert run_wait(monkeypatch, [snapshot]) == 5


def test_timeout_reports_pending_dependencies(monkeypatch, capsys):
    snapshots = []
    for timestamp in range(101, 105):
        snapshot = ready_snapshot()
        snapshot["updated_at"] = timestamp
        snapshot["format_status"] = "Mean reversion | z=n/a | leverage_not_ready"
        snapshots.append(snapshot)
    assert run_wait(monkeypatch, snapshots) == 5
    output = capsys.readouterr().out
    assert "leverage_not_ready" in output
    assert "does not stop or restart" in output


def test_stopped_process_fails_without_refresh(monkeypatch):
    assert run_wait(monkeypatch, [], alive=False) == 3


def test_wrong_config_cannot_pass_readiness(monkeypatch):
    assert run_wait(monkeypatch, [], config="other.yml") == 4


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(timeout):
    assert readiness.wait_for_ready(Mock(), Mock(), CONFIG, timeout) == 4


def test_absent_engine_connectors_and_format_status_are_pending():
    assert len(readiness.pending_dependencies({})) == 3
    snapshot = copy.deepcopy(ready_snapshot())
    snapshot["engine"]["clock_running"] = False
    assert readiness.pending_dependencies(snapshot) == ["strategy/clock initializing"]
