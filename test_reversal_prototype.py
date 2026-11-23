import numpy as np
import pandas as pd
from pathlib import Path

def load_data(symbol):
    path = Path(f'data/backtest/binance_um_1m_year/{symbol}.npz')
    data = np.load(path)['data']
    order = np.argsort(data[:, 0], kind='mergesort')
    data = data[order]
    _, idx = np.unique(data[:, 0], return_index=True)
    data = data[np.sort(idx)]
    return data

def aggregate_candles(data, interval_sec=300): # 5 min default
    ts = data[:, 0].astype(np.int64)
    op = data[:, 1]
    hi = data[:, 2]
    lo = data[:, 3]
    cl = data[:, 4]
    vol = data[:, 5]
    
    bucket = (ts // interval_sec) * interval_sec
    df = pd.DataFrame({'b': bucket, 'o': op, 'h': hi, 'l': lo, 'c': cl, 'v': vol})
    bars = df.groupby('b', sort=True).agg({
        'o': 'first',
        'h': 'max',
        'l': 'min',
        'c': 'last',
        'v': 'sum'
    })
    
    full_idx = np.arange(bars.index[0], bars.index[-1] + interval_sec, interval_sec, dtype=np.int64)
    bars = bars.reindex(full_idx)
    bars['c'] = bars['c'].ffill()
    bars['o'] = bars['o'].fillna(bars['c'])
    bars['h'] = bars['h'].fillna(bars['c'])
    bars['l'] = bars['l'].fillna(bars['c'])
    bars['v'] = bars['v'].fillna(0)
    bars['ts'] = bars.index
    return bars.reset_index(drop=True)

def run_backtest_symbol(symbol, z_thresh=2.8, move_lookback=24, base_window=864, stab_bars=4, exit_stall_bars=3):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 300)
    
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    ts = df['ts'].to_numpy(dtype=np.int64)
    n = len(c)
    
    # 1. TR & ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 2. Adaptive Volatility (baseline rolling std of returns)
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(base_window, min_periods=base_window // 2).std().to_numpy()
    
    # 3. Normalized Move Z-score over move_lookback (e.g. 24 bars = 2 hours)
    c_shift = pd.Series(c).shift(move_lookback).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(move_lookback)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    # Micro EMA for stabilization confirmation
    ema5 = pd.Series(c).ewm(span=5, adjust=False).mean().to_numpy()
    
    # Fees and slippage
    TAKER_FEE = 0.0005
    SLIP = 0.0002
    COST = (TAKER_FEE + SLIP)
    
    # Simulation
    in_pos = 0 # 1: Long, -1: Short
    entry_price = 0.0
    entry_idx = 0
    stop_price = 0.0
    peak_price = 0.0
    trough_price = 0.0
    min_pullback_met = False
    
    trades = []
    
    # We can start after base_window
    start_i = max(base_window, move_lookback + stab_bars + 1)
    
    for i in range(start_i, n - 1):
        curr_p = c[i]
        next_o = o[i+1] # fill on next bar open
        
        if in_pos == 0:
            # Check for Long condition:
            # Recent extreme dump: min z_move in last 8 bars <= -z_thresh
            recent_z = z_move[i-8:i+1]
            had_dump = np.nanmin(recent_z) <= -z_thresh
            
            if had_dump:
                # Check stabilization (跌到位):
                # Lowest low in recent move_lookback occurred >= stab_bars ago
                look_lo = l[i-move_lookback:i+1]
                min_idx = np.argmin(look_lo)
                # how many bars ago was the lowest low?
                bars_since_min = len(look_lo) - 1 - min_idx
                
                # Rate of change in last stab_bars is flat/rebounding:
                stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                
                # Price above EMA5 and didn't make lower low in last stab_bars
                if bars_since_min >= stab_bars and stab_ret >= 0 and c[i] >= ema5[i]:
                    # Enter LONG at next bar open
                    in_pos = 1
                    entry_price = next_o * (1 + SLIP)
                    entry_idx = i + 1
                    lowest_pt = np.min(look_lo)
                    # Stop loss slightly below the lowest point of the dump (or 1.5 ATR)
                    stop_price = min(lowest_pt - 0.5 * atr[i], entry_price * (1 - 0.03))
                    stop_price = max(stop_price, entry_price * 0.94) # at most 6% stop
                    peak_price = entry_price
                    min_pullback_met = False
                    continue
            
            # Check for Short condition:
            # Recent extreme pump: max z_move in last 8 bars >= z_thresh
            had_pump = np.nanmax(recent_z) >= z_thresh
            if had_pump:
                look_hi = h[i-move_lookback:i+1]
                max_idx = np.argmax(look_hi)
                bars_since_max = len(look_hi) - 1 - max_idx
                stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                
                if bars_since_max >= stab_bars and stab_ret <= 0 and c[i] <= ema5[i]:
                    # Enter SHORT at next bar open
                    in_pos = -1
                    entry_price = next_o * (1 - SLIP)
                    entry_idx = i + 1
                    highest_pt = np.max(look_hi)
                    stop_price = max(highest_pt + 0.5 * atr[i], entry_price * (1 + 0.03))
                    stop_price = min(stop_price, entry_price * 1.06)
                    trough_price = entry_price
                    min_pullback_met = False
                    continue
        
        elif in_pos == 1: # LONG
            # Update peak price
            peak_price = max(peak_price, h[i])
            curr_atr = atr[entry_idx]
            
            # Check if minimum pullback achieved (e.g. 0.8 * ATR above entry)
            if (peak_price - entry_price) >= 0.8 * curr_atr:
                min_pullback_met = True
            
            # Exit 1: Stop-loss hit
            if l[i] <= stop_price:
                exit_price = min(next_o, stop_price) * (1 - SLIP)
                raw_ret = (exit_price - entry_price) / entry_price
                net_ret = raw_ret - 2 * COST
                trades.append(('LONG', 'STOP', entry_idx, i, entry_price, exit_price, net_ret))
                in_pos = 0
                continue
            
            # Exit 2: Pullback momentum stalled (一段时间以内没有继续快速上涨)
            if min_pullback_met:
                # Have we stalled for exit_stall_bars?
                # Stalled means: high of last exit_stall_bars did not beat peak_price,
                # and recent return is non-positive or dropped below EMA5
                recent_c = c[i-exit_stall_bars:i+1]
                stalled = (c[i] < ema5[i]) or (c[i] <= recent_c[0])
                if stalled:
                    exit_price = next_o * (1 - SLIP)
                    raw_ret = (exit_price - entry_price) / entry_price
                    net_ret = raw_ret - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_idx, i+1, entry_price, exit_price, net_ret))
                    in_pos = 0
                    continue
            
            # Exit 3: Max hold time limit (e.g. 48 bars = 4 hours)
            if (i - entry_idx) >= 48:
                exit_price = next_o * (1 - SLIP)
                raw_ret = (exit_price - entry_price) / entry_price
                net_ret = raw_ret - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_idx, i+1, entry_price, exit_price, net_ret))
                in_pos = 0
                continue
                
        elif in_pos == -1: # SHORT
            trough_price = min(trough_price, l[i])
            curr_atr = atr[entry_idx]
            
            if (entry_price - trough_price) >= 0.8 * curr_atr:
                min_pullback_met = True
                
            # Exit 1: Stop-loss hit
            if h[i] >= stop_price:
                exit_price = max(next_o, stop_price) * (1 + SLIP)
                raw_ret = (entry_price - exit_price) / entry_price
                net_ret = raw_ret - 2 * COST
                trades.append(('SHORT', 'STOP', entry_idx, i, entry_price, exit_price, net_ret))
                in_pos = 0
                continue
                
            # Exit 2: Pullback stall
            if min_pullback_met:
                recent_c = c[i-exit_stall_bars:i+1]
                stalled = (c[i] > ema5[i]) or (c[i] >= recent_c[0])
                if stalled:
                    exit_price = next_o * (1 + SLIP)
                    raw_ret = (entry_price - exit_price) / entry_price
                    net_ret = raw_ret - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_idx, i+1, entry_price, exit_price, net_ret))
                    in_pos = 0
                    continue
                    
            # Exit 3: Time limit
            if (i - entry_idx) >= 48:
                exit_price = next_o * (1 + SLIP)
                raw_ret = (entry_price - exit_price) / entry_price
                net_ret = raw_ret - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_idx, i+1, entry_price, exit_price, net_ret))
                in_pos = 0
                continue

    return trades

trades = run_backtest_symbol('SUIUSDT')
print(f"Total trades: {len(trades)}")
if trades:
    df_t = pd.DataFrame(trades, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
    win_rate = (df_t['net_ret'] > 0).mean()
    total_ret = df_t['net_ret'].sum()
    print(f"Win rate: {win_rate:.2%}, Cumulative net return: {total_ret:.2%}")
    print(df_t['reason'].value_counts())
    print("Top 5 wins:")
    print(df_t.sort_values('net_ret', ascending=False)[['side', 'reason', 'net_ret']].head(5))
    print("Top 5 losses:")
    print(df_t.sort_values('net_ret')[['side', 'reason', 'net_ret']].head(5))
