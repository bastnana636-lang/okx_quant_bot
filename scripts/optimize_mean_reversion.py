"""Search mean-reversion parameter ratios and score them out of sample.

Protocol, fixed before any holdout result is computed:
- Fit:     2026-06-01 <= t < 2026-08-03
- Select:  2026-08-03 <= t < 2026-08-31
- Holdout: 2026-08-31 <= t < 2026-09-28  (scored only after the winner is locked)

OKX market data is not reachable from this machine. Candles are Binance USD-M
perpetual 1-minute bars. Fees are the OKX/Binance VIP0 schedule: maker 2 bps,
taker 5 bps, plus 2 bps of taker slippage. Funding is not modeled.

A config is adopted only when it beats the current runtime config on the
holdout window by a pre-set margin and is itself profitable there.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reversion_sim import (  # noqa: E402
    DEFAULT_HALF_SPREAD,
    DEFAULT_OFFSET,
    MAKER_FEE,
    TAKER_FEE,
    TAKER_SLIPPAGE,
    prepare_market,
    rolling_mean,
    shift_mean,
    simulate,
)
from controllers.market_making.mean_reversion import MeanReversionSettings, calculate_snapshot  # noqa: E402

FIT_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
SELECT_START = datetime(2026, 8, 3, tzinfo=timezone.utc)
HOLDOUT_START = datetime(2026, 8, 31, tzinfo=timezone.utc)
END = datetime(2026, 9, 28, tzinfo=timezone.utc)
DATA_START = date(2026, 6, 1)
DATA_END = date(2026, 9, 27)
N_RANDOM = 240
SEED = 20260928
CACHE = ROOT / "data" / "backtest" / "binance_um_1m"
RESULT_PATH = ROOT / "data" / "backtest" / "mean_reversion_search.json"
INTERVALS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
BINANCE_SYMBOL = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "SOL-USDT": "SOLUSDT",
    "XRP-USDT": "XRPUSDT",
    "DOGE-USDT": "DOGEUSDT",
    "SUI-USDT": "SUIUSDT",
    "ADA-USDT": "ADAUSDT",
    "ZEC-USDT": "ZECUSDT",
    "SNDK-USDT": "SNDKUSDT",
    "OKB-USDT": "OKBUSDT",
}
SHARED_KEYS = (
    "candles_interval", "mean_window", "entry_z_score", "max_entry_z_score", "exit_z_score",
    "min_std_pct", "atr_length", "atr_stop_multiplier", "min_stop_loss", "max_stop_loss",
    "round_trip_cost_pct", "min_net_edge_pct", "min_reward_risk", "take_profit_quote",
    "cooldown_time", "stop_loss_cooldown_time", "time_limit", "quote_offset_pct",
)

PREP = {}
BOOK = []
HOURS = 24 * 140
ORIGIN = int(FIT_START.timestamp())


def log(message):
    print(message, flush=True)


def http_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
        return response.read()


def parse_kline_zip(blob):
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("utf-8-sig")
    rows = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        parts = line.split(",")
        rows.append((int(parts[0]) // 1000, float(parts[1]), float(parts[2]), float(parts[3]),
                     float(parts[4]), float(parts[7])))
    return rows


def fetch_day(binance_symbol, day):
    url = (f"https://data.binance.vision/data/futures/um/daily/klines/{binance_symbol}/1m/"
           f"{binance_symbol}-1m-{day.isoformat()}.zip")
    for attempt in range(4):
        try:
            return parse_kline_zip(http_bytes(url)), False
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return [], True
            if attempt == 3:
                raise
        except Exception:
            if attempt == 3:
                raise
        time.sleep(1.5 * (attempt + 1))
    return [], True


def download_symbol(binance_symbol):
    path = CACHE / f"{binance_symbol}.npz"
    start_ts = int(datetime(DATA_START.year, DATA_START.month, DATA_START.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(DATA_END.year, DATA_END.month, DATA_END.day, tzinfo=timezone.utc).timestamp()) + 86340
    if path.exists():
        cached = np.load(path)["data"]
        if len(cached) > 1000 and cached[0, 0] <= start_ts + 86400 and cached[-1, 0] >= end_ts - 86400:
            log(f"cache {binance_symbol} {len(cached)} bars")
            return cached
    days = []
    day = DATA_START
    while day <= DATA_END:
        days.append(day)
        day += timedelta(days=1)
    rows = []
    misses = 0
    with ThreadPoolExecutor(8) as pool:
        for day_rows, missing in pool.map(lambda item: fetch_day(*item), [(binance_symbol, day) for day in days]):
            rows.extend(day_rows)
            misses += int(missing)
    if not rows:
        log(f"missing {binance_symbol}")
        return None
    array = np.array(rows, dtype=np.float64)
    _, index = np.unique(array[:, 0], return_index=True)
    array = array[np.sort(index)]
    if np.median(np.diff(array[:, 0])) != 60:
        log(f"reject {binance_symbol}: median spacing is not 60s")
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, data=array)
    log(f"saved {binance_symbol} {len(array)} bars misses {misses}")
    return array


def load_yaml_configs():
    script = yaml.safe_load((ROOT / "conf/scripts/conf_okx_multi.yml").read_text())
    runtime = {}
    template = {}
    for name in script["controllers_config"]:
        live = yaml.safe_load((ROOT / "conf/controllers" / name).read_text())
        stored = yaml.safe_load((ROOT / "strategy_configs/okx_mean_reversion" / name).read_text())
        runtime[live["trading_pair"]] = live
        template[stored["trading_pair"]] = stored
    reference = runtime["BTC-USDT"]
    for pair, config in runtime.items():
        for key in SHARED_KEYS:
            if config.get(key) != reference.get(key):
                raise SystemExit(f"Active configs do not share {key}: BTC-USDT={reference.get(key)} {pair}={config.get(key)}")
    return runtime, template


def params_from_config(name, config, tp_abs, sl_mult):
    return {
        "name": name,
        "interval": INTERVALS[str(config["candles_interval"])],
        "window": int(config["mean_window"]),
        "entry_z": float(config["entry_z_score"]),
        "max_z": float(config["max_entry_z_score"]),
        "exit_z": float(config["exit_z_score"]),
        "min_std": float(config["min_std_pct"]),
        "atr_mult": float(config["atr_stop_multiplier"]),
        "min_stop": float(config["min_stop_loss"]),
        "max_stop": float(config["max_stop_loss"]),
        "cost": float(config["round_trip_cost_pct"]),
        "min_edge": float(config["min_net_edge_pct"]),
        "min_rr": float(config["min_reward_risk"]),
        "tp_pct": None,
        "tp_abs": float(tp_abs),
        "time_limit": int(config["time_limit"]),
        "cooldown": int(config["cooldown_time"]),
        "sl_mult": float(sl_mult),
        "trend_k": 0,
        "trend_max": 0.0,
        "short_z_add": 0.0,
    }


def r4(value):
    return round(float(value), 4)


def sample_params(rng, index):
    interval = int(rng.choice([300, 900]))
    window = int(rng.choice([24, 36, 48, 72, 96, 120] if interval == 300 else [16, 24, 32, 48, 64]))
    entry = float(rng.choice([1.5, 1.8, 2.0, 2.2, 2.5, 2.8]))
    max_z = entry + float(rng.choice([0.8, 1.0, 1.2, 1.6, 2.0]))
    exit_z = entry * float(rng.choice([0.0, 0.05, 0.1, 0.15, 0.25]))
    min_stop = float(rng.choice([0.004, 0.005, 0.006, 0.008, 0.01, 0.012]))
    max_stop = min(0.05, min_stop * float(rng.choice([2.0, 2.5, 3.0, 4.0])))
    if max_stop <= min_stop:
        max_stop = min_stop * 2
    short_add = float(rng.choice([0.0, 0.0, 0.3, 0.6]))
    if entry + short_add >= max_z - 0.1:
        short_add = 0.0
    trend_k = int(rng.choice([0, 0, 6, 12, 24]))
    trend_max = float(rng.choice([0.5, 1.0, 1.5])) if trend_k else 0.0
    return {
        "name": f"r{index}",
        "interval": interval,
        "window": window,
        "entry_z": r4(entry),
        "max_z": r4(max_z),
        "exit_z": r4(exit_z),
        "min_std": float(rng.choice([0.0006, 0.001, 0.0015, 0.002])),
        "atr_mult": float(rng.choice([1.0, 1.25, 1.5, 2.0, 2.5, 3.0])),
        "min_stop": r4(min_stop),
        "max_stop": r4(max_stop),
        "cost": float(rng.choice([0.0008, 0.0012, 0.0016])),
        "min_edge": float(rng.choice([0.001, 0.0018, 0.0025, 0.004])),
        "min_rr": float(rng.choice([0.5, 0.8, 1.0, 1.2, 1.5, 2.0])),
        "tp_pct": float(rng.choice([0.006, 0.01, 0.015, 0.02, 0.03, 0.05])),
        "tp_abs": None,
        "time_limit": int(rng.choice([1800, 3600, 7200, 14400, 21600, 28800])),
        "cooldown": int(rng.choice([60, 180, 300, 600])),
        "sl_mult": float(rng.choice([1.0, 2.0, 3.0])),
        "trend_k": trend_k,
        "trend_max": trend_max,
        "short_z_add": r4(short_add),
    }


def hypotheses(baseline):
    """Pre-set alternatives. These are not tuned on the holdout window."""
    patient = dict(baseline)
    patient.update(name="patient_rr", exit_z=0.05, min_rr=1.2, time_limit=14400, tp_pct=0.02, tp_abs=None,
                   cooldown=300, sl_mult=2.0)
    trend = dict(baseline)
    trend.update(name="trend_filter", trend_k=12, trend_max=1.0, tp_pct=1.2 / 80.0, tp_abs=None)
    slow = dict(baseline)
    slow.update(name="slower_15m", interval=900, window=32, entry_z=1.8, max_z=3.2, exit_z=0.1,
                time_limit=21600, min_rr=1.0, tp_pct=0.015, tp_abs=None)
    wide = dict(baseline)
    wide.update(name="wide_stop", min_stop=0.008, max_stop=0.03, atr_mult=2.0, time_limit=21600,
                exit_z=0.0, entry_z=2.2, max_z=3.8, min_rr=1.0, tp_pct=0.012, tp_abs=None)
    return [patient, trend, slow, wide]


_SLOW_CACHE = {}


def _slow_mean(prepared, window):
    if window <= 1:
        return np.full(prepared.mean.shape, np.nan, dtype=np.float64)
    key = (id(prepared.signal_close), window)
    cached = _SLOW_CACHE.get(key)
    if cached is None:
        cached = np.ascontiguousarray(rolling_mean(prepared.signal_close, window), dtype=np.float64)
        _SLOW_CACHE[key] = cached
    return cached


def quote_target(params, cap):
    if params["tp_pct"] is not None:
        return float(params["tp_pct"]) * cap
    return float(params["tp_abs"])


def portfolio_drawdown(curves):
    total = np.zeros(HOURS, dtype=np.float64)
    for curve in curves:
        series = pd.Series(curve).ffill().fillna(0.0).to_numpy(dtype=np.float64)
        total += series
    peak = np.maximum.accumulate(total)
    return float(np.max(peak - total))


def evaluate_params(params, t0, t1):
    total = np.zeros(12, dtype=np.float64)
    pair_nets = {}
    pair_trades = {}
    curves = []
    for pair, cap, _symbol in BOOK:
        prepared = PREP[(pair, params["interval"], params["window"])]
        start = int(np.searchsorted(prepared.ts, t0, side="left"))
        end = int(np.searchsorted(prepared.ts, t1, side="left"))
        equity = np.full(HOURS, np.nan, dtype=np.float64)
        result = simulate(
            prepared.ts, prepared.open, prepared.high, prepared.low, prepared.close, prepared.quote_volume,
            prepared.signal_index, prepared.signal_close, prepared.mean, prepared.std, prepared.atr,
            shift_mean(prepared.mean, int(params["trend_k"])),
            float(params["entry_z"]), float(params["max_z"]), float(params["exit_z"]), float(params["min_std"]),
            float(params["atr_mult"]), float(params["min_stop"]), float(params["max_stop"]),
            float(params["cost"]), float(params["min_edge"]), float(params["min_rr"]),
            quote_target(params, cap), int(params["time_limit"]), int(params["cooldown"]), float(params["sl_mult"]),
            int(params["trend_k"]), float(params["trend_max"]), float(params["short_z_add"]), float(cap),
            float(params.get("offset", DEFAULT_OFFSET)), DEFAULT_HALF_SPREAD, MAKER_FEE, TAKER_FEE, TAKER_SLIPPAGE,
            _slow_mean(prepared, int(params.get("slow_window", 0))), int(params.get("entry_mode", 0)),
            start, end, ORIGIN, equity,
        )
        total += result
        pair_nets[pair] = float(result[0])
        pair_trades[pair] = int(result[1])
        curves.append(equity)
    gross_loss = float(total[4])
    gross_win = float(total[3])
    trades = int(total[1])
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (1e9 if gross_win > 0 else 0.0)
    support = sum(1 for pair, _cap, _symbol in BOOK
                  if pair_nets[pair] >= 0 and pair_trades[pair] >= 3)
    drawdown = portfolio_drawdown(curves)
    if not math.isfinite(drawdown) or drawdown < 0 or not math.isfinite(float(total[0])):
        raise RuntimeError(f"non-finite result for {params['name']}")
    return {
        "name": params["name"],
        "net": float(total[0]),
        "trades": trades,
        "wins": int(total[2]),
        "pf": pf,
        "max_dd": drawdown,
        "fees": float(total[6]),
        "hold_hours": (float(total[7]) / trades / 3600.0) if trades else 0.0,
        "n_tp": int(total[8]),
        "n_stop": int(total[9]),
        "n_time": int(total[10]),
        "n_eod": int(total[11]),
        "support": support,
        "pair_nets": pair_nets,
        "pair_trades": pair_trades,
        "params": params,
    }


def primary_ok(row, book_notional):
    needed = max(4, math.ceil(0.6 * len(BOOK)))
    return (row["trades"] >= 40 and row["pf"] >= 1.08 and row["net"] > 0
            and row["max_dd"] <= 0.15 * book_notional and row["support"] >= needed)


def fallback_ok(row, book_notional):
    needed = max(3, len(BOOK) // 2)
    return (row["trades"] >= 40 and row["pf"] >= 1.0 and row["net"] > 0
            and row["max_dd"] <= 0.25 * book_notional and row["support"] >= needed)


def select_ok(row, book_notional):
    return row["net"] > 0 and row["pf"] >= 1.0 and row["trades"] >= 15 and row["max_dd"] <= 0.15 * book_notional


def usable_history(array):
    if array is None or len(array) < 21 * 1440:
        return False
    earliest = SELECT_START.timestamp() - 21 * 86400
    latest = HOLDOUT_START.timestamp() + 20 * 86400
    return array[0, 0] <= earliest and array[-1, 0] >= latest


def assert_live_parity(array):
    ts = array[:, 0].astype(np.int64)
    prepared = prepare_market(ts, array[:, 1], array[:, 2], array[:, 3], array[:, 4], array[:, 5], 300, 48)
    bucket = (ts // 300) * 300
    by_stamp = {}
    for stamp in np.unique(bucket):
        mask = bucket == int(stamp)
        if int(mask.sum()) != 5:
            continue
        by_stamp[int(stamp)] = (int(stamp), float(array[mask, 1][0]), float(array[mask, 2].max()),
                                float(array[mask, 3].min()), float(array[mask, 4][-1]))
    first = int((ts[0] // 300) * 300)
    last = int((ts[-1] // 300) * 300)
    full = np.arange(first, last + 300, 300, dtype=np.int64)
    checked = 0
    for index in range(len(prepared.ts) - 1, 0, -5000):
        signal = int(prepared.signal_index[index])
        if signal < 48 or signal >= len(full):
            continue
        tail = []
        cursor = signal
        while cursor >= 0 and len(tail) < 48:
            row = by_stamp.get(int(full[cursor]))
            if row is None or (tail and row[0] != tail[-1][0] - 300):
                break
            tail.append(row)
            cursor -= 1
        if len(tail) < 48:
            continue
        tail.reverse()
        frame = pd.DataFrame(tail, columns=["timestamp", "open", "high", "low", "close"])
        now = float(prepared.ts[index] + 60)
        try:
            snapshot = calculate_snapshot(frame, now, Decimal(str(prepared.close[index])), MeanReversionSettings(
                entry_z_score="2", max_entry_z_score="3.5", exit_z_score="0.15"))
        except ValueError:
            continue
        z_live = (float(prepared.close[index]) - prepared.mean[signal]) / prepared.std[signal]
        if abs(float(snapshot.z_score) - z_live) > 1e-6 or abs(float(snapshot.mean) - prepared.mean[signal]) > 1e-6:
            raise SystemExit("Backtest features diverged from calculate_snapshot")
        checked += 1
        if checked == 3:
            break
    if checked < 3:
        raise SystemExit("Could not verify the backtest against the live signal")
    log(f"live signal parity ok on {checked} BTC timestamps")


def fmt(row):
    return (f"{row['name']:<16} net {row['net']:8.2f}  trades {row['trades']:4d}  "
            f"win {row['wins'] / row['trades'] * 100 if row['trades'] else 0:5.1f}%  "
            f"pf {row['pf']:5.2f}  dd {row['max_dd']:7.2f}  fees {row['fees']:7.2f}  "
            f"hold {row['hold_hours']:5.2f}h  tp/sl/time/eod "
            f"{row['n_tp']}/{row['n_stop']}/{row['n_time']}/{row['n_eod']}  support {row['support']}")


def ratios(params, book_notional):
    lookback = params["interval"] * params["window"]
    tp_pct = params["tp_pct"]
    if tp_pct is None and params["tp_abs"] is not None and book_notional:
        # Uniform quote target divided by the BTC runtime cap, which is the scale used in the search.
        btc_cap = next(cap for pair, cap, _symbol in BOOK if pair == "BTC-USDT")
        tp_pct = params["tp_abs"] / btc_cap
    return {
        "entry_z": params["entry_z"],
        "max_entry_z": params["max_z"],
        "exit_z": params["exit_z"],
        "exit_over_entry": params["exit_z"] / params["entry_z"],
        "max_over_entry": params["max_z"] / params["entry_z"],
        "cash_tp_over_notional": tp_pct,
        "stop_floor": params["min_stop"],
        "atr_stop_multiple": params["atr_mult"],
        "stop_cap_over_floor": params["max_stop"] / params["min_stop"],
        "min_reward_risk": params["min_rr"],
        "min_edge": params["min_edge"],
        "round_trip_cost_gate": params["cost"],
        "time_limit_over_lookback": params["time_limit"] / lookback,
        "cooldown_over_time_limit": params["cooldown"] / params["time_limit"],
        "stop_cooldown_multiple": params["sl_mult"],
        "lookback_hours": lookback / 3600,
        "interval": "15m" if params["interval"] == 900 else "5m",
        "mean_window": params["window"],
        "trend_lookback_bars": params["trend_k"],
        "trend_drift_cap": params["trend_max"],
        "short_entry_z_add": params["short_z_add"],
        "quote_offset": params.get("offset", DEFAULT_OFFSET),
        "entry_mode": params.get("entry_mode", 0),
        "slow_window_bars": params.get("slow_window", 0),
    }


def yaml_num(value):
    text = f"{float(value):.8f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def yaml_money(value):
    quantized = Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return yaml_num(quantized)


def replace_keys(text, updates):
    lines = text.splitlines()
    seen = set()
    for index, line in enumerate(lines):
        key = line.split(":", 1)[0]
        if key in updates:
            lines[index] = f"{key}: {updates[key]}"
            seen.add(key)
    missing = [key for key in updates if key not in seen]
    if missing:
        raise SystemExit(f"missing yaml keys: {missing}")
    return "\n".join(lines) + "\n"


def apply_winner(payload):
    if not payload.get("adopt"):
        raise SystemExit("Search did not adopt a new configuration; yaml files were left unchanged")
    params = payload["winner"]["params"]
    if params["trend_k"] or params["short_z_add"]:
        fields = MeanReversionSettings.model_fields
        if params["trend_k"] and "trend_lookback" not in fields:
            raise SystemExit("Winner uses a trend filter that is not in the live signal yet")
        if params["short_z_add"] and "short_entry_z_add" not in fields:
            raise SystemExit("Winner uses a short-side z add that is not in the live signal yet")
    runtime, template = load_yaml_configs()
    interval_name = "15m" if params["interval"] == 900 else "5m"
    for pair, live in runtime.items():
        for folder, config in (
            (ROOT / "conf/controllers", live),
            (ROOT / "strategy_configs/okx_mean_reversion", template[pair]),
        ):
            notional = float(config["total_amount_quote"])
            runtime_notional = float(live["total_amount_quote"])
            if params["tp_pct"] is not None:
                take_profit = params["tp_pct"] * notional
            else:
                take_profit = params["tp_abs"] * (notional / runtime_notional)
            fixed = take_profit * 1.25
            path = folder / f"conf_okx_pmm_{pair.split('-')[0].lower()}.yml"
            updates = {
                "candles_interval": interval_name,
                "mean_window": str(int(params["window"])),
                "entry_z_score": yaml_num(params["entry_z"]),
                "max_entry_z_score": yaml_num(params["max_z"]),
                "exit_z_score": yaml_num(params["exit_z"]),
                "min_std_pct": yaml_num(params["min_std"]),
                "atr_stop_multiplier": yaml_num(params["atr_mult"]),
                "min_stop_loss": yaml_num(params["min_stop"]),
                "max_stop_loss": yaml_num(params["max_stop"]),
                "round_trip_cost_pct": yaml_num(params["cost"]),
                "min_net_edge_pct": yaml_num(params["min_edge"]),
                "min_reward_risk": yaml_num(params["min_rr"]),
                "take_profit_quote": yaml_money(take_profit),
                "fixed_unrealized_tp_quote": yaml_money(fixed) if fixed > 1 else "null",
                "cooldown_time": str(int(params["cooldown"])),
                "stop_loss_cooldown_time": str(int(round(params["cooldown"] * params["sl_mult"]))),
                "time_limit": str(int(params["time_limit"])),
            }
            if params["trend_k"]:
                updates["trend_lookback"] = str(int(params["trend_k"]))
                updates["max_adverse_drift"] = yaml_num(params["trend_max"])
            if params["short_z_add"]:
                updates["short_entry_z_add"] = yaml_num(params["short_z_add"])
            path.write_text(replace_keys(path.read_text(), updates))
            log(f"updated {path.relative_to(ROOT)} tp {updates['take_profit_quote']}")


ALIGNED_PATH = ROOT / "data" / "backtest" / "aligned_search.json"
ALIGNED_RANDOM = 160
ALIGNED_SEED = 20260929


def _aligned_shell(name, **overrides):
    params = {
        "name": name, "interval": 300, "window": 48, "entry_z": 2.0, "max_z": 3.2, "exit_z": 0.0,
        "min_std": 0.001, "atr_mult": 2.0, "min_stop": 0.008, "max_stop": 0.03,
        "cost": 0.0012, "min_edge": 0.0018, "min_rr": 1.2, "tp_pct": 0.02, "tp_abs": None,
        "time_limit": 28800, "cooldown": 600, "sl_mult": 2.0, "trend_k": 0, "trend_max": 0.0,
        "short_z_add": 0.0, "offset": 0.001, "entry_mode": 1, "slow_window": 288,
    }
    params.update(overrides)
    return params


def _sample_aligned(rng, index):
    mode_roll = float(rng.random())
    if mode_roll < 0.15:
        entry_mode, slow_window = 0, 0
    elif mode_roll < 0.35:
        entry_mode, slow_window = 3, int(rng.choice([96, 144, 288]))
    else:
        entry_mode, slow_window = 1, int(rng.choice([96, 144, 288]))
    entry = float(rng.choice([1.8, 2.0, 2.2]))
    band = float(rng.choice([1.0, 1.4]))
    min_stop = float(rng.choice([0.006, 0.01]))
    return {
        "name": f"a{index}", "interval": 300, "window": int(rng.choice([36, 48, 72])),
        "entry_z": entry, "max_z": r4(entry + band), "exit_z": r4(entry * float(rng.choice([0.0, 0.1]))),
        "min_std": 0.001, "atr_mult": float(rng.choice([1.5, 2.0, 2.5])),
        "min_stop": min_stop, "max_stop": r4(min(0.04, min_stop * 3)),
        "cost": 0.0012, "min_edge": 0.0018, "min_rr": float(rng.choice([1.0, 1.5])),
        "tp_pct": float(rng.choice([0.015, 0.025, 0.04])), "tp_abs": None,
        "time_limit": int(rng.choice([14400, 28800, 43200])),
        "cooldown": int(rng.choice([300, 900])), "sl_mult": 2.0,
        "trend_k": 0, "trend_max": 0.0, "short_z_add": 0.0,
        "offset": float(rng.choice([0.0005, 0.001, 0.0015, 0.002])),
        "entry_mode": entry_mode, "slow_window": slow_window,
    }


def _aligned_primary(row, book_notional):
    needed = max(3, math.ceil(0.5 * len(BOOK)))
    return (row["trades"] >= 25 and row["pf"] >= 1.1 and row["net"] > 0
            and row["max_dd"] <= 0.12 * book_notional and row["support"] >= needed)


def _aligned_select(row, book_notional):
    return row["net"] > 0 and row["pf"] >= 1.05 and row["trades"] >= 8 and row["max_dd"] <= 0.12 * book_notional


def run_aligned(args):
    """Second experiment: fade only in the direction of a slower average, with a wider passive quote.

    The holdout window is scored once, after the select step locks a single config.
    """
    global BOOK, PREP
    runtime, _template = load_yaml_configs()
    baseline_cfg = runtime["BTC-USDT"]
    baseline = params_from_config(
        "baseline", baseline_cfg, baseline_cfg["take_profit_quote"],
        float(baseline_cfg["stop_loss_cooldown_time"]) / float(baseline_cfg["cooldown_time"]),
    )
    log("aligned search: loading cached Binance USD-M candles")
    arrays = {}
    for pair, symbol in BINANCE_SYMBOL.items():
        if pair not in runtime:
            continue
        arrays[pair] = (symbol, download_symbol(symbol))
    BOOK = []
    for pair, config in runtime.items():
        symbol, array = arrays.get(pair, (None, None))
        if usable_history(array):
            BOOK.append((pair, float(config["total_amount_quote"]), symbol))
    if len(BOOK) < 4:
        raise SystemExit("Not enough pairs with full history")
    book_notional = sum(cap for _pair, cap, _symbol in BOOK)
    configs = [
        baseline,
        _aligned_shell("aligned_24h"),
        _aligned_shell("long_trend_24h", entry_mode=3),
        _aligned_shell("deep_quote", entry_mode=0, slow_window=0, offset=0.0015, time_limit=14400),
        _aligned_shell("aligned_8h", slow_window=96, offset=0.0008),
    ]
    rng = np.random.default_rng(ALIGNED_SEED)
    seen = set()
    while len(configs) < ALIGNED_RANDOM + 5:
        params = _sample_aligned(rng, len(configs))
        identity = tuple(sorted((key, params[key]) for key in params if key != "name"))
        if identity not in seen:
            seen.add(identity)
            configs.append(params)
    PREP = {}
    _SLOW_CACHE.clear()
    needed = {(params["interval"], params["window"]) for params in configs}
    for pair, _cap, _symbol in BOOK:
        array = arrays[pair][1]
        ts = array[:, 0].astype(np.int64)
        for interval, window in needed:
            PREP[(pair, interval, window)] = prepare_market(
                ts, array[:, 1], array[:, 2], array[:, 3], array[:, 4], array[:, 5], interval, window)
    log(f"aligned book notional {book_notional:.0f}, configs {len(configs)}")

    def run_many(param_list, t0, t1):
        tasks = [(params, t0, t1) for params in param_list]
        return [_task(task) for task in tasks]

    fit_rows = run_many(configs, int(FIT_START.timestamp()), int(SELECT_START.timestamp()))
    by_name = {row["name"]: row for row in fit_rows}
    log("FIT baseline " + fmt(by_name["baseline"]))
    for name in ("aligned_24h", "long_trend_24h", "deep_quote", "aligned_8h"):
        log("FIT " + fmt(by_name[name]))
    if abs(by_name["baseline"]["net"]) > 3 * book_notional:
        raise SystemExit("Baseline PnL is outside a plausible band")
    eligible = [row for row in fit_rows if _aligned_primary(row, book_notional)]
    eligible.sort(key=lambda row: row["net"], reverse=True)
    shortlist = []
    for row in eligible:
        if row["name"] not in shortlist:
            shortlist.append(row["name"])
        if len(shortlist) == 12:
            break
    if "baseline" not in shortlist:
        shortlist.append("baseline")
    log(f"aligned shortlist ({len(eligible)} fit-eligible): {', '.join(shortlist)}")
    if not eligible:
        winner_params = baseline
        log("no aligned config passed the fit gate")
    else:
        select_params = [next(params for params in configs if params["name"] == name) for name in shortlist]
        select_rows = run_many(select_params, int(SELECT_START.timestamp()), int(HOLDOUT_START.timestamp()))
        select_rows.sort(key=lambda row: row["net"], reverse=True)
        log("SELECT")
        for row in select_rows:
            log("  " + fmt(row))
        winner_params = baseline
        for row in select_rows:
            if _aligned_select(row, book_notional):
                winner_params = row["params"]
                break
    log(f"SELECTION LOCKED {winner_params['name']}")
    holdout_rows = run_many([winner_params, baseline], int(HOLDOUT_START.timestamp()), int(END.timestamp()))
    holdout = {row["name"]: row for row in holdout_rows}
    winner_holdout = holdout[winner_params["name"]]
    baseline_holdout = holdout["baseline"]
    log("HOLDOUT winner   " + fmt(winner_holdout))
    log("HOLDOUT baseline " + fmt(baseline_holdout))
    margin = max(1.0, 0.05 * abs(baseline_holdout["net"]))
    adopt = (
        winner_params["name"] != "baseline"
        and winner_holdout["net"] > 0
        and winner_holdout["pf"] >= 1.05
        and winner_holdout["trades"] >= 8
        and winner_holdout["max_dd"] <= 0.15 * book_notional
        and winner_holdout["net"] >= baseline_holdout["net"] + margin
    )
    payload = {
        "experiment": "with-trend mean reversion and wider passive quotes",
        "adopt": adopt,
        "margin_usdt": margin,
        "winner_name": winner_params["name"],
        "winner": winner_holdout,
        "baseline_holdout": baseline_holdout,
        "ratios": ratios(winner_params if adopt else baseline, book_notional),
        "winner_ratios": ratios(winner_params, book_notional),
        "book_notional": book_notional,
    }
    ALIGNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALIGNED_PATH.write_text(json.dumps(payload, indent=2))
    log(f"adopt {adopt}")
    log("ratios " + json.dumps(payload["ratios"]))
    log(f"wrote {ALIGNED_PATH}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--aligned", action="store_true")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    args = parser.parse_args()
    if args.aligned:
        run_aligned(args)
        return
    if args.apply:
        apply_winner(json.loads(RESULT_PATH.read_text()))
        return

    runtime, template = load_yaml_configs()
    baseline_cfg = runtime["BTC-USDT"]
    baseline = params_from_config(
        "baseline", baseline_cfg, baseline_cfg["take_profit_quote"],
        float(baseline_cfg["stop_loss_cooldown_time"]) / float(baseline_cfg["cooldown_time"]),
    )
    template_tp = []
    for pair, live in runtime.items():
        stored = template[pair]
        scaled = float(stored["take_profit_quote"]) * float(live["total_amount_quote"]) / float(stored["total_amount_quote"])
        template_tp.append(scaled)
    if max(template_tp) - min(template_tp) > 1e-9:
        raise SystemExit(f"Template cash target does not scale uniformly: {template_tp}")
    template_cfg = template["BTC-USDT"]
    template_params = params_from_config(
        "template_scaled", template_cfg, template_tp[0],
        float(template_cfg["stop_loss_cooldown_time"]) / float(template_cfg["cooldown_time"]),
    )

    log("downloading Binance USD-M 1m candles (OKX market API is blocked here)")
    arrays = {}
    for pair, symbol in BINANCE_SYMBOL.items():
        if pair not in runtime:
            continue
        arrays[pair] = (symbol, download_symbol(symbol))

    global BOOK
    BOOK = []
    for pair, config in runtime.items():
        symbol, array = arrays.get(pair, (None, None))
        if usable_history(array):
            BOOK.append((pair, float(config["total_amount_quote"]), symbol))
        else:
            log(f"exclude {pair} from the objective; history is missing or shorter than the protocol window")
    if len(BOOK) < 4:
        raise SystemExit("Not enough pairs with full history to score the book")
    book_notional = sum(cap for _pair, cap, _symbol in BOOK)
    log("book " + ", ".join(f"{pair}:{cap:.0f}" for pair, cap, _symbol in BOOK) + f" notional {book_notional:.0f}")
    assert_live_parity(arrays["BTC-USDT"][1])

    configs = [baseline, template_params, *hypotheses(baseline)]
    rng = np.random.default_rng(SEED)
    seen = set()
    while len(configs) < N_RANDOM + 6:
        params = sample_params(rng, len(configs))
        identity = tuple(sorted((key, params[key]) for key in params if key != "name"))
        if identity not in seen:
            seen.add(identity)
            configs.append(params)

    global PREP
    PREP = {}
    needed = {(params["interval"], params["window"]) for params in configs}
    for pair, _cap, symbol in BOOK:
        array = arrays[pair][1]
        ts = array[:, 0].astype(np.int64)
        for interval, window in needed:
            PREP[(pair, interval, window)] = prepare_market(
                ts, array[:, 1], array[:, 2], array[:, 3], array[:, 4], array[:, 5], interval, window)
    log(f"prepared {len(PREP)} pair/window views, searching {len(configs)} configs")

    import multiprocessing as mp
    ctx = mp.get_context("fork")
    fit_t0, fit_t1 = int(FIT_START.timestamp()), int(SELECT_START.timestamp())

    def run_many(param_list, t0, t1):
        tasks = [(params, t0, t1) for params in param_list]
        if args.workers == 1:
            return [_task(task) for task in tasks]
        with ctx.Pool(args.workers) as pool:
            return pool.map(_task, tasks, chunksize=1)

    started = time.perf_counter()
    fit_rows = run_many(configs, fit_t0, fit_t1)
    log(f"fit finished in {time.perf_counter() - started:.1f}s")
    by_name = {row["name"]: row for row in fit_rows}
    log("FIT baseline " + fmt(by_name["baseline"]))
    if by_name["baseline"]["trades"] <= 0 or by_name["baseline"]["fees"] <= 0:
        raise SystemExit("Baseline produced no filled trades; the fill model is not usable")
    if abs(by_name["baseline"]["net"]) > 3 * book_notional:
        raise SystemExit("Baseline PnL is outside a plausible band; refusing to score the holdout")

    eligible = [row for row in fit_rows if primary_ok(row, book_notional)]
    tier = "primary"
    if not eligible:
        eligible = [row for row in fit_rows if fallback_ok(row, book_notional)]
        tier = "fallback"
    eligible.sort(key=lambda row: row["net"], reverse=True)
    shortlist_names = []
    for row in eligible:
        if row["name"] not in shortlist_names:
            shortlist_names.append(row["name"])
        if len(shortlist_names) == 12:
            break
    for required in ("baseline", "template_scaled"):
        if required not in shortlist_names:
            shortlist_names.append(required)
    log(f"shortlist tier {tier} ({len(eligible)} fit-eligible): {', '.join(shortlist_names)}")
    if not eligible:
        log("no fit-eligible config; selection stays on the current runtime parameters")
        winner_params = baseline
    else:
        select_params = [next(params for params in configs if params["name"] == name) for name in shortlist_names]
        select_rows = run_many(select_params, int(SELECT_START.timestamp()), int(HOLDOUT_START.timestamp()))
        select_rows.sort(key=lambda row: row["net"], reverse=True)
        log("SELECT")
        for row in select_rows:
            log("  " + fmt(row))
        winner_params = baseline
        for row in select_rows:
            if select_ok(row, book_notional):
                winner_params = row["params"]
                break
        if winner_params["name"] == "baseline":
            log("select lock is the current runtime baseline")
    log(f"SELECTION LOCKED {winner_params['name']}")

    holdout_rows = run_many(
        [winner_params, baseline], int(HOLDOUT_START.timestamp()), int(END.timestamp()))
    holdout = {row["name"]: row for row in holdout_rows}
    winner_holdout = holdout[winner_params["name"]]
    baseline_holdout = holdout["baseline"]
    log("HOLDOUT winner  " + fmt(winner_holdout))
    log("HOLDOUT baseline " + fmt(baseline_holdout))
    margin = max(1.0, 0.05 * abs(baseline_holdout["net"]))
    adopt = (
        winner_params["name"] != "baseline"
        and winner_holdout["net"] > 0
        and winner_holdout["pf"] >= 1.0
        and winner_holdout["trades"] >= 12
        and winner_holdout["max_dd"] <= 0.20 * book_notional
        and winner_holdout["net"] >= baseline_holdout["net"] + margin
    )
    payload = {
        "data": "Binance USD-M perpetual 1m, because OKX public market data was unreachable",
        "fees": {"maker": MAKER_FEE, "taker": TAKER_FEE, "taker_slippage": TAKER_SLIPPAGE},
        "funding_modeled": False,
        "book": [{"pair": pair, "notional": cap} for pair, cap, _symbol in BOOK],
        "excluded": [pair for pair in runtime if pair not in {item[0] for item in BOOK}],
        "fit_tier": tier if eligible else "none",
        "winner_name": winner_params["name"],
        "adopt": adopt,
        "margin_usdt": margin,
        "winner": winner_holdout,
        "baseline_holdout": baseline_holdout,
        "baseline_fit": by_name["baseline"],
        "template_fit": by_name["template_scaled"],
        "ratios": ratios(winner_params if adopt else baseline, book_notional),
        "winner_ratios": ratios(winner_params, book_notional),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(payload, indent=2))
    log(f"adopt {adopt}")
    log("ratios " + json.dumps(payload["ratios"]))
    log(f"wrote {RESULT_PATH}")


def _task(item):
    params, t0, t1 = item
    return evaluate_params(params, t0, t1)


if __name__ == "__main__":
    main()
