from pathlib import Path

import pytest

from dashboard.strategy_config import ConfigError, apply_config, load_config, render_updates
from scripts.ensure_setup import (
    needs_setup,
    parse_env,
    render_env,
    setup_status,
    shell_exports,
)

SAMPLE = """# comment stays
id: okx_pmm_btc
trading_pair: BTC-USDT
total_amount_quote: 180
leverage: 3
take_profit_quote: 0.75
candles_interval: 5m
mean_window: 48
entry_z_score: 2.0
max_entry_z_score: 3.5
exit_z_score: 0.4
min_std_pct: 0.001
atr_length: 14
atr_stop_multiplier: 1.5
min_stop_loss: 0.006
max_stop_loss: 0.02
round_trip_cost_pct: 0.0012
min_net_edge_pct: 0.0018
min_reward_risk: 0.5
quote_offset_pct: 0.0002
max_bid_ask_spread_pct: 0.002
executor_refresh_time: 60
cooldown_time: 300
stop_loss_cooldown_time: 900
time_limit: 3600
manual_kill_switch: false
"""


def _write_pair(directory: Path, name: str, pair: str, amount: str) -> None:
    text = SAMPLE.replace("id: okx_pmm_btc", f"id: {name[:-4]}").replace("BTC-USDT", pair)
    text = text.replace("total_amount_quote: 180", f"total_amount_quote: {amount}")
    (directory / name).write_text(text, encoding="utf-8")


def _layout(tmp_path: Path):
    controllers = tmp_path / "controllers"
    controllers.mkdir()
    script = tmp_path / "conf_okx_multi.yml"
    script.write_text(
        "script_file_name: v2_with_controllers.py\n"
        "controllers_config:\n"
        "  - conf_okx_pmm_btc.yml\n"
        "  - conf_okx_pmm_eth.yml\n",
        encoding="utf-8",
    )
    _write_pair(controllers, "conf_okx_pmm_btc.yml", "BTC-USDT", "180")
    _write_pair(controllers, "conf_okx_pmm_eth.yml", "ETH-USDT", "112.5")
    return script, controllers


def test_round_trip_preserves_comments_and_amounts(tmp_path: Path):
    script, controllers = _layout(tmp_path)
    view = load_config(script, controllers)
    assert view["pairs"][0]["total_amount_quote"] == "180"
    assert view["pairs"][1]["total_amount_quote"] == "112.5"
    assert view["defaults"]["leverage"] == 3
    assert view["shared_mixed"]["entry_z_score"] is False

    before = {path.name: path.read_text(encoding="utf-8") for path in controllers.glob("*.yml")}
    apply_config({"pairs": view["pairs"], "shared": view["shared"]}, script, controllers)
    after = {path.name: path.read_text(encoding="utf-8") for path in controllers.glob("*.yml")}
    assert after == before


def test_save_updates_leverage_and_notional(tmp_path: Path):
    script, controllers = _layout(tmp_path)
    view = load_config(script, controllers)
    view["pairs"][0]["leverage"] = "4"
    view["pairs"][0]["total_amount_quote"] = "200"
    view["shared"]["entry_z_score"] = "2.2"
    apply_config(view, script, controllers)
    text = (controllers / "conf_okx_pmm_btc.yml").read_text(encoding="utf-8")
    assert "leverage: 4" in text
    assert "total_amount_quote: 200" in text
    assert "entry_z_score: 2.2" in text
    assert text.startswith("# comment stays\n")
    eth = (controllers / "conf_okx_pmm_eth.yml").read_text(encoding="utf-8")
    assert "leverage: 3" in eth
    assert "entry_z_score: 2.2" in eth
    assert "total_amount_quote: 112.5" in eth


def test_rejects_invalid_leverage_and_z_order(tmp_path: Path):
    script, controllers = _layout(tmp_path)
    view = load_config(script, controllers)
    view["pairs"][0]["leverage"] = "9"
    with pytest.raises(ConfigError, match="杠杆"):
        apply_config(view, script, controllers)
    view["pairs"][0]["leverage"] = "3"
    view["shared"]["exit_z_score"] = "3"
    with pytest.raises(ConfigError, match="出场 Z"):
        apply_config(view, script, controllers)


def test_rejects_unknown_controller_file(tmp_path: Path):
    script, controllers = _layout(tmp_path)
    view = load_config(script, controllers)
    view["pairs"][0]["file"] = "../conf_okx_pmm_btc.yml"
    with pytest.raises(ConfigError, match="未知"):
        apply_config(view, script, controllers)


def test_render_updates_appends_missing_key():
    assert render_updates("leverage: 3\n", {"take_profit_quote": "0.75"}) == (
        "leverage: 3\ntake_profit_quote: 0.75\n"
    )


def test_live_controller_configs_match_editor_rules(tmp_path: Path):
    from dashboard.strategy_config import CONTROLLERS_DIR, SCRIPT_FILE, active_controller_names
    controllers = tmp_path / "controllers"
    controllers.mkdir()
    for name in active_controller_names():
        (controllers / name).write_text((CONTROLLERS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "conf_okx_multi.yml"
    script.write_text(SCRIPT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    view = load_config(script, controllers)
    before = {path.name: path.read_text(encoding="utf-8") for path in controllers.glob("*.yml")}
    apply_config({"pairs": view["pairs"], "shared": view["shared"]}, script, controllers)
    after = {path.name: path.read_text(encoding="utf-8") for path in controllers.glob("*.yml")}
    assert after == before


def test_setup_status_and_env_round_trip():
    env = parse_env("COMPOSE_PROFILES=\nHBOT_PASSWORD=secret\n# note\nOKX_HTTP_PROXY=\n")
    assert env["HBOT_PASSWORD"] == "secret"
    assert env["OKX_HTTP_PROXY"] == ""
    assert "note" not in render_env(env)
    status = setup_status(env, password_exists=True, connector_exists=False)
    assert needs_setup(status) is True
    status["connector"] = True
    assert needs_setup(status) is False
    exported = shell_exports({"HBOT_PASSWORD": "a'b"})
    assert exported == "export HBOT_PASSWORD='a'\\''b'"
