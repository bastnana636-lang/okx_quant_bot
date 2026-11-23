import requests
import time
import datetime
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path("data/okx_candles")
DATA_DIR.mkdir(parents=True, exist_ok=True)

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

def fetch_okx_candles(inst_id: str, bar: str = "5m", days: int = 30, overwrite: bool = False):
    save_path = DATA_DIR / f"{inst_id}_{bar}_last_month.csv"
    if not overwrite and save_path.exists():
        df_exist = pd.read_csv(save_path)
        if len(df_exist) >= 8000:
            print(f"[{inst_id} {bar}] Already downloaded ({len(df_exist)} records). Skipping.")
            return df_exist

    now_ms = int(time.time() * 1000)
    target_start_ms = now_ms - days * 24 * 3600 * 1000
    
    url_recent = "https://www.okx.com/api/v5/market/candles"
    url_history = "https://www.okx.com/api/v5/market/history-candles"
    
    all_rows = []
    print(f"[{inst_id} {bar}] Downloading from {datetime.datetime.fromtimestamp(target_start_ms/1000, tz=datetime.timezone.utc)} to {datetime.datetime.fromtimestamp(now_ms/1000, tz=datetime.timezone.utc)}...")
    
    # 1. Fetch most recent 100
    params = {"instId": inst_id, "bar": bar, "limit": "100"}
    try:
        r = requests.get(url_recent, params=params, timeout=10)
        data = r.json().get("data", [])
        if data:
            all_rows.extend(data)
            last_ts = data[-1][0]
        else:
            print(f"Failed to fetch initial candles: {r.text}")
            return None
    except Exception as e:
        print(f"Error fetching initial candles: {e}")
        return None
        
    # 2. Paginate backwards
    batch_count = 0
    while int(last_ts) > target_start_ms:
        batch_count += 1
        params = {"instId": inst_id, "bar": bar, "limit": "100", "after": last_ts}
        try:
            r = requests.get(url_history, params=params, timeout=10)
            res = r.json()
            if res.get("code") != "0":
                print(f"OKX API error: {res}")
                time.sleep(0.5)
                continue
            b = res.get("data", [])
            if not b:
                print(f"No more data returned after {last_ts}")
                break
            all_rows.extend(b)
            last_ts = b[-1][0]
            time.sleep(0.06)  # OKX rate limit: ~20 req/2s
        except Exception as e:
            print(f"Exception during pagination: {e}, retrying...")
            time.sleep(1.0)
            
    # Parse into DataFrame
    # OKX format: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    df = pd.DataFrame(all_rows, columns=["ts", "o", "h", "l", "c", "vol", "vol_ccy", "vol_quote", "confirm"])
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
    
    # Filter to requested time range
    df = df[df["ts"] >= target_start_ms].reset_index(drop=True)
    
    for col in ["o", "h", "l", "c", "vol", "vol_ccy", "vol_quote"]:
        df[col] = df[col].astype(np.float64)
        
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.to_csv(save_path, index=False)
    print(f"Saved {inst_id} {bar} to {save_path} ({len(df)} records from {df['dt'].iloc[0]} to {df['dt'].iloc[-1]})")
    return df

if __name__ == "__main__":
    for symbol in SYMBOLS_10:
        fetch_okx_candles(symbol, bar="5m", days=30)
