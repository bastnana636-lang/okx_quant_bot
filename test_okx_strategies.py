import numpy as np
import pandas as pd
from scalping_engine import load_all_symbols, SYMBOLS_10, evaluate_trades, BacktestResult

data = load_all_symbols()

# -------------------------------------------------------------
# Strategy A: Trend-Aligned Reversal (TAR)
# When in strong macro trend (EMA 200), look for counter-trend overextension (Z-score or RSI extreme)
# that stalls and then re-enters the trend!
# E.g. In Downtrend, price rallies up into EMA 20/50 (counter-trend bear flag), gets rejected (bearish pinbar / RSI turn),
# and we SHORT in trend direction with tight stop and high R:R.
# In Uptrend, price dips into EMA 20/50 (bull flag pullback), gets rejected (hammer / RSI turn),
# and we LONG in trend direction.
# -------------------------------------------------------------
def strat_trend_pullback_pro(
    df,
    trend_ema=120,
    pullback_ema=20,
    trigger_ema=7,
    atr_mult_sl=1.0,
    rr_ratio=2.0,
    be_mult=1.0,
    rsi_filter=True,
    max_hold=24,
    fee_rate=0.0006
):
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['vol'].to_numpy(dtype=np.float64)
    n = len(c)
    
    ema_t = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().to_numpy()
    ema_p = pd.Series(c).ewm(span=pullback_ema, adjust=False).mean().to_numpy()
    ema_trig = pd.Series(c).ewm(span=trigger_ema, adjust=False).mean().to_numpy()
    
    # ATR 14
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # RSI 14
    diff = pd.Series(c).diff()
    up = diff.clip(lower=0)
    down = -1 * diff.clip(upper=0)
    ma_up = up.ewm(alpha=1/14, adjust=False).mean()
    ma_down = down.ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - (100 / (1 + ma_up / (ma_down + 1e-9)))).to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    be_active = False
    
    for i in range(max(trend_ema, 50), n - 1):
        curr_c = c[i]
        curr_h = h[i]
        curr_l = l[i]
        curr_atr = atr[i]
        next_o = o[i+1]
        
        if in_pos == 1:
            if not be_active and curr_h >= entry_p + be_mult * curr_atr:
                be_active = True
                stop_p = max(stop_p, entry_p * 1.0005)
            exit_p = None
            if curr_h >= tp_p:
                exit_p = tp_p
            elif curr_l <= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = curr_c
            if exit_p is not None:
                net = (exit_p - entry_p) / entry_p - 2 * fee_rate
                trades.append({'ret': net, 'bars': i - entry_i, 'side': 'LONG'})
                in_pos = 0
                continue
                
        elif in_pos == -1:
            if not be_active and curr_l <= entry_p - be_mult * curr_atr:
                be_active = True
                stop_p = min(stop_p, entry_p * 0.9995)
            exit_p = None
            if curr_l <= tp_p:
                exit_p = tp_p
            elif curr_h >= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = curr_c
            if exit_p is not None:
                net = (entry_p - exit_p) / entry_p - 2 * fee_rate
                trades.append({'ret': net, 'bars': i - entry_i, 'side': 'SHORT'})
                in_pos = 0
                continue
                
        if in_pos == 0:
            # Downtrend: price below trend EMA
            if curr_c < ema_t[i] and ema_p[i] < ema_t[i]:
                # Pullback: price touched near ema_p in last 3 bars
                pulled_back = (h[i] >= ema_p[i] * 0.998) or (h[i-1] >= ema_p[i-1] * 0.998)
                # Reversal trigger: current close drops below trigger EMA and close < open
                triggered = (curr_c < ema_trig[i] and c[i-1] >= ema_trig[i-1]) and (curr_c < o[i])
                rsi_ok = (rsi[i] < 55 and rsi[i] < rsi[i-1]) if rsi_filter else True
                
                if pulled_back and triggered and rsi_ok:
                    in_pos = -1
                    entry_p = next_o
                    entry_i = i + 1
                    risk = max(curr_atr * atr_mult_sl, entry_p * 0.003)
                    stop_p = entry_p + risk
                    tp_p = entry_p - rr_ratio * risk
                    be_active = False
                    continue
                    
            # Uptrend: price above trend EMA
            elif curr_c > ema_t[i] and ema_p[i] > ema_t[i]:
                pulled_back = (l[i] <= ema_p[i] * 1.002) or (l[i-1] <= ema_p[i-1] * 1.002)
                triggered = (curr_c > ema_trig[i] and c[i-1] <= ema_trig[i-1]) and (curr_c > o[i])
                rsi_ok = (rsi[i] > 45 and rsi[i] > rsi[i-1]) if rsi_filter else True
                
                if pulled_back and triggered and rsi_ok:
                    in_pos = 1
                    entry_p = next_o
                    entry_i = i + 1
                    risk = max(curr_atr * atr_mult_sl, entry_p * 0.003)
                    stop_p = entry_p - risk
                    tp_p = entry_p + rr_ratio * risk
                    be_active = False
                    continue

    return evaluate_trades(trades)

