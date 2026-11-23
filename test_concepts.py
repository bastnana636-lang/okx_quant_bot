import numpy as np
import pandas as pd
from scalping_engine import load_all_symbols, SYMBOLS_10, evaluate_trades

data = load_all_symbols()

# Concept 1: Dual EMA (e.g. 9 & 21) with Trend Filter (e.g. 100) and ATR trailing
def run_dual_ema(df, fast=9, slow=21, trend=100, atr_len=14, trail_atr=2.5, fee=0.0006):
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    n = len(c)
    
    ef = pd.Series(c).ewm(span=fast, adjust=False).mean().to_numpy()
    es = pd.Series(c).ewm(span=slow, adjust=False).mean().to_numpy()
    et = pd.Series(c).ewm(span=trend, adjust=False).mean().to_numpy()
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(atr_len, min_periods=atr_len).mean().to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    
    for i in range(trend + 5, n - 1):
        next_o = o[i+1]
        if in_pos == 1:
            stop_p = max(stop_p, h[i] - trail_atr * atr[i])
            if l[i] <= stop_p or (ef[i] < es[i]):
                exit_p = min(c[i], stop_p)
                trades.append({'ret': (exit_p - entry_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
        elif in_pos == -1:
            stop_p = min(stop_p, l[i] + trail_atr * atr[i])
            if h[i] >= stop_p or (ef[i] > es[i]):
                exit_p = max(c[i], stop_p)
                trades.append({'ret': (entry_p - exit_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
                
        if in_pos == 0:
            if ef[i] > es[i] and ef[i-1] <= es[i-1] and c[i] > et[i]:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                stop_p = entry_p - trail_atr * atr[i]
            elif ef[i] < es[i] and ef[i-1] >= es[i-1] and c[i] < et[i]:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                stop_p = entry_p + trail_atr * atr[i]
                
    return evaluate_trades(trades)

# Concept 2: RSI Exhaustion Bounce / Reversal
def run_rsi_reversal(df, rsi_len=14, ob=75, os=25, tp_pct=0.015, sl_pct=0.01, max_hold=20, fee=0.0006):
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    n = len(c)
    
    diff = pd.Series(c).diff()
    up = diff.clip(lower=0)
    down = -1 * diff.clip(upper=0)
    ma_up = up.ewm(alpha=1/rsi_len, adjust=False).mean()
    ma_down = down.ewm(alpha=1/rsi_len, adjust=False).mean()
    rsi = (100 - (100 / (1 + ma_up / (ma_down + 1e-9)))).to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    
    for i in range(rsi_len + 5, n - 1):
        next_o = o[i+1]
        if in_pos == 1:
            exit_p = None
            if h[i] >= tp_p:
                exit_p = tp_p
            elif l[i] <= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = c[i]
            if exit_p is not None:
                trades.append({'ret': (exit_p - entry_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
        elif in_pos == -1:
            exit_p = None
            if l[i] <= tp_p:
                exit_p = tp_p
            elif h[i] >= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = c[i]
            if exit_p is not None:
                trades.append({'ret': (entry_p - exit_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
                
        if in_pos == 0:
            if rsi[i-1] <= os and rsi[i] > os and c[i] > o[i]:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                stop_p = entry_p * (1 - sl_pct)
                tp_p = entry_p * (1 + tp_pct)
            elif rsi[i-1] >= ob and rsi[i] < ob and c[i] < o[i]:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                stop_p = entry_p * (1 + sl_pct)
                tp_p = entry_p * (1 - tp_pct)
                
    return evaluate_trades(trades)

# Concept 3: Volume Surge Momentum Ignition (Volume Spike + Breakout)
def run_volume_ignition(df, vol_mult=2.5, break_len=15, trend_len=100, tp_mult=2.5, sl_mult=1.2, max_hold=24, fee=0.0006):
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['vol'].to_numpy(dtype=np.float64)
    n = len(c)
    
    vol_ma = pd.Series(v).rolling(30, min_periods=10).mean().to_numpy()
    ema_t = pd.Series(c).ewm(span=trend_len, adjust=False).mean().to_numpy()
    hh = pd.Series(h).shift(1).rolling(break_len).max().to_numpy()
    ll = pd.Series(l).shift(1).rolling(break_len).min().to_numpy()
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    
    for i in range(max(trend_len, break_len) + 5, n - 1):
        next_o = o[i+1]
        curr_atr = atr[i]
        if in_pos == 1:
            exit_p = None
            if h[i] >= tp_p:
                exit_p = tp_p
            elif l[i] <= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = c[i]
            if exit_p is not None:
                trades.append({'ret': (exit_p - entry_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
        elif in_pos == -1:
            exit_p = None
            if l[i] <= tp_p:
                exit_p = tp_p
            elif h[i] >= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = c[i]
            if exit_p is not None:
                trades.append({'ret': (entry_p - exit_p)/entry_p - 2*fee, 'bars': i - entry_i})
                in_pos = 0
                continue
                
        if in_pos == 0:
            vol_spike = (v[i] >= vol_mult * vol_ma[i])
            if vol_spike and c[i] > hh[i] and c[i] > ema_t[i] and c[i] > o[i]:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                risk = max(curr_atr * sl_mult, entry_p * 0.004)
                stop_p = entry_p - risk
                tp_p = entry_p + tp_mult * risk
            elif vol_spike and c[i] < ll[i] and c[i] < ema_t[i] and c[i] < o[i]:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                risk = max(curr_atr * sl_mult, entry_p * 0.004)
                stop_p = entry_p + risk
                tp_p = entry_p - tp_mult * risk
                
    return evaluate_trades(trades)

if __name__ == '__main__':
    for name, fn in [
        ('Dual EMA Cross', run_dual_ema),
        ('RSI Reversal', run_rsi_reversal),
        ('Volume Surge Ignition', run_volume_ignition)
    ]:
        rets = []
        pfs = []
        trades = []
        for s in SYMBOLS_10:
            res = fn(data[s])
            rets.append(res.total_ret)
            pfs.append(res.profit_factor)
            trades.append(res.trades_count)
        print(f'{name:22s} | Avg Ret: {np.mean(rets):+6.2f}% | Med Ret: {np.median(rets):+6.2f}% | Avg PF: {np.mean(pfs):.2f} | Total Trades: {sum(trades)}')
