"""Portable policy unit tests running production class/method bodies.

Only Hummingbot's compiled framework boundaries are doubled. These tests do not
claim to validate connectors, fills, or the full Hummingbot runtime.
"""
import __future__
import ast
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import List, Literal, Optional
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from controllers.market_making.mean_reversion import MeanReversionSettings, ReversionSnapshot, calculate_barriers, calculate_snapshot
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


def load_definitions(relative_path, names, scope):
    tree = ast.parse((ROOT / relative_path).read_text())
    definitions = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    assert {node.name for node in definitions} == set(names)
    for node in definitions:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / relative_path), 'exec',
                     flags=__future__.annotations.compiler_flag), scope)
        if issubclass(scope[node.name], BaseModel):
            scope[node.name].model_rebuild(_types_namespace=scope)


class ControllerConfigDouble(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra='forbid')
    id: str
    manual_kill_switch: bool = False


class ControllerDouble:
    def __init__(self, config, market_data_provider, **kwargs):
        self.config = config
        self.market_data_provider = market_data_provider
        self.executors_info = []
        self.positions_held = []
        self.processed_data = {}
        self.executors_update_event = asyncio.Event()

    def logger(self):
        return logging.getLogger(__name__)


class ExecutorConfigDouble(BaseModel):
    timestamp: float = 0
    model_config = ConfigDict(arbitrary_types_allowed=True)


@pytest.fixture
def policy():
    scope = dict(globals(), ControllerBase=ControllerDouble, ControllerConfigBase=ControllerConfigDouble,
                 ExecutorConfigBase=ExecutorConfigDouble, ConnectorPair=SimpleNamespace, CandlesConfig=SimpleNamespace,
                 CreateExecutorAction=SimpleNamespace, StopExecutorAction=SimpleNamespace,
                 parse_enum_value=lambda enum, value, field: enum[value] if isinstance(value, str) else value)
    load_definitions('hummingbot/strategy_v2/models/executors.py', ['CloseType'], scope)
    load_definitions('hummingbot/strategy_v2/models/base.py', ['RunnableStatus'], scope)
    load_definitions('hummingbot/strategy_v2/executors/position_executor/data_types.py',
                     ['TrailingStop', 'TripleBarrierConfig', 'PositionExecutorConfig'], scope)
    load_definitions('controllers/market_making/pmm_simple.py', ['PMMSimpleConfig', 'PMMSimpleController'], scope)
    # Execute the real barrier method without importing the compiled exchange stack.
    tree = ast.parse((ROOT / 'hummingbot/strategy_v2/executors/position_executor/position_executor.py').read_text())
    executor = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PositionExecutor')
    method = next(node for node in executor.body if isinstance(node, ast.FunctionDef) and node.name == 'control_barriers')
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<production control_barriers>', 'exec'), scope)
    return SimpleNamespace(**scope)


@pytest.fixture
def controller(policy):
    provider = MagicMock()
    provider.time.return_value = 1800000000
    provider.ready = True
    provider.get_connector.return_value.account_positions = {}
    provider.get_connector.return_value.get_leverage.return_value = 3
    provider.get_price_by_type.side_effect = lambda connector, pair, kind: {
        PriceType.BestBid: D('97.49'), PriceType.BestAsk: D('97.51'), PriceType.MidPrice: D('97.5'),
    }[kind]
    provider.quantize_order_price.side_effect = lambda connector, pair, value: value.quantize(D('.01'), rounding=ROUND_DOWN)
    provider.quantize_order_amount.side_effect = lambda connector, pair, value: value.quantize(D('.001'), rounding=ROUND_DOWN)
    provider.get_trading_rules.return_value = SimpleNamespace(min_order_size=D('.001'), min_notional_size=D('1'), min_order_value=D('0'))
    result = policy.PMMSimpleController(policy.PMMSimpleConfig(id='test', total_amount_quote='24'), provider)
    result._snapshot = ReversionSnapshot(1799999700, D('100'), D('1'), D('.4'), D('-2.5'), D('-2.5'), 1)
    result.processed_data = {'signal': 1}
    return result


