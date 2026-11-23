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

def aggregate_candles(data, interval_sec=900): # 15 min bars
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

def run_strategy(symbol, z_thresh=3.0, impulse_window=32, stab_bars=4, exit_stall_bars=3):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 900) # 15m
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    ts = df['ts'].to_numpy(dtype=np.int64)
    n = len(c)
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 14-day baseline volatility
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(1344, min_periods=400).std().to_numpy()
    
    # Dynamic Z-score of price move over impulse_window (32 bars = 8 hours)
    c_shift = pd.Series(c).shift(impulse_window).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    z_move = cum_ret / np.maximum(base_std * np.sqrt(impulse_window), 1e-6)
    
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    
    TAKER_FEE = 0.0005
    SLIP = 0.0002
    COST = (TAKER_FEE + SLIP)
    
    trades = []
    in_pos = 0 # 1: Long, -1: Short
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    dump_height = 0.0
    
    # Cooldown to prevent immediate re-entry after closing
    cooldown_until = 0
    
    for i in range(1344, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown_until:
                continue
                
            # Check for crash exhaustion -> BUY (Long)
            # 1. Had extreme dump in last 8 bars
            recent_z = z_move[i-8:i+1]
            if np.nanmin(recent_z) <= -z_thresh:
                # Find swing low over the impulse period
                impulse_lows = l[i-impulse_window:i+1]
                impulse_highs = h[i-impulse_window:i+1]
                min_idx = np.argmin(impulse_lows)
                bars_since_min = len(impulse_lows) - 1 - min_idx
                
                # Check stabilization (跌到位):
                # - Swing low happened >= stab_bars ago (no new low recently)
                # - Price in last stab_bars is not dropping further
                # - Price closed above EMA9
                if bars_since_min >= stab_bars:
                    recent_c = c[i-stab_bars:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_min = np.min(impulse_lows)
                    swing_max = np.max(impulse_highs)
                    risk_pct = (c[i] - swing_min) / c[i]
                    
                    if stab_ret >= 0 and c[i] > ema9[i] and risk_pct <= 0.035:
                        in_pos = 1
                        entry_p = next_o * (1 + SLIP)
                        entry_i = i + 1
                        dump_height = swing_max - swing_min
                        stop_p = min(swing_min - 0.2 * atr[i], entry_p * 0.975)
                        peak_p = entry_p
                        continue
                        
            # Check for pump exhaustion -> SELL (Short)
            if np.nanmax(recent_z) >= z_thresh:
                impulse_highs = h[i-impulse_window:i+1]
                impulse_lows = l[i-impulse_window:i+1]
                max_idx = np.argmax(impulse_highs)
                bars_since_max = len(impulse_highs) - 1 - max_idx
                
                if bars_since_max >= stab_bars:
                    recent_c = c[i-stab_bars:i+1]
                    stab_ret = (c[i] - recent_c[0]) / recent_c[0]
                    swing_max = np.max(impulse_highs)
                    swing_min = np.min(impulse_lows)
                    risk_pct = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema9[i] and risk_pct <= 0.035:
                        in_pos = -1
                        entry_p = next_o * (1 - SLIP)
                        entry_i = i + 1
                        dump_height = swing_max - swing_min
                        stop_p = max(swing_max + 0.2 * atr[i], entry_p * 1.025)
                        trough_p = entry_p
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # 1. Stop loss
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown_until = i + 8
                continue
                
            # Move stop to breakeven once profit reaches 1.2 ATR
            if (peak_p - entry_p) >= 1.2 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.001
                
            # 2. Pullback exhaustion exit (等一段回调；回调到一段时间以内没有继续快速上涨，就平仓)
            rebound_gain = (peak_p - entry_p) / entry_p
            # Required minimal pullback: at least 1.0 ATR or 15% retracement of dump
            min_target = max(1.0 * curr_atr / entry_p, 0.15 * dump_height / entry_p)
            if rebound_gain >= min_target:
                # Stall condition:
                # Over the last exit_stall_bars (e.g. 3 bars = 45m), no new high made,
                # and price closed below EMA9 or has pulled back from peak by 0.4 ATR
                recent_highs = h[i-exit_stall_bars:i+1]
                stalled = (np.max(recent_highs) < peak_p) and (c[i] < ema9[i] or (peak_p - c[i]) >= 0.4 * curr_atr)
                if stalled:
                    exit_p = next_o * (1 - SLIP)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(('LONG', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown_until = i + 4
                    continue
                    
            # 3. Time limit: 24 hours (96 bars)
            if (i - entry_i) >= 48: # 12 hours
                exit_p = next_o * (1 - SLIP)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(('LONG', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown_until = i + 4
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'STOP', entry_i, i, entry_p, exit_p, net))
                in_pos = 0
                cooldown_until = i + 8
                continue
                
            if (entry_p - trough_p) >= 1.2 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.999
                
            drop_gain = (entry_p - trough_p) / entry_p
            min_target = max(1.0 * curr_atr / entry_p, 0.15 * dump_height / entry_p)
            if drop_gain >= min_target:
                recent_lows = l[i-exit_stall_bars:i+1]
                stalled = (np.min(recent_lows) > trough_p) and (c[i] > ema9[i] or (c[i] - trough_p) >= 0.4 * curr_atr)
                if stalled:
                    exit_p = next_o * (1 + SLIP)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(('SHORT', 'PULLBACK_STALL', entry_i, i+1, entry_p, exit_p, net))
                    in_pos = 0
                    cooldown_until = i + 4
                    continue
                    
            if (i - entry_i) >= 48:
                exit_p = next_o * (1 + SLIP)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(('SHORT', 'TIME_LIMIT', entry_i, i+1, entry_p, exit_p, net))
                in_pos = 0
                cooldown_until = i + 4
                continue
                
    return trades

symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'SUIUSDT', 'ADAUSDT', 'ZECUSDT', 'XRPUSDT']
res = []
for s in symbols:
    tr = run_strategy(s, z_thresh=3.0, impulse_window=32, stab_bars=4, exit_stall_bars=3)
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
