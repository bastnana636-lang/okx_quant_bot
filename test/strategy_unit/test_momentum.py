"""The live momentum signal uses only completed candles and the locked size ratio."""

from decimal import Decimal

import numpy as np
import pandas as pd

from controllers.market_making.mean_reversion import MeanReversionSettings
from controllers.market_making.momentum import momentum_notional, momentum_snapshot

D = Decimal
NOW = 1_800_000_000


def _frame(last_jump=1.02):
    close = np.full(20, 100.0)
    close[-1] = 100.0 * last_jump
    return pd.DataFrame({
        "timestamp": NOW - (20 - np.arange(20)) * 3600,
        "open": close, "high": close + 0.4, "low": close - 0.4, "close": close,
    })


def _settings(**overrides):
    values = dict(
        candles_interval="1h", mean_window=16, momentum_lookback=16, momentum_threshold="0.008",
        atr_length=14, atr_stop_multiplier="3", min_stop_loss="0.004", max_stop_loss="0.06",
        long_only=True, signal_mode="momentum",
    )
    values.update(overrides)
    return MeanReversionSettings(**values)


def test_sixteen_hour_rise_is_a_long_and_a_decline_is_not():
    rising = momentum_snapshot(_frame(1.02), NOW, D("102"), _settings())
    assert rising.signal == 1
    assert rising.momentum > D("0.008")
    falling = momentum_snapshot(_frame(0.98), NOW, D("98"), _settings())
    assert falling.signal == 0


def test_unfinished_spike_cannot_create_the_signal():
    frame = _frame(1.0)
    before = momentum_snapshot(frame, NOW, D("100"), _settings())
    frame.loc[len(frame)] = [NOW, 500, 500, 500, 500]
    assert momentum_snapshot(frame, NOW, D("100"), _settings()) == before


def test_position_risk_is_two_percent_of_the_pair_cap():
    assert momentum_notional(D("80"), D("0.02"), D("0.04")) == D("40")
    assert momentum_notional(D("80"), D("0.02"), D("0.01")) == D("80")
