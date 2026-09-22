"""Pure mean-reversion calculations; no exchange access or order side effects."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


class MeanReversionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    candles_interval: Literal["1m", "3m", "5m", "15m", "30m", "1h"] = "5m"
    mean_window: int = Field(default=48, ge=10, le=500)
    entry_z_score: Decimal = Field(default=Decimal("2.0"), gt=0)
    max_entry_z_score: Decimal = Field(default=Decimal("3.5"), gt=0)
    exit_z_score: Decimal = Field(default=Decimal("0.15"), ge=0)
    min_std_pct: Decimal = Field(default=Decimal("0.001"), gt=0, lt=1)
    atr_length: int = Field(default=14, ge=2, le=100)
    atr_stop_multiplier: Decimal = Field(default=Decimal("1.5"), gt=0)
    min_stop_loss: Decimal = Field(default=Decimal("0.006"), gt=0, lt=1)
    max_stop_loss: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    round_trip_cost_pct: Decimal = Field(default=Decimal("0.0012"), ge=0, lt=1)
    min_net_edge_pct: Decimal = Field(default=Decimal("0.0018"), gt=0, lt=1)
    min_reward_risk: Decimal = Field(default=Decimal("1.0"), gt=0)
    quote_offset_pct: Decimal = Field(default=Decimal("0.0002"), ge=0, lt=1)
    max_bid_ask_spread_pct: Decimal = Field(default=Decimal("0.002"), gt=0, lt=1)
    executor_refresh_time: int = Field(default=60, ge=1)
    cooldown_time: int = Field(default=300, ge=1)
    stop_loss_cooldown_time: int = Field(default=900, ge=1)
    time_limit: int = Field(default=3600, ge=60)

    @model_validator(mode="after")
    def validate_relationships(self):
        if not self.exit_z_score < self.entry_z_score < self.max_entry_z_score:
            raise ValueError("Require exit_z_score < entry_z_score < max_entry_z_score")
        if self.min_stop_loss > self.max_stop_loss:
            raise ValueError("min_stop_loss must not exceed max_stop_loss")
        if self.stop_loss_cooldown_time < self.cooldown_time:
            raise ValueError("Stop-loss cooldown must be at least the normal cooldown")
        if self.executor_refresh_time >= self.time_limit:
            raise ValueError("Order refresh must be shorter than the position time limit")
        return self

    @property
    def interval_seconds(self):
        return INTERVAL_SECONDS[self.candles_interval]

    @property
    def candle_records(self):
        return max(self.mean_window, self.atr_length + 1) + 2


@dataclass(frozen=True)
class ReversionSnapshot:
    candle_timestamp: float
    mean: Decimal
    std: Decimal
    atr: Decimal
    closed_z_score: Decimal
    z_score: Decimal
    signal: int  # +1 buys below the mean; -1 sells above the mean.


def calculate_snapshot(candles: pd.DataFrame, now: float, mid: Decimal,
                       settings: MeanReversionSettings) -> ReversionSnapshot:
    """Use completed, contiguous candles only; stale or invalid data never enables entry."""
    if not mid.is_finite() or mid <= 0:
        raise ValueError("Invalid mid price")
    required = max(settings.mean_window, settings.atr_length + 1)
    if candles is None or len(candles) < required:
        raise ValueError("Warming up candles")
    frame = candles.loc[:, ["timestamp", "open", "high", "low", "close"]].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame["timestamp"].to_numpy(dtype=float)).all():
        raise ValueError("Invalid candle timestamp")
    frame = frame.sort_values("timestamp")
    frame = frame[frame["timestamp"] + settings.interval_seconds <= now].tail(required)
    if len(frame) < required:
        raise ValueError("Warming up completed candles")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Invalid candle prices")
    if not (frame["timestamp"].diff().dropna() == settings.interval_seconds).all():
        raise ValueError("Missing or duplicate candles")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)) |
            (frame["low"] > frame[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid candle range")
    last_timestamp = float(frame["timestamp"].iloc[-1])
    if now - (last_timestamp + settings.interval_seconds) > settings.interval_seconds + 30:
        raise ValueError("Stale candles")
    closes = frame["close"].tail(settings.mean_window)
    mean = Decimal(str(closes.mean()))
    std = Decimal(str(closes.std(ddof=0)))
    if std <= 0 or std / mean < settings.min_std_pct:
        raise ValueError("Insufficient price dispersion")
    previous = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"], (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr = Decimal(str(true_range.tail(settings.atr_length).mean()))
    closed_z = (Decimal(str(closes.iloc[-1])) - mean) / std
    live_z = (mid - mean) / std
    signal = reversion_side(closed_z, settings)
    if reversion_side(live_z, settings) != signal:
        signal = 0
    return ReversionSnapshot(last_timestamp, mean, std, atr, closed_z, live_z, signal)


def reversion_side(z_score: Decimal, settings: MeanReversionSettings) -> int:
    if settings.entry_z_score <= abs(z_score) <= settings.max_entry_z_score:
        return 1 if z_score < 0 else -1
    return 0


@dataclass(frozen=True)
class ReversionBarriers:
    take_profit: Decimal
    stop_loss: Decimal
    target_price: Decimal


def calculate_barriers(snapshot: ReversionSnapshot, entry_price: Decimal,
                       settings: MeanReversionSettings) -> ReversionBarriers:
    if not entry_price.is_finite() or entry_price <= 0 or snapshot.signal == 0:
        raise ValueError("No valid entry")
    if reversion_side((entry_price - snapshot.mean) / snapshot.std, settings) != snapshot.signal:
        raise ValueError("Entry price outside the reversion band")
    # Freeze the mean at entry so the profit target cannot drift away with a trend.
    target = snapshot.mean - snapshot.signal * settings.exit_z_score * snapshot.std
    reward = snapshot.signal * (target - entry_price) / entry_price
    stop = max(settings.min_stop_loss, settings.atr_stop_multiplier * snapshot.atr / entry_price)
    if stop > settings.max_stop_loss:
        raise ValueError("Required volatility stop exceeds the risk cap")
    net_reward = reward - settings.round_trip_cost_pct
    if net_reward < settings.min_net_edge_pct:
        raise ValueError("Mean reversion does not cover costs and minimum edge")
    if net_reward / (stop + settings.round_trip_cost_pct) < settings.min_reward_risk:
        raise ValueError("Insufficient reward/risk after costs")
    return ReversionBarriers(reward, stop, target)
