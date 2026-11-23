"""A rising market must be bought only after the signal bar has closed."""

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("trend_sim", ROOT / "scripts" / "trend_sim.py")
trend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trend)


def _rising(count=24):
    close = 100 * (1.01 ** np.arange(count))
    return (close * 0.999, close * 1.002, close * 0.997, close)


def _features(op, hi, lo, cl, lookback=4):
    atr, momentum, prior_high, prior_low = trend.trend_features(op, hi, lo, cl, lookback, 3)
    return atr, momentum, prior_high, prior_low


def _run(op, hi, lo, cl, end=None, exit_mode=1, mode=0):
    atr, momentum, prior_high, prior_low = _features(op, hi, lo, cl)
    if end is None:
        end = len(cl)
    equity = np.full(end, np.nan)
    return trend.simulate_trend(
        op, hi, lo, cl, atr, momentum, prior_high, prior_low,
        mode, 0.0, 2.0, exit_mode, 0, 0.02, 80.0,
        1, 0.004, 0.08, 0.0005, 0.0002, 1, end, equity,
    )


def test_uptrend_is_bought_on_the_following_open_and_profits():
    op, hi, lo, cl = _rising()
    result = _run(op, hi, lo, cl)
    assert result[1] >= 1
    assert result[0] > 0


def test_future_spike_cannot_fill_on_the_signal_bar():
    close = np.full(12, 100.0)
    close[-1] = 130.0
    op = close.copy()
    hi = close + 0.2
    lo = close - 0.2
    hi[-1] = 130.0
    result = _run(op, hi, lo, close)
    assert result[1] == 0
    assert result[0] == 0
