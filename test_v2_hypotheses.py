import numpy as np
import pandas as pd
from test_15m_native import load_and_aggregate_15m

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
cached_data = {s: load_and_aggregate_15m(s) for s in symbols}

def test_v2(
    df,
    impulse_bars=16,       # 4h
    z_long=2.6,            # Z threshold for long
    z_short=3.2,           # Higher Z threshold for short (prevent getting squeezed)
    volume_mult=1.8,       # Volume climax multiplier
    use_pinbar=True,       # Candlestick wick / engulfing confirmation
    reversion_target="SMA20", # Aim for SMA20
    trail_start_atr=1.2,
    max_stop_pct=0.025
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
                
            # LONG check:
            recent_z = z_move[i-4:i+1]
            min_z = np.nanmin(recent_z)
            if min_z <= -z_long:
                # 1. Volume Climax: did any bar in the crash have volume > volume_mult * MA?
                recent_v = v[i-impulse_bars:i+1]
                recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                has_vol_climax = np.any(recent_v >= volume_mult * recent_v_ma)
                
                # 2. Stabilization: swing low >= 2 bars ago
                recent_l = l[i-impulse_bars:i+1]
                min_idx = np.argmin(recent_l)
                bars_since_min = len(recent_l) - 1 - min_idx
                
                if has_vol_climax and bars_since_min >= 2:
                    swing_min = np.min(recent_l)
                    risk_pct = (c[i] - swing_min) / c[i]
                    
                    # 3. Candlestick confirmation:
                    # Lower wick on current or previous bar >= body, OR close > open and close > ema9
                    candle_body = abs(c[i] - o[i])
                    lower_wick = min(c[i], o[i]) - l[i]
                    is_bullish_candle = (lower_wick >= 0.8 * candle_body) or (c[i] > o[i] and c[i] > c[i-1])
                    
                    if is_bullish_candle and c[i] > ema9[i] and risk_pct <= max_stop_pct:
                        in_pos = 1
                        entry_p = next_o * (1 + COST)
                        entry_i = i + 1
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * (1 - max_stop_pct))
                        peak_p = entry_p
                        continue
                        
            # SHORT check:
            max_z = np.nanmax(recent_z)
            if max_z >= z_short:
                recent_v = v[i-impulse_bars:i+1]
                recent_v_ma = vol_ma20[i-impulse_bars:i+1]
                has_vol_climax = np.any(recent_v >= volume_mult * recent_v_ma)
                
                recent_h = h[i-impulse_bars:i+1]
                max_idx = np.argmax(recent_h)
                bars_since_max = len(recent_h) - 1 - max_idx
                
                if has_vol_climax and bars_since_max >= 2:
                    swing_max = np.max(recent_h)
                    risk_pct = (swing_max - c[i]) / c[i]
                    
                    candle_body = abs(c[i] - o[i])
                    upper_wick = h[i] - max(c[i], o[i])
                    is_bearish_candle = (upper_wick >= 0.8 * candle_body) or (c[i] < o[i] and c[i] < c[i-1])
                    
                    if is_bearish_candle and c[i] < ema9[i] and risk_pct <= max_stop_pct:
                        in_pos = -1
                        entry_p = next_o * (1 - COST)
                        entry_i = i + 1
                        stop_p = max(swing_max + 0.2 * atr[i], entry_p * (1 + max_stop_pct))
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
                
            # 2. Dynamic Trailing Stop:
            # Once price gains 1.2 ATR, move stop to entry + 0.2 ATR (lock profit)
            if (peak_p - entry_p) >= 1.2 * curr_atr:
                stop_p = max(stop_p, entry_p + 0.2 * curr_atr)
            if (peak_p - entry_p) >= 2.0 * curr_atr:
                stop_p = max(stop_p, entry_p + 1.0 * curr_atr)
                
            # 3. Target Reached: Mean Reversion to SMA20 or EMA20
            if c[i] >= sma20[i] and (peak_p - entry_p) >= 1.0 * curr_atr:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TARGET_SMA20', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
            # 4. Pullback Stall exit:
            if (peak_p - entry_p) >= 0.8 * curr_atr:
                recent_h = h[i-2:i+1]
                if np.max(recent_h) < peak_p and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr):
                    exit_p = next_o * (1 - COST)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            # 5. Time limit (24 bars = 6 hours)
            if (i - entry_i) >= 24:
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
                
            if (entry_p - trough_p) >= 1.2 * curr_atr:
                stop_p = min(stop_p, entry_p - 0.2 * curr_atr)
            if (entry_p - trough_p) >= 2.0 * curr_atr:
                stop_p = min(stop_p, entry_p - 1.0 * curr_atr)
                
            if c[i] <= sma20[i] and (entry_p - trough_p) >= 1.0 * curr_atr:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TARGET_SMA20', entry_i, i+1, entry_p, exit_p, net))
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
                    
            if (i - entry_i) >= 24:
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
    tr = test_v2(cached_data[s], z_long=2.6, z_short=3.2, volume_mult=1.6)
    all_t.extend(tr)
    pnls = [x[6] for x in tr]
    wins = [p for p in pnls if p > 0]
    wr = len(wins)/len(pnls) if len(pnls) > 0 else 0
    losses = [p for p in pnls if p <= 0]
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else 0
    res.append({'symbol': s, 'trades': len(tr), 'win_rate': wr, 'pf': pf, 'net_pnl': sum(pnls)})

df_res = pd.DataFrame(res)
print(df_res.to_string(index=False))
pnls = [x[6] for x in all_t]
wins = [p for p in pnls if p > 0]
print(f"Overall: {len(all_t)} trades, Win Rate: {len(wins)/len(pnls):.2%}, Net: {sum(pnls):.2%}")
df_t = pd.DataFrame(all_t, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
print(df_t['reason'].value_counts())
