"""Single-position mean reversion, retaining the existing pmm_simple entry point."""

from decimal import Decimal, InvalidOperation
from typing import Literal, Optional, Tuple

from pydantic import Field, field_validator

from controllers.market_making.mean_reversion import MeanReversionSettings, calculate_barriers, calculate_snapshot
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TrailingStop,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.utils.common import parse_enum_value


class PMMSimpleConfig(ControllerConfigBase, MeanReversionSettings):
    controller_name: Literal["pmm_simple"] = "pmm_simple"
    controller_type: Literal["market_making"] = "market_making"
    connector_name: Literal["okx_perpetual"] = "okx_perpetual"
    trading_pair: str = Field(default="BTC-USDT", pattern=r"^[A-Z0-9]+-USDT$")
    total_amount_quote: Decimal = Field(default=Decimal("50"), gt=0)
    leverage: int = Field(default=3, ge=1, le=20)
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
        if self.config.signal_mode == "human_swing":
            configs = [
                CandlesConfig(connector=self.config.connector_name, trading_pair=self.config.trading_pair,
                              interval="5m", max_records=310),
                CandlesConfig(connector=self.config.connector_name, trading_pair=self.config.trading_pair,
                              interval="15m", max_records=max(self.config.atr_length + 10, 32)),
                CandlesConfig(connector=self.config.connector_name, trading_pair=self.config.trading_pair,
                              interval="1h", max_records=40),
                CandlesConfig(connector=self.config.connector_name, trading_pair=self.config.trading_pair,
                              interval="4h", max_records=72),
            ]
            if self.config.require_btc_4h and self.config.trading_pair != "BTC-USDT":
                configs.extend([
                    CandlesConfig(connector=self.config.connector_name, trading_pair="BTC-USDT",
                                  interval="1h", max_records=40),
                    CandlesConfig(connector=self.config.connector_name, trading_pair="BTC-USDT",
                                  interval="4h", max_records=72),
                ])
            return configs
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

        # A cash target would flatten a trend early. Momentum / human_swing exit via barriers/trail.
        fixed_tp_actions = (
            None if self.config.signal_mode in ("momentum", "human_swing")
            else self.fixed_unrealized_tp_actions()
        )
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
            f"Immediately placing limit close order to lock in target profit."
        )
        self.processed_data["reason"] = f"fixed_unrealized_tp({current_unrealized:.2f}>{target:.2f})"
        now = self.market_data_provider.time()
        self._cooldown_until = max(self._cooldown_until, now + float(self.config.cooldown_time))

        # Collect client_order_ids of active close orders from executors so we don't cancel them
        active_close_order_ids = set()
        for executor in self.executors_info:
            if not executor.is_done:
                close_order = getattr(executor, "_close_order", None)
                if close_order and getattr(close_order, "order_id", None):
                    active_close_order_ids.add(close_order.order_id)
                tp_order = getattr(executor, "_take_profit_limit_order", None)
                if tp_order and getattr(tp_order, "order_id", None):
                    active_close_order_ids.add(tp_order.order_id)

        # Cancel hanging entry orders for this trading pair on the connector, preserving close orders
        if hasattr(connector, "in_flight_orders"):
            current_position = None
            if hasattr(connector, "account_positions") and connector.account_positions:
                for pos in connector.account_positions.values():
                    if getattr(pos, "trading_pair", None) == self.config.trading_pair:
                        current_position = pos
                        break
            pos_amount = getattr(current_position, "amount", Decimal("0"))
            for order in list(connector.in_flight_orders.values()):
                if getattr(order, "trading_pair", None) == self.config.trading_pair and not getattr(order, "is_done", True):
                    order_id = getattr(order, "client_order_id", None)
                    if order_id in active_close_order_ids:
                        continue
                    is_closing = False
                    if getattr(order, "position_action", None) == PositionAction.CLOSE:
                        is_closing = True
                    elif pos_amount > 0 and getattr(order, "trade_type", None) == TradeType.SELL:
                        is_closing = True
                    elif pos_amount < 0 and getattr(order, "trade_type", None) == TradeType.BUY:
                        is_closing = True
                    if not is_closing:
                        try:
                            connector.cancel(self.config.trading_pair, order.client_order_id)
                        except Exception:
                            pass

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
            self._limit_close_exchange_position(target)
        return actions

    def _limit_close_exchange_position(self, target_quote: Decimal):
        from decimal import Decimal

        from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType

        connector = self.market_data_provider.get_connector(self.config.connector_name)
        rules = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
        min_notional = max(rules.min_notional_size, getattr(rules, "min_order_value", Decimal("0")))
        for position in connector.account_positions.values():
            if position.trading_pair != self.config.trading_pair or position.amount == 0:
                continue
            pos_amount = Decimal(str(position.amount))
            amount = self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, abs(pos_amount))
            entry_price = getattr(position, "entry_price", None)
            if entry_price is None or entry_price <= 0:
                unrealized = getattr(position, "unrealized_pnl", Decimal("0"))
                mid = Decimal(str(self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)))
                if pos_amount > 0:
                    entry_price = mid - (Decimal(str(unrealized)) / abs(pos_amount))
                else:
                    entry_price = mid + (Decimal(str(unrealized)) / abs(pos_amount))
            else:
                entry_price = Decimal(str(entry_price))

            if pos_amount > 0:
                raw_close_price = entry_price + (target_quote / amount)
            else:
                raw_close_price = entry_price - (target_quote / amount)

            close_price = self.market_data_provider.quantize_order_price(
                self.config.connector_name, self.config.trading_pair, raw_close_price)

            if amount <= 0 or amount < rules.min_order_size or amount * close_price < min_notional:
                self.processed_data["reason"] = "manual_close_below_minimum"
                continue

            order = dict(
                trading_pair=self.config.trading_pair, amount=amount,
                order_type=OrderType.LIMIT, price=close_price,
                position_action=PositionAction.OPEN,
            )
            if pos_amount > 0:
                connector.sell(**order)
            else:
                connector.buy(**order)
            self.logger().info(f"Limit close {self.config.trading_pair} amount={amount} price={close_price} target={target_quote}")

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
        self.processed_data = {"strategy": self.config.signal_mode, "signal": 0, "reason": "waiting_for_data"}
        try:
            if not self.market_data_provider.ready:
                self.processed_data["reason"] = self._data_readiness_reason()
                return
            mid = Decimal(str(self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)))
            if self.config.signal_mode == "human_swing":
                from controllers.market_making.human_swing import human_swing_snapshot

                c5 = self.market_data_provider.get_candles_df(
                    connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                    interval="5m", max_records=310)
                c15 = self.market_data_provider.get_candles_df(
                    connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                    interval="15m", max_records=max(self.config.atr_length + 10, 32))
                c1h = self.market_data_provider.get_candles_df(
                    connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                    interval="1h", max_records=40)
                c4h = self.market_data_provider.get_candles_df(
                    connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                    interval="4h", max_records=72)
                btc4h = None
                if self.config.require_btc_4h:
                    btc4h = (c4h if self.config.trading_pair == "BTC-USDT"
                             else self.market_data_provider.get_candles_df(
                                 connector_name=self.config.connector_name, trading_pair="BTC-USDT",
                                 interval="4h", max_records=72))
                peer_feeds = self._collect_peer_regime_feeds()
                self._snapshot = human_swing_snapshot(
                    c5, c15, c1h, c4h, self.market_data_provider.time(), mid, self.config,
                    btc_candles_4h=btc4h, peer_feeds=peer_feeds,
                )
                snap = self._snapshot
                self.processed_data.update({
                    "signal": snap.signal, "reference_price": mid, "rsi9": snap.rsi9,
                    "atr": snap.atr15, "stop_pct": snap.stop_pct, "er4h": snap.er4h,
                    "regime_dir": snap.regime_dir, "candle_timestamp": snap.candle_timestamp,
                    "reason": snap.reason,
                })
                return
            candles = self.market_data_provider.get_candles_df(
                connector_name=self.config.connector_name, trading_pair=self.config.trading_pair,
                interval=self.config.candles_interval, max_records=self.config.candle_records,
            )
            if self.config.signal_mode == "momentum":
                from controllers.market_making.momentum import momentum_snapshot
                self._snapshot = momentum_snapshot(candles, self.market_data_provider.time(), mid, self.config)
                snapshot = self._snapshot
                self.processed_data.update({
                    "signal": snapshot.signal, "reference_price": mid, "momentum": snapshot.momentum,
                    "atr": snapshot.atr, "stop_pct": snapshot.stop_pct,
                    "candle_timestamp": snapshot.candle_timestamp,
                    "reason": "entry_candidate" if snapshot.signal else "momentum_below_threshold",
                })
                return
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

    def _collect_peer_regime_feeds(self):
        """Collect 1h/4h candle frames across all initialized feeds for cross-sectional breadth."""
        feeds = getattr(self.market_data_provider, "candles_feeds", {}) or {}
        prefix = f"{self.config.connector_name}_"
        by_pair = {}
        for key, feed in feeds.items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if rest.endswith("_1h"):
                pair = rest[:-3]
                df = getattr(feed, "candles_df", None)
                if df is not None and len(df) > 0:
                    by_pair.setdefault(pair, {})["1h"] = df
            elif rest.endswith("_4h"):
                pair = rest[:-3]
                df = getattr(feed, "candles_df", None)
                if df is not None and len(df) > 0:
                    by_pair.setdefault(pair, {})["4h"] = df
        return {
            pair: (pair_map["1h"], pair_map["4h"])
            for pair, pair_map in by_pair.items()
            if "1h" in pair_map and "4h" in pair_map
        }

    @staticmethod
    def _read_decimal_balance(connector, method_name: str, asset: str = "USDT") -> Optional[Decimal]:
        getter = getattr(connector, method_name, None)
        if not callable(getter):
            return None
        try:
            raw = getter(asset)
        except Exception:
            return None
        if isinstance(raw, (int, float, Decimal, str)) and not isinstance(raw, bool):
            try:
                val = Decimal(str(raw))
                if val.is_finite():
                    return val
            except (InvalidOperation, ValueError):
                return None
        return None

    def _resolve_equity_and_margin_cap(self, connector, leverage: int) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (usable_equity, max_available_notional) accounting for occupied margin."""
        total = self._read_decimal_balance(connector, "get_balance")
        avail = self._read_decimal_balance(connector, "get_available_balance")
        if avail is not None:
            if avail <= 0:
                raise ValueError("Insufficient available USDT margin")
            usable = min(total, avail) if (total is not None and total > 0) else avail
            max_avail_notional = avail * Decimal("0.90") * Decimal(leverage)
            return usable, max_avail_notional
        if total is not None:
            if total <= 0:
                raise ValueError("Insufficient USDT balance")
            return total, None
        return None, None

    def create_actions_proposal(self):
        if self.config.signal_mode == "human_swing":
            return self._human_swing_entry()
        if self.config.signal_mode == "momentum":
            return self._momentum_entry()
        if self.config.manual_kill_switch or self._snapshot is None or not self._snapshot.signal:
            return []
        if not self._can_enter():
            return []
        config = self.config
        provider = self.market_data_provider
        try:
            connector = provider.get_connector(config.connector_name)
            _, max_avail_notional = self._resolve_equity_and_margin_cap(connector, config.leverage)
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
            if max_avail_notional is not None:
                notional = min(notional, max_avail_notional)
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

    def _momentum_entry(self):
        if self.config.manual_kill_switch or self._snapshot is None or self._snapshot.signal != 1:
            return []
        if not self._can_enter():
            return []
        from controllers.market_making.momentum import momentum_notional

        config = self.config
        provider = self.market_data_provider
        try:
            connector = provider.get_connector(config.connector_name)
            _, max_avail_notional = self._resolve_equity_and_margin_cap(connector, config.leverage)
            bid = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestBid)))
            ask = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestAsk)))
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask:
                raise ValueError("Invalid order book")
            mid = (bid + ask) / 2
            if (ask - bid) / mid > config.max_bid_ask_spread_pct:
                raise ValueError("Bid/ask spread too wide")
            stop_pct = self._snapshot.stop_pct
            notional = momentum_notional(config.total_amount_quote, config.risk_frac, stop_pct)
            if max_avail_notional is not None:
                notional = min(notional, max_avail_notional)
            amount = provider.quantize_order_amount(config.connector_name, config.trading_pair, notional / ask)
            rules = provider.get_trading_rules(config.connector_name, config.trading_pair)
            min_notional = max(rules.min_notional_size, rules.min_order_value)
            if amount <= 0 or amount < rules.min_order_size or amount * ask < min_notional:
                raise ValueError("Budget below exchange minimum order size")
            if amount * ask > notional:
                raise ValueError("Quantized order exceeds notional budget")
        except (ValueError, ArithmeticError) as error:
            self.processed_data["reason"] = str(error)
            return []
        executor_config = PositionExecutorConfig(
            timestamp=provider.time(), level_id="buy_0",
            connector_name=config.connector_name, trading_pair=config.trading_pair,
            entry_price=ask, amount=amount, leverage=config.leverage,
            side=TradeType.BUY,
            triple_barrier_config=TripleBarrierConfig(
                take_profit=None, stop_loss=stop_pct, take_profit_quote=None,
                time_limit=config.time_limit,
                trailing_stop=TrailingStop(activation_price=Decimal("0.001"), trailing_delta=stop_pct),
                open_order_type=OrderType.MARKET, take_profit_order_type=OrderType.MARKET,
                stop_loss_order_type=OrderType.MARKET, time_limit_order_type=OrderType.MARKET,
            ),
        )
        self._last_entry_candle = self._snapshot.candle_timestamp
        self.processed_data.update({"reason": "entry_submitted", "stop_loss": stop_pct, "notional": notional})
        return [CreateExecutorAction(controller_id=config.id, executor_config=executor_config)]

    def _human_swing_entry(self):
        if self.config.manual_kill_switch or self._snapshot is None or not self._snapshot.signal:
            return []
        if not self._can_enter():
            return []
        from controllers.market_making.human_swing import human_swing_notional

        config = self.config
        provider = self.market_data_provider
        connector = provider.get_connector(config.connector_name)
        is_buy = self._snapshot.signal == 1
        if is_buy is False and config.long_only:
            return []

        # Enforce global concurrent position limit across account positions + active orders
        open_pairs = {
            getattr(p, "trading_pair", "")
            for p in getattr(connector, "account_positions", {}).values()
            if getattr(p, "amount", Decimal("0")) != 0
        }
        for order in getattr(connector, "in_flight_orders", {}).values():
            if not getattr(order, "is_done", True):
                open_pairs.add(getattr(order, "trading_pair", ""))
        if len(open_pairs - {config.trading_pair}) >= config.max_positions:
            self.processed_data["reason"] = "max_concurrent_positions"
            return []

        try:
            usable_equity, max_avail_notional = self._resolve_equity_and_margin_cap(connector, config.leverage)
            bid = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestBid)))
            ask = Decimal(str(provider.get_price_by_type(config.connector_name, config.trading_pair, PriceType.BestAsk)))
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask:
                raise ValueError("Invalid order book")
            mid = (bid + ask) / 2
            if (ask - bid) / mid > config.max_bid_ask_spread_pct:
                raise ValueError("Bid/ask spread too wide")
            raw_price = (min(bid, mid * (1 - config.quote_offset_pct)) if is_buy
                         else max(ask, mid * (1 + config.quote_offset_pct)))
            price = provider.quantize_order_price(config.connector_name, config.trading_pair, raw_price)
            if price <= 0 or (is_buy and price >= ask) or (not is_buy and price <= bid):
                raise ValueError("Quantized price would cross the book")

            stop_pct = self._snapshot.stop_pct
            effective_equity = usable_equity if usable_equity is not None else config.total_amount_quote
            notional = human_swing_notional(
                cap=config.total_amount_quote,
                equity=effective_equity,
                leverage=config.leverage,
                eff_risk_frac=self._snapshot.eff_risk_frac,
                stop_pct=stop_pct,
                margin_ratio=config.margin_ratio,
                max_margin_cap=config.max_margin_cap,
            )
            if max_avail_notional is not None:
                notional = min(notional, max_avail_notional)
            amount = provider.quantize_order_amount(config.connector_name, config.trading_pair, notional / price)
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
            timestamp=provider.time(),
            level_id="buy_0" if is_buy else "sell_0",
            connector_name=config.connector_name,
            trading_pair=config.trading_pair,
            entry_price=price,
            amount=amount,
            leverage=config.leverage,
            side=TradeType.BUY if is_buy else TradeType.SELL,
            triple_barrier_config=TripleBarrierConfig(
                take_profit=config.swing_tp_pct,
                stop_loss=stop_pct,
                take_profit_quote=None,
                time_limit=config.time_limit,
                trailing_stop=TrailingStop(
                    activation_price=self._snapshot.trail_trig,
                    trailing_delta=self._snapshot.trail_pb,
                ),
                open_order_type=OrderType.LIMIT_MAKER,
                take_profit_order_type=OrderType.LIMIT,
                stop_loss_order_type=OrderType.MARKET,
                time_limit_order_type=OrderType.MARKET,
            ),
        )
        self._last_entry_candle = self._snapshot.candle_timestamp
        self.processed_data.update({
            "reason": "entry_submitted",
            "stop_loss": stop_pct,
            "notional": notional,
            "trail_trig": self._snapshot.trail_trig,
            "trail_pb": self._snapshot.trail_pb,
        })
        return [CreateExecutorAction(controller_id=config.id, executor_config=executor_config)]

    def get_custom_info(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in self.processed_data.items()}

    def to_format_status(self):
        if self.config.signal_mode == "human_swing":
            return [f"Human swing | rsi9={self.processed_data.get('rsi9', 'n/a')} | "
                    f"er4h={self.processed_data.get('er4h', 'n/a')} | "
                    f"{self.processed_data.get('reason', 'waiting_for_data')}"]
        if self.config.signal_mode == "momentum":
            return [f"Momentum | move={self.processed_data.get('momentum', 'n/a')} | "
                    f"{self.processed_data.get('reason', 'waiting_for_data')}"]
        return [f"Mean reversion | z={self.processed_data.get('z_score', 'n/a')} | "
                f"{self.processed_data.get('reason', 'waiting_for_data')}"]
