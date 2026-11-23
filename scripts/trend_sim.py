"""Trend backtest on completed candles. Signals use only bars that have closed.

Entries fill on the next bar's open and pay the taker fee plus slippage.
A stop that gaps through fills at the open. Nothing here sends an exchange order.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*args, **kwargs):
        def wrap(fn):
            return fn
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return wrap


def aggregate_minutes(ts, op, hi, lo, cl, interval):
    """Build a regular grid. A bucket with missing minutes is left invalid."""
    if len(ts) == 0:
        raise ValueError("no minute bars")
    order = np.argsort(ts, kind="mergesort")
    ts = ts[order].astype(np.int64)
    op = op[order].astype(np.float64)
    hi = hi[order].astype(np.float64)
    lo = lo[order].astype(np.float64)
    cl = cl[order].astype(np.float64)
    bucket = (ts // interval) * interval
    frame = pd.DataFrame({"b": bucket, "o": op, "h": hi, "l": lo, "c": cl})
    grouped = frame.groupby("b", sort=True)
    bars = grouped.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), n=("c", "size"))
    expected = interval // 60
    full = np.arange(int(bars.index[0]), int(bars.index[-1]) + interval, interval, dtype=np.int64)
    bars = bars.reindex(full)
    count = bars["n"].to_numpy(dtype=np.float64)
    valid = count == expected
    out_o = bars["o"].to_numpy(dtype=np.float64).copy()
    out_h = bars["h"].to_numpy(dtype=np.float64).copy()
    out_l = bars["l"].to_numpy(dtype=np.float64).copy()
    out_c = bars["c"].to_numpy(dtype=np.float64).copy()
    out_o[~valid] = np.nan
    out_h[~valid] = np.nan
    out_l[~valid] = np.nan
    out_c[~valid] = np.nan
    return full, out_o, out_h, out_l, out_c


def trend_features(op, hi, lo, cl, lookback, atr_length):
    previous = np.empty_like(cl)
    previous[0] = np.nan
    previous[1:] = cl[:-1]
    true_range = np.maximum(hi - lo, np.maximum(np.abs(hi - previous), np.abs(lo - previous)))
    true_range[~np.isfinite(cl) | ~np.isfinite(previous)] = np.nan
    atr = pd.Series(true_range).rolling(atr_length, min_periods=atr_length).mean().to_numpy(dtype=np.float64)
    momentum = cl / pd.Series(cl).shift(lookback).to_numpy(dtype=np.float64) - 1.0
    prior_high = pd.Series(hi).rolling(lookback, min_periods=lookback).max().shift(1).to_numpy(dtype=np.float64)
    prior_low = pd.Series(lo).rolling(lookback, min_periods=lookback).min().shift(1).to_numpy(dtype=np.float64)
    return (np.ascontiguousarray(atr, dtype=np.float64),
            np.ascontiguousarray(momentum, dtype=np.float64),
            np.ascontiguousarray(prior_high, dtype=np.float64),
            np.ascontiguousarray(prior_low, dtype=np.float64))


@njit
def _book(side, entry, exit_px, qty, taker):
    gross = side * (exit_px - entry) * qty
    fee = (entry + exit_px) * qty * taker
    return gross - fee, fee


@njit
def _close(side, entry, exit_px, qty, taker, entry_i, i, reason, realized, fees, trades, wins,
           gross_win, gross_loss, hold_bars, n_stop, n_flip):
    pnl, fee = _book(side, entry, exit_px, qty, taker)
    realized += pnl
    fees += fee
    trades += 1.0
    hold_bars += float(i - entry_i)
    if pnl > 0.0:
        wins += 1.0
        gross_win += pnl
    elif pnl < 0.0:
        gross_loss += -pnl
    if reason == 2:
        n_stop += 1.0
    elif reason == 3:
        n_flip += 1.0
    return realized, fees, trades, wins, gross_win, gross_loss, hold_bars, n_stop, n_flip


@njit
def simulate_trend(op, hi, lo, cl, atr, momentum, prior_high, prior_low,
                   mode, threshold, atr_mult, exit_mode, long_only, risk_frac, cap,
                   cooldown, min_stop, max_stop, taker, slip, start, end, equity):
    """mode 0 follows the lookback return. mode 1 breaks the prior channel.

    A signal at the close fills on the next open. exit_mode 1 trails an ATR stop.
    exit_mode 2 sells the next open once the signal leaves the position's side.
    """
    realized = 0.0
    trades = 0.0
    wins = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    max_dd = 0.0
    fees = 0.0
    hold_bars = 0.0
    n_stop = 0.0
    n_flip = 0.0
    n_eod = 0.0
    peak = 0.0
    in_pos = False
    enter_side = 0.0
    enter_atr = 0.0
    exit_next = False
    side = 0.0
    entry = 0.0
    qty = 0.0
    stop_px = 0.0
    extreme = 0.0
    entry_i = 0
    cooldown_i = -1
    n = op.shape[0]
    if end > n:
        end = n
    if start < 1:
        start = 1

    for i in range(start, end):
        price_ok = lo[i] > 0.0 and lo[i] <= op[i] <= hi[i] and lo[i] <= cl[i] <= hi[i]
        if not price_ok:
            enter_side = 0.0
            exit_next = False
            continue

        if in_pos:
            reason = 0
            exit_px = 0.0
            if side > 0.0 and op[i] <= stop_px:
                exit_px = op[i]
                reason = 2
            elif side < 0.0 and op[i] >= stop_px:
                exit_px = op[i]
                reason = 2
            elif exit_next:
                exit_px = op[i] * (1.0 - slip) if side > 0.0 else op[i] * (1.0 + slip)
                reason = 3
            elif side > 0.0 and lo[i] <= stop_px:
                exit_px = stop_px * (1.0 - slip)
                reason = 2
            elif side < 0.0 and hi[i] >= stop_px:
                exit_px = stop_px * (1.0 + slip)
                reason = 2
            if reason != 0:
                realized, fees, trades, wins, gross_win, gross_loss, hold_bars, n_stop, n_flip = _close(
                    side, entry, exit_px, qty, taker, entry_i, i, reason, realized, fees, trades, wins,
                    gross_win, gross_loss, hold_bars, n_stop, n_flip)
                in_pos = False
                exit_next = False
                enter_side = 0.0
                cooldown_i = i + cooldown

        if (not in_pos) and enter_side != 0.0 and i >= cooldown_i:
            side = enter_side
            entry = op[i] * (1.0 + slip) if side > 0.0 else op[i] * (1.0 - slip)
            stop_pct = atr_mult * enter_atr / entry
            if stop_pct < min_stop:
                stop_pct = min_stop
            if entry > 0.0 and stop_pct <= max_stop:
                notional = cap if cap < risk_frac * cap / stop_pct else risk_frac * cap / stop_pct
                if notional >= 5.0:
                    qty = notional / entry
                    stop_px = entry * (1.0 - stop_pct) if side > 0.0 else entry * (1.0 + stop_pct)
                    extreme = entry
                    entry_i = i
                    in_pos = True
                    if side > 0.0 and lo[i] <= stop_px:
                        exit_px = stop_px * (1.0 - slip) if op[i] > stop_px else op[i]
                        realized, fees, trades, wins, gross_win, gross_loss, hold_bars, n_stop, n_flip = _close(
                            side, entry, exit_px, qty, taker, entry_i, i, 2, realized, fees, trades, wins,
                            gross_win, gross_loss, hold_bars, n_stop, n_flip)
                        in_pos = False
                        cooldown_i = i + cooldown
                    elif side < 0.0 and hi[i] >= stop_px:
                        exit_px = stop_px * (1.0 + slip) if op[i] < stop_px else op[i]
                        realized, fees, trades, wins, gross_win, gross_loss, hold_bars, n_stop, n_flip = _close(
                            side, entry, exit_px, qty, taker, entry_i, i, 2, realized, fees, trades, wins,
                            gross_win, gross_loss, hold_bars, n_stop, n_flip)
                        in_pos = False
                        cooldown_i = i + cooldown
            enter_side = 0.0

        if in_pos and exit_mode == 1 and np.isfinite(atr[i]) and atr[i] > 0.0:
            if side > 0.0:
                if cl[i] > extreme:
                    extreme = cl[i]
                trail = extreme - atr_mult * atr[i]
                if trail > stop_px:
                    stop_px = trail
            else:
                if cl[i] < extreme:
                    extreme = cl[i]
                trail = extreme + atr_mult * atr[i]
                if trail < stop_px:
                    stop_px = trail

        if i + 1 < end and i >= cooldown_i and np.isfinite(atr[i]) and atr[i] > 0.0 and cl[i] > 0.0:
            signal = 0.0
            if mode == 0 and np.isfinite(momentum[i]):
                if momentum[i] > threshold:
                    signal = 1.0
                elif momentum[i] < -threshold:
                    signal = -1.0
            elif mode == 1 and np.isfinite(prior_high[i]) and np.isfinite(prior_low[i]):
                if cl[i] > prior_high[i]:
                    signal = 1.0
                elif cl[i] < prior_low[i]:
                    signal = -1.0
            if long_only == 1 and signal < 0.0:
                signal = 0.0
            if in_pos and exit_mode == 2:
                if (mode == 0 and signal != side) or (mode == 1 and signal == -side):
                    exit_next = True
            elif (not in_pos) and signal != 0.0:
                stop_pct = atr_mult * atr[i] / cl[i]
                if stop_pct < min_stop:
                    stop_pct = min_stop
                if stop_pct <= max_stop:
                    enter_side = signal
                    enter_atr = atr[i]

        equity_i = realized
        if in_pos:
            equity_i = realized + side * (cl[i] - entry) * qty - (entry + cl[i]) * qty * taker
        if equity_i > peak:
            peak = equity_i
        drawdown = peak - equity_i
        if drawdown > max_dd:
            max_dd = drawdown
        slot = i - start
        if 0 <= slot < equity.shape[0]:
            equity[slot] = equity_i

    if in_pos and end > start:
        i = end - 1
        exit_px = cl[i] * (1.0 - slip) if side > 0.0 else cl[i] * (1.0 + slip)
        realized, fees, trades, wins, gross_win, gross_loss, hold_bars, n_stop, n_flip = _close(
            side, entry, exit_px, qty, taker, entry_i, i, 4, realized, fees, trades, wins,
            gross_win, gross_loss, hold_bars, n_stop, n_flip)
        n_eod += 1.0
        if realized > peak:
            peak = realized
        drawdown = peak - realized
        if drawdown > max_dd:
            max_dd = drawdown

    return _empty_trend(realized, trades, wins, gross_win, gross_loss, max_dd, fees, hold_bars,
                        n_stop, n_flip, n_eod)


@njit
def _empty_trend(realized, trades, wins, gross_win, gross_loss, max_dd, fees, hold_bars, n_stop, n_flip, n_eod):
    out = np.zeros(12, dtype=np.float64)
    out[0] = realized
    out[1] = trades
    out[2] = wins
    out[3] = gross_win
    out[4] = gross_loss
    out[5] = max_dd
    out[6] = fees
    out[7] = hold_bars
    out[9] = n_stop
    out[10] = n_flip
    out[11] = n_eod
    return out
