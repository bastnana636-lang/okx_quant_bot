import numpy as np
import pandas as pd
from test_15m_native import load_and_aggregate_15m

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
cached_data = {s: load_and_aggregate_15m(s) for s in symbols}

def simulate_refined_15m(
    df,
    impulse_bars=16,      # 4h impulse
    z_thresh=2.8,         # 2.8 sigma
    rsi_extreme=25,       # RSI oversold/overbought threshold
    stab_bars=3,          # 45m stabilization
    stall_bars=2,         # 30m stall
    trail_mult=1.0,       # 1.0 ATR trailing
    trend_filter=True     # alignment with 4d trend
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
    
    # 2. 14-day baseline return volatility (1344 bars of 15m)
    ret1 = np.zeros(n)
    ret1[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret1).rolling(1344, min_periods=300).std().to_numpy()
    
    # 3. Z-score of cumulative move
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(impulse_bars)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    # 4. RSI(14) on 15m
    delta = np.zeros(n)
    delta[1:] = c[1:] - c[:-1]
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    avg_loss = pd.Series(loss).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    rs = np.where(avg_loss == 0, 100.0, avg_gain / np.maximum(avg_loss, 1e-9))
    rsi = 100 - (100 / (1 + rs))
    
    # 5. EMAs
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    ema20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    macro_ema = pd.Series(c).ewm(span=384, adjust=False).mean().to_numpy() # 4-day
    
    COST = 0.0007
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    cooldown = 0
    
    start_i = max(1344, impulse_bars + stab_bars + 1)
    
    for i in range(start_i, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # Crash exhaustion -> BUY
            recent_z = z_move[i-4:i+1]
            had_dump = np.nanmin(recent_z) <= -z_thresh
            had_rsi = np.nanmin(rsi[i-4:i+1]) <= rsi_extreme
            macro_ok = not trend_filter or (c[i] > macro_ema[i])
            
            if had_dump and had_rsi and macro_ok:
                recent_l = l[i-impulse_bars:i+1]
                min_idx = np.argmin(recent_l)
                bars_since_min = len(recent_l) - 1 - min_idx
                
                # Stabilization: lowest low happened >= stab_bars ago (e.g. 45m ago)
                if bars_since_min >= stab_bars:
                    # In last stab_bars, price did not make new low
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_min = np.min(recent_l)
                    risk_pct = (c[i] - swing_min) / c[i]
                    
                    # Deceleration: rate of change >= 0, closed above EMA9, risk <= 3%
                    if stab_ret >= 0 and c[i] > ema9[i] and risk_pct <= 0.03:
                        in_pos = 1
                        entry_p = next_o * (1 + COST)
                        entry_i = i + 1
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.98)
                        peak_p = entry_p
                        continue
                        
            # Pump exhaustion -> SELL
            had_pump = np.nanmax(recent_z) >= z_thresh
            had_rsi_high = np.nanmax(rsi[i-4:i+1]) >= (100 - rsi_extreme)
            macro_ok_short = not trend_filter or (c[i] < macro_ema[i])
            
            if had_pump and had_rsi_high and macro_ok_short:
                recent_h = h[i-impulse_bars:i+1]
                max_idx = np.argmax(recent_h)
                bars_since_max = len(recent_h) - 1 - max_idx
                
                if bars_since_max >= stab_bars:
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_max = np.max(recent_h)
                    risk_pct = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema9[i] and risk_pct <= 0.03:
                        in_pos = -1
                        entry_p = next_o * (1 - COST)
                        entry_i = i + 1
                        stop_p = max(swing_max + 0.2 * atr[i], entry_p * 1.02)
                        trough_p = entry_p
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # 1. Stop loss
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # 2. Breakeven trailing
            if (peak_p - entry_p) >= 1.0 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.002
                
            # 3. Pullback stall exit:
            # Rebound gained at least 0.8 ATR
            if (peak_p - entry_p) >= 0.8 * curr_atr:
                # Stall in last stall_bars: no new high, and closed below EMA9 or dropped by 0.35 ATR
                recent_highs = h[i-stall_bars:i+1]
                stalled = (np.max(recent_highs) < peak_p) and (
                    c[i] < ema9[i] or (peak_p - c[i]) >= 0.35 * curr_atr
                )
                if stalled:
                    exit_p = next_o * (1 - COST)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            # 4. Reached mean (EMA20) and stalled
            if c[i] >= ema20[i] and (peak_p - entry_p) >= 1.2 * curr_atr:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'MEAN_REVERTED', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # 5. Max hold 16 bars (4h)
            if (i - entry_i) >= 16:
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
                
            if (entry_p - trough_p) >= 1.0 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.998
                
            if (entry_p - trough_p) >= 0.8 * curr_atr:
                recent_lows = l[i-stall_bars:i+1]
                stalled = (np.min(recent_lows) > trough_p) and (
                    c[i] > ema9[i] or (c[i] - trough_p) >= 0.35 * curr_atr
                )
                if stalled:
                    exit_p = next_o * (1 + COST)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            if c[i] <= ema20[i] and (entry_p - trough_p) >= 1.2 * curr_atr:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'MEAN_REVERTED', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            if (i - entry_i) >= 16:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue

    return trades

all_t = []
res_list = []
for s in symbols:
    tr = simulate_refined_15m(cached_data[s], impulse_bars=16, z_thresh=2.8, rsi_extreme=28, stab_bars=2, stall_bars=2)
    all_t.extend(tr)
    pnls = [x[6] for x in tr]
    wins = [p for p in pnls if p > 0]
    wr = len(wins)/len(pnls) if len(pnls) > 0 else 0
    pf = (sum(wins)/abs(sum([p for p in pnls if p<=0]))) if len(pnls)-len(wins)>0 and abs(sum([p for p in pnls if p<=0]))>0 else 0
    res_list.append({'symbol': s, 'trades': len(tr), 'win_rate': wr, 'profit_factor': pf, 'net_pnl': sum(pnls)})

df_out = pd.DataFrame(res_list)
print(df_out.to_string(index=False))

pnls = [x[6] for x in all_t]
wins = [p for p in pnls if p > 0]
print(f"Total: {len(all_t)} trades, Win Rate: {len(wins)/len(pnls):.2%}, Net: {sum(pnls):.2%}")
