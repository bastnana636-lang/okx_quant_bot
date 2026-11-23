import numpy as np
import pandas as pd
from test_reversal_prototype2 import load_data, aggregate_candles

def analyze(symbol='SUIUSDT'):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 300)
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    n = len(c)
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(24, min_periods=24).mean().to_numpy()
    
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(864, min_periods=288).std().to_numpy()
    
    # 2-hour move
    move_window = 24
    c_shift = pd.Series(c).shift(move_window).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(move_window), 1e-6)
    
    dump_events = []
    pump_events = []
    
    # Find events where |z_move| >= 3.0 and then stabilized (no new extreme for 4 bars)
    i = 864
    while i < n - 48:
        if z_move[i] <= -3.0:
            # check stabilization over next 4 to 8 bars
            # look for local trough
            trough_idx = i
            for j in range(i, min(i + 12, n - 48)):
                if l[j] < l[trough_idx]:
                    trough_idx = j
            
            # Did it stabilize for at least 4 bars after trough?
            if trough_idx + 4 < n - 48:
                # check if low in trough_idx+1..trough_idx+4 >= l[trough_idx]
                if np.min(l[trough_idx+1:trough_idx+5]) >= l[trough_idx]:
                    entry_p = c[trough_idx+4]
                    # look forward 48 bars (4 hours)
                    fwd_h = h[trough_idx+5:trough_idx+49]
                    fwd_l = l[trough_idx+5:trough_idx+49]
                    fwd_c = c[trough_idx+5:trough_idx+49]
                    
                    max_gain = (np.max(fwd_h) - entry_p) / entry_p
                    max_drawdown = (np.min(fwd_l) - entry_p) / entry_p
                    dump_events.append((entry_p, max_gain, max_drawdown, atr[trough_idx]/entry_p))
                    i = trough_idx + 48 # skip ahead
                    continue
        i += 1
        
    print(f"{symbol} Dump Reversal Events: {len(dump_events)}")
    if dump_events:
        d_df = pd.DataFrame(dump_events, columns=['entry_p', 'max_gain', 'max_dd', 'natr'])
        print(f"Mean Max Rebound: {d_df['max_gain'].mean():.2%}, Median: {d_df['max_gain'].median():.2%}")
        print(f"Mean Max Drawdown: {d_df['max_dd'].mean():.2%}, Median: {d_df['max_dd'].median():.2%}")
        print(f"Gain >= 1.5%: {(d_df['max_gain'] >= 0.015).mean():.2%}")
        print(f"Gain >= 3.0%: {(d_df['max_gain'] >= 0.03).mean():.2%}")
        print(f"DD <= -2.0%: {(d_df['max_dd'] <= -0.02).mean():.2%}")

for s in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'SUIUSDT', 'DOGEUSDT']:
    analyze(s)
