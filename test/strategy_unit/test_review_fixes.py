import ast
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest
import yaml

from dashboard.dashboard import (
    build_view,
    render_html,
    parse_performance_table,
    parse_positions,
    parse_orders,
    parse_executors,
    parse_controller_signals,
)
from dashboard.strategy_config import load_config, render_updates, _normalize_fields, PAIR_FIELDS
from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType

ROOT = Path(__file__).resolve().parents[2]


def _load_get_position_amount():
    tree = ast.parse((ROOT / "hummingbot/connector/derivative/okx_perpetual/okx_perpetual_derivative.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_position_amount":
            from typing import Any, Dict, Union
            scope = {"Decimal": Decimal, "s_decimal_0": Decimal("0"), "Optional": Optional, "Dict": Dict, "Any": Any, "Union": Union}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "okx_perpetual_derivative.py", "exec"), scope)
            return scope["get_position_amount"]
    raise RuntimeError("get_position_amount not found")


get_position_amount = _load_get_position_amount()


def test_okx_position_amount_no_integer_rounding():
    connector = MagicMock()
    connector._contract_sizes = {"BTC-USDT": Decimal("0.01")}
    connector.name = "okx_perpetual"

    # 60 contracts * 0.01 = 0.6 BTC
    msg = {
        "instId": "BTC-USDT-SWAP",
        "pos": "60",
        "ctVal": "0.01",
        "notionalUsd": "66000",
        "avgPx": "100000",
        "markPx": "110000",
    }
    amount = get_position_amount(connector, msg, "BTC-USDT")
    # Must be 0.6, NOT rounded up to 1
    assert amount == Decimal("0.6")


def test_dashboard_build_view_error_fallback():
    # When status has error, build_view should return a valid dictionary that render_html can safely format
    view = build_view({"error": "test error: status.json not found"})
    assert view["error"] == "test error: status.json not found"
    assert "status_html" in view
    assert "usdt_bal" in view
    assert "uptime_s" in view
    assert "uptime_str" in view

    # Test that render_html does not crash with KeyError
    html = render_html(view)
    assert "status.json not found" in html
    assert "OKX QUANTITATIVE TRADER" in html


def test_strategy_config_optional_fixed_tp():
    # If fixed_unrealized_tp_quote is empty, _normalize_fields should leave it empty and not force '2'
    source = {
        "leverage": "3",
        "total_amount_quote": "50",
        "take_profit_quote": "1.2",
        "fixed_unrealized_tp_quote": "",
    }
    normalized = _normalize_fields(source, PAIR_FIELDS)
    assert normalized["fixed_unrealized_tp_quote"] == ""

    # render_updates should render null when empty
    updated = render_updates("fixed_unrealized_tp_quote: 2\ntotal_amount_quote: 50", {"fixed_unrealized_tp_quote": ""})
    assert "fixed_unrealized_tp_quote: null" in updated


