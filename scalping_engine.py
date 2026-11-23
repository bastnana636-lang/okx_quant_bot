import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple

SYMBOLS_10 = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
    "ADA-USDT-SWAP",
    "SUI-USDT-SWAP",
    "LINK-USDT-SWAP",
    "LTC-USDT-SWAP",
    "AVAX-USDT-SWAP"
]

DATA_DIR = Path("data/okx_candles")

def load_data(symbol: str, bar: str = "5m") -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}_{bar}_last_month.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df["dt"])
    return df

def load_all_symbols():
    data = {}
    for s in SYMBOLS_10:
        data[s] = load_data(s)
    return data

@dataclass
class BacktestResult:
    trades_count: int
    win_rate: float
    total_ret: float
    profit_factor: float
    max_drawdown: float
    avg_hold_mins: float
    trades: list

def evaluate_trades(trades: list, initial_capital: float = 10000.0) -> BacktestResult:
    if not trades:
        return BacktestResult(0, 0.0, 0.0, 0.0, 0.0, 0.0, [])
    
    df = pd.DataFrame(trades)
    wins = df[df["ret"] > 0]
    losses = df[df["ret"] <= 0]
    win_rate = (len(wins) / len(df)) * 100.0
    
    # Cumulative compound return
    equity_curve = (1.0 + df["ret"]).cumprod()
    total_ret = (equity_curve.iloc[-1] - 1.0) * 100.0
    
    gain_sum = wins["ret"].sum()
    loss_sum = abs(losses["ret"].sum())
    pf = (gain_sum / loss_sum) if loss_sum > 0 else 999.0
    
    # Max Drawdown
    peak = equity_curve.cummax()
    dd = (equity_curve - peak) / peak
    max_dd = abs(dd.min()) * 100.0
    
    avg_hold = df["bars"].mean() * 5.0 # 5 min bars
    
    return BacktestResult(
        trades_count=len(df),
        win_rate=win_rate,
        total_ret=total_ret,
        profit_factor=pf,
        max_drawdown=max_dd,
        avg_hold_mins=avg_hold,
        trades=trades
    )

# ==========================================
# Strategy 1: Adaptive Trend-Pullback Scalper
# ==========================================
def backtest_trend_pullback(
    df: pd.DataFrame,
    ema_fast: int = 20,
    ema_slow: int = 60,
    ema_trigger: int = 9,
    rsi_len: int = 14,
    atr_len: int = 14,
    sl_atr_mult: float = 1.2,
    tp_atr_mult: float = 2.4,
    be_atr_mult: float = 1.0,
    max_hold_bars: int = 16,
    fee_rate: float = 0.0006 # 0.06% per side taker fee + slippage
) -> BacktestResult:
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # Precompute indicators
    ef = pd.Series(c).ewm(span=ema_fast, adjust=False).mean().to_numpy()
    es = pd.Series(c).ewm(span=ema_slow, adjust=False).mean().to_numpy()
    et = pd.Series(c).ewm(span=ema_trigger, adjust=False).mean().to_numpy()
    
    # ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(atr_len, min_periods=atr_len).mean().to_numpy()
    
    # RSI
    diff = pd.Series(c).diff()
    up = diff.clip(lower=0)
    down = -1 * diff.clip(upper=0)
    ma_up = up.ewm(alpha=1.0/rsi_len, adjust=False).mean()
    ma_down = down.ewm(alpha=1.0/rsi_len, adjust=False).mean()
    rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
    
    trades = []
    in_pos = 0 # 1: Long, -1: Short
    entry_price = 0.0
    entry_idx = 0
    stop_loss = 0.0
    take_profit = 0.0
    be_active = False
    
    start_bar = max(ema_slow, atr_len) + 5
    
    for i in range(start_bar, n - 1):
        curr_c = c[i]
        curr_o = o[i]
        curr_h = h[i]
        curr_l = l[i]
        curr_atr = atr[i]
        next_o = o[i+1]
        
        # Position Management
        if in_pos == 1:
            # Trailing breakeven
            if not be_active and curr_h >= entry_price + be_atr_mult * curr_atr:
                be_active = True
                stop_loss = max(stop_loss, entry_price * 1.0005)
                
            exit_p = None
            exit_reason = ""
            if curr_h >= take_profit:
                exit_p = take_profit
                exit_reason = "TP"
            elif curr_l <= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (exit_p - entry_price) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        elif in_pos == -1:
            if not be_active and curr_l <= entry_price - be_atr_mult * curr_atr:
                be_active = True
                stop_loss = min(stop_loss, entry_price * 0.9995)
                
            exit_p = None
            exit_reason = ""
            if curr_l <= take_profit:
                exit_p = take_profit
                exit_reason = "TP"
            elif curr_h >= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (entry_price - exit_p) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        # Signal Generation
        if in_pos == 0:
            body = abs(curr_c - curr_o)
            upper_wick = curr_h - max(curr_c, curr_o)
            lower_wick = min(curr_c, curr_o) - curr_l
            
            # Trend Regime
            uptrend = (ef[i] > es[i]) and (curr_c > es[i])
            downtrend = (ef[i] < es[i]) and (curr_c < es[i])
            
            # LONG:
            if uptrend:
                # Price pulled back to near or below EMA_fast, now rebounding
                pullback = (l[i-1] <= ef[i-1] or curr_l <= ef[i])
                bullish_reversal = (curr_c > curr_o and lower_wick >= 0.5 * body) or (curr_c > et[i] and c[i-1] <= et[i-1])
                rsi_valid = (rsi[i] >= 40 and rsi[i] <= 68 and rsi[i] > rsi[i-1])
                
                if pullback and bullish_reversal and rsi_valid:
                    in_pos = 1
                    entry_price = next_o
                    entry_idx = i + 1
                    risk = max(curr_atr * sl_atr_mult, entry_price * 0.003)
                    stop_loss = entry_price - risk
                    take_profit = entry_price + tp_atr_mult * risk
                    be_active = False
                    continue
                    
            # SHORT:
            if downtrend:
                pullback = (h[i-1] >= ef[i-1] or curr_h >= ef[i])
                bearish_reversal = (curr_c < curr_o and upper_wick >= 0.5 * body) or (curr_c < et[i] and c[i-1] >= et[i-1])
                rsi_valid = (rsi[i] <= 60 and rsi[i] >= 32 and rsi[i] < rsi[i-1])
                
                if pullback and bearish_reversal and rsi_valid:
                    in_pos = -1
                    entry_price = next_o
                    entry_idx = i + 1
                    risk = max(curr_atr * sl_atr_mult, entry_price * 0.003)
                    stop_loss = entry_price + risk
                    take_profit = entry_price - tp_atr_mult * risk
                    be_active = False
                    continue

    return evaluate_trades(trades)

