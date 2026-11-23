"""Pick one trend configuration on data before 2026-08-31, then score it once after that.

Fit is 2026-06-01 to 2026-07-16. Select is 2026-07-16 to 2026-08-31.
The holdout, 2026-08-31 to 2026-09-28, is not an input to the choice.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from trend_sim import aggregate_minutes, simulate_trend, trend_features  # noqa: E402

FIT_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
SELECT_START = datetime(2026, 7, 16, tzinfo=timezone.utc)
HOLDOUT_START = datetime(2026, 8, 31, tzinfo=timezone.utc)
END = datetime(2026, 9, 28, tzinfo=timezone.utc)
TAKER = 0.0005
SLIP = 0.0002
SEED = 20260929
N_RANDOM = 280
CACHE = ROOT / "data" / "backtest" / "binance_um_1m"
RESULT = ROOT / "data" / "backtest" / "trend_search.json"
PAIRS = [
    ("BTC-USDT", "BTCUSDT", 80.0, True),
    ("ETH-USDT", "ETHUSDT", 80.0, True),
    ("SOL-USDT", "SOLUSDT", 50.0, False),
    ("XRP-USDT", "XRPUSDT", 50.0, False),
    ("DOGE-USDT", "DOGEUSDT", 50.0, False),
    ("SUI-USDT", "SUIUSDT", 50.0, False),
    ("ADA-USDT", "ADAUSDT", 50.0, False),
    ("ZEC-USDT", "ZECUSDT", 50.0, False),
    ("SNDK-USDT", "SNDKUSDT", 50.0, False),
]


def log(message):
    print(message, flush=True)


def load_pair(symbol, interval):
    array = np.load(CACHE / f"{symbol}.npz")["data"]
    ts, op, hi, lo, cl = aggregate_minutes(array[:, 0], array[:, 1], array[:, 2], array[:, 3], array[:, 4], interval)
    return ts, op, hi, lo, cl


def index_at(ts, moment):
    return int(np.searchsorted(ts, int(moment.timestamp()), side="left"))


def portfolio_dd(curves):
    total = np.zeros(len(curves[0]), dtype=np.float64)
    for curve in curves:
        total += pd.Series(curve).ffill().fillna(0.0).to_numpy(dtype=np.float64)
    peak = np.maximum.accumulate(total)
    return float(np.max(peak - total))


def evaluate(params, markets, t0, t1):
    total = np.zeros(12, dtype=np.float64)
    pair_nets = {}
    pair_trades = {}
    curves = []
    traded = []
    for pair, _symbol, cap, major in PAIRS:
        use_cap = cap if (not params["majors_only"] or major) else 0.0
        if use_cap <= 0:
            continue
        traded.append(pair)
        ts, op, hi, lo, cl, features = markets[(pair, params["interval"], params["lookback"])]
        atr, momentum, prior_high, prior_low = features
        start = index_at(ts, t0)
        end = index_at(ts, t1)
        equity = np.full(max(end - start, 1), np.nan)
        result = simulate_trend(
            op, hi, lo, cl, atr, momentum, prior_high, prior_low,
            int(params["mode"]), float(params["threshold"]), float(params["atr_mult"]),
            int(params["exit_mode"]), int(params["long_only"]), float(params["risk_frac"]), float(use_cap),
            int(params["cooldown"]), 0.004, 0.06, TAKER, SLIP, start, end, equity,
        )
        total += result
        pair_nets[pair] = float(result[0])
        pair_trades[pair] = int(result[1])
        curves.append(equity)
    length = max(len(curve) for curve in curves)
    aligned = []
    for curve in curves:
        if len(curve) < length:
            padded = np.full(length, np.nan)
            padded[:len(curve)] = curve
            aligned.append(padded)
        else:
            aligned.append(curve[:length])
    gross_loss = float(total[4])
    gross_win = float(total[3])
    trades = int(total[1])
    support = sum(1 for pair in traded if pair_nets[pair] >= 0 and pair_trades[pair] >= 2)
    return {
        "name": params["name"],
        "net": float(total[0]),
        "trades": trades,
        "wins": int(total[2]),
        "pf": (gross_win / gross_loss) if gross_loss > 1e-9 else (1e9 if gross_win > 0 else 0.0),
        "max_dd": portfolio_dd(aligned),
        "fees": float(total[6]),
        "hold_bars": (float(total[7]) / trades) if trades else 0.0,
        "n_stop": int(total[9]),
        "n_flip": int(total[10]),
        "n_eod": int(total[11]),
        "support": support,
        "traded": len(traded),
        "pair_nets": pair_nets,
        "pair_trades": pair_trades,
        "params": params,
    }


def passes_select(row):
    needed = max(2, math.ceil(0.5 * row["traded"]))
    return (row["net"] > 0 and row["pf"] >= 1.15 and row["trades"] >= 20
            and row["max_dd"] <= 0.12 * notionals(row["params"]) and row["support"] >= needed)


def passes_holdout(row):
    return (row["net"] > 0 and row["pf"] >= 1.1 and row["trades"] >= 12
            and row["max_dd"] <= 0.20 * notionals(row["params"])
            and sum(1 for value in row["pair_nets"].values() if value > 0) >= 2)


def notionals(params):
    return sum(cap for _pair, _symbol, cap, major in PAIRS if not params["majors_only"] or major)


def sample(rng, index):
    interval = int(rng.choice([900, 3600]))
    lookback = int(rng.choice([8, 12, 16, 24, 36, 48, 72]))
    return {
        "name": f"t{index}",
        "interval": interval,
        "lookback": lookback,
        "mode": int(rng.choice([0, 1])),
        "threshold": float(rng.choice([0.0, 0.003, 0.008])),
        "atr_mult": float(rng.choice([1.5, 2.0, 3.0])),
        "exit_mode": int(rng.choice([1, 2])),
        "long_only": int(rng.choice([0, 1])),
        "majors_only": int(rng.choice([0, 1])),
        "risk_frac": float(rng.choice([0.01, 0.02, 0.03])),
        "cooldown": 1,
    }


def named():
    base = {
        "interval": 3600, "lookback": 24, "mode": 0, "threshold": 0.0, "atr_mult": 2.0,
        "exit_mode": 1, "long_only": 1, "majors_only": 1, "risk_frac": 0.02, "cooldown": 1,
    }
    specs = [
        ("hourly_trail_long_majors", {}),
        ("hourly_flip_all", {"exit_mode": 2, "long_only": 0, "majors_only": 0, "lookback": 24}),
        ("donchian_trail", {"mode": 1, "lookback": 24, "long_only": 0, "majors_only": 0}),
        ("fifteen_minute_trail", {"interval": 900, "lookback": 32, "long_only": 1, "majors_only": 0}),
    ]
    configs = []
    for name, overrides in specs:
        params = dict(base)
        params.update(overrides)
        params["name"] = name
        configs.append(params)
    return configs


def fmt(row):
    return (f"{row['name']:<24} net {row['net']:8.2f}  trades {row['trades']:4d}  "
            f"pf {row['pf']:5.2f}  dd {row['max_dd']:7.2f}  fees {row['fees']:7.2f}  "
            f"hold {row['hold_bars']:5.1f} bars  stop/flip/eod {row['n_stop']}/{row['n_flip']}/{row['n_eod']}  "
            f"support {row['support']}/{row['traded']}")


def main():
    configs = named()
    rng = np.random.default_rng(SEED)
    seen = set()
    while len(configs) < N_RANDOM + len(named()):
        params = sample(rng, len(configs))
        identity = tuple(sorted((key, params[key]) for key in params if key != "name"))
        if identity not in seen:
            seen.add(identity)
            configs.append(params)
    keys = {(params["interval"], params["lookback"]) for params in configs}
    markets = {}
    for pair, symbol, _cap, _major in PAIRS:
        loaded = {}
        for interval, lookback in keys:
            if interval not in loaded:
                loaded[interval] = load_pair(symbol, interval)
            ts, op, hi, lo, cl = loaded[interval]
            features = trend_features(op, hi, lo, cl, lookback, 14)
            markets[(pair, interval, lookback)] = (ts, op, hi, lo, cl, features)
    log(f"trend search configs {len(configs)}")

    def run(params, start, stop):
        return evaluate(params, markets, start, stop)

    fit_rows = [run(params, FIT_START, SELECT_START) for params in configs]
    fit_by = {row["name"]: row for row in fit_rows}
    for name in ("hourly_trail_long_majors", "hourly_flip_all", "donchian_trail", "fifteen_minute_trail"):
        log("FIT " + fmt(fit_by[name]))
    eligible_names = [row["name"] for row in fit_rows if row["net"] > 0 and row["trades"] >= 15 and row["pf"] >= 1.05]
    shortlist = sorted(eligible_names, key=lambda name: fit_by[name]["net"], reverse=True)[:40]
    log(f"fit-positive {len(eligible_names)}; select shortlist {len(shortlist)}")
    if not shortlist:
        log("SELECTION LOCKED none")
        payload = {"adopt": False, "reason": "no fit-positive configuration"}
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(payload, indent=2))
        return
    chosen = [params for params in configs if params["name"] in set(shortlist)]
    select_rows = [run(params, SELECT_START, HOLDOUT_START) for params in chosen]
    select_rows.sort(key=lambda row: row["net"], reverse=True)
    log("SELECT")
    for row in select_rows[:12]:
        log("  " + fmt(row))
    winner = None
    for row in select_rows:
        fit = fit_by[row["name"]]
        if fit["net"] > 0 and passes_select(row):
            winner = row["params"]
            break
    if winner is None:
        log("SELECTION LOCKED none")
        payload = {"adopt": False, "reason": "no configuration passed the select gate",
                   "select_top": select_rows[0]["name"], "select_net": select_rows[0]["net"]}
        RESULT.write_text(json.dumps(payload, indent=2))
        return
    log(f"SELECTION LOCKED {winner['name']}")
    holdout = run(winner, HOLDOUT_START, END)
    log("HOLDOUT " + fmt(holdout))
    adopt = passes_holdout(holdout)
    payload = {"adopt": adopt, "winner_name": winner["name"], "winner": holdout, "params": winner,
               "fit_net": fit_by[winner["name"]]["net"]}
    RESULT.write_text(json.dumps(payload, indent=2))
    log(f"adopt {adopt}")
    log("params " + json.dumps(winner))


if __name__ == "__main__":
    main()
