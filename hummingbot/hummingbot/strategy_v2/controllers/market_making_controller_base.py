from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.utils.common import parse_comma_separated_list, parse_enum_value


class MarketMakingControllerConfigBase(ControllerConfigBase):
    """
    This class represents the base configuration for a market making controller.
    """
    controller_type: str = "market_making"
    connector_name: str = Field(
        default="binance_perpetual",
        json_schema_extra={
            "prompt": "Enter the connector name (e.g., binance_perpetual): ",
            "prompt_on_new": True}
    )
    trading_pair: str = Field(
        default="WLD-USDT",
        json_schema_extra={
            "prompt": "Enter the trading pair to trade on (e.g., WLD-USDT): ",
            "prompt_on_new": True}
    )
    buy_spreads: List[float] = Field(
        default="0.01,0.02",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_spreads: List[float] = Field(
        default="0.01,0.02",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell spreads (e.g., '0.01, 0.02'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    buy_amounts_pct: Union[List[Decimal], None] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy amounts as percentages (e.g., '50, 50'), or leave blank to distribute equally: ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_amounts_pct: Union[List[Decimal], None] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell amounts as percentages (e.g., '50, 50'), or leave blank to distribute equally: ",
            "prompt_on_new": True, "is_updatable": True}
    )
    executor_refresh_time: int = Field(
        default=60 * 5,
        json_schema_extra={
            "prompt": "Enter the refresh time in seconds for executors (e.g., 300 for 5 minutes): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    cooldown_time: int = Field(
        default=15,
        json_schema_extra={
            "prompt": "Enter the cooldown time in seconds between replacing an executor that traded (e.g., 15): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    leverage: int = Field(
        default=20,
        json_schema_extra={
            "prompt": "Enter the leverage to use for trading (e.g., 20 for 20x leverage). Set it to 1 for spot trading: ",
            "prompt_on_new": True}
    )
    position_mode: PositionMode = Field(
        default="HEDGE",
        json_schema_extra={"prompt": "Enter the position mode (HEDGE/ONEWAY): "}
    )
    # Triple Barrier Configuration
    stop_loss: Optional[Decimal] = Field(
        default=Decimal("0.03"), gt=0,
        json_schema_extra={
            "prompt": "Enter the stop loss (as a decimal, e.g., 0.03 for 3%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    take_profit: Optional[Decimal] = Field(
        default=Decimal("0.02"), gt=0,
        json_schema_extra={
            "prompt": "Enter the take profit (as a decimal, e.g., 0.02 for 2%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    time_limit: Optional[int] = Field(
        default=60 * 45, gt=0,
        json_schema_extra={
            "prompt": "Enter the time limit in seconds (e.g., 2700 for 45 minutes): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    take_profit_order_type: OrderType = Field(
        default=OrderType.LIMIT,
        json_schema_extra={
            "prompt": "Enter the order type for take profit (LIMIT/MARKET): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    trailing_stop: Optional[TrailingStop] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trailing stop as activation_price,trailing_delta (e.g., 0.015,0.003): ",
            "prompt_on_new": True, "is_updatable": True},
    )
    # Position Management Configuration
    position_rebalance_threshold_pct: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={
            "prompt": "Enter the position rebalance threshold percentage (e.g., 0.05 for 5%): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    skip_rebalance: bool = Field(default=False)
    # Trend and volatility
    enable_trend_filter: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    ema_fast: int = Field(default=20, json_schema_extra={"is_updatable": True})
    ema_slow: int = Field(default=60, json_schema_extra={"is_updatable": True})
    trend_deadzone_pct: Decimal = Field(default=Decimal("0.002"), json_schema_extra={"is_updatable": True})
    atr_length: int = Field(default=14, json_schema_extra={"is_updatable": True})
    atr_low_ratio: Decimal = Field(default=Decimal("0.8"), json_schema_extra={"is_updatable": True})
    atr_high_ratio: Decimal = Field(default=Decimal("1.5"), json_schema_extra={"is_updatable": True})
    spread_multiplier_low: Decimal = Field(default=Decimal("0.8"), json_schema_extra={"is_updatable": True})
    spread_multiplier_normal: Decimal = Field(default=Decimal("1.0"), json_schema_extra={"is_updatable": True})
    spread_multiplier_high: Decimal = Field(default=Decimal("1.5"), json_schema_extra={"is_updatable": True})
    candles_interval: str = Field(default="5m")
    candles_connector: Optional[str] = Field(default=None)
    candles_trading_pair: Optional[str] = Field(default=None)
    # Per-level triple barrier. Index 0 is the closest grid level.
    level_take_profits: Optional[List[Decimal]] = Field(default=None, json_schema_extra={"is_updatable": True})
    level_stop_losses: Optional[List[Decimal]] = Field(default=None, json_schema_extra={"is_updatable": True})

    @field_validator("trailing_stop", mode="before")
    @classmethod
    def parse_trailing_stop(cls, v):
        if isinstance(v, str):
            if v == "":
                return None
            activation_price, trailing_delta = v.split(",")
            return TrailingStop(activation_price=Decimal(activation_price), trailing_delta=Decimal(trailing_delta))
        return v

    @field_validator("time_limit", "stop_loss", "take_profit", "position_rebalance_threshold_pct", mode="before")
    @classmethod
    def validate_target(cls, v):
        if isinstance(v, str):
            if v == "":
                return None
            return Decimal(v)
        return v

    @field_validator('take_profit_order_type', mode="before")
    @classmethod
    def validate_order_type(cls, v) -> OrderType:
        if v is None:
            return OrderType.MARKET
        if isinstance(v, str):
            v = v.replace("OrderType.", "")
        return parse_enum_value(OrderType, v, "take_profit_order_type")

    @field_validator('position_mode', mode="before")
    @classmethod
    def validate_position_mode(cls, v: str) -> PositionMode:
        return parse_enum_value(PositionMode, v, "position_mode")

    @field_validator('buy_spreads', 'sell_spreads', mode="before")
    @classmethod
    def parse_spreads(cls, v):
        return parse_comma_separated_list(v)

    @field_validator('buy_amounts_pct', 'sell_amounts_pct', mode="before")
    @classmethod
    def parse_and_validate_amounts(cls, v, validation_info: ValidationInfo):
        field_name = validation_info.field_name
        if v is None or v == "":
            spread_field = field_name.replace('amounts_pct', 'spreads')
            return [1 for _ in validation_info.data[spread_field]]
        parsed = parse_comma_separated_list(v)
        if isinstance(parsed, list) and len(parsed) != len(validation_info.data[field_name.replace('amounts_pct', 'spreads')]):
            raise ValueError(
                f"The number of {field_name} must match the number of {field_name.replace('amounts_pct', 'spreads')}.")
        return parsed

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v

    @field_validator("level_take_profits", "level_stop_losses", mode="before")
    @classmethod
    def parse_level_barriers(cls, v):
        if v is None or v == "":
            return None
        return parse_comma_separated_list(v)

    @field_validator(
        "trend_deadzone_pct", "atr_low_ratio", "atr_high_ratio",
        "spread_multiplier_low", "spread_multiplier_normal", "spread_multiplier_high",
        mode="before",
    )
    @classmethod
    def parse_decimal_fields(cls, v):
        if v is None or v == "":
            return v
        return Decimal(str(v))

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            time_limit=self.time_limit,
            trailing_stop=self.trailing_stop,
            open_order_type=OrderType.LIMIT,  # Defaulting to LIMIT as is a Maker Controller
            take_profit_order_type=self.take_profit_order_type,
            stop_loss_order_type=OrderType.MARKET,  # Defaulting to MARKET as per requirement
            time_limit_order_type=OrderType.MARKET  # Defaulting to MARKET as per requirement
        )

    def get_spreads_and_amounts_in_quote(self, trade_type: TradeType) -> Tuple[List[float], List[float]]:
        buy_amounts_pct = getattr(self, 'buy_amounts_pct')
        sell_amounts_pct = getattr(self, 'sell_amounts_pct')

        # Calculate total percentages across buys and sells
        total_pct = sum(buy_amounts_pct) + sum(sell_amounts_pct)

        # Normalize amounts_pct based on total percentages
        if trade_type == TradeType.BUY:
            normalized_amounts_pct = [amt_pct / total_pct for amt_pct in buy_amounts_pct]
        else:  # TradeType.SELL
            normalized_amounts_pct = [amt_pct / total_pct for amt_pct in sell_amounts_pct]

        spreads = getattr(self, f'{trade_type.name.lower()}_spreads')
        return spreads, [amt_pct * self.total_amount_quote for amt_pct in normalized_amounts_pct]

    def get_required_base_amount(self, reference_price: Decimal) -> Decimal:
        """
        Get the required base asset amount for sell orders.
        """
        _, sell_amounts_quote = self.get_spreads_and_amounts_in_quote(TradeType.SELL)
        total_sell_amount_quote = sum(sell_amounts_quote)
        return total_sell_amount_quote / reference_price

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class MarketMakingControllerBase(ControllerBase):
    """
    This class represents the base class for a market making controller.
    """

    def __init__(self, config: MarketMakingControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.market_data_provider.initialize_rate_sources([ConnectorPair(
            connector_name=config.connector_name, trading_pair=config.trading_pair)])

    def get_candles_config(self) -> List[CandlesConfig]:
        if not self.config.enable_trend_filter:
            return []
        max_records = max(self.config.ema_slow, self.config.atr_length) + 100
        return [CandlesConfig(
            connector=self.config.candles_connector or self.config.connector_name,
            trading_pair=self.config.candles_trading_pair or self.config.trading_pair,
            interval=self.config.candles_interval,
            max_records=max_records,
        )]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Determine actions based on the provided executor handler report.
        """
        actions = []
        actions.extend(self.create_actions_proposal())
        actions.extend(self.stop_actions_proposal())
        return actions

    def create_actions_proposal(self) -> List[ExecutorAction]:
        """
        Create actions proposal based on the current state of the controller.
        """
        create_actions = []

        # Check if we need to rebalance position first
        position_rebalance_action = self.check_position_rebalance()
        if position_rebalance_action:
            create_actions.append(position_rebalance_action)

        # Create normal market making levels
        levels_to_execute = self.get_levels_to_execute()
        for level_id in levels_to_execute:
            price, amount = self.get_price_and_amount(level_id)
            executor_config = self.get_executor_config(level_id, price, amount)
            if executor_config is not None:
                create_actions.append(CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=executor_config
                ))
        return create_actions

    def get_levels_to_execute(self) -> List[str]:
        working_levels = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active or (x.close_type == CloseType.STOP_LOSS and self.market_data_provider.time() - x.close_timestamp < self.config.cooldown_time)
        )
        working_levels_ids = [executor.custom_info["level_id"] for executor in working_levels]
        return self.get_not_active_levels_ids(working_levels_ids)

    def stop_actions_proposal(self) -> List[ExecutorAction]:
        """
        Create a list of actions to stop the executors based on order refresh and early stop conditions.
        """
        stop_actions = []
        stop_actions.extend(self.executors_to_refresh())
        stop_actions.extend(self.executors_to_early_stop())
        return stop_actions

    def executors_to_refresh(self) -> List[ExecutorAction]:
        executors_to_refresh = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: not x.is_trading and x.is_active and self.market_data_provider.time() - x.timestamp > self.config.executor_refresh_time)

        return [StopExecutorAction(
            controller_id=self.config.id,
            executor_id=executor.id) for executor in executors_to_refresh]

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        """
        Cancel unfilled quotes on the side that the current trend no longer wants to open.
        Filled positions are left to triple-barrier / trailing stop management.
        """
        trend = self.processed_data.get("trend", "SIDEWAYS")
        if trend not in ("UP", "DOWN"):
            return []
        blocked_side = "sell" if trend == "UP" else "buy"

        def _should_stop(executor):
            if not (executor.is_active and not executor.is_trading):
                return False
            level_id = str((executor.custom_info or {}).get("level_id", ""))
            return level_id.startswith(blocked_side)

        executors_to_stop = self.filter_executors(
            executors=self.executors_info,
            filter_func=_should_stop,
        )
        return [StopExecutorAction(
            controller_id=self.config.id,
            executor_id=executor.id) for executor in executors_to_stop]

    async def update_processed_data(self):
        """
        Update mid-price, EMA trend (UP / DOWN / SIDEWAYS) and ATR-based spread multiplier.
        """
        reference_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        processed = {
            "reference_price": Decimal(reference_price),
            "spread_multiplier": Decimal("1"),
            "trend": "SIDEWAYS",
            "ema_fast": None,
            "ema_slow": None,
            "ema_diff_pct": None,
            "atr": None,
            "atr_ratio": None,
        }
        if self.config.enable_trend_filter:
            try:
                processed.update(self._compute_trend_and_volatility())
            except Exception:
                self.logger().debug(
                    "Failed to compute trend/ATR, falling back to SIDEWAYS mid-price quoting.",
                    exc_info=True,
                )
        self.processed_data = processed

    def _compute_trend_and_volatility(self) -> Dict:
        max_records = max(self.config.ema_slow, self.config.atr_length) + 100
        candles = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector or self.config.connector_name,
            trading_pair=self.config.candles_trading_pair or self.config.trading_pair,
            interval=self.config.candles_interval,
            max_records=max_records,
        )
        if candles is None or len(candles) < self.config.ema_slow:
            return {}

        ema_fast = candles["close"].ewm(span=self.config.ema_fast, adjust=False).mean()
        ema_slow = candles["close"].ewm(span=self.config.ema_slow, adjust=False).mean()
        prev_close = candles["close"].shift(1)
        true_range = pd.concat([
            candles["high"] - candles["low"],
            (candles["high"] - prev_close).abs(),
            (candles["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.ewm(alpha=1 / self.config.atr_length, adjust=False).mean()
        ema_fast_val = ema_fast.iloc[-1] if len(ema_fast) else None
        ema_slow_val = ema_slow.iloc[-1] if len(ema_slow) else None
        if pd.isna(ema_fast_val) or pd.isna(ema_slow_val):
            return {}

        ema_fast_dec = Decimal(str(float(ema_fast_val)))
        ema_slow_dec = Decimal(str(float(ema_slow_val)))
        if ema_slow_dec == 0:
            return {}

        diff_pct = (ema_fast_dec - ema_slow_dec) / ema_slow_dec
        deadzone = Decimal(str(self.config.trend_deadzone_pct))
        if diff_pct > deadzone:
            trend = "UP"
        elif diff_pct < -deadzone:
            trend = "DOWN"
        else:
            trend = "SIDEWAYS"

        result = {
            "trend": trend,
            "ema_fast": ema_fast_dec,
            "ema_slow": ema_slow_dec,
            "ema_diff_pct": diff_pct,
        }
        if atr is None or len(atr) == 0:
            return result

        atr_val = atr.iloc[-1]
        atr_valid = atr.dropna()
        if pd.isna(atr_val) or len(atr_valid) == 0:
            return result

        atr_dec = Decimal(str(float(atr_val)))
        atr_mean = Decimal(str(float(atr_valid.tail(max(self.config.atr_length, 20)).mean())))
        spread_multiplier = Decimal(str(self.config.spread_multiplier_normal))
        atr_ratio = None
        if atr_mean > 0:
            atr_ratio = atr_dec / atr_mean
            if atr_ratio < Decimal(str(self.config.atr_low_ratio)):
                spread_multiplier = Decimal(str(self.config.spread_multiplier_low))
            elif atr_ratio > Decimal(str(self.config.atr_high_ratio)):
                spread_multiplier = Decimal(str(self.config.spread_multiplier_high))
        result.update({
            "spread_multiplier": spread_multiplier,
            "atr": atr_dec,
            "atr_ratio": atr_ratio,
        })
        return result

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        """
        Get the executor config for a given level id.
        """
        raise NotImplementedError

    def get_triple_barrier_for_level(self, level_id: str) -> TripleBarrierConfig:
        """
        Use per-level TP/SL when configured, otherwise the global triple barrier.
        Deeper grid levels (higher index) can keep a wider stop and a different take profit.
        """
        base = self.config.triple_barrier_config
        try:
            level = self.get_level_from_level_id(level_id)
        except (IndexError, ValueError):
            return base

        take_profits = self.config.level_take_profits or []
        stop_losses = self.config.level_stop_losses or []
        take_profit = (
            Decimal(str(take_profits[level])) if level < len(take_profits) else base.take_profit
        )
        stop_loss = (
            Decimal(str(stop_losses[level])) if level < len(stop_losses) else base.stop_loss
        )
        return TripleBarrierConfig(
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_limit=base.time_limit,
            trailing_stop=base.trailing_stop,
            open_order_type=base.open_order_type,
            take_profit_order_type=base.take_profit_order_type,
            stop_loss_order_type=base.stop_loss_order_type,
            time_limit_order_type=base.time_limit_order_type,
        )

    def get_price_and_amount(self, level_id: str) -> Tuple[Decimal, Decimal]:
        """
        Get the spread and amount in quote for a given level id.
        In UP/DOWN trends only the active side is quoted, so its weights are normalized
        against that side alone. In SIDEWAYS both sides share total_amount_quote.
        """
        level = self.get_level_from_level_id(level_id)
        trade_type = self.get_trade_type_from_level_id(level_id)
        spreads, _ = self.config.get_spreads_and_amounts_in_quote(trade_type)
        reference_price = Decimal(self.processed_data["reference_price"])
        spread_in_pct = Decimal(spreads[int(level)]) * Decimal(self.processed_data["spread_multiplier"])
        side_multiplier = Decimal("-1") if trade_type == TradeType.BUY else Decimal("1")
        order_price = reference_price * (1 + side_multiplier * spread_in_pct)
        amount_quote = self._get_level_amount_quote(level_id)
        return order_price, amount_quote / order_price

    def _get_level_amount_quote(self, level_id: str) -> Decimal:
        level = int(self.get_level_from_level_id(level_id))
        trade_type = self.get_trade_type_from_level_id(level_id)
        buy_amounts = [Decimal(str(x)) for x in self.config.buy_amounts_pct]
        sell_amounts = [Decimal(str(x)) for x in self.config.sell_amounts_pct]
        trend = self.processed_data.get("trend", "SIDEWAYS")
        if trend == "UP":
            total_pct = sum(buy_amounts)
            side_amounts = buy_amounts
        elif trend == "DOWN":
            total_pct = sum(sell_amounts)
            side_amounts = sell_amounts
        else:
            total_pct = sum(buy_amounts) + sum(sell_amounts)
            side_amounts = buy_amounts if trade_type == TradeType.BUY else sell_amounts
        if total_pct == 0 or level >= len(side_amounts):
            return Decimal("0")
        return (side_amounts[level] / total_pct) * self.config.total_amount_quote

    def get_level_id_from_side(self, trade_type: TradeType, level: int) -> str:
        """
        Get the level id based on the trade type and the level.
        """
        return f"{trade_type.name.lower()}_{level}"

    def get_trade_type_from_level_id(self, level_id: str) -> TradeType:
        return TradeType.BUY if level_id.startswith("buy") else TradeType.SELL

    def get_level_from_level_id(self, level_id: str) -> int:
        return int(level_id.split('_')[1])

    def get_not_active_levels_ids(self, active_levels_ids: List[str]) -> List[str]:
        """
        Missing grid levels for the current trend: BUY-only in UP, SELL-only in DOWN,
        both sides in SIDEWAYS.
        """
        buy_ids_missing = [self.get_level_id_from_side(TradeType.BUY, level) for level in range(len(self.config.buy_spreads))
                           if self.get_level_id_from_side(TradeType.BUY, level) not in active_levels_ids]
        sell_ids_missing = [self.get_level_id_from_side(TradeType.SELL, level) for level in range(len(self.config.sell_spreads))
                            if self.get_level_id_from_side(TradeType.SELL, level) not in active_levels_ids]
        trend = self.processed_data.get("trend", "SIDEWAYS")
        if trend == "UP":
            return buy_ids_missing
        if trend == "DOWN":
            return sell_ids_missing
        return buy_ids_missing + sell_ids_missing

    def get_custom_info(self) -> dict:
        return {
            "trend": self.processed_data.get("trend"),
            "spread_multiplier": str(self.processed_data.get("spread_multiplier")),
            "ema_fast": str(self.processed_data.get("ema_fast")),
            "ema_slow": str(self.processed_data.get("ema_slow")),
            "ema_diff_pct": str(self.processed_data.get("ema_diff_pct")),
            "atr_ratio": str(self.processed_data.get("atr_ratio")),
        }

    def check_position_rebalance(self) -> Optional[CreateExecutorAction]:
        """
        Check if position needs rebalancing and create OrderExecutor to acquire missing base asset.
        Only applies to spot trading (not perpetual contracts).
        """
        # Skip position rebalancing for perpetual contracts
        if "_perpetual" in self.config.connector_name or "reference_price" not in self.processed_data or self.config.skip_rebalance:
            return None

        active_rebalance = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active and x.custom_info.get("level_id") == "position_rebalance"
        )
        if len(active_rebalance) > 0:
            # If there's already an active rebalance executor, skip rebalancing
            return None

        required_base_amount = self.config.get_required_base_amount(Decimal(self.processed_data["reference_price"]))
        current_base_amount = self.get_current_base_position()

        # Calculate the difference
        base_amount_diff = required_base_amount - current_base_amount

        # Check if difference exceeds threshold
        threshold_amount = required_base_amount * self.config.position_rebalance_threshold_pct

        if abs(base_amount_diff) > threshold_amount:
            # We need to rebalance
            if base_amount_diff > 0:
                # Need to buy more base asset
                return self.create_position_rebalance_order(TradeType.BUY, abs(base_amount_diff))
            else:
                # Need to sell base asset (unlikely for market making but possible)
                return self.create_position_rebalance_order(TradeType.SELL, abs(base_amount_diff))

        return None

    def get_current_base_position(self) -> Decimal:
        """
        Get current base asset position from positions held.
        """
        total_base_amount = Decimal("0")

        for position in self.positions_held:
            if (position.connector_name == self.config.connector_name and
                    position.trading_pair == self.config.trading_pair):
                # Calculate net base position
                if position.side == TradeType.BUY:
                    total_base_amount += position.amount
                else:  # SELL position
                    total_base_amount -= position.amount

        return total_base_amount

    def create_position_rebalance_order(self, side: TradeType, amount: Decimal) -> CreateExecutorAction:
        """
        Create an OrderExecutor to rebalance position.
        """
        reference_price = self.processed_data["reference_price"]

        # Use market price for quick execution
        order_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            execution_strategy=ExecutionStrategy.MARKET,
            side=side,
            amount=amount,
            price=reference_price,  # Will be ignored for market orders
            level_id="position_rebalance",
        )

        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=order_config
        )
