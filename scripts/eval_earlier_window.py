"""Score the locked momentum configuration on 2026-01-01 through 2026-05-31.

That window ends before the June-September sample used to choose and confirm
the parameters. The configuration is not changed to fit this window.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from optimize_mean_reversion import fetch_day  # noqa: E402
from search_trend import PAIRS, evaluate  # noqa: E402
from trend_sim import aggregate_minutes, trend_features  # noqa: E402

START = date(2026, 1, 1)
END = date(2026, 5, 31)
CACHE = ROOT / "data" / "backtest" / "binance_um_1m_pre"
RESULT = ROOT / "data" / "backtest" / "trend_prewindow.json"
LOCKED = {
    "name": "t102",
    "interval": 3600,
    "lookback": 16,
    "mode": 0,
    "threshold": 0.008,
    "atr_mult": 3.0,
    "exit_mode": 1,
    "long_only": 1,
    "majors_only": 0,
    "risk_frac": 0.02,
    "cooldown": 1,
}


def log(message):
    print(message, flush=True)


def download(symbol):
    path = CACHE / f"{symbol}.npz"
    start_ts = int(datetime(START.year, START.month, START.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(END.year, END.month, END.day, tzinfo=timezone.utc).timestamp()) + 86340
    if path.exists():
        cached = np.load(path)["data"]
        if len(cached) > 1000 and cached[0, 0] <= start_ts + 2 * 86400 and cached[-1, 0] >= end_ts - 2 * 86400:
            log(f"cache {symbol} {len(cached)} bars")
            return cached
    days = []
    day = START
    while day <= END:
        days.append(day)
        day += timedelta(days=1)
    rows = []
    misses = 0
    with ThreadPoolExecutor(8) as pool:
        for day_rows, missing in pool.map(lambda item: fetch_day(*item), [(symbol, day) for day in days]):
            rows.extend(day_rows)
            misses += int(missing)
    if not rows:
        log(f"missing {symbol}")
        return None
    array = np.array(rows, dtype=np.float64)
    _, index = np.unique(array[:, 0], return_index=True)
    array = array[np.sort(index)]
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, data=array)
    log(f"saved {symbol} {len(array)} bars misses {misses} "
        f"from {datetime.fromtimestamp(array[0, 0], timezone.utc).date()} "
        f"to {datetime.fromtimestamp(array[-1, 0], timezone.utc).date()}")
    return array


def window(year, month, day, year2, month2, day2):
    return (datetime(year, month, day, tzinfo=timezone.utc),
            datetime(year2, month2, day2, tzinfo=timezone.utc))


def main():
    arrays = {}
    for pair, symbol, _cap, _major in PAIRS:
        arrays[pair] = download(symbol)
    markets = {}
    usable = []
    for pair, _symbol, _cap, _major in PAIRS:
        array = arrays[pair]
        if array is None or len(array) < 20 * 1440:
            log(f"exclude {pair}")
            continue
        usable.append(pair)
        ts, op, hi, lo, cl = aggregate_minutes(array[:, 0], array[:, 1], array[:, 2], array[:, 3], array[:, 4], 3600)
        features = trend_features(op, hi, lo, cl, LOCKED["lookback"], 14)
        markets[(pair, 3600, 16)] = (ts, op, hi, lo, cl, features)
        log(f"{pair} hourly bars {len(ts)} close {cl[np.isfinite(cl)][0]:.4f} -> {cl[np.isfinite(cl)][-1]:.4f}")

    ranges = [
        ("2026-01-01/2026-05-31", *window(2026, 1, 1, 2026, 6, 1)),
        ("2026-01", *window(2026, 1, 1, 2026, 2, 1)),
        ("2026-02", *window(2026, 2, 1, 2026, 3, 1)),
        ("2026-03", *window(2026, 3, 1, 2026, 4, 1)),
        ("2026-04", *window(2026, 4, 1, 2026, 5, 1)),
        ("2026-05", *window(2026, 5, 1, 2026, 6, 1)),
    ]
    # Evaluate only pairs that downloaded. search_trend.evaluate skips caps but still
    # indexes every PAIRS entry, so drop missing symbols from a local copy by zeroing later.
    report = {"window": "2026-01-01/2026-05-31", "params": LOCKED, "usable_pairs": usable, "slices": []}
    original = list(PAIRS)
    kept = [item for item in original if item[0] in usable]
    import search_trend
    search_trend.PAIRS = kept
    try:
        for name, start, stop in ranges:
            row = evaluate(LOCKED, markets, start, stop)
            report["slices"].append({
                "name": name,
                "net": row["net"],
                "trades": row["trades"],
                "wins": row["wins"],
                "pf": row["pf"],
                "max_dd": row["max_dd"],
                "fees": row["fees"],
                "support": row["support"],
                "pair_nets": row["pair_nets"],
                "pair_trades": row["pair_trades"],
            })
            log(f"{name} net {row['net']:.2f} trades {row['trades']} pf {row['pf']:.2f} "
                f"dd {row['max_dd']:.2f} fees {row['fees']:.2f} support {row['support']}")
    finally:
        search_trend.PAIRS = original
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, indent=2))
    log(f"wrote {RESULT}")


if __name__ == "__main__":
    main()
