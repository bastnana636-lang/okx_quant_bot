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

def fetch_symbol_1y(inst_id: str, days: int = 365):
    save_path = DATA_DIR / f"{inst_id}_5m_1year.csv"
    now_ms = int(time.time() * 1000)
    target_start_ms = now_ms - days * 24 * 3600 * 1000
    
    # 1. Check if 1year file already exists and has full data
    if save_path.exists():
        df_old = pd.read_csv(save_path)
        if len(df_old) >= 104000:
            print(f"[{inst_id}] Already downloaded ({len(df_old)} records). Skipping.")
            return df_old
        elif len(df_old) > 0:
            existing_rows = df_old.to_dict(orient="records")
            oldest_ts = int(df_old["ts"].min())
            print(f"[{inst_id}] Resuming from existing 1y file: {len(df_old)} records, oldest: {datetime.datetime.fromtimestamp(oldest_ts/1000, tz=datetime.timezone.utc)}")
    else:
        # Seed from 6-month file if present
        seed_path = DATA_DIR / f"{inst_id}_5m_6months.csv"
        if seed_path.exists():
            df_seed = pd.read_csv(seed_path)
            existing_rows = df_seed.to_dict(orient="records")
            oldest_ts = int(df_seed["ts"].min())
            print(f"[{inst_id}] Seeded with 6-month data ({len(df_seed)} records), oldest: {datetime.datetime.fromtimestamp(oldest_ts/1000, tz=datetime.timezone.utc)}")
        else:
            existing_rows = []
            oldest_ts = now_ms

    url_history = "https://www.okx.com/api/v5/market/history-candles"
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; QuantOKX/1.0)"})
    
    new_rows = []
    consecutive_errors = 0
    last_print = time.time()
    
    print(f"[{inst_id}] Fetching history back to {datetime.datetime.fromtimestamp(target_start_ms/1000, tz=datetime.timezone.utc)}...")
    
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
                total_so_far = len(existing_rows) + len(new_rows)
                print(f"  [{inst_id}] Total {total_so_far} bars so far, reached: {dt_str}")
                last_print = time.time()
                
            time.sleep(0.08) # safe pacing per thread
            
        except Exception as e:
            consecutive_errors += 1
            time.sleep(1.0)
            
    # Combine existing + new
    if new_rows:
        df_new = pd.DataFrame(new_rows, columns=["ts", "o", "h", "l", "c", "vol", "vol_ccy", "vol_quote", "confirm"])
        for col in ["ts"]:
            df_new[col] = df_new[col].astype(np.int64)
        for col in ["o", "h", "l", "c", "vol", "vol_ccy", "vol_quote"]:
            df_new[col] = df_new[col].astype(np.float64)
        df_new["dt"] = pd.to_datetime(df_new["ts"], unit="ms", utc=True)
        
        if existing_rows:
            df_exist = pd.DataFrame(existing_rows)
            df_exist["ts"] = df_exist["ts"].astype(np.int64)
            for col in ["o", "h", "l", "c", "vol", "vol_ccy", "vol_quote"]:
                df_exist[col] = df_exist[col].astype(np.float64)
            df_exist["dt"] = pd.to_datetime(df_exist["ts"], unit="ms", utc=True)
            df_combined = pd.concat([df_exist, df_new], ignore_index=True)
        else:
            df_combined = df_new
    else:
        df_combined = pd.DataFrame(existing_rows)
        
    df_combined = df_combined.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
    df_combined = df_combined[df_combined["ts"] >= target_start_ms].reset_index(drop=True)
    df_combined.to_csv(save_path, index=False)
    
    print(f"==> [{inst_id}] 1-Year Complete: {len(df_combined)} bars ({df_combined['dt'].iloc[0]} to {df_combined['dt'].iloc[-1]}) saved to {save_path}\n")
    return df_combined

def main():
    symbols_to_fetch = SYMBOLS_10
    if len(sys.argv) > 1:
        symbols_to_fetch = sys.argv[1:]
        
    print(f"Starting 1-Year (365 days) 5m K-line download for {len(symbols_to_fetch)} symbols...")
    start_t = time.time()
    
    # Use 3 concurrent workers to stay well within OKX rate limits
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch_symbol_1y, s, 365) for s in symbols_to_fetch]
        for f in futures:
            f.result()
            
    print(f"All 1-Year data downloaded successfully in {time.time() - start_t:.1f}s!")

if __name__ == "__main__":
    main()
