import numpy as np
import pandas as pd
from test_15m_native import load_and_aggregate_15m

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
cached_data = {s: load_and_aggregate_15m(s) for s in symbols}

def run_smart_exhaustion(
    df,
    impulse_bars=16,          # 4h impulse
    z_long=2.5,               # Dump Z-score threshold
    z_short=3.6,              # Strict pump Z-score (avoid squeeze)
    vol_climax_mult=1.6,      # Volume Climax filter
    max_risk_entry=0.015,     # Hard gate: don't enter if stop distance > 1.5%
    trail_be_atr=1.0,         # Move to BE at 1.0 ATR
    stall_lookback=2,         # 30m stall check
    max_hold_bars=10          # 2.5 hours max hold
):
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    v = df["v"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # 1. ATR(14)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 2. Baseline volatility (14d)
    ret1 = np.zeros(n)
    ret1[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret1).rolling(1344, min_periods=300).std().to_numpy()
    
    # 3. Z-score
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(impulse_bars)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    # 4. Volume MA20
    vol_ma20 = pd.Series(v).rolling(20, min_periods=10).mean().to_numpy()
    
    # 5. Indicators
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    sma20 = pd.Series(c).rolling(20, min_periods=10).mean().to_numpy()
    
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
                
            # LONG check (Buying Crash Exhaustion):
            recent_z = z_move[i-4:i+1]
            if np.nanmin(recent_z) <= -z_long:
                # 1. Volume Climax check
                recent_v = v[i-impulse_bars:i+1]
                recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                had_vol_spike = np.any(recent_v >= vol_climax_mult * recent_v_ma)
                
                # 2. Swing low anchor >= 2 bars ago
                recent_l = l[i-impulse_bars:i+1]
                min_idx = np.argmin(recent_l)
                bars_since_min = len(recent_l) - 1 - min_idx
                
                if had_vol_spike and bars_since_min >= 2:
                    swing_min = np.min(recent_l)
                    risk_dist = (c[i] - swing_min) / c[i]
                    
                    # 3. Pin bar / Reversal candle check
                    body = abs(c[i] - o[i])
                    lower_wick = min(c[i], o[i]) - l[i]
                    has_rejection = (lower_wick >= 0.8 * body) or (c[i] > o[i] and c[i] > c[i-1])
                    
                    # 4. Strict Risk Gate: Only enter if risk to swing low <= max_risk_entry
                    if has_rejection and c[i] > ema9[i] and risk_dist <= max_risk_entry:
                        in_pos = 1
                        entry_p = next_o * (1 + COST)
                        entry_i = i + 1
                        stop_p = swing_min * 0.998
                        peak_p = entry_p
                        continue
                        
            # SHORT check (Selling Pump Exhaustion):
            if np.nanmax(recent_z) >= z_short:
                recent_v = v[i-impulse_bars:i+1]
                recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                had_vol_spike = np.any(recent_v >= vol_climax_mult * recent_v_ma)
                
                recent_h = h[i-impulse_bars:i+1]
                max_idx = np.argmax(recent_h)
                bars_since_max = len(recent_h) - 1 - max_idx
                
                if had_vol_spike and bars_since_max >= 2:
                    swing_max = np.max(recent_h)
                    risk_dist = (swing_max - c[i]) / c[i]
                    
                    body = abs(c[i] - o[i])
                    upper_wick = h[i] - max(c[i], o[i])
                    has_rejection = (upper_wick >= 0.8 * body) or (c[i] < o[i] and c[i] < c[i-1])
                    
                    if has_rejection and c[i] < ema9[i] and risk_dist <= max_risk_entry:
                        in_pos = -1
                        entry_p = next_o * (1 - COST)
                        entry_i = i + 1
                        stop_p = swing_max * 1.002
                        trough_p = entry_p
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            gain = (peak_p - entry_p) / entry_p
            curr_atr = atr[entry_i]
            
            # 1. Stop loss hit
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # 2. Trailing Breakeven protection
            if gain >= trail_be_atr * curr_atr / entry_p:
                stop_p = max(stop_p, entry_p * 1.001)
            if gain >= 1.8 * curr_atr / entry_p:
                stop_p = max(stop_p, entry_p * 1.010)
                
            # 3. Target Reached: Mean reversion to 15m SMA20
            if c[i] >= sma20[i] and gain >= 1.0 * curr_atr / entry_p:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TARGET_SMA20', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # 4. Pullback Stall exit:
            if gain >= 0.8 * curr_atr / entry_p:
                recent_h = h[i-stall_lookback:i+1]
                stalled = (np.max(recent_h) < peak_p) and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr)
                if stalled:
                    exit_p = next_o * (1 - COST)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            # 5. Fast Time-Stop (cut failed bounces after max_hold_bars)
            if (i - entry_i) >= max_hold_bars:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            gain = (entry_p - trough_p) / entry_p
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            if gain >= trail_be_atr * curr_atr / entry_p:
                stop_p = min(stop_p, entry_p * 0.999)
            if gain >= 1.8 * curr_atr / entry_p:
                stop_p = min(stop_p, entry_p * 0.990)
                
            if c[i] <= sma20[i] and gain >= 1.0 * curr_atr / entry_p:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TARGET_SMA20', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            if gain >= 0.8 * curr_atr / entry_p:
                recent_l = l[i-stall_lookback:i+1]
                stalled = (np.min(recent_l) > trough_p) and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.4 * curr_atr)
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

all_t = []
res = []
for s in symbols:
    tr = run_smart_exhaustion(cached_data[s], z_long=2.5, z_short=3.6, vol_climax_mult=1.5, max_risk_entry=0.015, max_hold_bars=10)
    all_t.extend(tr)
    pnls = [x[6] for x in tr]
    wins = [p for p in pnls if p > 0]
    wr = len(wins)/len(pnls) if len(pnls) > 0 else 0
    losses = [p for p in pnls if p <= 0]
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else (999 if wins else 0)
    res.append({'symbol': s, 'trades': len(tr), 'win_rate': wr, 'pf': pf, 'net_pnl': sum(pnls)})

df_res = pd.DataFrame(res)
print(df_res.to_string(index=False))
pnls = [x[6] for x in all_t]
wins = [p for p in pnls if p > 0]
losses = [p for p in pnls if p <= 0]
print(f"Overall: {len(all_t)} trades, Win Rate: {len(wins)/len(pnls):.2%}, Net: {sum(pnls):.2%}, PF: {sum(wins)/abs(sum(losses)):.2f}")
df_t = pd.DataFrame(all_t, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
print(df_t['reason'].value_counts())
print('Avg Win:', df_t[df_t['net_ret']>0]['net_ret'].mean(), 'Avg Loss:', df_t[df_t['net_ret']<=0]['net_ret'].mean())
