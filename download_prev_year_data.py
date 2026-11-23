import requests
import time
import datetime
import pandas as pd
import numpy as np
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

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

def fetch_prev_year(inst_id: str):
    prev_save_path = DATA_DIR / f"{inst_id}_5m_prev_year.csv"
    two_year_save_path = DATA_DIR / f"{inst_id}_5m_2years.csv"
    
    now_ms = int(time.time() * 1000)
    target_start_ms = now_ms - 730 * 24 * 3600 * 1000 # 2 years ago (2024-11-23)
    
    # 1. Check if prev_year file already exists and has full data
    if prev_save_path.exists():
        df_old = pd.read_csv(prev_save_path)
        if len(df_old) >= 104000:
            print(f"[{inst_id}] Previous year already downloaded ({len(df_old)} records). Skipping fetch.")
            # Ensure 2years combined file exists
            if not two_year_save_path.exists() and (DATA_DIR / f"{inst_id}_5m_1year.csv").exists():
                df_1y = pd.read_csv(DATA_DIR / f"{inst_id}_5m_1year.csv")
                df_combined = pd.concat([df_old, df_1y], ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
                df_combined.to_csv(two_year_save_path, index=False)
                print(f"[{inst_id}] Created 2-year combined file: {len(df_combined)} bars.")
            return df_old

    # 2. Find starting point from 1year file
    one_year_path = DATA_DIR / f"{inst_id}_5m_1year.csv"
    if not one_year_path.exists():
        raise FileNotFoundError(f"1-year file not found for {inst_id}: {one_year_path}")
        
    df_1y = pd.read_csv(one_year_path)
    oldest_ts = int(df_1y["ts"].min())
    print(f"[{inst_id}] Starting backwards fetch from 1y boundary: {datetime.datetime.fromtimestamp(oldest_ts/1000, tz=datetime.timezone.utc)} back to {datetime.datetime.fromtimestamp(target_start_ms/1000, tz=datetime.timezone.utc)}...")

    url_history = "https://www.okx.com/api/v5/market/history-candles"
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; QuantOKX/1.0)"})
    
    new_rows = []
    consecutive_errors = 0
    last_print = time.time()
    
    while oldest_ts > target_start_ms:
        params = {"instId": inst_id, "bar": "5m", "limit": "100", "after": str(oldest_ts)}
        try:
            r = session.get(url_history, params=params, timeout=12)
            res = r.json()
            if res.get("code") != "0":
                consecutive_errors += 1
                if consecutive_errors > 5:
                    print(f"[{inst_id}] Warning: API error {res}. Pausing 2.5s...")
                    time.sleep(2.5)
                else:
                    time.sleep(0.5)
                continue
            
            b = res.get("data", [])
            if not b:
                print(f"[{inst_id}] Reached earliest data available at {datetime.datetime.fromtimestamp(oldest_ts/1000, tz=datetime.timezone.utc)}")
                break
                
            new_rows.extend(b)
            oldest_ts = int(b[-1][0])
            consecutive_errors = 0
            
            if time.time() - last_print > 15.0:
                dt_str = datetime.datetime.fromtimestamp(oldest_ts/1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
                print(f"  [{inst_id}] Fetched {len(new_rows)} bars so far, reached: {dt_str}")
                last_print = time.time()
                
            time.sleep(0.08)
            
        except Exception as e:
            consecutive_errors += 1
            time.sleep(1.0)
            
    if not new_rows:
        print(f"[{inst_id}] No new rows fetched!")
        return None
        
    df_new = pd.DataFrame(new_rows, columns=["ts", "o", "h", "l", "c", "vol", "vol_ccy", "vol_quote", "confirm"])
    for col in ["ts"]:
        df_new[col] = df_new[col].astype(np.int64)
    for col in ["o", "h", "l", "c", "vol", "vol_ccy", "vol_quote"]:
        df_new[col] = df_new[col].astype(np.float64)
    df_new["dt"] = pd.to_datetime(df_new["ts"], unit="ms", utc=True)
    
    # Clean and save previous year (2024-11 to 2025-11)
    df_prev = df_new.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
    df_prev = df_prev[df_prev["ts"] >= target_start_ms].reset_index(drop=True)
    df_prev.to_csv(prev_save_path, index=False)
    print(f"==> [{inst_id}] Prev-Year Saved: {len(df_prev)} bars ({df_prev['dt'].iloc[0]} to {df_prev['dt'].iloc[-1]}) -> {prev_save_path}")
    
    # Combine with 1year to form full 2-year dataset
    df_2y = pd.concat([df_prev, df_1y], ignore_index=True).sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
    df_2y.to_csv(two_year_save_path, index=False)
    print(f"==> [{inst_id}] 2-Year Combined Saved: {len(df_2y)} bars ({df_2y['dt'].iloc[0]} to {df_2y['dt'].iloc[-1]}) -> {two_year_save_path}\n")
    return df_prev

def main():
    symbols_to_fetch = SYMBOLS_10
    if len(sys.argv) > 1:
        symbols_to_fetch = sys.argv[1:]
        
    print(f"Starting Previous Year (2024-11 to 2025-11) 5m K-line download for {len(symbols_to_fetch)} symbols...")
    start_t = time.time()
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch_prev_year, s) for s in symbols_to_fetch]
        for f in futures:
            f.result()
            
    print(f"All Previous Year data downloaded successfully in {time.time() - start_t:.1f}s!")

if __name__ == "__main__":
    main()
