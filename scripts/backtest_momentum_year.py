"""One-year backtest of the live 1h long-only momentum rules.

Signals use completed hourly bars only. A signal fills on the next hour's open.
Stops, the fixed-percent trail, and the 7-day time limit follow pmm_simple.
Nothing here sends an exchange order.

Prices are Binance USDT-M 1-minute bars aggregated to hours. OKB falls back to
OKX hourly candles when Binance has no perp. Funding, when present, is the
Binance USDT-M rate.
"""

from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from optimize_mean_reversion import fetch_day  # noqa: E402
from trend_sim import aggregate_minutes, trend_features  # noqa: E402

DATA_START = date(2025, 8, 15)
REPORT_START = datetime(2025, 9, 1, tzinfo=timezone.utc)
OLD_CACHES = [
    ROOT / "data" / "backtest" / "binance_um_1m_pre",
    ROOT / "data" / "backtest" / "binance_um_1m",
]
CACHE = ROOT / "data" / "backtest" / "binance_um_1m_year"
RESULT = ROOT / "data" / "backtest" / "momentum_year.json"
TAKER = 0.0005
SLIP = 0.0002
ACTIVATION = 0.001
ATR_LENGTH = 14
MIN_NOTIONAL = 5.0


def log(message):
    print(message, flush=True)


def http_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode())


def load_configs():
    script = yaml.safe_load((ROOT / "conf/scripts/conf_okx_multi.yml").read_text())
    pairs = []
    settings = None
    for name in script["controllers_config"]:
        cfg = yaml.safe_load((ROOT / "conf/controllers" / name).read_text())
        if cfg.get("signal_mode") != "momentum" or not cfg.get("long_only"):
            raise SystemExit(f"{name} is not the live long-only momentum config")
        pair = cfg["trading_pair"]
        pairs.append((pair, pair.replace("-", ""), float(cfg["total_amount_quote"])))
        current = {
            "interval": 3600,
            "lookback": int(cfg["momentum_lookback"]),
            "threshold": float(cfg["momentum_threshold"]),
            "atr_mult": float(cfg["atr_stop_multiplier"]),
            "min_stop": float(cfg["min_stop_loss"]),
            "max_stop": float(cfg["max_stop_loss"]),
            "risk_frac": float(cfg["risk_frac"]),
            "time_limit": int(cfg["time_limit"]),
            "cooldown": int(cfg["cooldown_time"]),
        }
        if settings is None:
            settings = current
        elif current != settings:
            raise SystemExit(f"{pair} signal settings differ from the first pair")
    return pairs, settings


def load_minutes(symbol):
    frames = []
    for folder in [*OLD_CACHES, CACHE]:
        path = folder / f"{symbol}.npz"
        if path.exists():
            frames.append(np.load(path)["data"])
    if not frames:
        return None
    array = np.concatenate(frames, axis=0)
    _, index = np.unique(array[:, 0], return_index=True)
    return array[np.sort(index)]


