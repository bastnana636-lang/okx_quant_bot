import numpy as np
import pandas as pd
from pathlib import Path
from test_15m_native import load_and_aggregate_15m

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
cached_data = {s: load_and_aggregate_15m(s) for s in symbols}

def simulate_15m(
    df,
    impulse_bars=16,      # 4 hours
    z_thresh=2.8,         # Z-score threshold
    stab_bars=3,          # 45 mins of stabilization
    exit_stall_bars=2,    # 30 mins of stall
    min_pullback_atr=1.0, # min pullback required
    atr_stop_buf=0.2,     # stop buffer
    max_risk_pct=0.035,   # max allowed stop loss
    trend_filter=True,    # macro trend alignment
    max_hold_bars=24      # 6 hours max hold
):
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # 1. ATR(14)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 2. 14-day baseline return volatility (1344 bars of 15m)
    ret1 = np.zeros(n)
    ret1[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret1).rolling(1344, min_periods=300).std().to_numpy()
    
    # 3. Z-score of cumulative move
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(impulse_bars)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    # 4. EMAs
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    macro_ema = pd.Series(c).ewm(span=384, adjust=False).mean().to_numpy() # 4-day EMA
    
    COST = 0.0007 # 0.05% fee + 0.02% slip
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    dump_height = 0.0
    cooldown = 0
    
    start_i = max(1344, impulse_bars + stab_bars + 1)
    
    for i in range(start_i, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # LONG check: crash exhaustion
            recent_z = z_move[i-4:i+1]
            if np.nanmin(recent_z) <= -z_thresh:
                if not trend_filter or c[i] > macro_ema[i]:
                    recent_l = l[i-impulse_bars:i+1]
                    recent_h = h[i-impulse_bars:i+1]
                    min_idx = np.argmin(recent_l)
                    bars_since_min = len(recent_l) - 1 - min_idx
                    
                    if bars_since_min >= stab_bars:
                        stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                        swing_min = np.min(recent_l)
                        swing_max = np.max(recent_h)
                        risk_pct = (c[i] - swing_min) / c[i]
                        
                        if stab_ret >= 0 and c[i] > ema9[i] and risk_pct <= max_risk_pct:
                            in_pos = 1
                            entry_p = next_o * (1 + COST)
                            entry_i = i + 1
                            dump_height = swing_max - swing_min
                            stop_p = min(swing_min - atr_stop_buf * atr[i], entry_p * (1 - max_risk_pct))
                            peak_p = entry_p
                            continue
                            
            # SHORT check: pump exhaustion
            if np.nanmax(recent_z) >= z_thresh:
                if not trend_filter or c[i] < macro_ema[i]:
                    recent_h = h[i-impulse_bars:i+1]
                    recent_l = l[i-impulse_bars:i+1]
                    max_idx = np.argmax(recent_h)
                    bars_since_max = len(recent_h) - 1 - max_idx
                    
                    if bars_since_max >= stab_bars:
                        stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                        swing_max = np.max(recent_h)
                        swing_min = np.min(recent_l)
                        risk_pct = (swing_max - c[i]) / c[i]
                        
                        if stab_ret <= 0 and c[i] < ema9[i] and risk_pct <= max_risk_pct:
                            in_pos = -1
                            entry_p = next_o * (1 - COST)
                            entry_i = i + 1
                            dump_height = swing_max - swing_min
                            stop_p = max(swing_max + atr_stop_buf * atr[i], entry_p * (1 + max_risk_pct))
                            trough_p = entry_p
                            continue
                            
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # Stop loss
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 6
                continue
                
            # Trailing breakeven
            if (peak_p - entry_p) >= 1.2 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.002
                
            # Pullback stall exit
            rebound_gain = (peak_p - entry_p) / entry_p
            min_target = max(min_pullback_atr * curr_atr / entry_p, 0.15 * dump_height / entry_p)
            if rebound_gain >= min_target:
                recent_highs = h[i-exit_stall_bars:i+1]
                stalled = (np.max(recent_highs) < peak_p) and (
                    c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr
                )
                if stalled:
                    exit_p = next_o * (1 - COST)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            if (i - entry_i) >= max_hold_bars:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 6
                continue
                
            if (entry_p - trough_p) >= 1.2 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.998
                
            drop_gain = (entry_p - trough_p) / entry_p
            min_target = max(min_pullback_atr * curr_atr / entry_p, 0.15 * dump_height / entry_p)
            if drop_gain >= min_target:
                recent_lows = l[i-exit_stall_bars:i+1]
                stalled = (np.min(recent_lows) > trough_p) and (
                    c[i] > ema9[i] or (c[i] - trough_p) >= 0.4 * curr_atr
                )
                if stalled:
                    exit_p = next_o * (1 + COST)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            if (i - entry_i) >= max_hold_bars:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
    return trades

print("Testing 15m grid configurations...")
for z in [2.5, 2.8, 3.2]:
    for imp in [12, 16, 24]: # 3h, 4h, 6h
        for stab in [2, 3]: # 30m, 45m
            tot_trades = 0
            tot_net = 0.0
            wins = 0
            for s in symbols:
                tr = simulate_15m(cached_data[s], impulse_bars=imp, z_thresh=z, stab_bars=stab, trend_filter=True)
                tot_trades += len(tr)
                tot_net += sum(x[6] for x in tr)
                wins += sum(1 for x in tr if x[6] > 0)
            wr = wins / tot_trades if tot_trades > 0 else 0
            print(f"Z={z}, Imp={imp*15}m, Stab={stab*15}m: Trades={tot_trades}, WinRate={wr:.1%}, Net={tot_net:.2%}")
