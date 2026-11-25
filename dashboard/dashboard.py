#!/usr/bin/env python3
"""
OKX Quant Trader — Backend API & Static SPA Server
===================================================
提供结构化实时状态、OKX 永续行情/K 线、策略参数读写与启停控制接口，
并托管 dashboard/dist 下的 React 前端静态资源。仅使用 Python 标准库。

用法:
    python3 dashboard/dashboard.py
    # 浏览器打开 http://127.0.0.1:8888
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import mimetypes
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from strategy_config import (
        ConfigError,
        SHARED_FIELDS,
        STRATEGY_INFO,
        add_pair_config,
        apply_config,
        get_presets_info,
        get_strategy_info,
        load_config,
        read_side_mode,
        set_side_mode,
    )
except ImportError:  # imported as dashboard.dashboard
    from dashboard.strategy_config import (
        ConfigError,
        SHARED_FIELDS,
        STRATEGY_INFO,
        add_pair_config,
        apply_config,
        get_presets_info,
        get_strategy_info,
        load_config,
        read_side_mode,
        set_side_mode,
    )


# ─── 路径与常量 ──────────────────────────────────────────────────────────────
BASE_DIR = Path(os.environ.get("OKX_TRADER_ROOT", Path(__file__).parent.parent)).resolve()
STATUS_FILE = BASE_DIR / "data" / "bot" / "status.json"
LOG_FILE = BASE_DIR / "logs" / "logs_conf_okx_multi.log"
MANUAL_CLOSES_FILE = BASE_DIR / "data" / "dashboard" / "manual_closes.json"
SESSION_BASELINE_FILE = BASE_DIR / "data" / "dashboard" / "session_baseline.json"
PORT = int(os.environ.get("DASHBOARD_PORT", "8888"))
STALE_AFTER_S = 20

OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
TICKER_TTL_S = 5
CANDLES_TTL_S = 10

_ticker_lock = threading.Lock()
_ticker_cache: dict = {"fetched_at": 0.0, "by_inst": {}, "error": None}
_candles_lock = threading.Lock()
_candles_cache: dict[tuple[str, str], dict] = {}
_control_lock = threading.Lock()
_control_process: subprocess.Popen | None = None
_control_action: str | None = None
_control_exit_code: int | None = None
_control_started_at: float | None = None
_control_finished_at: float | None = None
_config_lock = threading.Lock()

OKX_BAR_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
}
INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def _log(msg: str) -> None:
    print(f"[dashboard] {msg}", flush=True)


def frontend_dist_dir() -> Path:
    candidates = [
        BASE_DIR / "dashboard" / "dist",
        Path(__file__).resolve().parent / "dist",
    ]
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand
    return candidates[0]


# ─── 进程启停控制 ────────────────────────────────────────────────────────────
def _launch_control(action: str) -> tuple[bool, str]:
    """Start one detached launcher control process; credentials stay local."""
    global _control_action, _control_process, _control_exit_code, _control_started_at, _control_finished_at
    labels = {"start": "重新启动", "stop": "停止"}
    label = labels[action]
    with _control_lock:
        if _control_process is not None and _control_process.poll() is None:
            active_label = labels.get(_control_action or "", "控制操作")
            return False, f"{active_label}已在进行中，请稍候。"
        launcher = BASE_DIR / "native" / "launcher.py"
        if not getattr(sys, "frozen", False) and not launcher.is_file():
            return False, f"找不到启动器：{launcher}"
        log_path = BASE_DIR / "dashboard" / f"{action}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("w", encoding="utf-8")
        log_handle.write(f"=== {action.upper()} initiated at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_handle.flush()
        env = os.environ.copy()
        env["OKX_TRADER_ROOT"] = str(BASE_DIR)
        env["OKX_TRADER_NO_BROWSER"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict = {
            "cwd": BASE_DIR,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, action]
            else:
                cmd = [sys.executable, str(launcher), action]
            _control_process = subprocess.Popen(cmd, **kwargs)
            _control_action = action
            _control_exit_code = None
            _control_started_at = time.time()
            _control_finished_at = None
        except OSError as exc:
            return False, f"无法启动{label}进程：{exc}"
        finally:
            log_handle.close()
        _log(f"{action} requested; pid={_control_process.pid}")
        if action == "stop":
            return True, "停止命令已发送，正在优雅关闭策略容器进程。"
        return True, "重新启动已开始，正在校验配置并启动实盘策略（约 15–60 秒）。"


def control_status() -> dict:
    global _control_exit_code, _control_finished_at
    with _control_lock:
        if _control_process is None:
            return {
                "in_progress": False,
                "action": _control_action,
                "pid": None,
                "exit_code": _control_exit_code,
                "started_at": _control_started_at,
                "finished_at": _control_finished_at,
            }
        rc = _control_process.poll()
        running = rc is None
        if not running and _control_exit_code is None:
            _control_exit_code = rc
            _control_finished_at = time.time()
        return {
            "in_progress": running,
            "action": _control_action,
            "pid": _control_process.pid if running else None,
            "exit_code": _control_exit_code,
            "started_at": _control_started_at,
            "finished_at": _control_finished_at,
        }


def restart_strategy() -> tuple[bool, str]:
    success, msg = _launch_control("start")
    if success:
        reset_session_baseline()
    return success, msg


def switch_side_mode(long_only: bool) -> tuple[bool, str, str]:
    """Save long-only or both-sides, then restart so the running process reloads it."""
    with _config_lock:
        set_side_mode(long_only)
    mode = "long_only" if long_only else "both"
    label = "只做多" if long_only else "多空双向"
    started, message = restart_strategy()
    if started:
        return True, f"已切换为{label}，并开始重启。{message}", mode
    return False, f"已切换为{label}，但重启没有开始：{message}", mode


def current_side_mode() -> str:
    try:
        return read_side_mode()
    except Exception:
        return "both"


def stop_strategy() -> tuple[bool, str]:
    return _launch_control("stop")


# ─── 状态与日志读取 ──────────────────────────────────────────────────────────
def read_status() -> dict:
    """读取引擎定时写入的 status.json。"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


