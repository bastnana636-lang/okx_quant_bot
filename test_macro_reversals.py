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

def analyze_15m(symbol='SUIUSDT'):
    raw = load_data(symbol)
    df = aggregate_candles(raw, 900) # 15m
    c = df['c'].to_numpy(dtype=np.float64)
    h = df['h'].to_numpy(dtype=np.float64)
    l = df['l'].to_numpy(dtype=np.float64)
    o = df['o'].to_numpy(dtype=np.float64)
    n = len(c)
    
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).rolling(24, min_periods=24).mean().to_numpy()
    
    # Daily baseline: 96 bars of 15m = 24h. 14 days = 1344 bars.
    returns = np.zeros(n)
    returns[1:] = (c[1:] - c[:-1]) / c[:-1]
    base_std = pd.Series(returns).rolling(1344, min_periods=400).std().to_numpy()
    
    # 24-hour move: 96 bars
    for lookback_hours in [6, 12, 24]:
        lookback_bars = lookback_hours * 4
        c_shift = pd.Series(c).shift(lookback_bars).to_numpy()
        cum_ret = (c - c_shift) / c_shift
        expected_std = base_std * np.sqrt(lookback_bars)
        z = cum_ret / np.maximum(expected_std, 1e-6)
        
        # Look at extreme moves: |z| >= 3.5
        dumps = z <= -3.5
        pumps = z >= 3.5
        print(f"{symbol} ({lookback_hours}h lookback): {dumps.sum()} dump bars, {pumps.sum()} pump bars")

for s in ['BTCUSDT', 'SOLUSDT', 'SUIUSDT', 'DOGEUSDT']:
    analyze_15m(s)