def test_position_executor_close_order_price_methods():
    # Load PositionExecutor methods via AST to verify logic without compiled C-extensions
    tree = ast.parse((ROOT / "hummingbot/strategy_v2/executors/position_executor/position_executor.py").read_text())
    executor_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PositionExecutor")
    methods = {n.name: n for n in executor_cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # Verify that in control_close_order, close_type is strictly checked
    close_order_src = ast.unparse(methods["control_close_order"])
    assert "or quote_target is not None" not in close_order_src
    assert "if self.close_type in (CloseType.TAKE_PROFIT, CloseType.EARLY_STOP) and self.open_filled_amount > 0:" in close_order_src

    # Verify that process_order_canceled_event accumulates filled amounts
    canceled_src = ast.unparse(methods["process_order_canceled_event"])
    assert "self._cum_close_filled_amount += self._close_order.executed_amount_base" in canceled_src
    assert "self._cum_open_filled_amount +=" in canceled_src


def test_strategy_v2_base_has_get_structured_status():
    tree = ast.parse((ROOT / "hummingbot/strategy/strategy_v2_base.py").read_text())
    strat_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StrategyV2Base")
    methods = {n.name: n for n in strat_cls.body if isinstance(n, ast.FunctionDef)}
    assert "get_structured_status" in methods
    src = ast.unparse(methods["get_structured_status"])
    assert "performance" in src
    assert "positions_held" in src
    assert "signals" in src
    assert "executors" in src


def test_structured_data_dashboard_parsing():
    structured_status = {
        "running": True,
        "updated_at": 1790560883.0,
        "engine": {"strategy_running": True, "start_time": 1790526212550, "uptime": 34671285},
        "balances": {"okx_perpetual": {"USDT": 1000.5}},
        "account_positions": {
            "okx_perpetual": [
                {
                    "connector": "okx_perpetual",
                    "trading_pair": "SOL-USDT",
                    "side": "LONG",
                    "amount": 10.5,
                    "entry_price": 150.25,
                    "unrealized_pnl": 25.54321,
                    "leverage": 5.0,
                }
            ]
        },
        "strategy_data": {
            "performance": [
                {
                    "controller": "okx_pmm_sol",
                    "realized": 12.34567,
                    "unrealized": 25.54321,
                    "global": 37.88888,
                    "global_pct": "3.78%",
                    "volume": 1000.0,
                }
            ],
            "positions_held": [
                {
                    "connector": "okx_perpetual",
                    "pair": "BTC-USDT",
                    "side": "BUY",
                    "amount": 0.5,
                    "value": 30000.0,
                    "breakeven": 60000.0,
                    "unrealized": 10.0,
                    "realized": 2.0,
                    "fees": 0.5,
                    "controller_id": "okx_pmm_btc",
                }
            ],
            "executors": [
                {
                    "controller": "okx_pmm_sol",
                    "side": "BUY",
                    "status": "RUNNING",
                    "net_pnl_pct": 0.025,
                    "net_pnl": 25.54321,
                    "volume": 500.0,
                    "is_trading": True,
                    "close_type": "",
                    "age": 42.0,
                }
            ],
            "orders": [
                {
                    "connector": "okx_perpetual",
                    "pair": "SOL-USDT",
                    "side": "BUY",
                    "price": 149.50,
                    "amount": 5.0,
                    "age": 15.0,
                }
            ],
            "signals": {
                "okx_pmm_sol": {
                    "z_score": 2.45,
                    "signal": 1,
                    "reason": "z_outside_band",
                }
            },
        },
    }

    # 1. Performance parsing
    perf = parse_performance_table(structured_status)
    assert len(perf) == 1
    assert perf[0]["controller"] == "okx_pmm_sol"
    # Full precision is kept, not truncated to 2 decimals
    assert perf[0]["realized"] == 12.34567
    assert perf[0]["unrealized"] == 25.54321

    # 2. Positions parsing: includes both exchange account_positions AND positions_held
    positions = parse_positions(structured_status)
    assert len(positions) == 2
    pairs = {p["pair"]: p for p in positions}
    assert "SOL-USDT" in pairs
    assert pairs["SOL-USDT"]["source"] == "OKX 账户仓位"
    assert pairs["SOL-USDT"]["amount"] == 10.5
    assert pairs["SOL-USDT"]["unrealized"] == 25.54321

    assert "BTC-USDT" in pairs
    assert pairs["BTC-USDT"]["source"] == "策略接管"
    assert pairs["BTC-USDT"]["amount"] == 0.5

    # 3. Orders parsing
    orders = parse_orders(structured_status)
    assert len(orders) == 1
    assert orders[0]["pair"] == "SOL-USDT"
    assert orders[0]["price"] == 149.50

    # 4. Executors parsing
    executors = parse_executors(structured_status)
    assert len(executors) == 1
    assert executors[0]["controller"] == "okx_pmm_sol"
    assert executors[0]["is_trading"] is True

    # 5. Signals parsing
    signals = parse_controller_signals(structured_status)
    assert "okx_pmm_sol" in signals
    assert signals["okx_pmm_sol"]["z"] == 2.45
    assert signals["okx_pmm_sol"]["signal"] == 1

    # 6. build_view rendering with structured data
    view = build_view(structured_status)
    assert "SOL-USDT" in view["pos_html"]
    assert "OKX 账户仓位" in view["pos_html"]
    assert "BTC-USDT" in view["pos_html"]
    assert "策略接管" in view["pos_html"]
    assert "⚡ +2.45 (BUY)" in view["perf_html"]

    html = render_html(structured_status)
    assert "OKX 账户仓位" in html
    assert "SOL-USDT" in html