def covered_days(array):
    if array is None or len(array) == 0:
        return set()
    day = (array[:, 0].astype(np.int64) // 86400).astype(np.int64)
    counts = pd.Series(1, index=day).groupby(level=0).sum()
    return {int(key) for key, count in counts.items() if count >= 1400}


def download_binance(symbol, start, end):
    cached = load_minutes(symbol)
    have = covered_days(cached)
    days = []
    day = start
    while day <= end:
        if int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) // 86400 not in have:
            days.append(day)
        day += timedelta(days=1)
    if cached is not None and len(cached):
        first_day = int(cached[:, 0].min()) // 86400
        days = [day for day in days
                if int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) // 86400 >= first_day]
    if not days:
        log(f"cache {symbol} complete")
        return cached
    if cached is None:
        probes = [days[0], days[len(days) // 2], days[-1]]
        probe_hits = 0
        for probe in probes:
            probe_rows, missing = fetch_day(symbol, probe)
            probe_hits += int(bool(probe_rows) and not missing)
        if probe_hits == 0:
            log(f"no binance perp {symbol}")
            return None
    log(f"fetch {symbol} {len(days)} days")
    rows = []
    misses = 0
    with ThreadPoolExecutor(12) as pool:
        for day_rows, missing in pool.map(lambda item: fetch_day(*item), [(symbol, day) for day in days]):
            rows.extend(day_rows)
            misses += int(missing)
    frames = []
    if cached is not None:
        frames.append(cached)
    if rows:
        frames.append(np.array(rows, dtype=np.float64))
    if not frames:
        log(f"missing {symbol} misses {misses}")
        return None
    array = np.concatenate(frames, axis=0)
    _, index = np.unique(array[:, 0], return_index=True)
    array = array[np.sort(index)]
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE / f"{symbol}.npz", data=array)
    log(f"saved {symbol} {len(array)} bars misses {misses}")
    return array


def download_okx_hours(inst_id, start, end):
    """Paginate OKX history candles, newest page first."""
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000) + 86400_000
    cursor = end_ms
    rows = []
    for _ in range(200):
        url = (f"https://www.okx.com/api/v5/market/history-candles?instId={inst_id}"
               f"&bar=1H&limit=100&after={cursor}")
        payload = None
        for attempt in range(5):
            try:
                payload = http_json(url)
                break
            except Exception as error:
                if attempt == 4:
                    log(f"okx {inst_id} failed at cursor={cursor}: {error}")
                    payload = None
                else:
                    time.sleep(1.5 * (attempt + 1))
        if payload is None:
            break
        batch = payload.get("data") or []
        if not batch:
            break
        oldest = int(batch[-1][0])
        for candle in batch:
            stamp = int(candle[0])
            if stamp < start_ms or stamp >= end_ms:
                continue
            if len(candle) > 8 and candle[8] not in ("1", 1):
                continue
            rows.append((stamp // 1000, float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])))
        if oldest <= start_ms:
            break
        cursor = oldest
        time.sleep(0.12)
    if not rows:
        return None
    array = np.array(rows, dtype=np.float64)
    _, index = np.unique(array[:, 0], return_index=True)
    array = array[np.sort(index)]
    log(f"okx {inst_id} {len(array)} hourly bars")
    return array


def hours_from_minutes(array, interval):
    ts, op, hi, lo, cl = aggregate_minutes(array[:, 0], array[:, 1], array[:, 2], array[:, 3], array[:, 4], interval)
    return ts, op, hi, lo, cl


def hours_from_bars(array, interval):
    if len(array) == 0:
        raise ValueError("no bars")
    order = np.argsort(array[:, 0], kind="mergesort")
    array = array[order]
    start = int(array[0, 0] // interval * interval)
    stop = int(array[-1, 0] // interval * interval)
    full = np.arange(start, stop + interval, interval, dtype=np.int64)
    frame = pd.DataFrame(array[:, :5], columns=["ts", "o", "h", "l", "c"]).drop_duplicates("ts")
    frame["ts"] = (frame["ts"] // interval) * interval
    frame = frame.set_index("ts").reindex(full)
    op = frame["o"].to_numpy(dtype=np.float64)
    hi = frame["h"].to_numpy(dtype=np.float64)
    lo = frame["l"].to_numpy(dtype=np.float64)
    cl = frame["c"].to_numpy(dtype=np.float64)
    return full, op, hi, lo, cl


def next_month(day):
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def download_funding(symbol, start, end):
    rates = {}
    month = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while month <= last:
        url = (f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/"
               f"{symbol}-fundingRate-{month.year}-{month.month:02d}.zip")
        try:
            blob = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=40, context=ssl.create_default_context(),
            ).read()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                log(f"funding {symbol} {month} http {error.code}")
            month = next_month(month)
            continue
        except Exception as error:
            log(f"funding {symbol} {month} {error}")
            month = next_month(month)
            continue
        import zipfile
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            text = archive.read(archive.namelist()[0]).decode("utf-8-sig")
        for line in text.splitlines():
            if not line or not line[0].isdigit():
                continue
            parts = line.split(",")
            stamp = int(float(parts[0]))
            if stamp > 10_000_000_000:
                stamp //= 1000
            rate = float(parts[-1])
            rates[stamp] = rate
        month = next_month(month)
    log(f"funding {symbol} {len(rates)} prints")
    return rates


def _book(entry, exit_px, qty, funding):
    gross = (exit_px - entry) * qty
    fee = (entry + exit_px) * qty * TAKER
    return gross - fee - funding, fee


def simulate(ts, op, hi, lo, cl, atr, momentum, cap, settings, funding, t0, t1):
    lookback = settings["lookback"]
    need = max(lookback, ATR_LENGTH)
    realized = 0.0
    in_pos = False
    entry = qty = stop_pct = 0.0
    entry_ts = 0
    trail_px = 0.0
    activated = False
    funding_paid = 0.0
    cooldown_until = 0
    pending = None
    trades = []
    equity_ts = []
    equity = []
    n = len(ts)

    for i in range(n):
        price_ok = np.isfinite(op[i]) and lo[i] > 0.0 and lo[i] <= op[i] <= hi[i] and lo[i] <= cl[i] <= hi[i]
        if not price_ok:
            pending = None
            if in_pos and t0 <= ts[i] < t1:
                mark = equity[-1] if equity else realized
                equity_ts.append(int(ts[i]))
                equity.append(mark)
            continue

        if in_pos and int(ts[i]) in funding:
            funding_paid += op[i] * qty * funding[int(ts[i])]

        if in_pos:
            hard = entry * (1.0 - stop_pct + TAKER)
            reason = None
            exit_px = 0.0
            if op[i] <= hard:
                reason, exit_px = "stop", op[i] * (1.0 - SLIP)
            elif activated and op[i] <= trail_px:
                reason, exit_px = "trail", op[i] * (1.0 - SLIP)
            elif ts[i] >= entry_ts + settings["time_limit"]:
                reason, exit_px = "time", op[i] * (1.0 - SLIP)
            elif lo[i] <= hard:
                reason, exit_px = "stop", hard * (1.0 - SLIP)
            elif activated and lo[i] <= trail_px:
                reason, exit_px = "trail", trail_px * (1.0 - SLIP)
            if reason:
                pnl, fee = _book(entry, exit_px, qty, funding_paid)
                realized += pnl
                trades.append({
                    "entry_ts": int(entry_ts), "exit_ts": int(ts[i]), "pnl": pnl, "fee": fee,
                    "funding": funding_paid, "reason": reason, "notional": entry * qty,
                })
                in_pos = False
                cooldown_until = int(ts[i]) + settings["cooldown"]
            else:
                net = hi[i] / entry - 1.0 - TAKER
                if net > ACTIVATION:
                    new_trail = hi[i] - entry * stop_pct
                    if (not activated) or new_trail > trail_px:
                        trail_px = new_trail
                        activated = True

        if (not in_pos) and pending is not None and pending[0] == i and int(ts[i]) >= cooldown_until:
            stop_pct = pending[1]
            entry = op[i] * (1.0 + SLIP)
            notional = min(cap, settings["risk_frac"] * cap / stop_pct)
            if notional >= MIN_NOTIONAL:
                qty = notional / entry
                entry_ts = int(ts[i])
                hard = entry * (1.0 - stop_pct + TAKER)
                funding_paid = 0.0
                activated = False
                trail_px = 0.0
                in_pos = True
                if lo[i] <= hard:
                    exit_px = op[i] * (1.0 - SLIP) if op[i] <= hard else hard * (1.0 - SLIP)
                    pnl, fee = _book(entry, exit_px, qty, 0.0)
                    realized += pnl
                    trades.append({
                        "entry_ts": entry_ts, "exit_ts": int(ts[i]), "pnl": pnl, "fee": fee,
                        "funding": 0.0, "reason": "stop", "notional": notional,
                    })
                    in_pos = False
                    cooldown_until = int(ts[i]) + settings["cooldown"]
                else:
                    net = hi[i] / entry - 1.0 - TAKER
                    if net > ACTIVATION:
                        trail_px = hi[i] - entry * stop_pct
                        activated = True
            pending = None

        if (not in_pos) and i + 1 < n and i >= need and int(ts[i]) + 3600 >= cooldown_until:
            window_ok = np.isfinite(cl[i - need:i + 1]).all() and np.isfinite(atr[i]) and np.isfinite(momentum[i])
            if window_ok and momentum[i] > settings["threshold"]:
                stop = settings["atr_mult"] * atr[i] / cl[i]
                if stop < settings["min_stop"]:
                    stop = settings["min_stop"]
                if stop <= settings["max_stop"]:
                    pending = (i + 1, float(stop))

        if t0 <= ts[i] < t1:
            mark = realized
            if in_pos:
                mtm = cl[i] * (1.0 - SLIP)
                gross = (mtm - entry) * qty
                fee = (entry + mtm) * qty * TAKER
                mark = realized + gross - fee - funding_paid
            equity_ts.append(int(ts[i]))
            equity.append(mark)

    if in_pos and n:
        i = n - 1
        while i > 0 and not np.isfinite(cl[i]):
            i -= 1
        exit_px = cl[i] * (1.0 - SLIP)
        pnl, fee = _book(entry, exit_px, qty, funding_paid)
        realized += pnl
        trades.append({
            "entry_ts": int(entry_ts), "exit_ts": int(ts[i]), "pnl": pnl, "fee": fee,
            "funding": funding_paid, "reason": "eod", "notional": entry * qty,
        })
        if equity:
            equity[-1] = realized
    return trades, np.array(equity_ts, dtype=np.int64), np.array(equity, dtype=np.float64)


def buy_hold(ts, op, cl, cap, t0, t1):
    """Same notional cap, bought at the first hour and marked each hour."""
    equity_ts = []
    equity = []
    entry = None
    for i in range(len(ts)):
        if not (t0 <= ts[i] < t1) or not np.isfinite(op[i]) or not np.isfinite(cl[i]):
            continue
        if entry is None:
            entry = op[i] * (1.0 + SLIP)
            qty = cap / entry
        mtm = cl[i] * (1.0 - SLIP)
        equity_ts.append(int(ts[i]))
        equity.append((mtm - entry) * qty - (entry + mtm) * qty * TAKER)
    return np.array(equity_ts, dtype=np.int64), np.array(equity, dtype=np.float64)


def month_key(stamp):
    moment = datetime.fromtimestamp(int(stamp), timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def max_drawdown(values):
    if len(values) == 0:
        return 0.0
    peak = np.maximum.accumulate(values)
    return float(np.max(peak - values))


def trade_stats(trades):
    pnls = np.array([trade["pnl"] for trade in trades], dtype=np.float64)
    if len(pnls) == 0:
        return {"net": 0.0, "trades": 0, "wins": 0, "pf": 0.0, "fees": 0.0, "funding": 0.0,
                "hold_hours": 0.0, "stops": 0, "trails": 0, "times": 0, "eods": 0}
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    hold = np.mean([(trade["exit_ts"] - trade["entry_ts"]) / 3600 for trade in trades])
    reasons = [trade["reason"] for trade in trades]
    return {
        "net": float(pnls.sum()),
        "trades": int(len(pnls)),
        "wins": int(len(wins)),
        "pf": (float(wins.sum()) / gross_loss) if gross_loss > 1e-9 else (1e9 if len(wins) else 0.0),
        "fees": float(sum(trade["fee"] for trade in trades)),
        "funding": float(sum(trade["funding"] for trade in trades)),
        "hold_hours": float(hold),
        "stops": reasons.count("stop"),
        "trails": reasons.count("trail"),
        "times": reasons.count("time"),
        "eods": reasons.count("eod"),
    }


def equity_table(curves, stamps):
    frame = pd.DataFrame(index=pd.Index(stamps, name="ts"))
    for name, (ts, values) in curves.items():
        if len(ts) == 0:
            continue
        series = pd.Series(values, index=ts)
        frame[name] = series.groupby(level=0).last()
    frame = frame.sort_index().ffill().fillna(0.0)
    frame["total"] = frame.sum(axis=1)
    return frame


def slice_change(frame, start, end):
    window = frame[(frame.index >= start) & (frame.index < end)]
    if window.empty:
        return 0.0, 0.0
    before = frame[frame.index < start]
    start_equity = float(before["total"].iloc[-1]) if len(before) else 0.0
    end_equity = float(window["total"].iloc[-1])
    return end_equity - start_equity, max_drawdown(window["total"].to_numpy(dtype=np.float64))


def self_check():
    count = 48
    close = 100 * (1.01 ** np.arange(count))
    op, hi, lo, cl = close * 0.999, close * 1.004, close * 0.997, close
    ts = np.arange(count, dtype=np.int64) * 3600
    atr, momentum, _high, _low = trend_features(op, hi, lo, cl, 16, 14)
    settings = {"lookback": 16, "threshold": 0.008, "atr_mult": 3.0, "min_stop": 0.004,
                "max_stop": 0.06, "risk_frac": 0.02, "time_limit": 604800, "cooldown": 3600}
    trades, _ts, equity = simulate(ts, op, hi, lo, cl, atr, momentum, 80.0, settings, {}, 0, 10**12)
    if not trades or equity[-1] <= 0:
        raise SystemExit(f"self-check failed on a rising market: {trades[:1]} {equity[-1] if len(equity) else None}")
    flat = np.full(30, 100.0)
    op = hi = lo = cl = flat.copy()
    hi = flat + 0.1
    lo = flat - 0.1
    atr, momentum, _high, _low = trend_features(op, hi, lo, cl, 16, 14)
    trades, _ts, _eq = simulate(ts[:30], op, hi, lo, cl, atr, momentum, 80.0, settings, {}, 0, 10**12)
    if trades:
        raise SystemExit("self-check failed: a flat market opened a trade")
    log("self-check ok")


def main():
    self_check()
    pairs, settings = load_configs()
    end = date(2026, 9, 28)
    markets = {}
    coverage = []
    for pair, symbol, cap in pairs:
        minutes = download_binance(symbol, DATA_START, end)
        source = "binance"
        hourly = None
        if minutes is not None and len(minutes) > 20 * 1440:
            hourly = hours_from_minutes(minutes, settings["interval"])
        elif pair == "OKB-USDT":
            for inst in ("OKB-USDT-SWAP", "OKB-USDT"):
                okx = download_okx_hours(inst, DATA_START, end)
                if okx is not None and len(okx) > 24 * 20:
                    hourly = hours_from_bars(okx, settings["interval"])
                    source = f"okx:{inst}"
                    break
        if hourly is None:
            log(f"exclude {pair}")
            coverage.append({"pair": pair, "cap": cap, "source": None, "bars": 0})
            continue
        ts, op, hi, lo, cl = hourly
        features = trend_features(op, hi, lo, cl, settings["lookback"], ATR_LENGTH)
        valid = np.isfinite(cl)
        first = datetime.fromtimestamp(int(ts[valid][0]), timezone.utc).date().isoformat()
        last = datetime.fromtimestamp(int(ts[valid][-1]), timezone.utc).date().isoformat()
        missing = int((~valid).sum())
        coverage.append({"pair": pair, "cap": cap, "source": source, "first": first, "last": last,
                         "hours": int(valid.sum()), "missing_hours": missing})
        markets[pair] = (cap, ts, op, hi, lo, cl, features)
        log(f"{pair} {source} {first} -> {last} hours {int(valid.sum())} missing {missing}")

    t0 = int(REPORT_START.timestamp())
    t1 = int(datetime(2026, 9, 29, tzinfo=timezone.utc).timestamp())
    log("downloading funding")
    curves = {}
    hold_curves = {}
    all_trades = []
    pair_trades = {}
    with ThreadPoolExecutor(6) as pool:
        funding_jobs = {pair: pool.submit(download_funding, symbol, DATA_START, end)
                        for pair, symbol, _cap in pairs if pair in markets}
        funding = {pair: job.result() for pair, job in funding_jobs.items()}
    for pair, (cap, ts, op, hi, lo, cl, features) in markets.items():
        atr, momentum, _prior_high, _prior_low = features
        trades, ets, eq = simulate(ts, op, hi, lo, cl, atr, momentum, cap, settings, funding.get(pair, {}), t0, t1)
        for trade in trades:
            trade["pair"] = pair
        all_trades.extend(trades)
        pair_trades[pair] = trades
        curves[pair] = (ets, eq)
        hold_curves[pair] = buy_hold(ts, op, cl, cap, t0, t1)
        log(f"{pair} trades {len(trades)} net {sum(trade['pnl'] for trade in trades):.2f}")

    stamps = sorted({int(stamp) for ts, _eq in curves.values() for stamp in ts})
    book = equity_table(curves, stamps)
    hold = equity_table(hold_curves, stamps)
    months = []
    cursor = REPORT_START
    while cursor < datetime.fromtimestamp(t1, timezone.utc):
        nxt = datetime(
            cursor.year + (cursor.month == 12),
            1 if cursor.month == 12 else cursor.month + 1,
            1,
            tzinfo=timezone.utc,
        )
        start, stop = int(cursor.timestamp()), int(nxt.timestamp())
        net, dd = slice_change(book, start, stop)
        hold_net, _hold_dd = slice_change(hold, start, stop)
        closed = [trade for trade in all_trades if start <= trade["exit_ts"] < stop]
        stats = trade_stats(closed)
        pair_nets = {}
        for pair in curves:
            sub = book.loc[(book.index >= start) & (book.index < stop), pair]
            before = book.loc[book.index < start, pair]
            begin = float(before.iloc[-1]) if len(before) else 0.0
            pair_nets[pair] = float(sub.iloc[-1] - begin) if len(sub) else 0.0
        months.append({
            "month": cursor.strftime("%Y-%m"),
            "net": net,
            "max_dd": dd,
            "hold_net": hold_net,
            "trades": stats["trades"],
            "wins": stats["wins"],
            "pf": stats["pf"] if stats["pf"] < 1e8 else None,
            "fees": stats["fees"],
            "funding": stats["funding"],
            "pair_nets": pair_nets,
        })
        cursor = nxt

    trail_start = int(datetime(2025, 9, 29, tzinfo=timezone.utc).timestamp())
    trail_net, trail_dd = slice_change(book, trail_start, t1)
    summary = trade_stats(all_trades)
    summary["equity_net"] = float(book["total"].iloc[-1]) if len(book) else 0.0
    summary["max_dd"] = max_drawdown(book["total"].to_numpy(dtype=np.float64)) if len(book) else 0.0
    summary["hold_net"] = float(hold["total"].iloc[-1]) if len(hold) else 0.0
    cap_sum = sum(item["cap"] for item in coverage if item.get("source"))
    summary["cap"] = cap_sum
    summary["trailing_365_net"] = trail_net
    summary["trailing_365_dd"] = trail_dd
    pair_rows = []
    for pair, trades in pair_trades.items():
        row = trade_stats(trades)
        row["pair"] = pair
        row["equity_net"] = float(book[pair].iloc[-1]) if pair in book else row["net"]
        pair_rows.append(row)

    report = {
        "window": [REPORT_START.date().isoformat(), datetime.fromtimestamp(min(t1, int(book.index[-1]) + 3600), timezone.utc).date().isoformat() if len(book) else None],
        "settings": settings,
        "costs": {"taker": TAKER, "slip": SLIP, "trail_activation": ACTIVATION},
        "coverage": coverage,
        "summary": summary,
        "months": months,
        "pairs": pair_rows,
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, indent=2))
    log(f"wrote {RESULT}")
    log(f"equity {summary['equity_net']:.2f} closed {summary['net']:.2f} trades {summary['trades']} "
        f"pf {summary['pf']:.2f} dd {summary['max_dd']:.2f} fees {summary['fees']:.2f} "
        f"funding {summary['funding']:.2f} hold {summary['hold_net']:.2f}")
    for row in months:
        log(f"{row['month']} net {row['net']:8.2f} trades {row['trades']:4d} "
            f"pf {0 if row['pf'] is None else row['pf']:5.2f} dd {row['max_dd']:7.2f} "
            f"fees {row['fees']:7.2f} funding {row['funding']:7.2f} hold {row['hold_net']:8.2f}")


if __name__ == "__main__":
    main()
