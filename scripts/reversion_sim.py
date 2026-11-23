"""Event backtest of the live mean-reversion rules on 1-minute bars.

Signal math matches calculate_snapshot / calculate_barriers:
completed signal candles only, population std, simple ATR, both the closed
candle and the live price must sit in the entry band. Orders are post-only,
live for the next minute, and a position is one at a time.

This module does not place exchange orders.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

try:
    from numba import njit
except ImportError:  # pragma: no cover - exercised only when numba is absent
    def njit(*args, **kwargs):
        def wrap(fn):
            return fn
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return wrap


MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
TAKER_SLIPPAGE = 0.0002
DEFAULT_OFFSET = 0.0002
DEFAULT_HALF_SPREAD = 0.0001
MIN_NOTIONAL = 5.0

# Exit reasons written into the result vector.
REASON_TP = 1
REASON_STOP = 2
REASON_TIME = 3
REASON_EOD = 4


class PreparedMarket(NamedTuple):
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    quote_volume: np.ndarray
    signal_index: np.ndarray
    signal_close: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    atr: np.ndarray


def prepare_market(minute_ts: np.ndarray, minute_open: np.ndarray, minute_high: np.ndarray,
                   minute_low: np.ndarray, minute_close: np.ndarray, minute_quote_volume: np.ndarray,
                   interval: int, window: int, atr_length: int = 14) -> PreparedMarket:
    """Aggregate 1-minute bars and rolling mean/std/ATR used by the live signal."""
    if interval % 60 != 0:
        raise ValueError("interval must be a multiple of 60 seconds")
    order = np.argsort(minute_ts, kind="mergesort")
    ts = np.ascontiguousarray(minute_ts[order], dtype=np.int64)
    op = np.ascontiguousarray(minute_open[order], dtype=np.float64)
    hi = np.ascontiguousarray(minute_high[order], dtype=np.float64)
    lo = np.ascontiguousarray(minute_low[order], dtype=np.float64)
    cl = np.ascontiguousarray(minute_close[order], dtype=np.float64)
    qv = np.ascontiguousarray(minute_quote_volume[order], dtype=np.float64)
    if len(ts) == 0:
        raise ValueError("no minute bars")

    bucket = (ts // interval) * interval
    frame = pd.DataFrame({"b": bucket, "o": op, "h": hi, "l": lo, "c": cl, "q": qv})
    grouped = frame.groupby("b", sort=True)
    bars = grouped.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
                       q=("q", "sum"), n=("c", "size"))
    expected = interval // 60
    full_index = np.arange(int(bars.index[0]), int(bars.index[-1]) + interval, interval, dtype=np.int64)
    bars = bars.reindex(full_index)
    incomplete = bars["n"].to_numpy(dtype=np.float64) != expected
    close = np.array(bars["c"].to_numpy(dtype=np.float64), dtype=np.float64, copy=True)
    high = np.array(bars["h"].to_numpy(dtype=np.float64), dtype=np.float64, copy=True)
    low = np.array(bars["l"].to_numpy(dtype=np.float64), dtype=np.float64, copy=True)
    close[incomplete] = np.nan
    high[incomplete] = np.nan
    low[incomplete] = np.nan

    previous = np.empty_like(close)
    previous[0] = np.nan
    previous[1:] = close[:-1]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    true_range[~np.isfinite(close) | ~np.isfinite(previous)] = np.nan
    mean = pd.Series(close).rolling(window, min_periods=window).mean().to_numpy(dtype=np.float64)
    std = pd.Series(close).rolling(window, min_periods=window).std(ddof=0).to_numpy(dtype=np.float64)
    atr = pd.Series(true_range).rolling(atr_length, min_periods=atr_length).mean().to_numpy(dtype=np.float64)

    bar_ts = full_index
    now = ts + 60
    last_open = (now // interval - 1) * interval
    pos = np.searchsorted(bar_ts, last_open)
    signal_index = np.full(len(ts), -1, dtype=np.int32)
    in_range = pos < len(bar_ts)
    clipped = np.minimum(pos, len(bar_ts) - 1)
    matched = in_range & (bar_ts[clipped] == last_open)
    signal_index[matched] = pos[matched].astype(np.int32)

    return PreparedMarket(
        ts=ts, open=op, high=hi, low=lo, close=cl, quote_volume=qv,
        signal_index=signal_index, signal_close=np.ascontiguousarray(close, dtype=np.float64),
        mean=np.ascontiguousarray(mean, dtype=np.float64),
        std=np.ascontiguousarray(std, dtype=np.float64),
        atr=np.ascontiguousarray(atr, dtype=np.float64),
    )


@njit
def _empty_result():
    return np.zeros(12, dtype=np.float64)


@njit
def simulate(ts, op, hi, lo, cl, qvol, signal_index, signal_close, mean, std, atr, mean_prev,
             entry_z, max_z, exit_z, min_std, atr_mult, min_stop, max_stop,
             cost_gate, min_edge, min_rr, tp_quote, time_limit, cooldown, sl_mult,
             trend_k, trend_max, short_z_add, notional_cap,
             offset, half_spread, maker, taker, slip, slow_mean, entry_mode,
             start, end, origin, equity_hour):
    """Replay one pair. Result slots: net, trades, wins, gross_win, gross_loss, max_dd, fees, hold_s, n_tp, n_stop, n_time, n_eod."""
    realized = 0.0
    trades = 0.0
    wins = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    max_dd = 0.0
    fees = 0.0
    hold_s = 0.0
    n_tp = 0.0
    n_stop = 0.0
    n_time = 0.0
    n_eod = 0.0
    peak = 0.0

    armed = False
    in_pos = False
    cooldown_until = 0.0
    order_side = 0.0
    order_price = 0.0
    order_notional = 0.0
    order_qty = 0.0
    order_stop = 0.0
    order_tp = 0.0
    order_expiry = 0.0
    side = 0.0
    entry = 0.0
    qty = 0.0
    stop_px = 0.0
    tp_px = 0.0
    expiry = 0.0
    fill_ts = 0

    n = ts.shape[0]
    if start < 0:
        start = 0
    if end > n:
        end = n

    for i in range(start, end):
        price_ok = lo[i] > 0.0 and lo[i] <= op[i] <= hi[i] and lo[i] <= cl[i] <= hi[i]
        if in_pos:
            if price_ok:
                reason = 0
                exit_px = 0.0
                if side > 0.0:
                    if op[i] <= stop_px:
                        exit_px = op[i]
                        reason = 2
                    elif ts[i] >= expiry:
                        exit_px = op[i] * (1.0 - slip)
                        reason = 3
                    elif lo[i] <= stop_px:
                        exit_px = stop_px * (1.0 - slip)
                        reason = 2
                    elif hi[i] >= tp_px:
                        exit_px = tp_px
                        reason = 1
                else:
                    if op[i] >= stop_px:
                        exit_px = op[i]
                        reason = 2
                    elif ts[i] >= expiry:
                        exit_px = op[i] * (1.0 + slip)
                        reason = 3
                    elif hi[i] >= stop_px:
                        exit_px = stop_px * (1.0 + slip)
                        reason = 2
                    elif lo[i] <= tp_px:
                        exit_px = tp_px
                        reason = 1
                if reason != 0 and exit_px > 0.0:
                    fee_rate = maker if reason == 1 else taker
                    gross = side * (exit_px - entry) * qty
                    fee = entry * qty * maker + exit_px * qty * fee_rate
                    pnl = gross - fee
                    realized += pnl
                    fees += fee
                    trades += 1.0
                    hold_s += float((ts[i] + 60) - fill_ts)
                    if pnl > 0.0:
                        wins += 1.0
                        gross_win += pnl
                    elif pnl < 0.0:
                        gross_loss += -pnl
                    if reason == 1:
                        n_tp += 1.0
                    elif reason == 2:
                        n_stop += 1.0
                    else:
                        n_time += 1.0
                    cool = cooldown * (sl_mult if reason == 2 else 1.0)
                    cooldown_until = float(ts[i] + 60) + cool
                    in_pos = False
        elif armed:
            filled = False
            if price_ok and qvol[i] >= order_notional:
                if order_side > 0.0 and lo[i] <= order_price:
                    filled = True
                elif order_side < 0.0 and hi[i] >= order_price:
                    filled = True
            armed = False
            if filled:
                in_pos = True
                side = order_side
                entry = order_price
                qty = order_qty
                stop_px = order_stop
                tp_px = order_tp
                expiry = order_expiry
                fill_ts = ts[i]
                reason = 0
                exit_px = 0.0
                if side > 0.0:
                    if op[i] <= stop_px:
                        exit_px = op[i]
                        reason = 2
                    elif ts[i] >= expiry:
                        exit_px = op[i] * (1.0 - slip)
                        reason = 3
                    elif lo[i] <= stop_px:
                        exit_px = stop_px * (1.0 - slip)
                        reason = 2
                    elif hi[i] >= tp_px:
                        exit_px = tp_px
                        reason = 1
                else:
                    if op[i] >= stop_px:
                        exit_px = op[i]
                        reason = 2
                    elif ts[i] >= expiry:
                        exit_px = op[i] * (1.0 + slip)
                        reason = 3
                    elif hi[i] >= stop_px:
                        exit_px = stop_px * (1.0 + slip)
                        reason = 2
                    elif lo[i] <= tp_px:
                        exit_px = tp_px
                        reason = 1
                if reason != 0 and exit_px > 0.0:
                    fee_rate = maker if reason == 1 else taker
                    gross = side * (exit_px - entry) * qty
                    fee = entry * qty * maker + exit_px * qty * fee_rate
                    pnl = gross - fee
                    realized += pnl
                    fees += fee
                    trades += 1.0
                    hold_s += float((ts[i] + 60) - fill_ts)
                    if pnl > 0.0:
                        wins += 1.0
                        gross_win += pnl
                    elif pnl < 0.0:
                        gross_loss += -pnl
                    if reason == 1:
                        n_tp += 1.0
                    elif reason == 2:
                        n_stop += 1.0
                    else:
                        n_time += 1.0
                    cool = cooldown * (sl_mult if reason == 2 else 1.0)
                    cooldown_until = float(ts[i] + 60) + cool
                    in_pos = False

        if (not in_pos) and (not armed) and price_ok and (i + 1 < end) and (float(ts[i] + 60) >= cooldown_until):
            j = signal_index[i]
            if j >= 0:
                m = mean[j]
                s = std[j]
                a = atr[j]
                closed = signal_close[j]
                live = cl[i]
                if np.isfinite(m) and np.isfinite(s) and np.isfinite(a) and np.isfinite(closed) and s > 0.0 and m > 0.0 and (s / m) >= min_std:
                    z_closed = (closed - m) / s
                    z_live = (live - m) / s
                    side_c = 0.0
                    side_l = 0.0
                    az_c = z_closed if z_closed >= 0.0 else -z_closed
                    az_l = z_live if z_live >= 0.0 else -z_live
                    if z_closed < 0.0:
                        if entry_z <= az_c <= max_z:
                            side_c = 1.0
                    else:
                        if entry_z + short_z_add <= az_c <= max_z:
                            side_c = -1.0
                    if z_live < 0.0:
                        if entry_z <= az_l <= max_z:
                            side_l = 1.0
                    else:
                        if entry_z + short_z_add <= az_l <= max_z:
                            side_l = -1.0
                    if side_c != 0.0 and side_c == side_l:
                        if trend_k > 0:
                            prev_mean = mean_prev[j]
                            if not np.isfinite(prev_mean):
                                side_c = 0.0
                            else:
                                drift = (m - prev_mean) / s
                                if side_c > 0.0 and drift < -trend_max:
                                    side_c = 0.0
                                elif side_c < 0.0 and drift > trend_max:
                                    side_c = 0.0
                        if entry_mode == 2 or entry_mode == 3:
                            if side_c < 0.0:
                                side_c = 0.0
                        if (entry_mode == 1 or entry_mode == 3) and side_c != 0.0:
                            slow = slow_mean[j]
                            if not np.isfinite(slow):
                                side_c = 0.0
                            elif side_c > 0.0 and (closed <= slow or live <= slow):
                                side_c = 0.0
                            elif side_c < 0.0 and (closed >= slow or live >= slow):
                                side_c = 0.0
                        if side_c != 0.0 and tp_quote > 0.0 and notional_cap > 0.0:
                            pad = offset if offset > half_spread else half_spread
                            if side_c > 0.0:
                                price = live * (1.0 - pad)
                            else:
                                price = live * (1.0 + pad)
                            if price > 0.0:
                                z_entry = (price - m) / s
                                in_band = False
                                if side_c > 0.0:
                                    in_band = (-max_z <= z_entry <= -entry_z)
                                else:
                                    in_band = (entry_z + short_z_add <= z_entry <= max_z)
                                if in_band:
                                    target = m - side_c * exit_z * s
                                    reward = side_c * (target - price) / price
                                    stop = min_stop
                                    atr_stop = atr_mult * a / price
                                    if atr_stop > stop:
                                        stop = atr_stop
                                    if reward > 0.0 and stop <= max_stop:
                                        net_reward = reward - cost_gate
                                        denom = stop + cost_gate
                                        if net_reward >= min_edge and denom > 0.0 and (net_reward / denom) >= min_rr:
                                            risk_budget = tp_quote / (min_rr * denom)
                                            notional = notional_cap if notional_cap < risk_budget else risk_budget
                                            if notional >= MIN_NOTIONAL:
                                                order_qty = notional / price
                                                if side_c > 0.0:
                                                    cash_px = price + tp_quote / order_qty
                                                    take_px = target if target < cash_px else cash_px
                                                    stop_level = price * (1.0 - stop - maker)
                                                else:
                                                    cash_px = price - tp_quote / order_qty
                                                    take_px = target if target > cash_px else cash_px
                                                    stop_level = price * (1.0 + stop + maker)
                                                if take_px > 0.0 and stop_level > 0.0:
                                                    armed = True
                                                    order_side = side_c
                                                    order_price = price
                                                    order_notional = notional
                                                    order_stop = stop_level
                                                    order_tp = take_px
                                                    order_expiry = float(ts[i] + 60 + time_limit)

        equity = realized
        if in_pos:
            equity = realized + side * (cl[i] - entry) * qty - entry * qty * maker - cl[i] * qty * taker
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        if origin >= 0:
            hour = (ts[i] - origin) // 3600
            if 0 <= hour < equity_hour.shape[0]:
                equity_hour[hour] = equity

    if in_pos and end > start:
        i = end - 1
        if side > 0.0:
            exit_px = cl[i] * (1.0 - slip)
        else:
            exit_px = cl[i] * (1.0 + slip)
        if exit_px > 0.0:
            gross = side * (exit_px - entry) * qty
            fee = entry * qty * maker + exit_px * qty * taker
            pnl = gross - fee
            realized += pnl
            fees += fee
            trades += 1.0
            hold_s += float((ts[i] + 60) - fill_ts)
            if pnl > 0.0:
                wins += 1.0
                gross_win += pnl
            elif pnl < 0.0:
                gross_loss += -pnl
            n_eod += 1.0
            in_pos = False
            equity = realized
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > max_dd:
                max_dd = drawdown

    out = _empty_result()
    out[0] = realized
    out[1] = trades
    out[2] = wins
    out[3] = gross_win
    out[4] = gross_loss
    out[5] = max_dd
    out[6] = fees
    out[7] = hold_s
    out[8] = n_tp
    out[9] = n_stop
    out[10] = n_time
    out[11] = n_eod
    return out


def shift_mean(mean: np.ndarray, trend_k: int) -> np.ndarray:
    previous = np.full(mean.shape, np.nan, dtype=np.float64)
    if trend_k > 0:
        previous[trend_k:] = mean[:-trend_k]
    return previous


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return np.full(values.shape, np.nan, dtype=np.float64)
    return pd.Series(values).rolling(window, min_periods=window).mean().to_numpy(dtype=np.float64)
