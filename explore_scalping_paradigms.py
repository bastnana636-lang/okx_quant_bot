import numpy as np
import pandas as pd
from scalping_engine import load_all_symbols, SYMBOLS_10, evaluate_trades, BacktestResult

# Paradigm 1: Donchian Range Breakout Scalper (Channel Breakout + ATR Trailing Stop + Trend Filter)
def backtest_breakout(
    df: pd.DataFrame,
    channel_len: int = 20,
    atr_len: int = 14,
    atr_mult_sl: float = 1.5,
    atr_mult_tp: float = 3.0,
    trend_ema: int = 100,
    trail_be_mult: float = 1.0,
    max_hold_bars: int = 36,
    fee_rate: float = 0.0006
) -> BacktestResult:
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    v = df["vol"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # Donchian Upper & Lower (using past candles only, shifted by 1)
    hh = pd.Series(h).shift(1).rolling(channel_len).max().to_numpy()
    ll = pd.Series(l).shift(1).rolling(channel_len).min().to_numpy()
    
    # ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(atr_len, min_periods=atr_len).mean().to_numpy()
    
    # Trend filter
    ema_t = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().to_numpy()
    
    # Volume filter
    vol_ma = pd.Series(v).rolling(20, min_periods=10).mean().to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    be_active = False
    
    start_bar = max(channel_len, trend_ema) + 5
    for i in range(start_bar, n - 1):
        curr_c = c[i]
        curr_h = h[i]
        curr_l = l[i]
        curr_atr = atr[i]
        next_o = o[i+1]
        
        # Position Exit Check
        if in_pos == 1:
            # Trailing BE
            if not be_active and curr_h >= entry_p + trail_be_mult * curr_atr:
                be_active = True
                stop_p = max(stop_p, entry_p * 1.0005)
                
            exit_p = None
            exit_reason = ""
            if curr_h >= tp_p:
                exit_p = tp_p
                exit_reason = "TP"
            elif curr_l <= stop_p:
                exit_p = stop_p
                exit_reason = "SL"
            elif i - entry_i >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (exit_p - entry_p) / entry_p - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_p, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        elif in_pos == -1:
            if not be_active and curr_l <= entry_p - trail_be_mult * curr_atr:
                be_active = True
                stop_p = min(stop_p, entry_p * 0.9995)
                
            exit_p = None
            exit_reason = ""
            if curr_l <= tp_p:
                exit_p = tp_p
                exit_reason = "TP"
            elif curr_h >= stop_p:
                exit_p = stop_p
                exit_reason = "SL"
            elif i - entry_i >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (entry_p - exit_p) / entry_p - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_p, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        # Signal Generation
        if in_pos == 0:
            vol_ok = v[i] >= 1.0 * vol_ma[i]
            
            # Long Breakout: Close breaks above channel high, above trend EMA
            if curr_c > hh[i] and curr_c > ema_t[i] and vol_ok:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                stop_dist = max(curr_atr * atr_mult_sl, entry_p * 0.004)
                stop_p = entry_p - stop_dist
                tp_p = entry_p + atr_mult_tp * stop_dist
                be_active = False
                continue
                
            # Short Breakdown: Close breaks below channel low, below trend EMA
            if curr_c < ll[i] and curr_c < ema_t[i] and vol_ok:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                stop_dist = max(curr_atr * atr_mult_sl, entry_p * 0.004)
                stop_p = entry_p + stop_dist
                tp_p = entry_p - atr_mult_tp * stop_dist
                be_active = False
                continue

    return evaluate_trades(trades)

# Paradigm 2: MACD + EMA Momentum Pullback Scalper
def backtest_macd_momentum(
    df: pd.DataFrame,
    fast_ema: int = 12,
    slow_ema: int = 26,
    signal_len: int = 9,
    trend_ema: int = 150,
    atr_mult_sl: float = 1.2,
    atr_mult_tp: float = 2.5,
    trail_be_mult: float = 1.2,
    max_hold_bars: int = 24,
    fee_rate: float = 0.0006
) -> BacktestResult:
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # MACD
    ema_fast = pd.Series(c).ewm(span=fast_ema, adjust=False).mean()
    ema_slow = pd.Series(c).ewm(span=slow_ema, adjust=False).mean()
    macd_line = (ema_fast - ema_slow).to_numpy()
    signal_line = pd.Series(macd_line).ewm(span=signal_len, adjust=False).mean().to_numpy()
    hist = macd_line - signal_line
    
    # ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    ema_t = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    be_active = False
    
    start_bar = trend_ema + 5
    for i in range(start_bar, n - 1):
        curr_c = c[i]
        curr_h = h[i]
        curr_l = l[i]
        curr_atr = atr[i]
        next_o = o[i+1]
        
        if in_pos == 1:
            if not be_active and curr_h >= entry_p + trail_be_mult * curr_atr:
                be_active = True
                stop_p = max(stop_p, entry_p * 1.0005)
            exit_p = None
            exit_reason = ""
            if curr_h >= tp_p:
                exit_p = tp_p
                exit_reason = "TP"
            elif curr_l <= stop_p:
                exit_p = stop_p
                exit_reason = "SL"
            elif i - entry_i >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
            if exit_p is not None:
                net_ret = (exit_p - entry_p) / entry_p - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_p, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                })
                in_pos = 0
                continue
        elif in_pos == -1:
            if not be_active and curr_l <= entry_p - trail_be_mult * curr_atr:
                be_active = True
                stop_p = min(stop_p, entry_p * 0.9995)
            exit_p = None
            exit_reason = ""
            if curr_l <= tp_p:
                exit_p = tp_p
                exit_reason = "TP"
            elif curr_h >= stop_p:
                exit_p = stop_p
                exit_reason = "SL"
            elif i - entry_i >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
            if exit_p is not None:
                net_ret = (entry_p - exit_p) / entry_p - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_i, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_p, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        if in_pos == 0:
            # Bullish MACD turn when above trend EMA
            if curr_c > ema_t[i] and hist[i] > 0 and hist[i-1] <= 0:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                stop_dist = max(curr_atr * atr_mult_sl, entry_p * 0.004)
                stop_p = entry_p - stop_dist
                tp_p = entry_p + atr_mult_tp * stop_dist
                be_active = False
                continue
                
            # Bearish MACD turn when below trend EMA
            if curr_c < ema_t[i] and hist[i] < 0 and hist[i-1] >= 0:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                stop_dist = max(curr_atr * atr_mult_sl, entry_p * 0.004)
                stop_p = entry_p + stop_dist
                tp_p = entry_p - atr_mult_tp * stop_dist
                be_active = False
                continue

    return evaluate_trades(trades)

if __name__ == "__main__":
    data = load_all_symbols()
    print("--- Donchian Breakout Scalper ---")
    ret_breakout = []
    for s in SYMBOLS_10:
        res = backtest_breakout(data[s])
        ret_breakout.append(res.total_ret)
        print(f"{s[:4]}: Trades={res.trades_count}, WR={res.win_rate:.1f}%, Ret={res.total_ret:+.2f}%, PF={res.profit_factor:.2f}, MaxDD={res.max_drawdown:.2f}%")
    print(f"Avg Return Breakout: {np.mean(ret_breakout):+.2f}%\n")
    
    print("--- MACD Momentum Scalper ---")
    ret_macd = []
    for s in SYMBOLS_10:
        res = backtest_macd_momentum(data[s])
        ret_macd.append(res.total_ret)
        print(f"{s[:4]}: Trades={res.trades_count}, WR={res.win_rate:.1f}%, Ret={res.total_ret:+.2f}%, PF={res.profit_factor:.2f}, MaxDD={res.max_drawdown:.2f}%")
    print(f"Avg Return MACD: {np.mean(ret_macd):+.2f}%")
