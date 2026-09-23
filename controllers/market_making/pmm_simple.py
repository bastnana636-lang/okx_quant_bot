"""Single-position mean reversion, retaining the existing pmm_simple entry point."""

from decimal import Decimal
from typing import Literal, Optional

from pydantic import Field, field_validator

from controllers.market_making.mean_reversion import MeanReversionSettings, calculate_barriers, calculate_snapshot
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.utils.common import parse_enum_value


class PMMSimpleConfig(ControllerConfigBase, MeanReversionSettings):
    controller_name: Literal["pmm_simple"] = "pmm_simple"
    controller_type: Literal["market_making"] = "market_making"
    connector_name: Literal["okx_perpetual"] = "okx_perpetual"
    trading_pair: str = Field(default="BTC-USDT", pattern=r"^[A-Z0-9]+-USDT$")
    total_amount_quote: Decimal = Field(default=Decimal("50"), gt=0)
    leverage: int = Field(default=3, ge=1, le=5)
    position_mode: PositionMode = PositionMode.ONEWAY
    take_profit_quote: Decimal = Field(default=Decimal("0.3"), gt=0)
    fixed_unrealized_tp_quote: Optional[Decimal] = Field(default=None, gt=1)

    @field_validator("fixed_unrealized_tp_quote", mode="before")
    @classmethod
    def validate_fixed_unrealized_tp(cls, value):
        if value is None or value == "" or value == "null" or value == "None":
            return None
        val = Decimal(str(value))
        if val <= 1:
            raise ValueError("fixed_unrealized_tp_quote must be greater than 1")
        return val

    @field_validator("position_mode", mode="before")
    @classmethod
    def validate_position_mode(cls, value):
        return parse_enum_value(PositionMode, value, "position_mode")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class PMMSimpleController(ControllerBase):
    def __init__(self, config: PMMSimpleConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self._snapshot = None
        self._last_entry_candle = None
        self._cooldown_until = 0.0
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

    def get_candles_config(self):
        return [CandlesConfig(
            connector=self.config.connector_name, trading_pair=self.config.trading_pair,
            interval=self.config.candles_interval, max_records=self.config.candle_records,
        )]

    async def control_task(self):
        # A dashboard close is handled even when the candle feed is down.
        close_actions = self.manual_close_actions()
        if close_actions is not None:
            if close_actions:
                await self.send_actions(close_actions)
            return

        # Fixed unrealized take-profit check:
        # Closes immediately when UNREALIZED (USDT) in PERFORMANCE MATRIX > fixed_unrealized_tp_quote (> 1)
        fixed_tp_actions = self.fixed_unrealized_tp_actions()
        if fixed_tp_actions is not None:
            if fixed_tp_actions:
                await self.send_actions(fixed_tp_actions)
            return

        # Cancel stale entry orders even when the candle feed is unavailable.
        # Filled positions have independent executor-level profit/loss checks.
        if self.executors_update_event.is_set():
            await self.update_processed_data()
            actions = self.determine_executor_actions()
            if actions:
                await self.send_actions(actions)

    def _close_request_path(self):
        import os
        import re
        from pathlib import Path

        controller_id = str(self.config.id)
        if not re.fullmatch(r"[A-Za-z0-9_]+", controller_id):
            return None
        root = Path(os.environ.get("OKX_TRADER_ROOT") or Path.cwd())
        return root / "data" / "dashboard" / "close_requests" / controller_id

    def manual_close_actions(self):
        """Consume one dashboard close request and flatten this controller."""
        path = self._close_request_path()
        if path is None or not path.is_file():
            return None
        try:
            path.unlink()
        except OSError:
            return None
        self.processed_data["reason"] = "manual_close"
        now = self.market_data_provider.time()
        self._cooldown_until = max(self._cooldown_until, now + float(self.config.cooldown_time))
        actions = []
        owns_position = False
        for executor in self.executors_info:
            if not executor.is_done:
                owns_position = True
            if executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=executor.id,
                    keep_position=False,
                ))
        # An active executor market-closes its own fill. A second order would reverse it.
        if not owns_position:
            self._market_close_exchange_position()
        return actions

    def get_current_unrealized_pnl(self) -> Decimal:
        """Get current unrealized PnL matching the UNREALIZED (USDT) column in PERFORMANCE MATRIX."""
        if hasattr(self, "performance_report") and self.performance_report:
            val = getattr(self.performance_report, "unrealized_pnl_quote", None)
            if val is not None:
                return Decimal(str(val))
        total = Decimal("0")
        for ex in getattr(self, "executors_info", []):
            if not ex.is_done and hasattr(ex, "net_pnl_quote"):
                total += Decimal(str(ex.net_pnl_quote))
        for pos in getattr(self, "positions_held", []):
            if hasattr(pos, "unrealized_pnl_quote"):
                total += Decimal(str(pos.unrealized_pnl_quote))
        if total == Decimal("0") and hasattr(self, "market_data_provider"):
            try:
                connector = self.market_data_provider.get_connector(self.config.connector_name)
                for pos in getattr(connector, "account_positions", {}).values():
                    if pos.trading_pair == self.config.trading_pair and getattr(pos, "unrealized_pnl", None) is not None:
                        total += Decimal(str(pos.unrealized_pnl))
            except Exception:
                pass
        return total

    def fixed_unrealized_tp_actions(self):
        """If UNREALIZED (USDT) in PERFORMANCE MATRIX > fixed_unrealized_tp_quote (> 1),
        close position immediately without needing any other conditions."""
        target = getattr(self.config, "fixed_unrealized_tp_quote", None)
        if target is None:
            return None
        try:
            target = Decimal(str(target))
        except (ValueError, TypeError, ArithmeticError):
            return None
        if target <= 1:
            return None

        current_unrealized = self.get_current_unrealized_pnl()
        if current_unrealized <= target:
            return None

        has_active_executor = any(
            ex.is_active and (ex.is_trading or ex.filled_amount_quote > 0)
            for ex in getattr(self, "executors_info", [])
        )
        has_position = any(p.amount != 0 for p in getattr(self, "positions_held", []))
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        has_exchange_pos = any(
            pos.trading_pair == self.config.trading_pair and pos.amount != 0
            for pos in getattr(connector, "account_positions", {}).values()
        )
        if not (has_active_executor or has_position or has_exchange_pos):
            return None

        self.logger().info(
            f"[{self.config.trading_pair}] Fixed unrealized TP triggered: "
            f"unrealized {current_unrealized:.4f} > target {target:.4f} USDT. "
            f"Immediately closing position."
        )
        self.processed_data["reason"] = f"fixed_unrealized_tp({current_unrealized:.2f}>{target:.2f})"
        now = self.market_data_provider.time()
        self._cooldown_until = max(self._cooldown_until, now + float(self.config.cooldown_time))
        actions = []
        owns_position = False
        for executor in self.executors_info:
            if not executor.is_done:
                owns_position = True
            if executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=executor.id,
                    keep_position=False,
                ))
        if not owns_position:
            self._market_close_exchange_position()
        return actions

    def _market_close_exchange_position(self):
        from decimal import Decimal

        from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType

        connector = self.market_data_provider.get_connector(self.config.connector_name)
        price = Decimal(str(self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)))
        rules = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
        min_notional = max(rules.min_notional_size, getattr(rules, "min_order_value", Decimal("0")))
        for position in connector.account_positions.values():
            if position.trading_pair != self.config.trading_pair or position.amount == 0:
                continue
            amount = self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, abs(Decimal(str(position.amount))))
            if amount <= 0 or amount < rules.min_order_size or amount * price < min_notional:
                self.processed_data["reason"] = "manual_close_below_minimum"
                continue
            order = dict(
                trading_pair=self.config.trading_pair, amount=amount,
                order_type=OrderType.MARKET, price=Decimal("NaN"),
                position_action=PositionAction.OPEN,
            )
            if position.amount > 0:
                connector.sell(**order)
            else:
                connector.buy(**order)
            self.logger().info(f"Manual close {self.config.trading_pair} amount={amount}")

    async def update_processed_data(self):
        self._snapshot = None
        self.processed_data = {"strategy": "mean_reversion", "signal": 0, "reason": "waiting_for_data"}
        try:
            if not self.market_data_provider.ready:
                self.processed_data["reason"] = self._data_readiness_reason()
                return
            mid = Decimal(str(self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)))
            candles = self.market_data_provider.get_candles_df(
                connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                interval=self.config.candles_interval, max_records=self.config.candle_records,
            )
            self._snapshot = calculate_snapshot(candles, self.market_data_provider.time(), mid, self.config)
            snapshot = self._snapshot
            self.processed_data.update({
                "signal": snapshot.signal, "reference_price": mid, "mean": snapshot.mean,
                "std": snapshot.std, "atr": snapshot.atr, "z_score": snapshot.z_score,
                "closed_z_score": snapshot.closed_z_score, "candle_timestamp": snapshot.candle_timestamp,
                "reason": "entry_candidate" if snapshot.signal else "outside_entry_band",
            })
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            self.processed_data["reason"] = str(error)
        except Exception:
            self.processed_data["reason"] = "market_data_error"
            self.logger().warning("Mean-reversion data unavailable; new entries suspended.", exc_info=True)

    def _data_readiness_reason(self):
        # Keep the shared readiness gate, but expose exactly which dependency blocks it.
        pending = []
        for name, connector in self.market_data_provider.connectors.items():
            if not connector.ready:
                missing = ",".join(key for key, ready in connector.status_dict.items() if not ready)
                pending.append(f"{name}[{missing or 'not_ready'}]")
        for name, feed in self.market_data_provider.candles_feeds.items():
            if not feed.ready:
                pending.append(f"{name}[candles={len(feed.candles_df)}/{feed.max_records}]")
        return "waiting_for_data" + (": " + "; ".join(pending) if pending else "")

    def determine_executor_actions(self):
        stops = self.stop_actions_proposal()
        return stops if stops else self.create_actions_proposal()

    def stop_actions_proposal(self):
        now = self.market_data_provider.time()
        signal = self.processed_data.get("signal", 0)
        stops = []
        for executor in self.executors_info:
            if not executor.is_active:
                continue
            # Do not cancel partially filled exposure as if it were an empty order.
            if executor.is_trading or executor.filled_amount_quote > 0:
                continue
            side = 1 if executor.config.side == TradeType.BUY else -1
            if signal != side or now - executor.timestamp >= self.config.executor_refresh_time:
                stops.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))
        return stops

    def _can_enter(self):
        now = self.market_data_provider.time()
        for executor in self.executors_info:
            # A shutting-down executor can still hold exposure. Wait until it is terminated.
            if not executor.is_done:
                self.processed_data["reason"] = "position_or_order_in_progress"
                return False
            if executor.close_timestamp is not None:
                is_filled_or_position = (
                    executor.filled_amount_quote > 0
                    or executor.close_type in (CloseType.STOP_LOSS, CloseType.TAKE_PROFIT, CloseType.TIME_LIMIT)
                )
                if is_filled_or_position:
                    cooldown = (self.config.stop_loss_cooldown_time if executor.close_type == CloseType.STOP_LOSS
                                else self.config.cooldown_time)
                    self._cooldown_until = max(self._cooldown_until, executor.close_timestamp + cooldown)
                else:
                    # An unfilled order was stopped/cancelled without filling any volume.
                    # Reset _last_entry_candle after 5s grace period so we don't lock out the candle
                    # if the signal is still valid.
                    if self._snapshot and self._snapshot.candle_timestamp == self._last_entry_candle:
                        if now >= executor.close_timestamp + 5:
                            self._last_entry_candle = None
        if any(position.amount != 0 for position in self.positions_held):
            self.processed_data["reason"] = "held_position_present"
            return False
        # Block untracked/manual exchange exposure after a process restart, too.
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        if connector.get_leverage(self.config.trading_pair) != self.config.leverage:
            self.processed_data["reason"] = "leverage_not_ready"
            return False
        if any(position.trading_pair == self.config.trading_pair and position.amount != 0
               for position in connector.account_positions.values()):
            self.processed_data["reason"] = "exchange_position_present"
            return False
        if now < self._cooldown_until:
            self.processed_data["reason"] = "cooldown"
            return False
        if self._snapshot.candle_timestamp == self._last_entry_candle:
            self.processed_data["reason"] = "already_attempted_this_candle"
            return False
        return True

    def create_actions_proposal(self):
        if self.config.manual_kill_switch or self._snapshot is None or not self._snapshot.signal:
            return []
        if not self._can_enter():
            return []
        config = self.config
        provider = self.market_data_provider
        try:
            bid = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestBid)))
            ask = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestAsk)))
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask:
                raise ValueError("Invalid order book")
            mid = (bid + ask) / 2
            if (ask - bid) / mid > config.max_bid_ask_spread_pct:
                raise ValueError("Bid/ask spread too wide")
            is_buy = self._snapshot.signal == 1
            raw_price = (min(bid, mid * (1 - config.quote_offset_pct)) if is_buy
                         else max(ask, mid * (1 + config.quote_offset_pct)))
            price = provider.quantize_order_price(config.connector_name, config.trading_pair, raw_price)
            if price <= 0 or (is_buy and price >= ask) or (not is_buy and price <= bid):
                raise ValueError("Quantized price would cross the book")
            barriers = calculate_barriers(self._snapshot, price, config)
            # The cash profit cap must not silently make the trade's reward/risk worse.
            # Budget is total NOTIONAL, never multiplied by leverage or split across grids.
            risk_budget = config.take_profit_quote / (
                config.min_reward_risk * (barriers.stop_loss + config.round_trip_cost_pct)
            )
            notional = min(config.total_amount_quote, risk_budget)
            amount = provider.quantize_order_amount(config.connector_name, config.trading_pair,
                                                    notional / price)
            rules = provider.get_trading_rules(config.connector_name, config.trading_pair)
            min_notional = max(rules.min_notional_size, rules.min_order_value)
            if amount <= 0 or amount < rules.min_order_size or amount * price < min_notional:
                raise ValueError("Budget below exchange minimum order size")
            if amount * price > notional:
                raise ValueError("Quantized order exceeds notional budget")
        except (ValueError, ArithmeticError) as error:
            self.processed_data["reason"] = str(error)
            return []
        executor_config = PositionExecutorConfig(
            timestamp=provider.time(), level_id="buy_0" if is_buy else "sell_0",
            connector_name=config.connector_name, trading_pair=config.trading_pair,
            entry_price=price, amount=amount, leverage=config.leverage,
            side=TradeType.BUY if is_buy else TradeType.SELL,
            triple_barrier_config=TripleBarrierConfig(
                take_profit=barriers.take_profit, stop_loss=barriers.stop_loss,
                take_profit_quote=config.take_profit_quote,
                time_limit=config.time_limit, trailing_stop=None,
                open_order_type=OrderType.LIMIT_MAKER, take_profit_order_type=OrderType.LIMIT,
                stop_loss_order_type=OrderType.MARKET, time_limit_order_type=OrderType.MARKET,
            ),
        )
        self._last_entry_candle = self._snapshot.candle_timestamp
        self.processed_data.update({"reason": "entry_submitted", "target_price": barriers.target_price,
                                    "stop_loss": barriers.stop_loss})
        return [CreateExecutorAction(controller_id=config.id, executor_config=executor_config)]

    def get_custom_info(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in self.processed_data.items()}

    def to_format_status(self):
        return [f"Mean reversion | z={self.processed_data.get('z_score', 'n/a')} | "
                f"{self.processed_data.get('reason', 'waiting_for_data')}"]
