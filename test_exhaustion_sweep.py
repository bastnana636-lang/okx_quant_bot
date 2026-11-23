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

def aggregate_candles(data, interval_sec=300): # 5m candles
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

# Let's test a clean implementation with 5m candles
def backtest_exhaustion(symbol, z_thresh=2.8, impulse_bars=48, stab_bars=6, stall_bars=4):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 300) # 5m
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['v'].to_numpy(dtype=np.float64)
    n = len(c)
    
    # ATR 14
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 7-day rolling std of 5m returns (2016 bars)
    ret = np.zeros(n)
    ret[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret).rolling(2016, min_periods=500).std().to_numpy()
    
    # Impulse move over impulse_bars (e.g. 48 bars = 4 hours)
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(impulse_bars), 1e-6)
    
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
    cooldown = 0
    
    for i in range(2016, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # Crash exhaustion -> BUY
            # 1. Extreme crash happened recently
            recent_z = z_move[i-12:i+1]
            if np.nanmin(recent_z) <= -z_thresh and np.nanmin(rsi[i-12:i+1]) <= 25:
                # 2. Check stabilization: lowest low happened >= stab_bars ago
                recent_lows = l[i-impulse_bars:i+1]
                min_idx = np.argmin(recent_lows)
                bars_since_min = len(recent_lows) - 1 - min_idx
                
                if bars_since_min >= stab_bars:
                    # In last stab_bars, price did not make new low
                    # and price rate of change >= 0
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_min = np.min(recent_lows)
                    risk_dist = (c[i] - swing_min) / c[i]
                    
                    # Entry condition: RSI crossed back above 30 or price > EMA9
                    if stab_ret >= 0 and c[i] > ema9[i] and rsi[i] >= 30 and risk_dist <= 0.03:
                        in_pos = 1
                        entry_p = next_o * (1 + SLIP)
                        entry_i = i + 1
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.98)
                        peak_p = entry_p
                        continue
                        
            # Pump exhaustion -> SELL
            if np.nanmax(recent_z) >= z_thresh and np.nanmax(rsi[i-12:i+1]) >= 75:
                recent_highs = h[i-impulse_bars:i+1]
                max_idx = np.argmax(recent_highs)
                bars_since_max = len(recent_highs) - 1 - max_idx
                
                if bars_since_max >= stab_bars:
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_max = np.max(recent_highs)
                    risk_dist = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema9[i] and rsi[i] <= 70 and risk_dist <= 0.03:
                        in_pos = -1
                        entry_p = next_o * (1 - SLIP)
                        entry_i = i + 1
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
                cooldown = i + 12
                continue
                
            # Trailing breakeven
            if (peak_p - entry_p) >= 1.0 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.001
                
            # Pullback stall exit:
            # Price gained at least 0.8 ATR
            if (peak_p - entry_p) >= 0.8 * curr_atr:
                # Did momentum stall over last stall_bars?
                # No new high in last stall_bars, and price falling or below EMA9
                recent_h = h[i-stall_bars:i+1]
                if np.max(recent_h) < peak_p and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.5 * curr_atr):
                    exit_p = next_o * (1 - SLIP)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 6
                    continue
                    
            if (i - entry_i) >= 72: # 6 hours max hold
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 6
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 12
                continue
                
            if (entry_p - trough_p) >= 1.0 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.999
                
            if (entry_p - trough_p) >= 0.8 * curr_atr:
                recent_l = l[i-stall_bars:i+1]
                if np.min(recent_l) > trough_p and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.5 * curr_atr):
                    exit_p = next_o * (1 + SLIP)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown = i + 6
                    continue
                    
            if (i - entry_i) >= 72:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown = i + 6
                continue

    return trades

symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'SUIUSDT', 'ADAUSDT', 'ZECUSDT', 'XRPUSDT']
res = []
for s in symbols:
    tr = backtest_exhaustion(s, z_thresh=2.8, impulse_bars=48, stab_bars=6, stall_bars=4)
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
