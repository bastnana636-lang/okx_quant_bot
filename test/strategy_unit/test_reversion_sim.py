"""The research simulator must use the same signal and barrier math as the live strategy."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from controllers.market_making.mean_reversion import MeanReversionSettings, calculate_snapshot

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("reversion_sim", ROOT / "scripts" / "reversion_sim.py")
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


def test_aggregated_features_match_live_snapshot():
    interval = 300
    window = 48
    count = window * (interval // 60)
    start = 1_700_000_000
    start -= start % interval
    ts = start + np.arange(count, dtype=np.int64) * 60
    close = 100 + np.sin(np.arange(count) * 0.13) * 1.1
    close[-1] = 97.5
    high = close + 0.08
    low = close - 0.08
    prepared = sim.prepare_market(ts, close, high, low, close, np.full(count, 1_000_000.0), interval, window)
    last = int(prepared.signal_index[-1])
    assert last == window - 1
    bucket = (ts // interval) * interval
    rows = []
    for stamp in range(start, start + window * interval, interval):
        mask = bucket == stamp
        rows.append((stamp, close[mask][0], high[mask].max(), low[mask].min(), close[mask][-1]))
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    now = float(ts[-1] + 60)
    snapshot = calculate_snapshot(frame, now, Decimal("97.5"), MeanReversionSettings())
    assert abs(float(snapshot.mean) - prepared.mean[last]) < 1e-8
    assert abs(float(snapshot.std) - prepared.std[last]) < 1e-8
    assert abs(float(snapshot.atr) - prepared.atr[last]) < 1e-8
    live_z = (97.5 - prepared.mean[last]) / prepared.std[last]
    assert abs(float(snapshot.z_score) - live_z) < 1e-8
    assert abs(float(snapshot.closed_z_score) - (prepared.signal_close[last] - prepared.mean[last]) / prepared.std[last]) < 1e-8


def _run_hand_path(cooldown=0, slow=0.0, entry_mode=0):
    ts = np.array([0, 60, 120, 180], dtype=np.int64)
    op = np.array([97.5, 97.6, 97.7, 100.0])
    hi = np.array([97.6, 97.8, 99.2, 100.2])
    lo = np.array([97.4, 97.4, 97.6, 99.8])
    cl = np.array([97.5, 97.5, 98.4, 100.0])
    qv = np.full(4, 1_000.0)
    signal_index = np.zeros(4, dtype=np.int32)
    mean = np.array([100.0])
    std = np.array([1.0])
    atr = np.array([0.4])
    closed = np.array([97.5])
    previous = np.array([np.nan])
    equity = np.full(4, np.nan)
    return sim.simulate(
        ts, op, hi, lo, cl, qv, signal_index, closed, mean, std, atr, previous,
        2.0, 3.5, 0.15, 0.001, 1.5, 0.006, 0.02,
        0.0012, 0.0018, 0.5, 1.2, 3600, cooldown, 1.0,
        0, 0.0, 0.0, 80.0,
        0.0, 0.0, 0.0002, 0.0005, 0.0002, np.array([slow]), entry_mode,
        0, 4, 0, equity,
    )


def test_cash_take_profit_fills_next_bar_and_nets_fees():
    result = _run_hand_path()
    price = 97.5
    quantity = 80.0 / price
    cash_price = price + 1.2 / quantity
    mean_target = 100.0 - 0.15
    take_price = min(mean_target, cash_price)
    gross = (take_price - price) * quantity
    fee = 80.0 * 0.0002 + take_price * quantity * 0.0002
    assert result[1] == 1.0
    assert result[8] == 1.0  # take profit, not the signal-bar wick
    assert abs(result[0] - (gross - fee)) < 1e-8
    assert result[0] > 1.0


def test_signal_bar_wick_does_not_fill_the_entry():
    ts = np.array([0, 60], dtype=np.int64)
    op = np.array([97.5, 99.0])
    hi = np.array([101.0, 99.4])
    lo = np.array([97.4, 98.9])
    cl = np.array([97.5, 99.1])
    qv = np.full(2, 1_000.0)
    equity = np.full(2, np.nan)
    result = sim.simulate(
        ts, op, hi, lo, cl, qv, np.zeros(2, dtype=np.int32),
        np.array([97.5]), np.array([100.0]), np.array([1.0]), np.array([0.4]), np.array([np.nan]),
        2.0, 3.5, 0.15, 0.001, 1.5, 0.006, 0.02,
        0.0012, 0.0018, 0.5, 1.2, 3600, 0, 1.0,
        0, 0.0, 0.0, 80.0,
        0.0, 0.0, 0.0002, 0.0005, 0.0002, np.zeros(1), 0,
        0, 2, 0, equity,
    )
    assert result[1] == 0.0
    assert result[0] == 0.0


def test_with_trend_buys_dips_only_above_the_slow_average():
    blocked = _run_hand_path(slow=99.0, entry_mode=1)
    allowed = _run_hand_path(slow=90.0, entry_mode=1)
    assert blocked[1] == 0.0
    assert allowed[1] == 1.0
    assert allowed[0] > 1.0
