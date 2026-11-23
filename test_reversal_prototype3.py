import numpy as np
import pandas as pd
from test_reversal_prototype2 import load_data, aggregate_candles

def test_symbol_v3(symbol, z_thresh=3.0, move_window=36, stab_window=6, exit_stall_window=4):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 300)
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['v'].to_numpy(dtype=np.float64)
    n = len(c)
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(24, min_periods=24).mean().to_numpy()
    
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(864, min_periods=288).std().to_numpy()
    
    c_shift = pd.Series(c).shift(move_window).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(move_window), 1e-6)
    
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    ema21 = pd.Series(c).ewm(span=21, adjust=False).mean().to_numpy()
    
    TAKER_FEE = 0.0005
    SLIP = 0.0002
    COST = (TAKER_FEE + SLIP)
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    dump_height = 0.0
    
    for i in range(864, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            recent_z = z_move[i-12:i+1]
            min_z = np.nanmin(recent_z)
            if min_z <= -z_thresh:
                crash_lows = l[i-move_window:i+1]
                crash_highs = h[i-move_window:i+1]
                min_idx = np.argmin(crash_lows)
                bars_since_min = len(crash_lows) - 1 - min_idx
                
                if bars_since_min >= stab_window:
                    recent_c = c[i-stab_window:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_min = np.min(crash_lows)
                    swing_max = np.max(crash_highs)
                    h_wave = swing_max - swing_min
                    risk_dist = (c[i] - swing_min) / c[i]
                    
                    # Stabilization: price >= EMA9 and within reasonable distance from low
                    if stab_ret >= 0 and c[i] > ema9[i] and risk_dist <= 0.025:
                        in_pos = 1
                        entry_p = next_o * (1 + SLIP)
                        entry_i = i + 1
                        dump_height = h_wave
                        # Initial stop below swing low
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.98)
                        peak_p = entry_p
                        continue
                        
            max_z = np.nanmax(recent_z)
            if max_z >= z_thresh:
                crash_highs = h[i-move_window:i+1]
                crash_lows = l[i-move_window:i+1]
                max_idx = np.argmax(crash_highs)
                bars_since_max = len(crash_highs) - 1 - max_idx
                
                if bars_since_max >= stab_window:
                    recent_c = c[i-stab_window:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_max = np.max(crash_highs)
                    swing_min = np.min(crash_lows)
                    h_wave = swing_max - swing_min
                    risk_dist = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema9[i] and risk_dist <= 0.025:
                        in_pos = -1
                        entry_p = next_o * (1 - SLIP)
                        entry_i = i + 1
                        dump_height = h_wave
                        stop_p = max(swing_max + 0.2 * atr[i], entry_p * 1.02)
                        trough_p = entry_p
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # Hard stop
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                continue
            
            # Move stop to breakeven once price is up 1.5 ATR
            if (peak_p - entry_p) >= 1.5 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.001
                
            # Pullback stall check:
            # Need a decent pullback (either >= 2.0 ATR or >= 25% retracement of the dump wave)
            min_rebound = max(1.8 * curr_atr, 0.25 * dump_height)
            if (peak_p - entry_p) >= min_rebound:
                # Stall definition: for exit_stall_window bars, price has failed to push higher
                # and closed below EMA9 or dropped by 0.5 ATR from peak
                recent_highs = h[i-exit_stall_window:i+1]
                stalled = (np.max(recent_highs) < peak_p) and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.6 * curr_atr)
                if stalled:
                    exit_p = next_o * (1 - SLIP)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    continue
                    
            # Fast timeout if no bounce within 36 bars (3 hours)
            if (i - entry_i) >= 36 and (peak_p - entry_p) < 1.0 * curr_atr:
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'FAIL_TIMEOUT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                continue
                
            # Max timeout 72 bars (6 hours)
            if (i - entry_i) >= 72:
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'MAX_TIME', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                continue
                
            if (entry_p - trough_p) >= 1.5 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.999
                
            min_rebound = max(1.8 * curr_atr, 0.25 * dump_height)
            if (entry_p - trough_p) >= min_rebound:
                recent_lows = l[i-exit_stall_window:i+1]
                stalled = (np.min(recent_lows) > trough_p) and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.6 * curr_atr)
                if stalled:
                    exit_p = next_o * (1 + SLIP)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    continue
                    
            if (i - entry_i) >= 36 and (entry_p - trough_p) < 1.0 * curr_atr:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'FAIL_TIMEOUT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                continue
                
            if (i - entry_i) >= 72:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'MAX_TIME', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                continue
                
    return trades

symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'SUIUSDT', 'ADAUSDT', 'ZECUSDT', 'XRPUSDT']
all_res = []
for s in symbols:
    tr = test_symbol_v3(s, z_thresh=3.2, move_window=36, stab_window=6, exit_stall_window=4)
    if tr:
        df_t = pd.DataFrame(tr, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
        wr = (df_t['net_ret'] > 0).mean()
        tot = df_t['net_ret'].sum()
        cnt = len(df_t)
        wins = df_t[df_t['net_ret'] > 0]['net_ret']
        losses = df_t[df_t['net_ret'] <= 0]['net_ret']
        pf = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else np.nan
        all_res.append({'symbol': s, 'trades': cnt, 'win_rate': wr, 'total_net_ret': tot, 'profit_factor': pf})

res_df = pd.DataFrame(all_res)
print(res_df.to_string(index=False))