# -------------------------------------------------------------
# Strategy B: VWAP Mean Reversion with Volatility Climax
# -------------------------------------------------------------
def strat_vwap_climax(
    df,
    vwap_window=72, # 6 hours rolling VWAP
    z_thresh=2.2,
    vol_mult=1.5,
    tp_ratio=0.5, # take profit at 50% reversion to VWAP
    sl_pct=0.012,
    max_hold=18,
    fee_rate=0.0006
):
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['vol'].to_numpy(dtype=np.float64)
    n = len(c)
    
    # Rolling VWAP: sum(c * v) / sum(v)
    cv = c * v
    rolling_cv = pd.Series(cv).rolling(vwap_window, min_periods=vwap_window//2).sum()
    rolling_v = pd.Series(v).rolling(vwap_window, min_periods=vwap_window//2).sum()
    vwap = (rolling_cv / np.maximum(rolling_v, 1e-6)).to_numpy()
    
    # Standard deviation around VWAP
    dev = c - vwap
    std = pd.Series(dev).rolling(vwap_window, min_periods=vwap_window//2).std().to_numpy()
    z_score = dev / np.maximum(std, 1e-6)
    
    vol_ma = pd.Series(v).rolling(20, min_periods=10).mean().to_numpy()
    
    trades = []
    in_pos = 0
    entry_p = 0.0
    entry_i = 0
    stop_p = 0.0
    tp_p = 0.0
    
    for i in range(vwap_window, n - 1):
        curr_c = c[i]
        curr_h = h[i]
        curr_l = l[i]
        next_o = o[i+1]
        
        if in_pos == 1:
            exit_p = None
            if curr_h >= tp_p:
                exit_p = tp_p
            elif curr_l <= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = curr_c
            if exit_p is not None:
                net = (exit_p - entry_p) / entry_p - 2 * fee_rate
                trades.append({'ret': net, 'bars': i - entry_i, 'side': 'LONG'})
                in_pos = 0
                continue
                
        elif in_pos == -1:
            exit_p = None
            if curr_l <= tp_p:
                exit_p = tp_p
            elif curr_h >= stop_p:
                exit_p = stop_p
            elif i - entry_i >= max_hold:
                exit_p = curr_c
            if exit_p is not None:
                net = (entry_p - exit_p) / entry_p - 2 * fee_rate
                trades.append({'ret': net, 'bars': i - entry_i, 'side': 'SHORT'})
                in_pos = 0
                continue
                
        if in_pos == 0:
            vol_spike = (v[i] >= vol_mult * vol_ma[i])
            # LONG: z_score <= -z_thresh and reversing (close > open)
            if z_score[i] <= -z_thresh and vol_spike and curr_c > o[i]:
                in_pos = 1
                entry_p = next_o
                entry_i = i + 1
                stop_p = curr_l * (1.0 - 0.002)
                # Target: partial reversion to VWAP
                tp_p = entry_p + tp_ratio * (vwap[i] - entry_p)
                if tp_p <= entry_p * 1.002: # edge too small
                    in_pos = 0
                    continue
                continue
                
            # SHORT: z_score >= z_thresh and reversing (close < open)
            if z_score[i] >= z_thresh and vol_spike and curr_c < o[i]:
                in_pos = -1
                entry_p = next_o
                entry_i = i + 1
                stop_p = curr_h * (1.0 + 0.002)
                tp_p = entry_p - tp_ratio * (entry_p - vwap[i])
                if tp_p >= entry_p * 0.998:
                    in_pos = 0
                    continue
                continue

    return evaluate_trades(trades)

if __name__ == '__main__':
    print('Testing Strategy A (Trend Pullback Pro)...')
    rets_a = []
    for s in SYMBOLS_10:
        res = strat_trend_pullback_pro(data[s])
        rets_a.append(res.total_ret)
        print(f'{s[:4]}: Trades={res.trades_count}, WR={res.win_rate:.1f}%, Ret={res.total_ret:+.2f}%, PF={res.profit_factor:.2f}, MaxDD={res.max_drawdown:.2f}%')
    print(f'Strategy A Avg Return: {np.mean(rets_a):+.2f}%\n')
    
    print('Testing Strategy B (VWAP Climax Reversal)...')
    rets_b = []
    for s in SYMBOLS_10:
        res = strat_vwap_climax(data[s])
        rets_b.append(res.total_ret)
        print(f'{s[:4]}: Trades={res.trades_count}, WR={res.win_rate:.1f}%, Ret={res.total_ret:+.2f}%, PF={res.profit_factor:.2f}, MaxDD={res.max_drawdown:.2f}%')
    print(f'Strategy B Avg Return: {np.mean(rets_b):+.2f}%')
