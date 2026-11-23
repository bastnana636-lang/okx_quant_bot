import numpy as np
import pandas as pd
from scripts.adaptive_reversal_strategy import load_and_clean_data, aggregate_candles, TradeRecord, evaluate_trades

def test_interval(symbol, interval_sec=300, z_thresh=3.0, impulse_hours=4, stab_mins=30, stall_mins=15):
    raw = load_and_clean_data(symbol)
    df = aggregate_candles(raw, interval_sec)
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    ts = df["ts"].to_numpy(dtype=np.int64)
    n = len(c)
    
    bars_per_hour = 3600 // interval_sec
    impulse_bars = impulse_hours * bars_per_hour
    stab_bars = max(2, (stab_mins * 60) // interval_sec)
    stall_bars = max(2, (stall_mins * 60) // interval_sec)
    base_window = 14 * 24 * bars_per_hour
    
    # ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # 14-day rolling baseline volatility
    ret = np.zeros(n)
    ret[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(ret).rolling(base_window, min_periods=base_window // 4).std().to_numpy()
    
    # Z-Score of price change
    c_shift = pd.Series(c).shift(impulse_bars).to_numpy()
    cum_ret = (c - c_shift) / c_shift
    expected_std = base_std * np.sqrt(impulse_bars)
    z_move = cum_ret / np.maximum(expected_std, 1e-6)
    
    ema5 = pd.Series(c).ewm(span=5, adjust=False).mean().to_numpy()
    ema20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    
    COST = 0.0007 # 0.05% fee + 0.02% slippage
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    peak_p = 0.0
    trough_p = 0.0
    
    start_i = max(base_window, impulse_bars + stab_bars + 1)
    cooldown = 0
    
    for i in range(start_i, n - 1):
        next_o = o[i+1]
        
        if in_pos == 0:
            if i < cooldown:
                continue
                
            # Crash exhaustion -> BUY
            recent_z = z_move[i-8:i+1]
            if np.nanmin(recent_z) <= -z_thresh:
                recent_lows = l[i-impulse_bars:i+1]
                min_idx = np.argmin(recent_lows)
                bars_since_min = len(recent_lows) - 1 - min_idx
                
                if bars_since_min >= stab_bars:
                    # Flat / slowing down in last stab_bars
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_min = np.min(recent_lows)
                    risk_dist = (c[i] - swing_min) / c[i]
                    
                    if stab_ret >= 0 and c[i] > ema5[i] and risk_dist <= 0.02:
                        in_pos = 1
                        entry_p = next_o * (1 + COST)
                        entry_i = i + 1
                        stop_p = swing_min - 0.2 * atr[i]
                        # Cap max stop loss to 2%
                        stop_p = max(stop_p, entry_p * 0.98)
                        peak_p = entry_p
                        continue
                        
            # Pump exhaustion -> SELL
            if np.nanmax(recent_z) >= z_thresh:
                recent_highs = h[i-impulse_bars:i+1]
                max_idx = np.argmax(recent_highs)
                bars_since_max = len(recent_highs) - 1 - max_idx
                
                if bars_since_max >= stab_bars:
                    stab_ret = (c[i] - c[i-stab_bars]) / c[i-stab_bars]
                    swing_max = np.max(recent_highs)
                    risk_dist = (swing_max - c[i]) / c[i]
                    
                    if stab_ret <= 0 and c[i] < ema5[i] and risk_dist <= 0.02:
                        in_pos = -1
                        entry_p = next_o * (1 - COST)
                        entry_i = i + 1
                        stop_p = swing_max + 0.2 * atr[i]
                        stop_p = min(stop_p, entry_p * 1.02)
                        trough_p = entry_p
                        continue
                        
        elif in_pos == 1:
            peak_p = max(peak_p, h[i])
            curr_atr = atr[entry_i]
            
            # Stop loss
            if l[i] <= stop_p:
                exit_p = min(next_o, stop_p) * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(TradeRecord(symbol, 'LONG', 'STOP', ts[entry_i], ts[i], entry_p, exit_p, net, i-entry_i))
                in_pos = 0
                cooldown = i + 12
                continue
                
            # Trailing breakeven after 1.0 ATR
            if (peak_p - entry_p) >= 1.0 * curr_atr and stop_p < entry_p:
                stop_p = entry_p * 1.001
                
            # Pullback stall exit:
            # We waited for a pullback (price gained at least 0.5 ATR)
            if (peak_p - entry_p) >= 0.5 * curr_atr:
                # Stall in last stall_bars: no new high and close < ema5
                recent_highs = h[i-stall_bars:i+1]
                if np.max(recent_highs) < peak_p and (c[i] < ema5[i] or (peak_p - c[i]) >= 0.4 * curr_atr):
                    exit_p = next_o * (1 - COST)
                    net = (exit_p - entry_p) / entry_p - 2 * COST
                    trades.append(TradeRecord(symbol, 'LONG', 'PULLBACK_STALL', ts[entry_i], ts[i+1], entry_p, exit_p, net, i+1-entry_i))
                    in_pos = 0
                    cooldown = i + 6
                    continue
                    
            # Time limit: 2 hours (24 bars of 5m)
            if (i - entry_i) >= 24:
                exit_p = next_o * (1 - COST)
                net = (exit_p - entry_p) / entry_p - 2 * COST
                trades.append(TradeRecord(symbol, 'LONG', 'TIME_LIMIT', ts[entry_i], ts[i+1], entry_p, exit_p, net, i+1-entry_i))
                in_pos = 0
                cooldown = i + 6
                continue
                
        elif in_pos == -1:
            trough_p = min(trough_p, l[i])
            curr_atr = atr[entry_i]
            
            if h[i] >= stop_p:
                exit_p = max(next_o, stop_p) * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(TradeRecord(symbol, 'SHORT', 'STOP', ts[entry_i], ts[i], entry_p, exit_p, net, i-entry_i))
                in_pos = 0
                cooldown = i + 12
                continue
                
            if (entry_p - trough_p) >= 1.0 * curr_atr and stop_p > entry_p:
                stop_p = entry_p * 0.999
                
            if (entry_p - trough_p) >= 0.5 * curr_atr:
                recent_lows = l[i-stall_bars:i+1]
                if np.min(recent_lows) > trough_p and (c[i] > ema5[i] or (c[i] - trough_p) >= 0.4 * curr_atr):
                    exit_p = next_o * (1 + COST)
                    net = (entry_p - exit_p) / entry_p - 2 * COST
                    trades.append(TradeRecord(symbol, 'SHORT', 'PULLBACK_STALL', ts[entry_i], ts[i+1], entry_p, exit_p, net, i+1-entry_i))
                    in_pos = 0
                    cooldown = i + 6
                    continue
                    
            if (i - entry_i) >= 24:
                exit_p = next_o * (1 + COST)
                net = (entry_p - exit_p) / entry_p - 2 * COST
                trades.append(TradeRecord(symbol, 'SHORT', 'TIME_LIMIT', ts[entry_i], ts[i+1], entry_p, exit_p, net, i+1-entry_i))
                in_pos = 0
                cooldown = i + 6
                continue

    return trades

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "DOGEUSDT", "ADAUSDT", "ZECUSDT", "XRPUSDT"]
reports = []
all_t = []
for s in symbols:
    tr = test_interval(s, interval_sec=300, z_thresh=3.0, impulse_hours=3, stab_mins=20, stall_mins=15)
    all_t.extend(tr)
    st = evaluate_trades(tr)
    st['symbol'] = s
    reports.append(st)

df_rep = pd.DataFrame(reports)[['symbol', 'total_trades', 'win_rate', 'profit_factor', 'total_net_pnl', 'avg_trade_pnl', 'max_drawdown', 'pullback_exit_ratio']]
print(df_rep.to_string(index=False))
tot_st = evaluate_trades(all_t)
print(f"Overall Win Rate: {tot_st['win_rate']:.2%}, Profit Factor: {tot_st['profit_factor']:.2f}, Net: {tot_st['total_net_pnl']:.2%}, Pullback Exits: {tot_st['pullback_exit_ratio']:.2%}")