def position(**kwargs):
    defaults = dict(id='position', is_active=True, is_done=False, is_trading=False, filled_amount_quote=D('0'),
                    timestamp=1800000000, config=SimpleNamespace(side=TradeType.BUY), close_timestamp=None, close_type=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize('pnl,filled,enabled,hit', [
    ('0.299999', '1', True, False), ('0.3', '1', True, True), ('0.300001', '1', True, True),
    ('-0.3', '1', True, False), ('0.4', '0', True, False), ('0.4', '1', False, False),
])
def test_cash_profit_threshold_is_inclusive_and_independent_of_mean(policy, pnl, filled, enabled, hit):
    executor = MagicMock()
    executor.config.triple_barrier_config = policy.TripleBarrierConfig(take_profit_quote=D('.3') if enabled else None)
    executor.open_filled_amount = D(filled)
    executor.trade_pnl_quote = D(pnl)
    executor.net_pnl_quote = D(pnl) - D('.05')
    executor._open_order = None  # Also exercises partial fills before entry completes.
    policy.control_barriers(executor)
    assert executor.place_close_order_and_cancel_open_orders.called is hit
    if hit:
        executor.place_close_order_and_cancel_open_orders.assert_called_once_with(close_type=policy.CloseType.TAKE_PROFIT)
        executor.control_time_limit.assert_not_called()
    else:
        executor.control_time_limit.assert_called_once()


def test_profit_is_per_position_not_summed(policy):
    for _ in range(2):
        executor = MagicMock()
        executor.config.triple_barrier_config = policy.TripleBarrierConfig(take_profit_quote=D('.3'))
        executor.open_filled_amount = D('1')
        executor.trade_pnl_quote = D('.2')
        executor.net_pnl_quote = D('.4')
        executor._open_order = None
        policy.control_barriers(executor)
        executor.place_close_order_and_cancel_open_orders.assert_not_called()


def test_cash_threshold_is_not_scaled_by_volatility(policy):
    original = policy.TripleBarrierConfig(take_profit_quote='.3', take_profit='.01')
    adjusted = original.new_instance_with_adjusted_volatility(2)
    assert adjusted.take_profit_quote == D('.3')
    assert adjusted.take_profit == D('.02')


@pytest.mark.parametrize('invalid', ['0', '-0.3', 'NaN', 'Infinity'])
def test_invalid_quote_threshold_rejected(policy, invalid):
    with pytest.raises(ValidationError):
        policy.TripleBarrierConfig(take_profit_quote=invalid)
    with pytest.raises(ValidationError):
        policy.PMMSimpleConfig(id='test', take_profit_quote=invalid)


def test_entry_has_post_only_open_cash_tp_no_trailing_or_grids(controller):
    actions = controller.create_actions_proposal()
    assert len(actions) == 1
    config = actions[0].executor_config
    assert config.side == TradeType.BUY
    assert config.amount * config.entry_price <= D('24')
    assert config.triple_barrier_config.open_order_type == OrderType.LIMIT_MAKER
    assert config.triple_barrier_config.take_profit_quote == D('.3')
    assert config.triple_barrier_config.trailing_stop is None
    assert controller.create_actions_proposal() == []  # No duplicate before executor report arrives.


def test_large_budget_is_capped_to_respect_cash_tp_risk(controller, policy):
    controller.config = policy.PMMSimpleConfig(id='test', total_amount_quote='200')
    config = controller.create_actions_proposal()[0].executor_config
    estimated_loss = config.amount * config.entry_price * (config.triple_barrier_config.stop_loss + controller.config.round_trip_cost_pct)
    assert estimated_loss * controller.config.min_reward_risk <= D('.3')


@pytest.mark.parametrize('state', ['open', 'partial', 'closing', 'held', 'exchange'])
def test_never_stack_or_reverse_over_existing_exposure(controller, state):
    if state == 'held':
        controller.positions_held = [SimpleNamespace(amount=D('1'))]
    elif state == 'exchange':
        controller.market_data_provider.get_connector.return_value.account_positions = {
            'btc': SimpleNamespace(trading_pair='BTC-USDT', amount=D('-1')),
        }
    else:
        controller.executors_info = [position(is_active=state != 'closing', is_trading=state == 'partial')]
    assert controller.create_actions_proposal() == []


@pytest.mark.parametrize('stop_loss,seconds', [(False, 300), (True, 900)])
def test_cooldown_after_exit_and_after_archival(controller, policy, stop_loss, seconds):
    now = controller.market_data_provider.time()
    controller.executors_info = [position(is_done=True, is_active=False, close_timestamp=now,
                                         close_type=policy.CloseType.STOP_LOSS if stop_loss else policy.CloseType.TAKE_PROFIT)]
    assert controller.create_actions_proposal() == []
    controller.executors_info = []
    controller.market_data_provider.time.return_value = now + seconds - 1
    assert controller.create_actions_proposal() == []
    controller.market_data_provider.time.return_value = now + seconds
    assert len(controller.create_actions_proposal()) == 1


def test_invalid_signal_cancels_only_unfilled_order(controller):
    controller.processed_data['signal'] = 0
    controller.executors_info = [position(id='unfilled'), position(id='partial', filled_amount_quote=D('1')),
                                 position(id='filled', is_trading=True, filled_amount_quote=D('24'))]
    assert [action.executor_id for action in controller.stop_actions_proposal()] == ['unfilled']


@pytest.mark.parametrize('case', ['minimum_size', 'wide_book', 'crossed_book', 'kill_switch'])
def test_untradeable_orders_are_not_submitted(controller, case):
    if case == 'minimum_size':
        controller.market_data_provider.get_trading_rules.return_value.min_order_size = D('10')
    elif case in ('wide_book', 'crossed_book'):
        controller.market_data_provider.get_price_by_type.side_effect = lambda connector, pair, kind: D('97') if kind == PriceType.BestBid else D('99' if case == 'wide_book' else '96')
    else:
        controller.config.manual_kill_switch = True
    assert controller.create_actions_proposal() == []


def test_failed_data_cannot_reuse_old_signal(controller):
    controller.market_data_provider.get_candles_df.side_effect = RuntimeError('disconnected')
    asyncio.run(controller.update_processed_data())
    assert controller._snapshot is None
    assert controller.create_actions_proposal() == []


def test_unconfirmed_leverage_blocks_entry(controller):
    controller.market_data_provider.get_connector.return_value.get_leverage.return_value = 1
    assert controller.create_actions_proposal() == []
    assert controller.processed_data['reason'] == 'leverage_not_ready'


def test_unready_account_stream_is_visible_and_blocks_entry(controller):
    provider = controller.market_data_provider
    provider.ready = False
    provider.connectors = {'okx_perpetual': SimpleNamespace(
        ready=False, status_dict={'account_balance': True, 'user_stream_initialized': False})}
    provider.candles_feeds = {}
    asyncio.run(controller.update_processed_data())
    assert controller.processed_data['reason'] == 'waiting_for_data: okx_perpetual[user_stream_initialized]'
    assert controller._snapshot is None
    assert controller.create_actions_proposal() == []


def test_unready_shared_candle_feed_shows_warmup_progress(controller):
    provider = controller.market_data_provider
    provider.ready = False
    provider.connectors = {'okx_perpetual': SimpleNamespace(ready=True)}
    provider.candles_feeds = {'okx_perpetual_AAPL-USDT_5m': SimpleNamespace(
        ready=False, candles_df=[None] * 12, max_records=50)}
    asyncio.run(controller.update_processed_data())
    assert 'okx_perpetual_AAPL-USDT_5m[candles=12/50]' in controller.to_format_status()[0]
    assert controller.create_actions_proposal() == []


def test_every_local_config_loads_and_only_uses_new_parameters(policy):
    paths = list((ROOT / 'strategy_configs/okx_mean_reversion').glob('conf_okx_pmm*.yml'))
    assert len(paths) == 17
    for path in paths:
        config = policy.PMMSimpleConfig(**yaml.safe_load(path.read_text()))
        assert config.take_profit_quote > 0
        assert config.leverage <= 5
        assert not hasattr(config, 'enable_trend_filter')


def test_active_configs_use_scaled_notional_and_cash_take_profit(policy):
    script = yaml.safe_load((ROOT / 'conf/scripts/conf_okx_multi.yml').read_text())
    expected = {
        'conf_okx_pmm_btc.yml': D('320'),
        'conf_okx_pmm_eth.yml': D('320'),
        'conf_okx_pmm_sol.yml': D('200'),
        'conf_okx_pmm_xrp.yml': D('200'),
        'conf_okx_pmm_doge.yml': D('200'),
        'conf_okx_pmm_sui.yml': D('200'),
        'conf_okx_pmm_sndk.yml': D('200'),
        'conf_okx_pmm_zec.yml': D('200'),
        'conf_okx_pmm_okb.yml': D('200'),
        'conf_okx_pmm_ada.yml': D('200'),
    }
    assert set(script['controllers_config']) == set(expected)
    for name, amount in expected.items():
        runtime = policy.PMMSimpleConfig(**yaml.safe_load((ROOT / 'conf/controllers' / name).read_text()))
        template = policy.PMMSimpleConfig(**yaml.safe_load(
            (ROOT / 'strategy_configs/okx_mean_reversion' / name).read_text()))
        assert runtime.total_amount_quote == template.total_amount_quote == amount
        assert runtime.take_profit_quote == template.take_profit_quote == D('2')


def test_dashboard_close_stops_the_open_executor(controller, tmp_path, monkeypatch):
    monkeypatch.setenv("OKX_TRADER_ROOT", str(tmp_path))
    request = tmp_path / "data" / "dashboard" / "close_requests" / "test"
    request.parent.mkdir(parents=True)
    request.write_text("1\n")
    controller.executors_info = [position(
        id="live", is_active=True, is_done=False, is_trading=True, filled_amount_quote=D("10"),
    )]
    actions = controller.manual_close_actions()
    assert len(actions) == 1
    assert actions[0].executor_id == "live"
    assert actions[0].keep_position is False
    assert not request.exists()
    connector = controller.market_data_provider.get_connector.return_value
    connector.buy.assert_not_called()
    connector.sell.assert_not_called()
    assert controller.determine_executor_actions() == []


def test_dashboard_close_market_flattens_an_untracked_short(controller, tmp_path, monkeypatch):
    monkeypatch.setenv("OKX_TRADER_ROOT", str(tmp_path))
    request = tmp_path / "data" / "dashboard" / "close_requests" / "test"
    request.parent.mkdir(parents=True)
    request.write_text("1\n")
    connector = controller.market_data_provider.get_connector.return_value
    connector.account_positions = {"btc": SimpleNamespace(trading_pair="BTC-USDT", amount=D("-1.25"))}
    assert controller.manual_close_actions() == []
    connector.buy.assert_called_once()
    assert connector.buy.call_args.kwargs["amount"] == D("1.250")
    connector.sell.assert_not_called()


def test_unfilled_order_timeout_allows_requote_without_full_cooldown(controller, policy):
    now = controller.market_data_provider.time()
    actions = controller.create_actions_proposal()
    assert len(actions) == 1
    closed_executor = position(
        id='unfilled_order', is_active=False, is_done=True, is_trading=False,
        filled_amount_quote=D('0'), timestamp=now, close_timestamp=now + 60,
        close_type=policy.CloseType.EARLY_STOP,
    )
    controller.executors_info = [closed_executor]
    controller.market_data_provider.time.return_value = now + 62
    assert controller.create_actions_proposal() == []
    controller.market_data_provider.time.return_value = now + 65
    new_actions = controller.create_actions_proposal()
    assert len(new_actions) == 1


def test_fixed_unrealized_tp_quote_must_be_greater_than_one(policy):
    valid_data = yaml.safe_load((ROOT / 'conf/controllers/conf_okx_pmm_btc.yml').read_text())
    valid_data["fixed_unrealized_tp_quote"] = D("1.01")
    cfg = policy.PMMSimpleConfig(**valid_data)
    assert cfg.fixed_unrealized_tp_quote == D("1.01")

    # Boundary 1.0 must fail
    with pytest.raises(ValidationError):
        valid_data["fixed_unrealized_tp_quote"] = D("1.0")
        policy.PMMSimpleConfig(**valid_data)

    # Values < 1 must fail
    with pytest.raises(ValidationError):
        valid_data["fixed_unrealized_tp_quote"] = D("0.5")
        policy.PMMSimpleConfig(**valid_data)


def test_fixed_unrealized_tp_actions_triggers_when_threshold_exceeded(controller, policy):
    controller.config.fixed_unrealized_tp_quote = D("2.5")
    # Case 1: no active position, unrealized = 0 -> returns None
    assert controller.fixed_unrealized_tp_actions() is None

    # Case 2: active executor with net_pnl_quote = 2.0 (<= 2.5) -> returns None
    active_exec = position(
        id="pos1", is_active=True, is_done=False, is_trading=True,
        filled_amount_quote=D("100"), net_pnl_quote=D("2.0")
    )
    controller.executors_info = [active_exec]
    assert controller.get_current_unrealized_pnl() == D("2.0")
    assert controller.fixed_unrealized_tp_actions() is None

    # Case 3: active executor with net_pnl_quote = 2.51 (> 2.5) -> immediate StopExecutorAction
    active_exec.net_pnl_quote = D("2.51")
    assert controller.get_current_unrealized_pnl() == D("2.51")
    actions = controller.fixed_unrealized_tp_actions()
    assert len(actions) == 1
    assert actions[0].executor_id == "pos1"
    assert actions[0].keep_position is False

    # Case 4: position tracked directly on exchange connector without executor
    controller.executors_info = []
    connector = controller.market_data_provider.get_connector.return_value
    connector.account_positions = {
        "btc": SimpleNamespace(trading_pair="BTC-USDT", amount=D("0.5"), unrealized_pnl=D("3.0"))
    }
    assert controller.get_current_unrealized_pnl() == D("3.0")
    actions = controller.fixed_unrealized_tp_actions()
    assert actions == []
    connector.sell.assert_called_once()
    assert connector.sell.call_args.kwargs["amount"] == D("0.500")


def test_fixed_unrealized_tp_quote_does_not_affect_entry_proposal(controller, policy):
    # Ensure entry proposal actions and order sizing are identical regardless of fixed_unrealized_tp_quote
    controller.config.fixed_unrealized_tp_quote = None
    actions_none = controller.create_actions_proposal()
    assert len(actions_none) == 1
    amount_none = actions_none[0].executor_config.amount

    # Reset entry candle marker so controller can evaluate proposal again
    controller._last_entry_candle = None
    controller.config.fixed_unrealized_tp_quote = D("10.0")
    actions_with_tp = controller.create_actions_proposal()
    assert len(actions_with_tp) == 1
    assert actions_with_tp[0].executor_config.amount == amount_none
    assert actions_with_tp[0].executor_config.side == actions_none[0].executor_config.side


