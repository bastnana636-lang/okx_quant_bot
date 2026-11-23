import numpy as np
import pandas as pd
from test_15m_native import load_and_aggregate_15m

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
cached_data = {s: load_and_aggregate_15m(s) for s in symbols}

def simulate(
    df,
    impulse_bars=16,
    z_long=2.8,
    z_short=3.5,
    vol_mult=1.5,
    tp_atr_mult=2.0,       # Take profit at N * ATR
    trail_atr_mult=1.0,    # Trail at N * ATR
    max_risk_allowed=0.015,
    max_hold_bars=12,
    trend_filter=True,
    long_only=False
):
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    v = df["v"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # 1. ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 2. 14d base std
    ret1 = np.zeros(n)
    ret1[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret1).rolling(1344, min_periods=300).std().to_numpy()
    
    # 3. Z-score
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(impulse_bars)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    vol_ma20 = pd.Series(v).rolling(20, min_periods=10).mean().to_numpy()
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    macro_ema = pd.Series(c).ewm(span=384, adjust=False).mean().to_numpy()
    
    COST = 0.0007
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    cooldown = 0
    
    start_i = max(1344, impulse_bars + 4)
    
    for i in range(start_i, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # LONG:
            recent_z = z_move[i-4:i+1]
            if np.nanmin(recent_z) <= -z_long:
                if not trend_filter or c[i] > macro_ema[i]:
                    recent_v = v[i-impulse_bars:i+1]
                    recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                    had_vol_spike = np.any(recent_v >= vol_mult * recent_v_ma)
                    
                    recent_l = l[i-impulse_bars:i+1]
                    min_idx = np.argmin(recent_l)
                    bars_since_min = len(recent_l) - 1 - min_idx
                    
                    if had_vol_spike and bars_since_min >= 2:
                        swing_min = np.min(recent_l)
                        risk_dist = (c[i] - swing_min) / c[i]
                        
                        body = abs(c[i] - o[i])
                        lower_wick = min(c[i], o[i]) - l[i]
                        has_rejection = (lower_wick >= 0.7 * body) or (c[i] > o[i] and c[i] > c[i-1])
                        
                        if has_rejection and c[i] > ema9[i] and risk_dist <= max_risk_allowed:
                            in_pos = 1
                            entry_p = next_o * (1 + COST)
                            entry_i = i + 1
                            stop_p = swing_min * 0.998
                            peak_p = entry_p
                            continue
                            
            # SHORT:
            if not long_only and np.nanmax(recent_z) >= z_short:
                if not trend_filter or c[i] < macro_ema[i]:
                    recent_v = v[i-impulse_bars:i+1]
                    recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                    had_vol_spike = np.any(recent_v >= vol_mult * recent_v_ma)
                    
                    recent_h = h[i-impulse_bars:i+1]
                    max_idx = np.argmax(recent_h)
                    bars_since_max = len(recent_h) - 1 - max_idx
                    
                    if had_vol_spike and bars_since_max >= 2:
                        swing_max = np.max(recent_h)
                        risk_dist = (swing_max - c[i]) / c[i]
                        
                        body = abs(c[i] - o[i])
                        upper_wick = h[i] - max(c[i], o[i])
                        has_rejection = (upper_wick >= 0.7 * body) or (c[i] < o[i] and c[i] < c[i-1])
                        
                        if has_rejection and c[i] < ema9[i] and risk_dist <= max_risk_allowed:
                            in_pos = -1
                            entry_p = next_o * (1 - COST)
                            entry_i = i + 1
                            stop_p = swing_max * 1.002
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
                cooldown = i + 4
                continue
                
            # Trailing stop
            if (peak_p - entry_p) >= 1.0 * curr_atr:
                stop_p = max(stop_p, entry_p * 1.001) # BE
            if (peak_p - entry_p) >= 1.8 * curr_atr:
                stop_p = max(stop_p, peak_p - trail_atr_mult * curr_atr)
                
            # Take Profit target
            if (h[i] - entry_p) >= tp_atr_mult * curr_atr:
                exit_p = (entry_p + tp_atr_mult * curr_atr) * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TP_ATR', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # Stall exit
            if (peak_p - entry_p) >= 0.8 * curr_atr:
                recent_h = h[i-2:i+1]
                if np.max(recent_h) < peak_p and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr):
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
                cooldown = i + 4
                continue
                
            if (entry_p - trough_p) >= 1.0 * curr_atr:
                stop_p = min(stop_p, entry_p * 0.999)
            if (entry_p - trough_p) >= 1.8 * curr_atr:
                stop_p = min(stop_p, trough_p + trail_atr_mult * curr_atr)
                
            if (entry_p - l[i]) >= tp_atr_mult * curr_atr:
                exit_p = (entry_p - tp_atr_mult * curr_atr) * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TP_ATR', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            if (entry_p - trough_p) >= 0.8 * curr_atr:
                recent_l = l[i-2:i+1]
                if np.min(recent_l) > trough_p and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.4 * curr_atr):
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

# Search grid
results = []
for z_l in [2.4, 2.8, 3.2]:
    for z_s in [3.2, 3.6, 4.0]:
        for tp_atr in [1.5, 2.0, 2.5]:
            for tf in [True, False]:
                for lo in [False, True]:
                    all_t = []
                    for s in symbols:
                        tr = simulate(
                            cached_data[s],
                            z_long=z_l,
                            z_short=z_s,
                            tp_atr_mult=tp_atr,
                            trend_filter=tf,
                            long_only=lo
                        )
                        all_t.extend(tr)
                    if not all_t:
                        continue
                    pnls = [x[6] for x in all_t]
                    wins = [p for p in pnls if p > 0]
                    losses = [p for p in pnls if p <= 0]
                    net = sum(pnls)
                    wr = len(wins)/len(pnls)
                    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else 0
                    results.append({
                        'z_l': z_l, 'z_s': z_s, 'tp_atr': tp_atr,
                        'tf': tf, 'lo': lo, 'trades': len(all_t),
                        'wr': wr, 'pf': pf, 'net': net
                    })

res_df = pd.DataFrame(results)
positives = res_df[res_df['net'] > 0].sort_values('net', ascending=False)
print(f"Total combinations tested: {len(res_df)}")
print(f"Positive combinations found: {len(positives)}")
if not positives.empty:
    print("\nTop 10 Positive Configurations:")
    print(positives.head(10).to_string(index=False))
else:
    print("\nBest near-positive configurations:")
    print(res_df.sort_values('net', ascending=False).head(10).to_string(index=False))
