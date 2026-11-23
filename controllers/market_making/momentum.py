"""16-hour momentum. Long only after a completed candle; no exchange calls."""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from controllers.market_making.mean_reversion import MeanReversionSettings


@dataclass(frozen=True)
class MomentumSnapshot:
    candle_timestamp: float
    momentum: Decimal
    atr: Decimal
    stop_pct: Decimal
    signal: int

    @property
    def closed_z_score(self) -> Decimal:
        return self.momentum

    @property
    def z_score(self) -> Decimal:
        return self.momentum


def momentum_snapshot(candles: pd.DataFrame, now: float, mid: Decimal,
                      settings: MeanReversionSettings) -> MomentumSnapshot:
    """Buy only when the completed lookback return clears the threshold."""
    if not mid.is_finite() or mid <= 0:
        raise ValueError("Invalid mid price")
    interval = settings.interval_seconds
    lookback = settings.momentum_lookback
    required = max(lookback + 1, settings.atr_length + 1)
    if candles is None or len(candles) < required:
        raise ValueError("Warming up candles")
    frame = candles.loc[:, ["timestamp", "open", "high", "low", "close"]].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame["timestamp"].to_numpy(dtype=float)).all():
        raise ValueError("Invalid candle timestamp")
    frame = frame.sort_values("timestamp")
    frame = frame[frame["timestamp"] + interval <= now].tail(required)
    if len(frame) < required:
        raise ValueError("Warming up completed candles")
    if not np.isfinite(frame[["open", "high", "low", "close"]].to_numpy(dtype=float)).all():
        raise ValueError("Invalid candle prices")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Invalid candle prices")
    if not (frame["timestamp"].diff().dropna() == interval).all():
        raise ValueError("Missing or duplicate candles")
    last_timestamp = float(frame["timestamp"].iloc[-1])
    if now - (last_timestamp + interval) > interval + 30:
        raise ValueError("Stale candles")
    previous = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr = Decimal(str(true_range.tail(settings.atr_length).mean()))
    if not atr.is_finite() or atr <= 0:
        raise ValueError("Invalid ATR")
    last_close = Decimal(str(frame["close"].iloc[-1]))
    base_close = Decimal(str(frame["close"].iloc[-1 - lookback]))
    move = last_close / base_close - 1
    stop = settings.atr_stop_multiplier * atr / mid
    if stop < settings.min_stop_loss:
        stop = settings.min_stop_loss
    if stop > settings.max_stop_loss:
        raise ValueError("Required volatility stop exceeds the risk cap")
    if move > settings.momentum_threshold:
        signal = 1
    elif move < -settings.momentum_threshold and not settings.long_only:
        signal = -1
    else:
        signal = 0
    return MomentumSnapshot(last_timestamp, move, atr, stop, signal)


def momentum_notional(cap: Decimal, risk_frac: Decimal, stop_pct: Decimal) -> Decimal:
    """Size the position so a stop loses risk_frac of the pair's notional cap."""
    if stop_pct <= 0 or cap <= 0 or risk_frac <= 0:
        raise ValueError("Invalid momentum size")
    risk_budget = risk_frac * cap / stop_pct
    return min(cap, risk_budget)