def tail_file(path: Path, n: int = 80) -> list[str]:
    """读取文件末尾 n 行。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            buf, lines_found = bytearray(), 0
            pos = size - 1
            while pos >= 0 and lines_found < n + 1:
                f.seek(pos)
                ch = f.read(1)
                if ch == b"\n":
                    lines_found += 1
                buf.extend(ch)
                pos -= 1
            raw = bytes(reversed(buf)).decode("utf-8", errors="replace")
            return raw.splitlines()[-n:]
    except Exception:
        return []


def tail_log(n: int = 80) -> list[str]:
    return tail_file(LOG_FILE, n)


_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*(?:-\s*|\|\s*)?([A-Za-z0-9_.-]+)?\s*(?:-\s*|\|\s*)?(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)?\s*(?:-\s*|\|\s*)?(.*)$"
)


def parse_log_entries(lines: list[str]) -> list[dict]:
    entries = []
    for idx, line in enumerate(lines):
        level = "INFO"
        upper = line.upper()
        if "CRITICAL" in upper or "ERROR" in upper or "TRACEBACK" in upper or "EXCEPTION" in upper:
            level = "ERROR"
        elif "WARNING" in upper or "WARN" in upper:
            level = "WARNING"
        elif "DEBUG" in upper:
            level = "DEBUG"
        ts = ""
        m = _LOG_LINE_RE.match(line)
        if m and m.group(1):
            ts = m.group(1)
        entries.append({
            "id": idx,
            "raw": line,
            "level": level,
            "timestamp": ts,
        })
    return entries


# ─── OKX 公开行情与 K 线 ─────────────────────────────────────────────────────
def _num(row: dict, key: str) -> float | None:
    try:
        value = float(row[key])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _okx_openers():
    """本机访问 OKX 走本地代理；代理不可用时再直连。"""
    seen = set()
    candidates = ["http://127.0.0.1:7897"]
    for key in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    openers = []
    for proxy in candidates:
        if proxy in seen:
            continue
        seen.add(proxy)
        openers.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ))
    openers.append(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    return openers


def _okx_get_json(url: str, timeout: int = 8) -> tuple[dict | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "okx-quant-dashboard", "Accept": "application/json"},
    )
    last_error = "quotes unavailable"
    for opener in _okx_openers():
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = json.load(resp)
            if isinstance(payload, dict) and str(payload.get("code")) == "0":
                return payload, None
            if isinstance(payload, dict) and payload.get("msg"):
                last_error = str(payload.get("msg"))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


def fetch_swap_tickers() -> tuple[dict[str, dict], str | None]:
    """拉 OKX USDT 永续公开行情，短缓存，失败时沿用上一份。"""
    now = time.time()
    with _ticker_lock:
        cached = _ticker_cache["by_inst"]
        age = now - float(_ticker_cache["fetched_at"] or 0)
        if cached and age < TICKER_TTL_S:
            return cached, _ticker_cache["error"]
        payload, err = _okx_get_json(OKX_TICKERS_URL, timeout=8)
        if not payload:
            _ticker_cache["error"] = err or "quotes unavailable"
            if cached:
                return cached, _ticker_cache["error"]
            return {}, _ticker_cache["error"]
        by_inst = {}
        for row in payload.get("data") or []:
            inst = row.get("instId") or ""
            if inst.endswith("-USDT-SWAP"):
                by_inst[inst] = row
        _ticker_cache["by_inst"] = by_inst
        _ticker_cache["fetched_at"] = time.time()
        _ticker_cache["error"] = None
        return by_inst, None


def strategy_pairs(status: dict, perf_rows: list[dict]) -> list[str]:
    """优先使用当前启用的控制器/运行状态币种列表。"""
    pairs: list[str] = []
    for row in perf_rows:
        ctrl = row["controller"]
        if ctrl == "GLOBAL TOTAL":
            continue
        pair = f"{ctrl.split('_')[-1].upper()}-USDT"
        if pair not in pairs:
            pairs.append(pair)
    if pairs:
        return pairs
    try:
        cfg = load_config()
        for item in cfg.get("pairs", []):
            p = item.get("trading_pair")
            if p and p not in pairs:
                pairs.append(p)
        if pairs:
            return pairs
    except Exception:
        pass
    connectors = ((status.get("engine") or {}).get("connectors") or {})
    for conn in connectors.values():
        for pair in conn.get("trading_pairs") or []:
            if pair not in pairs:
                pairs.append(pair)
    return pairs or ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


def build_markets_data(pairs: list[str]) -> dict:
    tickers, error = fetch_swap_tickers()
    fetched_at = float(_ticker_cache["fetched_at"] or 0)
    age = (time.time() - fetched_at) if fetched_at else None
    if _ticker_cache["by_inst"]:
        meta = f"OKX SWAP · {age:.0f}s" if age is not None else "OKX SWAP"
        if error:
            meta += " · STALE"
    else:
        meta = error or "no quotes"

    strategy_set = set(pairs)
    ordered_pairs = list(pairs)
    for bench in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        if bench not in ordered_pairs:
            ordered_pairs.append(bench)

    items = []
    for pair in ordered_pairs:
        inst_id = f"{pair}-SWAP"
        row = tickers.get(inst_id) or {}
        last = _num(row, "last")
        bid = _num(row, "bidPx")
        ask = _num(row, "askPx")
        open24 = _num(row, "open24h")
        high = _num(row, "high24h")
        low = _num(row, "low24h")
        vol_base = _num(row, "volCcy24h")
        chg_pct = ((last - open24) / open24 * 100.0) if (last is not None and open24) else None
        spread_pct = None
        spread_bps = None
        if bid and ask and ask >= bid:
            mid = (bid + ask) / 2.0
            if mid > 0:
                spread_pct = (ask - bid) / mid * 100.0
                spread_bps = (ask - bid) / mid * 10000.0
        vol_quote = (vol_base * last) if (vol_base is not None and last is not None) else None
        items.append({
            "pair": pair,
            "symbol": pair.split("-")[0],
            "inst_id": inst_id,
            "is_strategy_pair": pair in strategy_set,
            "last": last,
            "open24h": open24,
            "change_pct": chg_pct,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "spread_bps": spread_bps,
            "high24h": high,
            "low24h": low,
            "vol_base": vol_base,
            "vol_quote": vol_quote,
        })
    return {
        "error": error,
        "stale": bool(error and _ticker_cache["by_inst"]),
        "meta": meta,
        "fetched_at": fetched_at,
        "tickers": items,
    }


def _load_cached_csv_candles(pair: str, interval: str, limit: int) -> list[dict]:
    """Fallback to local 5m CSV candle archives in data/okx_candles/ when offline."""
    candles_dir = BASE_DIR / "data" / "okx_candles"
    if not candles_dir.is_dir():
        return []
    inst = f"{pair}-SWAP"
    candidates = [
        candles_dir / f"{inst}_5m_last_month.csv",
        candles_dir / f"{inst}_5m_6months.csv",
        candles_dir / f"{inst}_5m_1year.csv",
    ]
    csv_path = next((p for p in candidates if p.is_file()), None)
    if csv_path is None:
        return []

    sec = INTERVAL_SECONDS.get(interval, 3600)
    buckets: dict[int, dict] = {}
    try:
        rows = tail_file(csv_path, max(limit * max(1, sec // 300) + 10, 400))
        reader = csv.reader(rows)
        for parts in reader:
            if len(parts) < 6 or parts[0].lower() in ("timestamp", "ts", "datetime", "time"):
                continue
            try:
                raw_ts = parts[0].strip()
                if re.fullmatch(r"\d+(\.\d+)?", raw_ts):
                    ts = int(float(raw_ts) / (1000.0 if float(raw_ts) > 1e11 else 1.0))
                else:
                    iso = raw_ts.replace(" ", "T").replace("Z", "+00:00")
                    parsed_dt = dt.datetime.fromisoformat(iso)
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)
                    ts = int(parsed_dt.timestamp())
                o, h, l, c, v = map(float, parts[1:6])
            except Exception:
                continue
            b = (ts // sec) * sec
            if b not in buckets:
                buckets[b] = {"time": b, "open": o, "high": h, "low": l, "close": c, "volume": v}
            else:
                cur = buckets[b]
                cur["high"] = max(cur["high"], h)
                cur["low"] = min(cur["low"], l)
                cur["close"] = c
                cur["volume"] += v
    except Exception:
        return []
    ordered = [buckets[k] for k in sorted(buckets)]
    return ordered[-limit:]


def _fetch_okx_candles(pair: str, interval: str, limit: int = 200) -> tuple[list[dict], str, str | None]:
    bar = OKX_BAR_MAP.get(interval, "1H")
    cache_key = (pair, bar)
    now = time.time()
    with _candles_lock:
        cached = _candles_cache.get(cache_key)
        if cached and now - cached["fetched_at"] < CANDLES_TTL_S and len(cached["candles"]) >= min(limit, 50):
            return cached["candles"][-limit:], cached["source"], None

    query = urllib.parse.urlencode({"instId": f"{pair}-SWAP", "bar": bar, "limit": str(min(max(limit, 60), 300))})
    payload, err = _okx_get_json(f"{OKX_CANDLES_URL}?{query}", timeout=8)
    candles: list[dict] = []
    source = "okx_live"
    if payload and isinstance(payload.get("data"), list):
        for row in reversed(payload["data"]):
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                ts_sec = int(int(row[0]) // 1000)
                candles.append({
                    "time": ts_sec,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (TypeError, ValueError):
                continue
        if candles:
            with _candles_lock:
                _candles_cache[cache_key] = {"fetched_at": time.time(), "candles": candles, "source": "okx_live"}
            return candles[-limit:], "okx_live", None

    # Offline or network-blocked fallback to cached historical CSVs if present
    local = _load_cached_csv_candles(pair, interval, limit)
    if local:
        return local, "local_cache", err
    with _candles_lock:
        if cached:
            return cached["candles"][-limit:], f"{cached['source']}_stale", err
    return [], "unavailable", err or "无法获取 K 线数据"


def _pair_settings_for_candles(pair: str) -> dict:
    defaults = {
        "signal_mode": "momentum",
        "candles_interval": "1h",
        "mean_window": 16,
        "entry_z_score": 2.0,
        "max_entry_z_score": 3.2,
        "exit_z_score": 0.2,
        "atr_length": 14,
        "atr_stop_multiplier": 3.0,
        "min_stop_loss": 0.004,
        "max_stop_loss": 0.06,
        "momentum_lookback": 16,
        "momentum_threshold": 0.008,
    }
    try:
        cfg = load_config(include_inactive=True)
        match = next((p for p in cfg.get("all_pairs", []) if p["trading_pair"] == pair), None)
        vals = (match or {}).get("values") or cfg.get("shared") or {}
        return {
            "signal_mode": str(vals.get("signal_mode", defaults["signal_mode"])),
            "candles_interval": str(vals.get("candles_interval", defaults["candles_interval"])),
            "mean_window": int(vals.get("mean_window", defaults["mean_window"])),
            "entry_z_score": float(vals.get("entry_z_score", defaults["entry_z_score"])),
            "max_entry_z_score": float(vals.get("max_entry_z_score", defaults["max_entry_z_score"])),
            "exit_z_score": float(vals.get("exit_z_score", defaults["exit_z_score"])),
            "atr_length": int(vals.get("atr_length", defaults["atr_length"])),
            "atr_stop_multiplier": float(vals.get("atr_stop_multiplier", defaults["atr_stop_multiplier"])),
            "min_stop_loss": float(vals.get("min_stop_loss", defaults["min_stop_loss"])),
            "max_stop_loss": float(vals.get("max_stop_loss", defaults["max_stop_loss"])),
            "momentum_lookback": int(vals.get("momentum_lookback", defaults["momentum_lookback"])),
            "momentum_threshold": float(vals.get("momentum_threshold", defaults["momentum_threshold"])),
        }
    except Exception:
        return defaults


def build_candles_view(pair: str, interval: str | None = None, limit: int = 200) -> dict:
    clean_pair = pair.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}-USDT", clean_pair):
        raise ValueError("交易对格式无效")
    settings = _pair_settings_for_candles(clean_pair)
    chosen_interval = (interval or settings["candles_interval"] or "1h").strip()
    if chosen_interval not in OKX_BAR_MAP:
        chosen_interval = "1h"

    candles, source, err = _fetch_okx_candles(clean_pair, chosen_interval, limit=limit)
    mean_line = []
    upper_band = []
    lower_band = []
    z_series = []
    momentum_series = []

    mean_win = max(2, settings["mean_window"])
    entry_z = settings["entry_z_score"]
    atr_len = max(2, settings["atr_length"])
    mom_lb = max(2, settings["momentum_lookback"])

    closes = [c["close"] for c in candles]
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_c = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev_c), abs(c["low"] - prev_c))
        trs.append(tr)

        if i + 1 >= mean_win:
            window = closes[i + 1 - mean_win: i + 1]
            mu = sum(window) / mean_win
            std = statistics.pstdev(window)
            mean_line.append({"time": c["time"], "value": round(mu, 6)})
            upper_band.append({"time": c["time"], "value": round(mu + entry_z * std, 6)})
            lower_band.append({"time": c["time"], "value": round(mu - entry_z * std, 6)})
            z_val = (c["close"] - mu) / std if std > 1e-12 else 0.0
            z_series.append({"time": c["time"], "value": round(z_val, 4)})

        if i >= mom_lb:
            base = closes[i - mom_lb]
            mom_pct = ((c["close"] / base) - 1.0) * 100.0 if base > 0 else 0.0
            momentum_series.append({"time": c["time"], "value": round(mom_pct, 4)})

    latest_atr = sum(trs[-atr_len:]) / len(trs[-atr_len:]) if trs else None
    latest_close = closes[-1] if closes else None
    stop_pct = None
    atr_stop_long = None
    atr_stop_short = None
    if latest_atr is not None and latest_close and latest_close > 0:
        raw_stop = settings["atr_stop_multiplier"] * latest_atr / latest_close
        stop_pct = max(settings["min_stop_loss"], min(settings["max_stop_loss"], raw_stop))
        atr_stop_long = latest_close * (1.0 - stop_pct)
        atr_stop_short = latest_close * (1.0 + stop_pct)

    # Check if an open position exists in status.json for this pair
    status = read_status()
    active_pos = None
    if not status.get("error"):
        for pos in parse_positions(status):
            if pos["pair"] == clean_pair:
                active_pos = pos
                break

    return {
        "pair": clean_pair,
        "interval": chosen_interval,
        "source": source,
        "error": err,
        "candles": candles,
        "overlays": {
            "mean": mean_line,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "z_score": z_series,
            "momentum_pct": momentum_series,
            "atr": latest_atr,
            "stop_pct": stop_pct,
            "atr_stop_long": atr_stop_long,
            "atr_stop_short": atr_stop_short,
        },
        "settings": settings,
        "active_position": active_pos,
    }


# ─── JSON / format_status 解析 ────────────────────────────────────────────────
def parse_controller_signals(status: dict) -> dict[str, dict]:
    """优先从 strategy_data.signals 获取控制器信号，否则从 format_status 解析（支持回归与动量）。"""
    strategy_data = status.get("strategy_data")
    if isinstance(strategy_data, dict) and isinstance(strategy_data.get("signals"), dict):
        controllers = {}
        for ctrl, info in strategy_data["signals"].items():
            if not isinstance(info, dict):
                continue
            z_val = None
            raw_z = info.get("z_score")
            if raw_z not in (None, "n/a", ""):
                try:
                    z_val = float(raw_z)
                except (ValueError, TypeError):
                    pass
            mom_val = None
            raw_mom = info.get("momentum")
            if raw_mom not in (None, "n/a", ""):
                try:
                    mom_val = float(raw_mom)
                except (ValueError, TypeError):
                    pass
            rsi9_val = None
            raw_rsi = info.get("rsi9")
            if raw_rsi not in (None, "n/a", ""):
                try:
                    rsi9_val = float(raw_rsi)
                except (ValueError, TypeError):
                    pass
            er4h_val = None
            raw_er = info.get("er4h")
            if raw_er not in (None, "n/a", ""):
                try:
                    er4h_val = float(raw_er)
                except (ValueError, TypeError):
                    pass
            actual_sig = 0
            if "signal" in info:
                try:
                    actual_sig = int(info["signal"])
                except (ValueError, TypeError):
                    pass
            controllers[ctrl] = {
                "z": z_val,
                "momentum": mom_val,
                "rsi9": rsi9_val,
                "er4h": er4h_val,
                "strategy": str(info.get("strategy", "")),
                "reason": str(info.get("reason", "")),
                "signal": actual_sig,
            }
        if controllers:
            return controllers

    text = status.get("format_status") or ""
    controllers = {}
    current_ctrl = None
    for line in text.splitlines():
        ctrl_m = re.match(r"Controller:\s*(\S+)", line)
        if ctrl_m:
            current_ctrl = ctrl_m.group(1)
            continue
        if current_ctrl and ("Mean reversion |" in line or "Momentum |" in line or "Human swing |" in line):
            parts = [p.strip() for p in line.split("|")]
            z_val = None
            mom_val = None
            rsi9_val = None
            er4h_val = None
            reason = ""
            strat = (
                "human_swing" if "Human swing |" in line
                else ("momentum" if "Momentum |" in line else "reversion")
            )
            for p in parts:
                if p.startswith("z="):
                    try:
                        z_val = float(p.replace("z=", "").strip())
                    except ValueError:
                        z_val = None
                elif p.startswith("move="):
                    try:
                        mom_val = float(p.replace("move=", "").strip())
                    except ValueError:
                        mom_val = None
                elif p.startswith("rsi9="):
                    try:
                        rsi9_val = float(p.replace("rsi9=", "").strip())
                    except ValueError:
                        rsi9_val = None
                elif p.startswith("er4h="):
                    try:
                        er4h_val = float(p.replace("er4h=", "").strip())
                    except ValueError:
                        er4h_val = None
                elif p not in ("Mean reversion", "Momentum", "Human swing"):
                    reason = p
            controllers[current_ctrl] = {
                "z": z_val,
                "momentum": mom_val,
                "rsi9": rsi9_val,
                "er4h": er4h_val,
                "strategy": strat,
                "reason": reason,
                "signal": 0,
            }
            current_ctrl = None
    return controllers


def parse_performance_table(status: dict) -> list[dict]:
    strategy_data = status.get("strategy_data")
    if isinstance(strategy_data, dict) and isinstance(strategy_data.get("performance"), list):
        rows = []
        for item in strategy_data["performance"]:
            if not isinstance(item, dict):
                continue
            rows.append({
                "controller": str(item.get("controller", "")),
                "realized": float(item.get("realized", 0.0)),
                "unrealized": float(item.get("unrealized", 0.0)),
                "global": float(item.get("global", 0.0)),
                "global_pct": str(item.get("global_pct", "0.00%")),
                "volume": float(item.get("volume", 0.0)),
            })
        if rows:
            return rows

    text = status.get("format_status") or ""
    rows = []
    in_perf = False
    for line in text.splitlines():
        if "PERFORMANCE SUMMARY" in line:
            in_perf = True
            continue
        if in_perf:
            m = re.match(
                r"\|\s*(.+?)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+%)\s*\|\s*([\d.]+)\s*\|",
                line,
            )
            if m:
                rows.append({
                    "controller": m.group(1).strip(),
                    "realized": float(m.group(2)),
                    "unrealized": float(m.group(3)),
                    "global": float(m.group(4)),
                    "global_pct": m.group(5).strip(),
                    "volume": float(m.group(6)),
                })
    return rows


def parse_positions(status: dict) -> list[dict]:
    acc_pos_raw = status.get("account_positions")
    strat_pos_raw = (status.get("strategy_data") or {}).get("positions_held") if isinstance(status.get("strategy_data"), dict) else None

    if acc_pos_raw is not None or strat_pos_raw is not None:
        positions = []
        seen_pairs = set()
        held_by_pair = {}
        if isinstance(strat_pos_raw, list):
            for p in strat_pos_raw:
                if not isinstance(p, dict):
                    continue
                pair = str(p.get("pair") or p.get("trading_pair", "")).upper()
                side = str(p.get("side", "")).upper()
                side = "BUY" if side in ("BUY", "LONG") else "SELL"
                amount = float(p.get("amount", 0.0))
                breakeven = float(p.get("breakeven", 0.0) or p.get("breakeven_price", 0.0))
                held_by_pair[pair] = {
                    "connector": str(p.get("connector", "okx_perpetual")),
                    "pair": pair,
                    "symbol": pair.split("-")[0],
                    "side": side,
                    "amount": amount,
                    "value": float(p.get("value", 0.0) or (amount * breakeven)),
                    "breakeven": breakeven,
                    "unrealized": float(p.get("unrealized", 0.0) or p.get("unrealized_pnl_quote", 0.0)),
                    "realized": float(p.get("realized", 0.0) or p.get("realized_pnl_quote", 0.0)),
                    "fees": float(p.get("fees", 0.0) or p.get("cum_fees_quote", 0.0)),
                    "leverage": float(p.get("leverage", 0.0) or 0.0),
                    "controller_id": str(p.get("controller_id", "")),
                    "source": "策略接管",
                }

        def _merge_acc_pos(conn_name: str, pos: dict) -> None:
            amount = float(pos.get("amount", 0.0))
            if amount == 0.0:
                return
            pair = str(pos.get("trading_pair") or pos.get("pair", "")).upper()
            side = str(pos.get("side", "")).upper()
            side = "BUY" if side in ("BUY", "LONG") else "SELL"
            entry_px = float(pos.get("entry_price", 0.0))
            upl = float(pos.get("unrealized_pnl", 0.0))
            lev = float(pos.get("leverage", 0.0) or 0.0)
            seen_pairs.add(pair)

            if pair in held_by_pair:
                item = held_by_pair[pair]
                item["amount"] = amount
                item["unrealized"] = upl
                item["value"] = amount * (entry_px or item["breakeven"])
                if entry_px > 0:
                    item["breakeven"] = entry_px
                if lev > 0:
                    item["leverage"] = lev
                item["source"] = "OKX 账户仓位"
                positions.append(item)
            else:
                positions.append({
                    "connector": conn_name,
                    "pair": pair,
                    "symbol": pair.split("-")[0],
                    "side": side,
                    "amount": amount,
                    "value": amount * entry_px,
                    "breakeven": entry_px,
                    "unrealized": upl,
                    "realized": 0.0,
                    "fees": 0.0,
                    "leverage": lev,
                    "controller_id": f"okx_pmm_{pair.split('-')[0].lower()}",
                    "source": "OKX 账户仓位",
                })

        if isinstance(acc_pos_raw, dict):
            for conn_name, pos_list in acc_pos_raw.items():
                if isinstance(pos_list, list):
                    for pos in pos_list:
                        if isinstance(pos, dict):
                            _merge_acc_pos(conn_name, pos)
        elif isinstance(acc_pos_raw, list):
            for pos in acc_pos_raw:
                if isinstance(pos, dict):
                    _merge_acc_pos(str(pos.get("connector", "okx_perpetual")), pos)

        for pair, item in held_by_pair.items():
            if pair not in seen_pairs:
                positions.append(item)
        return positions

    text = status.get("format_status") or ""
    positions = []
    for line in text.splitlines():
        m = re.match(
            r"\|\s*(\S+)\s*\|\s*(\S+-\S+)\s*\|\s*(BUY|SELL)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([\d.]+)\s*\|",
            line,
        )
        if m:
            pair = m.group(2).upper()
            positions.append({
                "connector": m.group(1),
                "pair": pair,
                "symbol": pair.split("-")[0],
                "side": m.group(3),
                "amount": float(m.group(4)),
                "value": float(m.group(5)),
                "breakeven": float(m.group(6)),
                "unrealized": float(m.group(7)),
                "realized": float(m.group(8)),
                "fees": float(m.group(9)),
                "leverage": 0.0,
                "controller_id": f"okx_pmm_{pair.split('-')[0].lower()}",
                "source": "策略接管",
            })
    return positions


def parse_orders(status: dict) -> list[dict]:
    strategy_data = status.get("strategy_data")
    if isinstance(strategy_data, dict) and isinstance(strategy_data.get("orders"), list):
        orders = []
        for o in strategy_data["orders"]:
            if not isinstance(o, dict):
                continue
            age_val = o.get("age", 0)
            if isinstance(age_val, (int, float)):
                age_str = f"{int(age_val) // 3600:02d}:{(int(age_val) % 3600) // 60:02d}:{int(age_val) % 60:02d}"
            else:
                age_str = str(age_val)
            pair = str(o.get("pair", ""))
            orders.append({
                "connector": str(o.get("connector", "")),
                "pair": pair,
                "symbol": pair.split("-")[0] if "-" in pair else pair,
                "side": str(o.get("side", "")).upper(),
                "price": float(o.get("price", 0.0)),
                "amount": float(o.get("amount", 0.0)),
                "age": age_str,
            })
        return orders

    text = status.get("format_status") or ""
    orders = []
    in_orders = False
    for line in text.splitlines():
        if line.strip().startswith("Orders:"):
            in_orders = True
            continue
        if in_orders:
            m = re.match(
                r"\s{2,}(\S+)\s+(\S+-\S+)\s+(buy|sell)\s+([\d.]+)\s+([\d.]+)\s+(\S+)",
                line,
                re.IGNORECASE,
            )
            if m:
                pair = m.group(2)
                orders.append({
                    "connector": m.group(1),
                    "pair": pair,
                    "symbol": pair.split("-")[0] if "-" in pair else pair,
                    "side": m.group(3).upper(),
                    "price": float(m.group(4)),
                    "amount": float(m.group(5)),
                    "age": m.group(6),
                })
            elif line.strip() == "" or line.strip().startswith("==="):
                in_orders = False
    return orders


def parse_executors(status: dict) -> list[dict]:
    strategy_data = status.get("strategy_data")
    if isinstance(strategy_data, dict) and isinstance(strategy_data.get("executors"), list):
        executors = []
        for e in strategy_data["executors"]:
            if not isinstance(e, dict):
                continue
            ctrl = str(e.get("controller", ""))
            sym = ctrl.split("_")[-1].upper() if ctrl else ""
            executors.append({
                "controller": ctrl,
                "symbol": sym,
                "pair": f"{sym}-USDT" if sym else "",
                "side": str(e.get("side", "")).replace("TradeType.", ""),
                "status": str(e.get("status", "")).replace("RunnableStatus.", ""),
                "net_pnl_pct": float(e.get("net_pnl_pct", 0.0)),
                "net_pnl": float(e.get("net_pnl", 0.0)),
                "volume": float(e.get("volume", 0.0)),
                "is_trading": bool(e.get("is_trading", False)),
                "close_type": str(e.get("close_type", "") or "").replace("CloseType.", ""),
                "age": float(e.get("age", 0.0)),
            })
        return executors

    text = status.get("format_status") or ""
    executors = []
    current_ctrl = None
    for line in text.splitlines():
        ctrl_m = re.match(r"Controller:\s*(\S+)", line)
        if ctrl_m:
            current_ctrl = ctrl_m.group(1)
        exec_m = re.match(
            r"\|\s*(position_executor)\s*\|\s*(TradeType\.\S+)\s*\|\s*(RunnableStatus\.\S+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([\d.]+)\s*\|\s*(True|False)\s*\|\s*([\w.]*)\s*\|\s*([\d.]+)\s*\|",
            line,
        )
        if exec_m and current_ctrl:
            sym = current_ctrl.split("_")[-1].upper()
            executors.append({
                "controller": current_ctrl,
                "symbol": sym,
                "pair": f"{sym}-USDT",
                "side": exec_m.group(2).replace("TradeType.", ""),
                "status": exec_m.group(3).replace("RunnableStatus.", ""),
                "net_pnl_pct": float(exec_m.group(4)),
                "net_pnl": float(exec_m.group(5)),
                "volume": float(exec_m.group(6)),
                "is_trading": exec_m.group(7) == "True",
                "close_type": exec_m.group(8).replace("CloseType.", "") if exec_m.group(8) else "",
                "age": float(exec_m.group(9)),
            })
    return executors


def parse_warnings(status: dict) -> list[str]:
    text = status.get("format_status") or ""
    warnings = []
    in_warn = False
    for line in text.splitlines():
        if "*** WARNINGS ***" in line:
            in_warn = True
            continue
        if in_warn:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("==="):
                break
            warnings.append(stripped)
    return warnings


# ─── 手动平仓与会话基线 ──────────────────────────────────────────────────────
def load_manual_closes() -> list[dict]:
    try:
        data = json.loads(MANUAL_CLOSES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("closes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    closes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            closes.append({
                "pair": str(row["pair"]).upper(),
                "side": str(row["side"]).upper(),
                "amount": float(row["amount"]),
                "breakeven": float(row["breakeven"]),
                "pnl": float(row["pnl"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return closes


def save_manual_closes(closes: list[dict]) -> None:
    MANUAL_CLOSES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"closes": closes}
    MANUAL_CLOSES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def known_controllers() -> set[str]:
    names = set()
    status = read_status()
    if not status.get("error"):
        names.update(
            row["controller"] for row in parse_performance_table(status)
            if row["controller"] != "GLOBAL TOTAL"
        )
    conf_dir = BASE_DIR / "conf" / "controllers"
    if conf_dir.is_dir():
        for path in conf_dir.glob("*.yml"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(r"(?m)^id:\s*([A-Za-z0-9_]+)\s*$", text)
            if match:
                names.add(match.group(1))
    return names


def request_position_close(controller: str) -> str:
    """Ask the running strategy to market-close this controller on its next tick."""
    controller = controller.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", controller):
        raise ValueError("控制器无效")
    if controller not in known_controllers():
        raise ValueError("没有这个控制器")
    request_dir = BASE_DIR / "data" / "dashboard" / "close_requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / controller).write_text(f"{time.time()}\n", encoding="utf-8")
    return f"{controller} 平仓请求已发送。策略运行时会在下一轮市价平仓，并撤销未成交开仓单。"


def _same_closed_position(close: dict, position: dict) -> bool:
    return (
        close["pair"] == str(position["pair"]).upper()
        and close["side"] == str(position["side"]).upper()
        and abs(close["amount"] - float(position["amount"])) < 1e-6
        and abs(close["breakeven"] - float(position["breakeven"])) < 5e-4
    )


def mark_manual_close(pair: str, side: str, amount: float, breakeven: float, pnl: float) -> list[dict]:
    pair = pair.strip().upper()
    side = side.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", pair):
        raise ValueError("交易对无效")
    if side not in ("BUY", "SELL"):
        raise ValueError("方向无效")
    amount = float(amount)
    breakeven = float(breakeven)
    pnl = float(pnl)
    if not all(map(math.isfinite, (amount, breakeven, pnl))):
        raise ValueError("盈亏金额无效")
    closes = [
        row for row in load_manual_closes()
        if not _same_closed_position(row, {"pair": pair, "side": side, "amount": amount, "breakeven": breakeven})
    ]
    closes.append({
        "pair": pair,
        "side": side,
        "amount": amount,
        "breakeven": breakeven,
        "pnl": pnl,
    })
    save_manual_closes(closes)
    return closes


def remove_manual_close(pair: str, side: str) -> list[dict]:
    pair = pair.strip().upper()
    side = side.strip().upper()
    closes = [
        row for row in load_manual_closes()
        if not (row["pair"] == pair and row["side"] == side)
    ]
    save_manual_closes(closes)
    return closes


def _pnl_pct(global_pnl: float, volume: float) -> str:
    if not volume:
        return "0.00%"
    return f"{global_pnl / volume * 100:.2f}%"


def apply_manual_closes(positions: list[dict], perf_rows: list[dict]) -> tuple[list[dict], list[dict], str]:
    closes = load_manual_closes()
    if not closes:
        return positions, perf_rows, ""

    visible = []
    removed_by_symbol: dict[str, float] = {}
    for position in positions:
        match = next((row for row in closes if _same_closed_position(row, position)), None)
        if match is None:
            visible.append(position)
            continue
        symbol = position["pair"].split("-", 1)[0].upper()
        removed_by_symbol[symbol] = removed_by_symbol.get(symbol, 0.0) + position["unrealized"]

    credited_by_symbol: dict[str, float] = {}
    for close in closes:
        symbol = close["pair"].split("-", 1)[0]
        credited_by_symbol[symbol] = credited_by_symbol.get(symbol, 0.0) + close["pnl"]
    removed_total = sum(removed_by_symbol.values())
    credited_total = sum(credited_by_symbol.values())
    for row in perf_rows:
        controller = row["controller"]
        if controller == "GLOBAL TOTAL":
            removed, credited = removed_total, credited_total
        else:
            symbol = controller.split("_")[-1].upper()
            removed = removed_by_symbol.get(symbol, 0.0)
            credited = credited_by_symbol.get(symbol, 0.0)
        if not removed and not credited:
            continue
        row["unrealized"] -= removed
        row["realized"] += credited
        row["global"] += credited - removed
        row["global_pct"] = _pnl_pct(row["global"], row["volume"])
    parts = [f"{row['pair']} {row['pnl']:+.4f}" for row in closes]
    return visible, perf_rows, "已手动平仓：" + "、".join(parts)


def _uptime_s(engine: dict) -> float:
    raw = float(engine.get("uptime", 0) or 0)
    start_time = float(engine.get("start_time", 0) or 0)
    if start_time > 1e11:
        return max(0.0, time.time() - start_time / 1000.0)
    if start_time > 1e8:
        return max(0.0, time.time() - start_time)
    return max(0.0, raw / 1000.0)


def fmt_uptime(uptime_s: float) -> str:
    uptime_s = max(0, int(uptime_s))
    return f"{uptime_s // 3600:02d}h {(uptime_s % 3600) // 60:02d}m {uptime_s % 60:02d}s"


_session_lock = threading.Lock()
_session_data = {
    "session_key": None,
    "baseline_assets": None,
    "baseline_realized_by_ctrl": {},
    "history": [],
}


def _load_session_baseline() -> dict | None:
    try:
        if SESSION_BASELINE_FILE.is_file():
            with open(SESSION_BASELINE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "session_key" in data and "baseline_realized_by_ctrl" in data:
                    return data
    except Exception as exc:
        _log(f"无法读取 session_baseline.json: {exc}")
    return None


def _save_session_baseline(data: dict) -> None:
    try:
        SESSION_BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = SESSION_BASELINE_FILE.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_file.replace(SESSION_BASELINE_FILE)
    except Exception as exc:
        _log(f"无法保存 session_baseline.json: {exc}")


def reset_session_baseline() -> None:
    with _session_lock:
        _session_data["session_key"] = None
        _session_data["baseline_assets"] = None
        _session_data["baseline_realized_by_ctrl"] = {}
        _session_data["history"] = []
        try:
            if SESSION_BASELINE_FILE.is_file():
                SESSION_BASELINE_FILE.unlink()
            if MANUAL_CLOSES_FILE.is_file():
                MANUAL_CLOSES_FILE.unlink()
        except OSError:
            pass


def _sync_session_metrics(
    engine: dict,
    updated_at: float,
    perf_rows: list[dict],
    usdt_bal: float,
    bot_pid: int | None = None,
) -> tuple[float, float, float, str, list[dict]]:
    raw_start = engine.get("start_time") or 0
    if raw_start:
        session_key = str(raw_start)
    elif bot_pid:
        session_key = f"pid_{bot_pid}"
    else:
        session_key = str(engine.get("uptime") or 0)

    with _session_lock:
        if _session_data["session_key"] is None:
            persisted = _load_session_baseline()
            if (
                persisted
                and persisted.get("session_key") == session_key
                and persisted.get("baseline_realized_by_ctrl")
            ):
                _session_data["session_key"] = persisted["session_key"]
                _session_data["baseline_assets"] = persisted.get("baseline_assets")
                _session_data["baseline_realized_by_ctrl"] = dict(persisted.get("baseline_realized_by_ctrl", {}))
                _session_data["history"] = list(persisted.get("history", []))

        if _session_data["session_key"] != session_key or not _session_data["baseline_realized_by_ctrl"]:
            _session_data["session_key"] = session_key
            _session_data["baseline_assets"] = float(usdt_bal)
            _session_data["baseline_realized_by_ctrl"] = {}
            for r in perf_rows:
                _session_data["baseline_realized_by_ctrl"][r["controller"]] = float(r["realized"])
            _session_data["history"] = [
                {"t": int(updated_at - 60) if updated_at else int(time.time() - 60), "bot_pnl": 0.0, "assets": round(float(usdt_bal), 2)},
                {"t": int(updated_at) if updated_at else int(time.time()), "bot_pnl": 0.0, "assets": round(float(usdt_bal), 2)},
            ]
            if _session_data["baseline_realized_by_ctrl"]:
                _save_session_baseline(_session_data)
        else:
            changed = False
            for r in perf_rows:
                if r["controller"] not in _session_data["baseline_realized_by_ctrl"]:
                    _session_data["baseline_realized_by_ctrl"][r["controller"]] = float(r["realized"])
                    changed = True
            if changed:
                _save_session_baseline(_session_data)

        base_map = dict(_session_data["baseline_realized_by_ctrl"])

    ctrl_rows = [r for r in perf_rows if r["controller"] != "GLOBAL TOTAL"]
    for r in ctrl_rows:
        ctrl = r["controller"]
        base_r = base_map.get(ctrl, 0.0)
        r["realized"] = round(r["realized"] - base_r, 4)
        r["global"] = round(r["realized"] + r["unrealized"], 4)
        r["global_pct"] = _pnl_pct(r["global"], r["volume"])

    global_realized = round(sum(r["realized"] for r in ctrl_rows), 4)
    global_unrealized = round(sum(r["unrealized"] for r in ctrl_rows), 4)
    global_pnl = round(global_realized + global_unrealized, 4)
    total_volume = sum(r["volume"] for r in ctrl_rows)
    glob_pct = _pnl_pct(global_pnl, total_volume)

    global_row = next((r for r in perf_rows if r["controller"] == "GLOBAL TOTAL"), None)
    if global_row:
        global_row["realized"] = global_realized
        global_row["unrealized"] = global_unrealized
        global_row["global"] = global_pnl
        global_row["global_pct"] = glob_pct
        global_row["volume"] = total_volume

    with _session_lock:
        if updated_at > 0:
            hist = _session_data["history"]
            if not hist or (updated_at - hist[-1]["t"] >= 2.0) or hist[-1]["bot_pnl"] != global_pnl or hist[-1]["assets"] != round(float(usdt_bal), 2):
                hist.append({"t": int(updated_at), "bot_pnl": global_pnl, "assets": round(float(usdt_bal), 2)})
                if len(hist) > 200:
                    hist.pop(0)
                if _session_data["session_key"] is not None:
                    _save_session_baseline(_session_data)
        session_history = list(_session_data["history"])

    return global_realized, global_unrealized, global_pnl, glob_pct, session_history


def _controller_config_index() -> dict[str, dict]:
    """Map controller id -> active config summary without raising."""
    mapping: dict[str, dict] = {}
    try:
        cfg = load_config(include_inactive=True)
        for pair_item in cfg.get("all_pairs", []):
            vals = pair_item.get("values") or {}
            mapping[pair_item["id"]] = {
                "pair": pair_item["trading_pair"],
                "symbol": pair_item["trading_pair"].split("-")[0],
                "enabled": pair_item["enabled"],
                "leverage": int(vals.get("leverage", 3)),
                "total_amount_quote": float(vals.get("total_amount_quote", 0)),
                "signal_mode": vals.get("signal_mode", "momentum"),
                "candles_interval": vals.get("candles_interval", "1h"),
                "entry_z_score": float(vals.get("entry_z_score", 2.0)),
                "max_entry_z_score": float(vals.get("max_entry_z_score", 3.2)),
                "momentum_threshold": float(vals.get("momentum_threshold", 0.008)),
                "long_only": str(vals.get("long_only", "false")).lower() == "true",
                "manual_kill_switch": str(vals.get("manual_kill_switch", "false")).lower() == "true",
            }
    except Exception:
        pass
    return mapping


def build_view(status: dict) -> dict:
    """将 status.json 与控制器配置聚合成干净的结构化 JSON 视图。"""
    strategy_info = get_strategy_info()
    ctrl_configs = _controller_config_index()
    log_entries = parse_log_entries(tail_log(120))
    ctrl_state = control_status()

    err = status.get("error")
    if err:
        fallback_rows = []
        for cid, cinfo in ctrl_configs.items():
            if not cinfo.get("enabled"):
                continue
            fallback_rows.append({
                "controller": cid,
                "pair": cinfo["pair"],
                "symbol": cinfo["symbol"],
                "realized": 0.0,
                "unrealized": 0.0,
                "global": 0.0,
                "global_pct": "0.00%",
                "volume": 0.0,
                "z_score": None,
                "momentum": None,
                "rsi9": None,
                "er4h": None,
                "signal": 0,
                "reason": "engine_offline",
                **{k: cinfo[k] for k in (
                    "signal_mode", "leverage", "total_amount_quote",
                    "candles_interval", "entry_z_score", "max_entry_z_score",
                    "momentum_threshold", "long_only", "manual_kill_switch",
                )},
            })
        return {
            "error": str(err),
            "updated_at": 0,
            "updated_str": "-",
            "snapshot_age_s": None,
            "uptime_s": 0,
            "uptime_str": "-",
            "running": False,
            "stale": True,
            "strategy": "-",
            "strategy_info": strategy_info,
            "side_mode": current_side_mode(),
            "control": ctrl_state,
            "usdt_bal": 0.0,
            "usdt_bal_str": "0.00",
            "balances": {},
            "realized": 0.0,
            "unrealized": 0.0,
            "global_pnl": 0.0,
            "global_pct": "0.00%",
            "total_volume": 0.0,
            "session_peak": 0.0,
            "session_min": 0.0,
            "history": [],
            "perf_rows": fallback_rows,
            "positions": [],
            "orders": [],
            "executors": [],
            "signals": {},
            "manual_closes": load_manual_closes(),
            "manual_close_note": "",
            "warnings": [],
            "logs": log_entries,
        }

    engine = status.get("engine", {}) or {}
    raw_balances = (status.get("balances", {}) or {}).get("okx_perpetual", {}) or {}
    balances = {k: float(v or 0) for k, v in raw_balances.items()}
    perf_rows = parse_performance_table(status)
    raw_positions = parse_positions(status)
    positions, perf_rows, manual_close_note = apply_manual_closes(raw_positions, perf_rows)
    orders = parse_orders(status)
    executors = parse_executors(status)
    signals = parse_controller_signals(status)
    warnings = parse_warnings(status)

    updated_at = float(status.get("updated_at", 0) or 0)
    uptime_s = _uptime_s(engine)
    running = bool(engine.get("strategy_running", False))
    snapshot_age = (time.time() - updated_at) if updated_at else None
    stale = snapshot_age is None or snapshot_age > STALE_AFTER_S
    updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at)) if updated_at else "-"

    usdt_bal = float(balances.get("USDT", 0.0) or 0.0)
    realized, unrealized, global_pnl, glob_pct, session_history = _sync_session_metrics(
        engine, updated_at, perf_rows, usdt_bal, bot_pid=status.get("pid")
    )

    enriched_perf = []
    for row in perf_rows:
        ctrl = row["controller"]
        if ctrl == "GLOBAL TOTAL":
            continue
        sig = signals.get(ctrl) or {}
        cinfo = ctrl_configs.get(ctrl) or {}
        symbol = cinfo.get("symbol") or ctrl.split("_")[-1].upper()
        pair = cinfo.get("pair") or f"{symbol}-USDT"
        enriched_perf.append({
            **row,
            "pair": pair,
            "symbol": symbol,
            "z_score": sig.get("z"),
            "momentum": sig.get("momentum"),
            "rsi9": sig.get("rsi9"),
            "er4h": sig.get("er4h"),
            "signal": sig.get("signal", 0),
            "reason": sig.get("reason", ""),
            "signal_mode": sig.get("strategy") or cinfo.get("signal_mode", "momentum"),
            "leverage": cinfo.get("leverage", 3),
            "total_amount_quote": cinfo.get("total_amount_quote", 0),
            "candles_interval": cinfo.get("candles_interval", strategy_info.get("interval", "1h")),
            "entry_z_score": cinfo.get("entry_z_score", 2.0),
            "max_entry_z_score": cinfo.get("max_entry_z_score", 3.2),
            "momentum_threshold": cinfo.get("momentum_threshold", 0.008),
            "long_only": cinfo.get("long_only", False),
            "manual_kill_switch": cinfo.get("manual_kill_switch", False),
        })

    # Include active configured controllers not yet present in status performance
    seen_ctrls = {r["controller"] for r in enriched_perf}
    for cid, cinfo in ctrl_configs.items():
        if cinfo.get("enabled") and cid not in seen_ctrls:
            sig = signals.get(cid) or {}
            enriched_perf.append({
                "controller": cid,
                "pair": cinfo["pair"],
                "symbol": cinfo["symbol"],
                "realized": 0.0,
                "unrealized": 0.0,
                "global": 0.0,
                "global_pct": "0.00%",
                "volume": 0.0,
                "z_score": sig.get("z"),
                "momentum": sig.get("momentum"),
                "rsi9": sig.get("rsi9"),
                "er4h": sig.get("er4h"),
                "signal": sig.get("signal", 0),
                "reason": sig.get("reason", "waiting"),
                "signal_mode": cinfo["signal_mode"],
                "leverage": cinfo["leverage"],
                "total_amount_quote": cinfo["total_amount_quote"],
                "candles_interval": cinfo["candles_interval"],
                "entry_z_score": cinfo["entry_z_score"],
                "max_entry_z_score": cinfo["max_entry_z_score"],
                "momentum_threshold": cinfo["momentum_threshold"],
                "long_only": cinfo["long_only"],
                "manual_kill_switch": cinfo["manual_kill_switch"],
            })

    total_volume = sum(r["volume"] for r in enriched_perf)
    pnl_points = [h["bot_pnl"] for h in session_history] if session_history else [global_pnl]
    session_peak = max(pnl_points)
    session_min = min(pnl_points)

    return {
        "error": None,
        "updated_at": updated_at,
        "updated_str": updated_str,
        "snapshot_age_s": round(snapshot_age, 1) if snapshot_age is not None else None,
        "uptime_s": uptime_s,
        "uptime_str": fmt_uptime(uptime_s) if uptime_s else "-",
        "running": running,
        "stale": stale,
        "strategy": engine.get("strategy_file_name", "-") or "-",
        "strategy_info": strategy_info,
        "side_mode": current_side_mode(),
        "control": ctrl_state,
        "usdt_bal": round(usdt_bal, 4),
        "usdt_bal_str": f"{usdt_bal:,.2f}",
        "balances": balances,
        "realized": realized,
        "unrealized": unrealized,
        "global_pnl": global_pnl,
        "global_pct": glob_pct,
        "total_volume": round(total_volume, 2),
        "session_peak": round(session_peak, 4),
        "session_min": round(session_min, 4),
        "history": session_history,
        "perf_rows": enriched_perf,
        "positions": positions,
        "orders": orders,
        "executors": executors,
        "signals": signals,
        "manual_closes": load_manual_closes(),
        "manual_close_note": manual_close_note,
        "warnings": warnings,
        "logs": log_entries,
    }


def config_view() -> dict:
    return load_config(include_inactive=True)


def save_config(payload: dict) -> dict:
    with _config_lock:
        return apply_config(payload)


def markets_view(status: dict) -> dict:
    perf = [] if status.get("error") else parse_performance_table(status)
    pairs = strategy_pairs(status, perf)
    return build_markets_data(pairs)


# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, content_type: str, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _require_local_post(self, action_label: str, max_bytes: int = 256_000) -> dict | None:
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self._send_json(403, {"ok": False, "message": f"只允许从本机{action_label}。"})
            return None
        if (self.headers.get("X-Requested-With") or "").strip().lower() != "okx-dashboard":
            self._send_json(403, {"ok": False, "message": "请求校验失败。"})
            return None
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > max_bytes:
            self._send_json(413, {"ok": False, "message": "请求过大"})
            return None
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "message": "请求不是 JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "message": "请求体必须是 JSON 对象"})
            return None
        return payload

    def _serve_spa(self, rel_path: str) -> bool:
        dist = frontend_dist_dir()
        if not dist.is_dir():
            return False
        dist_resolved = dist.resolve()
        clean = rel_path.lstrip("/")
        if clean and "." in Path(clean).name:
            candidate = (dist / clean).resolve()
            if candidate.is_relative_to(dist_resolved) and candidate.is_file():
                ctype, _ = mimetypes.guess_type(str(candidate))
                cache = "public, max-age=31536000, immutable" if clean.startswith("assets/") else "no-store"
                self._send(200, candidate.read_bytes(), ctype or "application/octet-stream", cache_control=cache)
                return True
            return False

        index_file = dist / "index.html"
        if index_file.is_file():
            self._send(200, index_file.read_bytes(), "text/html; charset=utf-8")
            return True
        return False

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/api/config":
                try:
                    self._send_json(200, config_view())
                except ConfigError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                return

            if path == "/api/presets":
                self._send_json(200, get_presets_info())
                return

            if path == "/api/status":
                self._send_json(200, read_status())
                return

            if path == "/api/view":
                self._send_json(200, build_view(read_status()))
                return

            if path == "/api/markets":
                self._send_json(200, markets_view(read_status()))
                return

            if path == "/api/candles":
                pair = (qs.get("pair") or ["BTC-USDT"])[0]
                interval = (qs.get("interval") or [""])[0] or None
                try:
                    limit = int((qs.get("limit") or ["200"])[0])
                except ValueError:
                    limit = 200
                try:
                    self._send_json(200, build_candles_view(pair, interval=interval, limit=min(max(limit, 30), 300)))
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                return

            if path == "/api/logs":
                try:
                    n = min(max(int((qs.get("lines") or ["250"])[0]), 20), 1000)
                except ValueError:
                    n = 250
                lines = tail_log(n)
                self._send_json(200, {"lines": parse_log_entries(lines), "total": len(lines)})
                return

            if path == "/api/control-log":
                action = (qs.get("action") or ["start"])[0]
                if action not in ("start", "stop"):
                    self._send_json(400, {"ok": False, "message": "无效 action"})
                    return
                lines = tail_file(BASE_DIR / "dashboard" / f"{action}.log", 80)
                self._send_json(200, {"action": action, "lines": lines, "control": control_status()})
                return

            if self._serve_spa(path):
                return

            if path in ("/", "/index.html", "/overview", "/markets", "/positions", "/config", "/logs"):
                fallback = (
                    "<!doctype html><html><body style='background:#0B0E14;color:#E5E7EB;font-family:sans-serif;padding:40px;'>"
                    "<h2>Frontend build missing</h2>"
                    "<p>Run <code>cd dashboard/frontend && npm install && npm run build</code> to generate <code>dashboard/dist</code>.</p>"
                    "</body></html>"
                ).encode("utf-8")
                self._send(503, fallback, "text/html; charset=utf-8")
                return

            self.send_response(404)
            self.end_headers()
        except BrokenPipeError:
            return
        except Exception as exc:
            _log(f"request {self.path} failed: {type(exc).__name__}: {exc}")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"

            if path in ("/api/restart", "/api/stop"):
                payload = self._require_local_post("控制策略", max_bytes=4_000)
                if payload is None:
                    return
                control = restart_strategy if path == "/api/restart" else stop_strategy
                started, message = control()
                self._send_json(202 if started else 409, {"ok": started, "message": message})
                return

            if path == "/api/side-mode":
                payload = self._require_local_post("控制策略", max_bytes=4_000)
                if payload is None:
                    return
                if not isinstance(payload.get("long_only"), bool):
                    self._send_json(400, {"ok": False, "message": "需要 long_only 布尔值"})
                    return
                try:
                    started, message, mode = switch_side_mode(payload["long_only"])
                except ConfigError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                self._send_json(202 if started else 409, {"ok": started, "message": message, "side_mode": mode})
                return

            if path == "/api/close-position":
                payload = self._require_local_post("平仓", max_bytes=4_000)
                if payload is None:
                    return
                try:
                    message = request_position_close(str(payload.get("controller") or ""))
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                self._send_json(200, {"ok": True, "message": message})
                return

            if path == "/api/manual-close":
                payload = self._require_local_post("标记手动平仓", max_bytes=8_000)
                if payload is None:
                    return
                try:
                    pair = str(payload.get("pair") or "")
                    closes = mark_manual_close(
                        pair,
                        str(payload.get("side") or ""),
                        payload.get("amount"),
                        payload.get("breakeven"),
                        payload.get("pnl"),
                    )
                except (ValueError, TypeError) as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                self._send_json(200, {
                    "ok": True,
                    "closes": closes,
                    "message": f"{pair.strip().upper()} 已记入手动平仓盈亏。",
                })
                return

            if path == "/api/manual-close/delete":
                payload = self._require_local_post("撤销手动平仓记录", max_bytes=4_000)
                if payload is None:
                    return
                closes = remove_manual_close(str(payload.get("pair") or ""), str(payload.get("side") or ""))
                self._send_json(200, {"ok": True, "closes": closes, "message": "已移除手动平仓记录。"})
                return

            if path == "/api/config/add-pair":
                payload = self._require_local_post("添加交易对", max_bytes=16_000)
                if payload is None:
                    return
                try:
                    with _config_lock:
                        created = add_pair_config(
                            trading_pair=str(payload.get("trading_pair") or ""),
                            leverage=payload.get("leverage", 3),
                            total_amount_quote=payload.get("total_amount_quote", 50),
                            enable=bool(payload.get("enable", True)),
                        )
                except ConfigError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                self._send_json(200, {
                    "ok": True,
                    "created": created,
                    "message": f"已新增 {created['trading_pair']} ({created['file']})。",
                })
                return

            if path in ("/api/config", "/api/config/save-and-restart"):
                payload = self._require_local_post("修改配置", max_bytes=512_000)
                if payload is None:
                    return
                try:
                    written = save_config(payload)
                except ConfigError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return

                if path == "/api/config/save-and-restart":
                    started, restart_msg = restart_strategy()
                    self._send_json(200 if started else 409, {
                        "ok": started,
                        "saved": True,
                        "files": written["files"],
                        "enabled_files": written.get("enabled_files", []),
                        "message": f"已保存 {len(written['files'])} 个控制器配置；{restart_msg}",
                    })
                    return

                self._send_json(200, {
                    "ok": True,
                    "files": written["files"],
                    "enabled_files": written.get("enabled_files", []),
                    "message": f"已写入 conf/controllers（{len(written['files'])} 个文件）。重启策略后生效。",
                })
                return

            self.send_response(404)
            self.end_headers()
        except BrokenPipeError:
            return
        except Exception as exc:
            _log(f"request {self.path} failed: {type(exc).__name__}: {exc}")
            try:
                self._send_json(500, {"ok": False, "message": "服务器处理异常"})
            except Exception:
                pass


def main():
    print("📊 OKX Quant Dashboard 启动中...", flush=True)
    print(f"   状态文件 : {STATUS_FILE}", flush=True)
    print(f"   日志文件 : {LOG_FILE}", flush=True)
    print(f"   前端产物 : {frontend_dist_dir()}", flush=True)
    print(f"   访问地址 : http://127.0.0.1:{PORT}", flush=True)
    print("   按 Ctrl+C 停止", flush=True)
    server = ThreadingHTTPServer((os.environ.get("DASHBOARD_HOST", "127.0.0.1"), PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)


if __name__ == "__main__":
    main()
