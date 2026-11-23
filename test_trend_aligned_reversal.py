import numpy as np
import pandas as pd
from test_exhaustion_sweep import load_data, aggregate_candles

def test_aligned_strategy(symbol='SOLUSDT'):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 900) # 15m candles
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['v'].to_numpy(dtype=np.float64)
    n = len(c)
    
    # Macro trend: 4-day EMA (384 bars of 15m)
    macro_ema = pd.Series(c).ewm(span=384, adjust=False).mean().to_numpy()
    
    # ATR 14
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 7-day rolling std of returns (672 bars)
    ret = np.zeros(n)
    ret[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret).rolling(672, min_periods=200).std().to_numpy()
    
    # Impulse move over 16 bars (4 hours)
    c_shift = pd.Series(c).shift(16).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(16), 1e-6)
    
    # RSI 14
    delta = np.zeros(n)
    delta[1:] = c[1:] - c[:-1]
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    avg_loss = pd.Series(loss).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    rs = np.where(avg_loss == 0, 100.0, avg_gain / np.maximum(avg_loss, 1e-9))
    rsi = 100 - (100 / (1 + rs))
    
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    
    # Volume spike check: volume > 1.8 * 20-period moving average
    vol_ma20 = pd.Series(v).rolling(20).mean().to_numpy()
    
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
    cooldown = 0
    
    for i in range(672, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # LONG ONLY when macro trend is BULLISH (c[i] > macro_ema[i])
            # We buy the deep dip after it stabilizes
            if c[i] > macro_ema[i]:
                recent_z = z_move[i-8:i+1]
                if np.nanmin(recent_z) <= -2.5 and np.nanmin(rsi[i-8:i+1]) <= 30:
                    # check stabilization: no new low for last 4 bars (1 hour)
                    recent_l = l[i-16:i+1]
                    min_idx = np.argmin(recent_l)
                    bars_since_min = len(recent_l) - 1 - min_idx
                    
                    if bars_since_min >= 3:
                        recent_c = c[i-3:i+1]
                        stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                        swing_min = np.min(recent_l)
                        swing_max = np.max(h[i-16:i+1])
                        risk_dist = (c[i] - swing_min) / c[i]
                        
                        if stab_ret >= 0 and c[i] > ema9[i] and risk_dist <= 0.025:
                            in_pos = 1
                            entry_p = next_o * (1 + SLIP)
                            entry_i = i + 1
                            dump_height = swing_max - swing_min
                            stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.98)
                            peak_p = entry_p
                            continue
                            
            # SHORT ONLY when macro trend is BEARISH (c[i] < macro_ema[i])
            # We short the relief squeeze after it stabilizes
            elif c[i] < macro_ema[i]:
                recent_z = z_move[i-8:i+1]
                if np.nanmax(recent_z) >= 2.5 and np.nanmax(rsi[i-8:i+1]) >= 70:
                    recent_h = h[i-16:i+1]
                    max_idx = np.argmax(recent_h)
                    bars_since_max = len(recent_h) - 1 - max_idx
                    
                    if bars_since_max >= 3:
                        recent_c = c[i-3:i+1]
                        stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                        swing_max = np.max(recent_h)
                        swing_min = np.min(l[i-16:i+1])
                        risk_dist = (swing_max - c[i]) / c[i]
                        
                        if stab_ret <= 0 and c[i] < ema9[i] and risk_dist <= 0.025:
                            in_pos = -1
                            entry_p = next_o * (1 - SLIP)
                            entry_i = i + 1
                            dump_height = swing_max - swing_min
                            stop_p = max(swing_max + 0.2 * atr[i], entry_p * 1.02)
                            trough_p = entry_p
                            continue
                            
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # Stop loss
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 8
                continue
                
            # Trailing breakeven
            if (peak_p - entry_p) >= 1.2 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.002
                
            # Pullback stall exit:
            # Price gained >= 1.2 ATR or 20% of dump height
            min_gain = max(1.2 * curr_atr, 0.2 * dump_height)
            if (peak_p - entry_p) >= min_gain:
                recent_h = h[i-3:i+1]
                if np.max(recent_h) < peak_p and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr):
                    exit_p = next_o * (1 - SLIP)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            if (i - entry_i) >= 48: # 12h
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 8
                continue
                
            if (entry_p - trough_p) >= 1.2 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.998
                
            min_gain = max(1.2 * curr_atr, 0.2 * dump_height)
            if (entry_p - trough_p) >= min_gain:
                recent_l = l[i-3:i+1]
                if np.min(recent_l) > trough_p and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.4 * curr_atr):
                    exit_p = next_o * (1 + SLIP)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 4
                    continue
                    
            if (i - entry_i) >= 48:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 4
                continue
                
    return trades

symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'SUIUSDT', 'ADAUSDT', 'ZECUSDT', 'XRPUSDT']
res = []
for s in symbols:
    tr = test_aligned_strategy(s)
    if tr:
        df_t = pd.DataFrame(tr, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
        wr = (df_t['net_ret'] > 0).mean()
        tot = df_t['net_ret'].sum()
        cnt = len(df_t)
        wins = df_t[df_t['net_ret'] > 0]['net_ret']
        losses = df_t[df_t['net_ret'] <= 0]['net_ret']
        pf = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else np.nan
        res.append({'symbol': s, 'trades': cnt, 'win_rate': wr, 'total_net_ret': tot, 'profit_factor': pf})
    else:
        res.append({'symbol': s, 'trades': 0, 'win_rate': 0, 'total_net_ret': 0, 'profit_factor': 0})

print(pd.DataFrame(res).to_string(index=False))
