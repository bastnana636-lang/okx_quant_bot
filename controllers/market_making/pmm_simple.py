"""Single-position mean reversion, retaining the existing pmm_simple entry point."""

from decimal import Decimal
from typing import Literal

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
        # Cancel stale entry orders even when the candle feed is unavailable.
        # Filled positions have independent executor-level profit/loss checks.
        if self.executors_update_event.is_set():
            await self.update_processed_data()
            actions = self.determine_executor_actions()
            if actions:
                await self.send_actions(actions)

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