# ==============================================================
# Strategy 2: Dynamic Bollinger Bands & RSI Mean Reversion Scalper
# ==============================================================
def backtest_bollinger_mr(
    df: pd.DataFrame,
    bb_len: int = 20,
    bb_std: float = 2.0,
    rsi_len: int = 14,
    rsi_ob: float = 70.0,
    rsi_os: float = 30.0,
    sl_pct: float = 0.012,
    max_hold_bars: int = 12,
    fee_rate: float = 0.0006
) -> BacktestResult:
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    n = len(c)
    
    mid = pd.Series(c).rolling(bb_len, min_periods=bb_len).mean().to_numpy()
    std = pd.Series(c).rolling(bb_len, min_periods=bb_len).std().to_numpy()
    upper = mid + bb_std * std
    lower = mid - bb_std * std
    
    # RSI
    diff = pd.Series(c).diff()
    up = diff.clip(lower=0)
    down = -1 * diff.clip(upper=0)
    ma_up = up.ewm(alpha=1.0/rsi_len, adjust=False).mean()
    ma_down = down.ewm(alpha=1.0/rsi_len, adjust=False).mean()
    rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
    
    trades = []
    in_pos = 0
    entry_price = 0.0
    entry_idx = 0
    stop_loss = 0.0
    
    start_bar = bb_len + 5
    for i in range(start_bar, n - 1):
        curr_c = c[i]
        curr_o = o[i]
        curr_h = h[i]
        curr_l = l[i]
        next_o = o[i+1]
        
        if in_pos == 1:
            exit_p = None
            exit_reason = ""
            # TP when price crosses above middle band
            if curr_h >= mid[i]:
                exit_p = max(mid[i], curr_o)
                exit_reason = "TP_MID"
            elif curr_l <= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (exit_p - entry_price) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        elif in_pos == -1:
            exit_p = None
            exit_reason = ""
            # TP when price crosses below middle band
            if curr_l <= mid[i]:
                exit_p = min(mid[i], curr_o)
                exit_reason = "TP_MID"
            elif curr_h >= stop_loss:
                exit_p = stop_loss
                exit_reason = "SL"
            elif i - entry_idx >= max_hold_bars:
                exit_p = curr_c
                exit_reason = "TIME"
                
            if exit_p is not None:
                net_ret = (entry_price - exit_p) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": exit_reason
                })
                in_pos = 0
                continue
                
        if in_pos == 0:
            # LONG: Pierced lower band, and now closed back above lower band with RSI oversold
            if l[i-1] < lower[i-1] and curr_c > lower[i] and rsi[i] < rsi_os + 5 and curr_c > curr_o:
                in_pos = 1
                entry_price = next_o
                entry_idx = i + 1
                stop_loss = entry_price * (1.0 - sl_pct)
                continue
                
            # SHORT: Pierced upper band, and now closed back below upper band with RSI overbought
            if h[i-1] > upper[i-1] and curr_c < upper[i] and rsi[i] > rsi_ob - 5 and curr_c < curr_o:
                in_pos = -1
                entry_price = next_o
                entry_idx = i + 1
                stop_loss = entry_price * (1.0 + sl_pct)
                continue
                
    return evaluate_trades(trades)

