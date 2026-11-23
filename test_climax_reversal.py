import numpy as np
import pandas as pd
from test_15m_strategy import load_data, aggregate_candles

def test_symbol_climax(symbol='SOLUSDT'):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 900) # 15m candles
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    v = df['v'].to_numpy(dtype=np.float64)
    n = len(c)
    
    # ATR (14 bars of 15m)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
    
    # Daily ATR: rolling mean of daily TR (96 bars = 1 day, 14 days = 1344 bars)
    daily_tr = pd.Series(tr).rolling(96).sum().to_numpy()
    daily_atr = pd.Series(daily_tr).rolling(14 * 96, min_periods=7 * 96).mean().to_numpy()
    
    # Rolling 20-period Bollinger Bands on 15m
    sma20 = pd.Series(c).rolling(20).mean().to_numpy()
    std20 = pd.Series(c).rolling(20).std().to_numpy()
    upper_3 = sma20 + 3.0 * std20
    lower_3 = sma20 - 3.0 * std20
    
    # RSI (14)
    delta = np.zeros(n)
    delta[1:] = c[1:] - c[:-1]
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    avg_loss = pd.Series(loss).ewm(alpha=1/14, adjust=False).mean().to_numpy()
    rs = np.where(avg_loss == 0, 100.0, avg_gain / np.maximum(avg_loss, 1e-9))
    rsi = 100 - (100 / (1 + rs))
    
    # Measure cumulative move over 24 hours (96 bars) and 12 hours (48 bars)
    c_48 = pd.Series(c).shift(48).to_numpy()
    move_48 = c - c_48 # price difference
    move_in_daily_atr = np.abs(move_48) / np.maximum(daily_atr, 1e-6)
    
    print(f"--- {symbol} ---")
    print(f"Daily ATR %: {(daily_atr / c).mean():.2%}")
    print(f"Max 12h move in Daily ATRs: {np.nanmax(move_in_daily_atr):.2f}")
    print(f"99th percentile of 12h move in Daily ATRs: {np.nanpercentile(move_in_daily_atr, 99):.2f}")

for s in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'SUIUSDT', 'DOGEUSDT']:
    test_symbol_climax(s)
