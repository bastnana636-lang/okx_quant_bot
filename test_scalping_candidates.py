import numpy as np
import pandas as pd
from pathlib import Path

def load_data(symbol="BTC-USDT-SWAP", bar="5m"):
    path = Path(f"data/okx_candles/{symbol}_{bar}_last_month.csv")
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df["dt"])
    return df

# Candidate 1: Trend Pullback Scalper (EMA Trend + Pullback Rejection + ATR Trailing)
# Logic:
# 1. Macro Trend: EMA(fast) vs EMA(slow), e.g., EMA(20) vs EMA(60) or EMA(50) vs EMA(200).
# 2. Pullback Condition: In downtrend (c < EMA50 < EMA200), price pulls back up towards EMA20/50, shows upper shadow/bearish pinbar, RSI > 50 but turns down.
#    In uptrend (c > EMA50 > EMA200), price pulls back to EMA20/50, shows lower shadow/bullish pinbar, RSI < 50 turns up.
# 3. Dynamic Stop Loss: 1.0 * ATR or swing extreme.
# 4. Take Profit: 1.5 * ATR fixed TP or Trailing Stop after reaching 1.0 * ATR profit.

def simulate_trend_pullback(df, fee_rate=0.0006): # 0.06% per side = 0.12% round trip
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    v = df["v"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # EMAs
    ema9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
    ema21 = pd.Series(c).ewm(span=21, adjust=False).mean().to_numpy()
    ema50 = pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy()
    ema200 = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()
    
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
    in_pos = 0 # 1: Long, -1: Short
    entry_price = 0.0
    entry_idx = 0
    stop_loss = 0.0
    take_profit = 0.0
    be_triggered = False
    
    for i in range(200, n - 1):
        curr_c = c[i]
        curr_o = o[i]
        curr_h = h[i]
        curr_l = l[i]
        curr_atr = atr[i]
        next_o = o[i+1]
        
        # Position Management
        if in_pos == 1:
            # Check exit on next bar
            # High hits TP?
            exit_p = None
            exit_reason = ""
            
            # Trailing breakeven: if price reached +1.2 ATR, move stop to entry_price
            if not be_triggered and curr_h >= entry_price + 1.2 * curr_atr:
                be_triggered = True
                stop_loss = max(stop_loss, entry_price * 1.0005) # small green
                
            if curr_h >= take_profit:
                exit_p = take_profit
                exit_reason = "TP"
            elif curr_l <= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= 24: # max hold 2 hours (24 * 5m)
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                # Calculate PnL
                net_pnl = (exit_p - entry_price) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_pnl, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        elif in_pos == -1:
            exit_p = None
            exit_reason = ""
            
            # Trailing breakeven
            if not be_triggered and curr_l <= entry_price - 1.2 * curr_atr:
                be_triggered = True
                stop_loss = min(stop_loss, entry_price * 0.9995)
                
            if curr_l <= take_profit:
                exit_p = take_profit
                exit_reason = "TP"
            elif curr_h >= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= 24:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_pnl = (entry_price - exit_p) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_pnl, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        # Entry Logic
        if in_pos == 0:
            body = abs(curr_c - curr_o)
            upper_wick = curr_h - max(curr_c, curr_o)
            lower_wick = min(curr_c, curr_o) - curr_l
            
            # Trend condition
            uptrend = (ema21[i] > ema50[i]) and (curr_c > ema50[i])
            downtrend = (ema21[i] < ema50[i]) and (curr_c < ema50[i])
            
            # LONG Entry: Uptrend + Pullback to EMA21/50 + Rejection (Hammer / Bullish wick) + RSI bounce
            if uptrend:
                pullback = (l[i-1] <= ema21[i-1] or curr_l <= ema21[i])
                candle_bullish = (curr_c > curr_o and lower_wick >= 0.6 * body) or (curr_c > ema9[i] and c[i-1] <= ema9[i-1])
                rsi_ok = (rsi[i] >= 42 and rsi[i] <= 65 and rsi[i] > rsi[i-1])
                
                if pullback and candle_bullish and rsi_ok:
                    in_pos = 1
                    entry_price = next_o
                    entry_idx = i + 1
                    risk = max(curr_atr * 1.2, entry_price * 0.003)
                    stop_loss = entry_price - risk
                    take_profit = entry_price + 2.0 * risk
                    be_triggered = False
                    continue
                    
            # SHORT Entry: Downtrend + Pullback up to EMA21/50 + Rejection (Shooting star / Bearish wick) + RSI drop
            if downtrend:
                pullback = (h[i-1] >= ema21[i-1] or curr_h >= ema21[i])
                candle_bearish = (curr_c < curr_o and upper_wick >= 0.6 * body) or (curr_c < ema9[i] and c[i-1] >= ema9[i-1])
                rsi_ok = (rsi[i] <= 58 and rsi[i] >= 35 and rsi[i] < rsi[i-1])
                
                if pullback and candle_bearish and rsi_ok:
                    in_pos = -1
                    entry_price = next_o
                    entry_idx = i + 1
                    risk = max(curr_atr * 1.2, entry_price * 0.003)
                    stop_loss = entry_price + risk
                    take_profit = entry_price - 2.0 * risk
                    be_triggered = False
                    continue

    return analyze_trades(trades, "Trend Pullback Scalper")

def analyze_trades(trades, name=""):
    if not trades:
        return {"name": name, "trades": 0, "win_rate": 0, "total_ret": 0, "pf": 0}
    df_t = pd.DataFrame(trades)
    wins = df_t[df_t["ret"] > 0]
    losses = df_t[df_t["ret"] <= 0]
    win_rate = len(wins) / len(df_t) * 100
    cum_ret = (1 + df_t["ret"]).prod() - 1
    total_gain = wins["ret"].sum()
    total_loss = abs(losses["ret"].sum())
    pf = total_gain / total_loss if total_loss > 0 else np.nan
    avg_bars = df_t["bars"].mean() * 5 # in minutes
    
    # Max drawdown
    equity = (1 + df_t["ret"]).cumprod()
    peak = equity.cummax()
    dd = (equity - peak) / peak
    max_dd = dd.min() * 100
    
    res = {
        "name": name,
        "trades": len(df_t),
        "win_rate": f"{win_rate:.1f}%",
        "cum_ret": f"{cum_ret*100:+.2f}%",
        "pf": f"{pf:.2f}",
        "max_dd": f"{max_dd:.2f}%",
        "avg_hold_mins": f"{avg_bars:.1f}",
        "trades_list": trades
    }
    return res

if __name__ == "__main__":
    for sym in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]:
        df = load_data(sym)
        res = simulate_trend_pullback(df)
        print(f"[{sym}] Trades: {res['trades']}, WinRate: {res['win_rate']}, Return: {res['cum_ret']}, PF: {res['pf']}, MaxDD: {res['max_dd']}, AvgHold: {res['avg_hold_mins']}m")