# ==============================================================
# Strategy 3: Supertrend Trailing Breakout Scalper
# ==============================================================
def backtest_supertrend(
    df: pd.DataFrame,
    atr_period: int = 10,
    atr_multiplier: float = 3.0,
    trend_ema: int = 100,
    fee_rate: float = 0.0006
) -> BacktestResult:
    c = df["c"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    o = df["o"].to_numpy(dtype=np.float64)
    n = len(c)
    
    # ATR
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(atr_period, min_periods=atr_period).mean().to_numpy()
    
    ema_t = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().to_numpy()
    
    # Calculate Supertrend
    upper_band = np.zeros(n)
    lower_band = np.zeros(n)
    st = np.zeros(n)
    direction = np.zeros(n, dtype=np.int32) # 1: bull, -1: bear
    
    hl2 = (h + l) / 2.0
    for i in range(atr_period, n):
        basic_upper = hl2[i] + atr_multiplier * atr[i]
        basic_lower = hl2[i] - atr_multiplier * atr[i]
        
        if basic_upper < upper_band[i-1] or c[i-1] > upper_band[i-1]:
            upper_band[i] = basic_upper
        else:
            upper_band[i] = upper_band[i-1]
            
        if basic_lower > lower_band[i-1] or c[i-1] < lower_band[i-1]:
            lower_band[i] = basic_lower
        else:
            lower_band[i] = lower_band[i-1]
            
        if direction[i-1] == 1:
            if c[i] < lower_band[i]:
                direction[i] = -1
                st[i] = upper_band[i]
            else:
                direction[i] = 1
                st[i] = lower_band[i]
        else:
            if c[i] > upper_band[i]:
                direction[i] = 1
                st[i] = lower_band[i]
            else:
                direction[i] = -1
                st[i] = upper_band[i]
                
    trades = []
    in_pos = 0
    entry_price = 0.0
    entry_idx = 0
    stop_loss = 0.0
    
    for i in range(trend_ema, n - 1):
        curr_c = c[i]
        curr_h = h[i]
        curr_l = l[i]
        next_o = o[i+1]
        
        # Position check
        if in_pos == 1:
            # Trailing stop with Supertrend lower band
            stop_loss = max(stop_loss, lower_band[i])
            if curr_l <= stop_loss or direction[i] == -1:
                exit_p = min(curr_c, stop_loss)
                net_ret = (exit_p - entry_price) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "LONG", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": "ST_FLIP"
                })
                in_pos = 0
                continue
        elif in_pos == -1:
            stop_loss = min(stop_loss, upper_band[i])
            if curr_h >= stop_loss or direction[i] == 1:
                exit_p = max(curr_c, stop_loss)
                net_ret = (entry_price - exit_p) / entry_price - 2 * fee_rate
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "side": "SHORT", "entry_p": entry_price, "exit_p": exit_p,
                    "ret": net_ret, "bars": i - entry_idx, "reason": "ST_FLIP"
                })
                in_pos = 0
                continue
                
        if in_pos == 0:
            # Bull flip aligned with trend EMA
            if direction[i] == 1 and direction[i-1] == -1 and curr_c > ema_t[i]:
                in_pos = 1
                entry_price = next_o
                entry_idx = i + 1
                stop_loss = lower_band[i]
                continue
            # Bear flip aligned with trend EMA
            if direction[i] == -1 and direction[i-1] == 1 and curr_c < ema_t[i]:
                in_pos = -1
                entry_price = next_o
                entry_idx = i + 1
                stop_loss = upper_band[i]
                continue
                
    return evaluate_trades(trades)
