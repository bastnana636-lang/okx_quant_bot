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

def aggregate_candles(data, interval_sec=300):
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

def test_symbol(symbol, z_thresh=3.5, move_window=36, stab_window=6, exit_stall_window=4):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 300)
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['v'].to_numpy(dtype=np.float64)
    n = len(c)
    
    # 1. TR & ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(24, min_periods=24).mean().to_numpy()
    
    # 2. Daily baseline volatility (288 5m bars = 24h, 864 = 72h)
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(864, min_periods=288).std().to_numpy()
    
    # 3. Dynamic Z-score of price move over move_window (e.g. 36 * 5m = 3 hours)
    c_shift = pd.Series(c).shift(move_window).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(move_window), 1e-6)
    
    # 4. Moving averages and indicators
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    ema21 = pd.Series(c).ewm(span=21, adjust=False).mean().to_numpy()
    
    # Fees and slippage
    TAKER_FEE = 0.0005
    SLIP = 0.0002
    COST = (TAKER_FEE + SLIP)
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    swing_ext = 0.0
    peak_p = 0.0
    trough_p = 0.0
    pullback_ready = False
    
    for i in range(864, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            # Check Crash -> Long:
            # Did we have an extreme crash within the past 12 bars?
            recent_z = z_move[i-12:i+1]
            min_z = np.nanmin(recent_z)
            if min_z <= -z_thresh:
                # Find swing low over the crash period
                crash_lows = l[i-move_window:i+1]
                min_idx = np.argmin(crash_lows)
                bars_since_min = len(crash_lows) - 1 - min_idx
                
                # Stabilization criteria:
                # 1. Swing low was hit at least stab_window bars ago (no new low recently)
                # 2. Price in the last stab_window bars has stabilized: rate of change >= 0
                # 3. Price breaks above EMA9
                if bars_since_min >= stab_window:
                    recent_c = c[i-stab_window:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_min = np.min(crash_lows)
                    
                    # Risk distance: entry to swing_min
                    risk_dist = (c[i] - swing_min) / c[i]
                    
                    if stab_ret >= 0 and c[i] > ema9[i] and risk_dist <= 0.035:
                        in_pos = 1
                        entry_p = next_o * (1 + SLIP)
                        entry_i = i + 1
                        swing_ext = swing_min
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.975)
                        peak_p = entry_p
                        pullback_ready = False
                        continue
                        
            # Check Pump -> Short:
            max_z = np.nanmax(recent_z)
            if max_z >= z_thresh:
                crash_highs = h[i-move_window:i+1]
                max_idx = np.argmax(crash_highs)
                bars_since_max = len(crash_highs) - 1 - max_idx
                
                if bars_since_max >= stab_window:
                    recent_c = c[i-stab_window:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_max = np.max(crash_highs)
                    risk_dist = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema9[i] and risk_dist <= 0.035:
                        in_pos = -1
                        entry_p = next_o * (1 - SLIP)
                        entry_i = i + 1
                        swing_ext = swing_max
                        stop_p = max(swing_max + 0.2 * atr[i], entry_p * 1.025)
                        trough_p = entry_p
                        pullback_ready = False
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # Stop-loss check
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                continue
                
            # Pullback condition: price has recovered at least 1.0 ATR
            pullback_gain = (peak_p - entry_p) / entry_p
            if pullback_gain >= max(0.01, 1.0 * curr_atr / entry_p):
                pullback_ready = True
                
            if pullback_ready:
                # Check if pullback has stalled:
                # In the last exit_stall_window bars, price has not made new highs,
                # and price closed below EMA9 (indicating momentum exhaustion of the bounce)
                recent_highs = h[i-exit_stall_window:i+1]
                stalled = (np.max(recent_highs) < peak_p) and (c[i] < ema9[i])
                if stalled:
                    exit_p = next_o * (1 - SLIP)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    continue
                    
            # Time exit: max hold 24 hours (288 5m bars)
            if (i - entry_i) >= 288:
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
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
                
            pullback_gain = (entry_p - trough_p) / entry_p
            if pullback_gain >= max(0.01, 1.0 * curr_atr / entry_p):
                pullback_ready = True
                
            if pullback_ready:
                recent_lows = l[i-exit_stall_window:i+1]
                stalled = (np.min(recent_lows) > trough_p) and (c[i] > ema9[i])
                if stalled:
                    exit_p = next_o * (1 + SLIP)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    continue
                    
            if (i - entry_i) >= 288:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                continue
                
    return trades

symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'SUIUSDT', 'ADAUSDT', 'ZECUSDT', 'XRPUSDT']
all_res = []
for s in symbols:
    tr = test_symbol(s, z_thresh=3.2, move_window=36, stab_window=6, exit_stall_window=4)
    if tr:
        df_t = pd.DataFrame(tr, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
        wr = (df_t['net_ret'] > 0).mean()
        tot = df_t['net_ret'].sum()
        cnt = len(df_t)
        p_stall = (df_t['reason'] == 'PULLBACK_STALL').sum()
        all_res.append({'symbol': s, 'trades': cnt, 'win_rate': wr, 'total_net_ret': tot, 'pullback_exits': p_stall})
    else:
        all_res.append({'symbol': s, 'trades': 0, 'win_rate': 0, 'total_net_ret': 0, 'pullback_exits': 0})

res_df = pd.DataFrame(all_res)
print(res_df.to_string(index=False))
