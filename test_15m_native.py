import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path('.')
DATA_DIR = ROOT / "data" / "backtest" / "binance_um_1m_year"

def load_and_aggregate_15m(symbol: str):
    path = DATA_DIR / f"{symbol}.npz"
    raw = np.load(path)["data"]
    order = np.argsort(raw[:, 0], kind="mergesort")
    raw = raw[order]
    _, uidx = np.unique(raw[:, 0], return_index=True)
    raw = raw[np.sort(uidx)]
    
    ts = raw[:, 0].astype(np.int64)
    op = raw[:, 1]
    hi = raw[:, 2]
    lo = raw[:, 3]
    cl = raw[:, 4]
    vol = raw[:, 5]
    
    interval = 900 # 15 min
    bucket = (ts // interval) * interval
    df = pd.DataFrame({"b": bucket, "o": op, "h": hi, "l": lo, "c": cl, "v": vol})
    bars = df.groupby("b", sort=True).agg({
        "o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"
    })
    
    full_idx = np.arange(bars.index[0], bars.index[-1] + interval, interval, dtype=np.int64)
    bars = bars.reindex(full_idx)
    bars["c"] = bars["c"].ffill()
    bars["o"] = bars["o"].fillna(bars["c"])
    bars["h"] = bars["h"].fillna(bars["c"])
    bars["l"] = bars["l"].fillna(bars["c"])
    bars["v"] = bars["v"].fillna(0)
    bars["ts"] = bars.index
    return bars.reset_index(drop=True)

# Test loading
df = load_and_aggregate_15m("SUIUSDT")
print(f"Loaded SUIUSDT 15m bars: {len(df)}")
