#!/usr/bin/env python3
"""
OKX Quant Trader — Real-time Dashboard
=======================================
读取 data/bot/status.json，以 HTTP 服务方式暴露实时面板和策略配置页。
配置页把杠杆、开单金额和策略参数写回 conf/controllers。
不需要额外安装任何依赖。

用法:
    python dashboard/dashboard.py
    # 然后在浏览器打开 http://localhost:8888
"""

import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from strategy_config import (
        SHARED_FIELDS, ConfigError, apply_config,
        get_presets_info, get_strategy_info, load_config, STRATEGY_INFO,
    )
except ImportError:  # imported as dashboard.dashboard
    from dashboard.strategy_config import (
        SHARED_FIELDS, ConfigError, apply_config,
        get_presets_info, get_strategy_info, load_config, STRATEGY_INFO,
    )


# ─── 路径配置 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.environ.get("OKX_TRADER_ROOT", Path(__file__).parent.parent)).resolve()
STATUS_FILE = BASE_DIR / "data" / "bot" / "status.json"
LOG_FILE    = BASE_DIR / "logs" / "logs_conf_okx_multi.log"
MANUAL_CLOSES_FILE = BASE_DIR / "data" / "dashboard" / "manual_closes.json"
PORT        = 8888
STALE_AFTER_S = 20  # 引擎每 5 秒写一次快照；超过该秒数视为过期
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
TICKER_TTL_S = 5
_ticker_lock = threading.Lock()
_ticker_cache: dict = {"fetched_at": 0.0, "by_inst": {}, "error": None}
_control_lock = threading.Lock()
_control_process: subprocess.Popen | None = None
_control_action: str | None = None


def _log(msg: str) -> None:
    print(f"[dashboard] {msg}", flush=True)


def _launch_control(action: str) -> tuple[bool, str]:
    """Start one detached launcher control process; credentials stay local."""
    global _control_action, _control_process
    labels = {"start": "重新启动", "stop": "停止"}
    label = labels[action]
    with _control_lock:
        if _control_process is not None and _control_process.poll() is None:
            active_label = labels.get(_control_action or "", "控制操作")
            return False, f"{active_label}已在进行中，请稍候。"
        launcher = BASE_DIR / "native" / "launcher.py"
        if not launcher.is_file():
            return False, f"找不到启动器：{launcher}"
        log_path = BASE_DIR / "dashboard" / f"{action}.log"
        log_handle = log_path.open("a", encoding="utf-8")
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
            _control_process = subprocess.Popen([sys.executable, str(launcher), action], **kwargs)
            _control_action = action
        except OSError as exc:
            return False, f"无法启动{label}进程：{exc}"
        finally:
            log_handle.close()
        _log(f"{action} requested; pid={_control_process.pid}")
        if action == "stop":
            return True, "停止命令已发送。策略停止后 Dashboard 将自动关闭。"
        return True, "重新启动已开始，通常需要 1–2 分钟。"


def restart_strategy() -> tuple[bool, str]:
    reset_session_baseline()
    return _launch_control("start")


def stop_strategy() -> tuple[bool, str]:
    return _launch_control("stop")


def read_status() -> dict:
    """读取引擎定时写入的 status.json。"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


def tail_log(n: int = 80) -> list[str]:
    """读取日志末尾 n 行。"""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
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


# ─── JSON → 结构化数据 ───────────────────────────────────────────────────────
# ─── 币种图标映射 ─────────────────────────────────────────────────────────────
COIN_ICONS: dict[str, str] = {
    "BTC": "https://cdn.simpleicons.org/bitcoin/00e5ff",
    "ETH": "https://cdn.simpleicons.org/ethereum/00e5ff",
    "SOL": "https://cdn.simpleicons.org/solana/00e5ff",
    "XRP": "https://cdn.simpleicons.org/xrp/00e5ff",
    "DOGE": "https://cdn.simpleicons.org/dogecoin/00e5ff",
    "SUI": "https://cdn.simpleicons.org/sui/00e5ff",
    "ZEC": "https://cdn.simpleicons.org/zcash/00e5ff",
    "UNI": "https://cdn.jsdelivr.net/gh/spothq/cryptocurrency-icons@master/svg/white/uni.svg",
    "OKB": "https://cdn.jsdelivr.net/gh/spothq/cryptocurrency-icons@master/svg/white/okb.svg",
    "ADA": "https://cdn.simpleicons.org/cardano/00e5ff",
}


def _coin_icon(symbol: str) -> str:
    """Render a fixed-size monochrome icon with a readable offline fallback."""
    safe_symbol = html.escape(symbol)
    fallback = html.escape(symbol[:1] or "?")
    src = COIN_ICONS.get(symbol)
    image = ""
    if src:
        image = (
            f'<img src="{html.escape(src, quote=True)}" alt="" width="14" height="14" '
            'style="width:14px;height:14px;max-width:14px;max-height:14px;object-fit:contain;display:block;" '
            'loading="eager" decoding="async" referrerpolicy="no-referrer" '
            'onload="this.previousElementSibling.hidden=true" '
            'onerror="this.hidden=true;this.previousElementSibling.hidden=false">'
        )
    return (
        f'<span class="coin-icon" aria-hidden="true">'
        f'<span class="coin-fallback">{fallback}</span>{image}</span>'
        f'<span class="coin-symbol">{safe_symbol}</span>'
    )


def coin_label(controller: str) -> str:
    """将 okx_pmm_btc 之类的 controller 名转换为统一图标标签。"""
    if controller == "GLOBAL TOTAL":
        return '<span class="coin-label"><span class="coin-icon total-icon">Σ</span><span>TOTAL</span></span>'
    symbol = controller.split("_")[-1].upper()
    return f'<span class="coin-label">{_coin_icon(symbol)}</span>'


def pair_label(pair: str) -> str:
    symbol = pair.split("-")[0].upper()
    return f'<span class="coin-label">{_coin_icon(symbol)}</span>'


def strategy_pairs(status: dict, perf_rows: list[dict]) -> list[str]:
    """面板上的币种跟随当前策略，不扫全市场。"""
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
    connectors = ((status.get("engine") or {}).get("connectors") or {})
    for conn in connectors.values():
        for pair in conn.get("trading_pairs") or []:
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _num(row: dict, key: str) -> float | None:
    try:
        value = float(row[key])
    except (TypeError, ValueError, KeyError):
        return None
    if value != value:  # NaN
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


def fetch_swap_tickers() -> tuple[dict[str, dict], str | None]:
    """拉 OKX USDT 永续公开行情，短缓存，失败时沿用上一份。"""
    now = time.time()
    with _ticker_lock:
        cached = _ticker_cache["by_inst"]
        age = now - float(_ticker_cache["fetched_at"] or 0)
        if cached and age < TICKER_TTL_S:
            return cached, _ticker_cache["error"]
        req = urllib.request.Request(
            OKX_TICKERS_URL,
            headers={"User-Agent": "okx-quant-dashboard", "Accept": "application/json"},
        )
        last_error = "quotes unavailable"
        payload = None
        for opener in _okx_openers():
            try:
                with opener.open(req, timeout=8) as resp:
                    payload = json.load(resp)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        if not payload or str(payload.get("code")) != "0":
            if payload and payload.get("msg"):
                last_error = str(payload.get("msg"))
            _ticker_cache["error"] = last_error
            if cached:
                return cached, last_error
            return {}, last_error
        by_inst = {}
        for row in payload.get("data") or []:
            inst = row.get("instId") or ""
            if inst.endswith("-USDT-SWAP"):
                by_inst[inst] = row
        _ticker_cache["by_inst"] = by_inst
        _ticker_cache["fetched_at"] = time.time()
        _ticker_cache["error"] = None
        return by_inst, None


def fmt_px(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.6f}"


def fmt_quote_vol(base_vol: float | None, last: float | None) -> str:
    if base_vol is None or last is None:
        return "-"
    quote = base_vol * last
    if quote >= 1e9:
        return f"{quote / 1e9:.2f}B"
    if quote >= 1e6:
        return f"{quote / 1e6:.2f}M"
    if quote >= 1e3:
        return f"{quote / 1e3:.1f}K"
    return f"{quote:,.0f}"


def build_markets(pairs: list[str]) -> tuple[str, str]:
    tickers, error = fetch_swap_tickers()
    age = time.time() - float(_ticker_cache["fetched_at"] or 0)
    if _ticker_cache["by_inst"]:
        meta = f"OKX SWAP · {age:.0f}s"
        if error:
            meta += " · STALE"
    else:
        meta = error or "no quotes"
    rows = []
    for pair in pairs:
        row = tickers.get(f"{pair}-SWAP") or {}
        last = _num(row, "last")
        bid = _num(row, "bidPx")
        ask = _num(row, "askPx")
        open24 = _num(row, "open24h")
        high = _num(row, "high24h")
        low = _num(row, "low24h")
        vol = _num(row, "volCcy24h")
        if last is None:
            rows.append(
                f"<tr><td>{pair_label(pair)}</td>"
                + '<td colspan="8" class="empty">暂无公开行情报价</td></tr>'
            )
            continue
        chg = ((last - open24) / open24) if open24 else None
        chg_html = "-" if chg is None else f'<span class="{color_val(chg)}">{chg * 100:+.2f}%</span>'
        if bid and ask and ask >= bid:
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid if mid else 0
            spread_cls = "neg" if spread > 0.002 else ""
            spread_html = f'<span class="{spread_cls}">{spread * 100:.3f}%</span>'
        else:
            spread_html = "-"
        rows.append(
            "<tr>"
            f"<td>{pair_label(pair)}</td>"
            f"<td>{fmt_px(last)}</td>"
            f"<td>{chg_html}</td>"
            f"<td>{fmt_px(bid)}</td>"
            f"<td>{fmt_px(ask)}</td>"
            f"<td>{spread_html}</td>"
            f"<td>{fmt_px(high)}</td>"
            f"<td>{fmt_px(low)}</td>"
            f"<td>{fmt_quote_vol(vol, last)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="9" class="empty">暂无监控交易对行情</td></tr>')
    return "".join(rows), meta


def parse_controller_signals(status: dict) -> dict[str, dict]:
    """从 format_status 中解析出每个 Controller 的 z-score 和信号状态。"""
    text = status.get("format_status") or ""
    controllers = {}
    current_ctrl = None
    for line in text.splitlines():
        ctrl_m = re.match(r"Controller:\s*(\S+)", line)
        if ctrl_m:
            current_ctrl = ctrl_m.group(1)
            continue
        if current_ctrl and "Mean reversion |" in line:
            parts = [p.strip() for p in line.split("|")]
            z_val = None
            reason = ""
            for p in parts:
                if p.startswith("z="):
                    z_raw = p.replace("z=", "").strip()
                    try:
                        z_val = float(z_raw)
                    except ValueError:
                        z_val = None
                elif p != "Mean reversion":
                    reason = p
            controllers[current_ctrl] = {
                "z": z_val,
                "reason": reason,
            }
            current_ctrl = None
    return controllers


def parse_performance_table(status: dict) -> list[dict]:
    """从 format_status 文本解析出 PERFORMANCE SUMMARY 表格行。"""
    text = status.get("format_status") or ""
    rows = []
    in_perf = False
    for line in text.splitlines():
        if "PERFORMANCE SUMMARY" in line:
            in_perf = True
            continue
        if in_perf:
            # 匹配数据行（controller 名可含空格，如 "GLOBAL TOTAL"）:
            # | controller | realized | unrealized | global | pct | volume |
            m = re.match(
                r"\|\s*(.+?)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+%)\s*\|\s*([\d.]+)\s*\|",
                line,
            )
            if m:
                rows.append({
                    "controller": m.group(1).strip(),
                    "realized":   float(m.group(2)),
                    "unrealized": float(m.group(3)),
                    "global":     float(m.group(4)),
                    "global_pct": m.group(5).strip(),
                    "volume":     float(m.group(6)),
                })
    return rows


def parse_positions(status: dict) -> list[dict]:
    """从 format_status 解析持仓行（Positions Held 表格）。"""
    text = status.get("format_status") or ""
    positions = []
    for line in text.splitlines():
        m = re.match(
            r"\|\s*(\S+)\s*\|\s*(\S+-\S+)\s*\|\s*(BUY|SELL)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([\d.]+)\s*\|",
            line,
        )
        if m:
            positions.append({
                "connector":   m.group(1),
                "pair":        m.group(2),
                "side":        m.group(3),
                "amount":      float(m.group(4)),
                "value":       float(m.group(5)),
                "breakeven":   float(m.group(6)),
                "unrealized":  float(m.group(7)),
                "realized":    float(m.group(8)),
                "fees":        float(m.group(9)),
            })
    return positions


def load_manual_closes() -> list[dict]:
    """已确认的手动平仓。pnl 是用户填写的最终盈亏，会加进 realized 和 global。"""
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


def _same_closed_position(close: dict, position: dict) -> bool:
    return (
        close["pair"] == str(position["pair"]).upper()
        and close["side"] == str(position["side"]).upper()
        and abs(close["amount"] - float(position["amount"])) < 1e-6
        and abs(close["breakeven"] - float(position["breakeven"])) < 5e-4
    )


def _pnl_pct(global_pnl: float, volume: float) -> str:
    if not volume:
        return "0.00%"
    return f"{global_pnl / volume * 100:.2f}%"


def apply_manual_closes(positions: list[dict], perf_rows: list[dict]) -> tuple[list[dict], list[dict], str]:
    """隐藏已手动平仓的持仓，去掉其未实现盈亏，并把填写的最终盈亏计入 realized 与 global。"""
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


def parse_orders(status: dict) -> list[dict]:
    """从 format_status 解析当前挂单行（空格分隔格式）。"""
    text = status.get("format_status") or ""
    orders = []
    in_orders = False
    for line in text.splitlines():
        if line.strip().startswith("Orders:"):
            in_orders = True
            continue
        if in_orders:
            # 格式: "    okx_perpetual UNI-USDT sell 9.06110561       5 00:01:34"
            m = re.match(
                r"\s{2,}(\S+)\s+(\S+-\S+)\s+(buy|sell)\s+([\d.]+)\s+([\d.]+)\s+(\S+)",
                line,
                re.IGNORECASE,
            )
            if m:
                orders.append({
                    "connector": m.group(1),
                    "pair":      m.group(2),
                    "side":      m.group(3).upper(),
                    "price":     float(m.group(4)),
                    "amount":    float(m.group(5)),
                    "age":       m.group(6),
                })
            # 遇到空行或分隔线退出
            elif line.strip() == "" or line.strip().startswith("==="):
                in_orders = False
    return orders


def parse_executors(status: dict) -> list[dict]:
    """从 format_status 解析 Recent Executors 行。"""
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
            status_val = exec_m.group(3).replace("RunnableStatus.", "")
            close_type = exec_m.group(8).replace("CloseType.", "") if exec_m.group(8) else ""
            executors.append({
                "controller":  current_ctrl,
                "side":        exec_m.group(2).replace("TradeType.", ""),
                "status":      status_val,
                "net_pnl_pct": float(exec_m.group(4)),
                "net_pnl":     float(exec_m.group(5)),
                "volume":      float(exec_m.group(6)),
                "is_trading":  exec_m.group(7) == "True",
                "close_type":  close_type,
                "age":         float(exec_m.group(9)),
            })
    return executors


# ─── HTML 渲染 ───────────────────────────────────────────────────────────────
def color_val(v: float, invert: bool = False) -> str:
    if v > 0:
        return "pos" if not invert else "neg"
    if v < 0:
        return "neg" if not invert else "pos"
    return "zero"


def _uptime_s(engine: dict) -> float:
    """计算策略运行秒数。Hummingbot trading_core 的 uptime 和 start_time 单位均为毫秒。"""
    raw = float(engine.get("uptime", 0) or 0)
    start_time = float(engine.get("start_time", 0) or 0)
    if start_time > 1e11:  # 毫秒时间戳 (e.g. 1.7e12)
        return max(0.0, time.time() - start_time / 1000.0)
    elif start_time > 1e8:  # 秒时间戳 (e.g. 1.7e9)
        return max(0.0, time.time() - start_time)
    return max(0.0, raw / 1000.0)


def fmt_uptime(uptime_s: float) -> str:
    uptime_s = max(0, int(uptime_s))
    return f"{uptime_s // 3600:02d}h {(uptime_s % 3600) // 60:02d}m {uptime_s % 60:02d}s"


def fmt_snapshot_age(age: float | None) -> str:
    if age is None:
        return "no snapshot"
    if age < 60:
        return f"{age:.0f}s ago"
    return f"{age / 60:.1f}m ago"


_session_lock = threading.Lock()
_session_data = {
    "session_key": None,
    "baseline_assets": None,
    "baseline_realized_by_ctrl": {},
    "history": [],  # list of {"t": int, "bot_pnl": float, "assets": float}
}


def reset_session_baseline() -> None:
    """策略重新启动时调用，强制归零本机挂单操作盈亏与已实现/未实现盈亏。"""
    with _session_lock:
        _session_data["session_key"] = None
        _session_data["baseline_assets"] = None
        _session_data["baseline_realized_by_ctrl"] = {}
        _session_data["history"] = []


def _sync_session_metrics(engine: dict, updated_at: float, perf_rows: list[dict], usdt_bal: float) -> tuple[float, float, float, str, list[dict]]:
    """
    每次重启清零重算 REALIZED / UNREALIZED；
    GLOBAL PNL 不使用历史统计，就用 REALIZED 和 UNREALIZED 求和；
    返回: (global_realized, global_unrealized, global_pnl, glob_pct, session_history)
    """
    raw_start = engine.get("start_time") or engine.get("uptime") or 0
    session_key = str(raw_start)

    with _session_lock:
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
        else:
            for r in perf_rows:
                if r["controller"] not in _session_data["baseline_realized_by_ctrl"]:
                    _session_data["baseline_realized_by_ctrl"][r["controller"]] = float(r["realized"])

        base_map = dict(_session_data["baseline_realized_by_ctrl"])

    # 1. 各个 controller 扣除启动基线，重启归零重算
    ctrl_rows = [r for r in perf_rows if r["controller"] != "GLOBAL TOTAL"]
    for r in ctrl_rows:
        ctrl = r["controller"]
        base_r = base_map.get(ctrl, 0.0)
        r["realized"] = round(r["realized"] - base_r, 4)
        r["global"] = round(r["realized"] + r["unrealized"], 4)
        r["global_pct"] = _pnl_pct(r["global"], r["volume"])

    # 2. GLOBAL TOTAL: REALIZED 和 UNREALIZED 严格求和，不带历史统计
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

    # 3. 记录历史曲线点（挂单盈亏从0开始，总资产从实际余额开始）
    with _session_lock:
        if updated_at > 0:
            hist = _session_data["history"]
            if not hist or (updated_at - hist[-1]["t"] >= 2.0) or hist[-1]["bot_pnl"] != global_pnl or hist[-1]["assets"] != round(float(usdt_bal), 2):
                hist.append({"t": int(updated_at), "bot_pnl": global_pnl, "assets": round(float(usdt_bal), 2)})
                if len(hist) > 150:
                    hist.pop(0)
        session_history = list(_session_data["history"])

    return global_realized, global_unrealized, global_pnl, glob_pct, session_history


def build_view(status: dict) -> dict:
    """把 status.json 收成面板可原地补丁的字段（数字 + 表格 HTML）。"""
    err = status.get("error")
    if err:
        return {"error": str(err)}

    engine    = status.get("engine", {}) or {}
    balances  = (status.get("balances", {}) or {}).get("okx_perpetual", {}) or {}
    perf_rows = parse_performance_table(status)
    positions, perf_rows, manual_close_note = apply_manual_closes(parse_positions(status), perf_rows)
    orders    = parse_orders(status)
    executors = parse_executors(status)
    signals   = parse_controller_signals(status)

    updated_at = float(status.get("updated_at", 0) or 0)
    uptime_s   = _uptime_s(engine)
    running    = bool(engine.get("strategy_running", False))
    snapshot_age = (time.time() - updated_at) if updated_at else None
    stale = snapshot_age is None or snapshot_age > STALE_AFTER_S
    updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at)) if updated_at else "-"

    usdt_bal = float(balances.get("USDT", 0) or 0)
    realized, unrealized, global_pnl, glob_pct, session_history = _sync_session_metrics(
        engine, updated_at, perf_rows, usdt_bal
    )

    perf_html = ""
    for r in perf_rows:
        ctrl = r["controller"]
        cls = "total-row" if ctrl == "GLOBAL TOTAL" else ""
        if ctrl == "GLOBAL TOTAL":
            z_cell = "<td>-</td>"
        else:
            sig = signals.get(ctrl)
            z_val = sig.get("z") if sig else None
            reason = sig.get("reason", "") if sig else ""
            if z_val is None:
                z_cell = '<td><span class="badge gray">n/a</span></td>'
            elif z_val >= 2.0:
                z_cell = f'<td><span class="badge yellow" title="{reason}">⚡ {z_val:+.2f} (SELL)</span></td>'
            elif z_val <= -2.0:
                z_cell = f'<td><span class="badge yellow" title="{reason}">⚡ {z_val:+.2f} (BUY)</span></td>'
            else:
                z_cls = "pos" if z_val > 0 else ("neg" if z_val < 0 else "zero")
                z_cell = f'<td><span class="{z_cls}" title="{reason}">{z_val:+.2f}</span></td>'

        if ctrl == "GLOBAL TOTAL":
            close_cell = "<td>-</td>"
        else:
            close_cell = (
                f'<td><button type="button" class="close-position" '
                f'data-controller="{html.escape(ctrl, quote=True)}">平仓</button></td>'
            )
        perf_html += (
            f'<tr class="{cls}">'
            f'<td>{coin_label(ctrl)}</td>'
            f"{close_cell}"
            f'{z_cell}'
            f'<td class="{color_val(r["realized"])}">{r["realized"]:+.4f}</td>'
            f'<td class="{color_val(r["unrealized"])}">{r["unrealized"]:+.4f}</td>'
            f'<td class="{color_val(r["global"])}">{r["global"]:+.4f}</td>'
            f'<td class="{color_val(r["global"])}">{r["global_pct"]}</td>'
            f'<td>{r["volume"]:,.2f}</td>'
            f"</tr>"
        )

    pos_html = ""
    for p in positions:
        side_cls = "pos" if p["side"] == "BUY" else "neg"
        pair = html.escape(p["pair"], quote=True)
        side = html.escape(p["side"], quote=True)
        amount = f"{p['amount']:.8f}"
        breakeven = f"{p['breakeven']:.4f}"
        pos_html += (
            f"<tr>"
            f"<td>{html.escape(p['pair'])}</td>"
            f'<td><button type="button" class="manual-close" data-pair="{pair}" '
            f'data-side="{side}" data-amount="{amount}" data-breakeven="{breakeven}">已手动平仓</button></td>'
            f'<td class="{side_cls}">{p["side"]}</td>'
            f"<td>{p['amount']}</td>"
            f"<td>{p['value']:,.2f}</td>"
            f"<td>{p['breakeven']:,.4f}</td>"
            f'<td class="{color_val(p["unrealized"])}">{p["unrealized"]:+.4f}</td>'
            f'<td class="{color_val(p["realized"])}">{p["realized"]:+.4f}</td>'
            f"<td>{p['fees']:.4f}</td>"
            f"</tr>"
        )
    if not pos_html:
        pos_html = '<tr><td colspan="9" class="empty">当前暂无活跃持仓，策略持续监控入场机会</td></tr>'

    ord_html = ""
    for o in orders:
        side_cls = "pos" if o["side"] == "BUY" else "neg"
        ord_html += (
            f"<tr>"
            f"<td>{o['pair']}</td>"
            f'<td class="{side_cls}">{o["side"]}</td>'
            f"<td>{o['price']:,.6f}</td>"
            f"<td>{o['amount']}</td>"
            f"<td>{o['age']}</td>"
            f"</tr>"
        )
    if not ord_html:
        ord_html = '<tr><td colspan="5" class="empty">当前暂无待成交挂单</td></tr>'

    exec_html = ""
    for e in executors:
        side_cls = "pos" if e["side"] == "BUY" else "neg"
        badge_cls = {"RUNNING": "badge green", "TERMINATED": "badge gray"}.get(e["status"], "badge gray")
        trading_ind = "●" if e["is_trading"] else "○"
        if e["close_type"] == "TAKE_PROFIT":
            close_lbl = f'<span class="badge green">{e["close_type"]}</span>'
        elif e["close_type"] == "STOP_LOSS":
            close_lbl = f'<span class="badge red">{e["close_type"]}</span>'
        else:
            close_lbl = e["close_type"]
        exec_html += (
            f"<tr>"
            f"<td>{coin_label(e['controller'])}</td>"
            f'<td class="{side_cls}">{e["side"]}</td>'
            f'<td><span class="{badge_cls}">{e["status"]}</span></td>'
            f'<td class="{color_val(e["net_pnl"])}">{e["net_pnl"]:+.4f}</td>'
            f'<td class="{color_val(e["net_pnl_pct"])}">{e["net_pnl_pct"] * 100:+.4f}%</td>'
            f"<td>{e['volume']:,.2f}</td>"
            f'<td class="center">{trading_ind}</td>'
            f"<td>{close_lbl}</td>"
            f"<td>{e['age']:.0f}s</td>"
            f"</tr>"
        )
    if not exec_html:
        exec_html = '<tr><td colspan="9" class="empty">当前暂无进行中的执行器</td></tr>'

    log_html = "\n".join(
        f'<div class="log-line {"log-warn" if "WARNING" in line or "ERROR" in line else ""}">{line}</div>'
        for line in tail_log(60)
    )
    stale_html = (
        f'<div class="card error">⚠️ 快照停留在 {updated_str}，挂单可能与 OKX 不一致</div>'
        if stale else ""
    )

    bot_session_pnl = global_pnl
    strategy_info = get_strategy_info()
    return {
        "error": None,
        "updated_at": updated_at,
        "snapshot_age_s": round(snapshot_age, 1) if snapshot_age is not None else None,
        "uptime_s": uptime_s,
        "running": running,
        "stale": stale,
        "strategy": engine.get("strategy_file_name", "-") or "-",
        "status_html": (
            '<span class="badge green"><span class="status-dot"></span>ON</span>' if running
            else '<span class="badge red"><span class="status-dot"></span>OFF</span>'
        ),
        "snapshot_badge_html": (
            '<span class="badge green">LIVE</span>' if not stale
            else '<span class="badge red">STALE</span>'
        ),
        "usdt_bal": f"{usdt_bal:,.2f}",
        "bot_session_pnl_html": f"{bot_session_pnl:+.4f}",
        "bot_session_pnl_cls": color_val(bot_session_pnl),
        "history": session_history,
        "global_pnl_html": f"{global_pnl:+.4f} ({glob_pct})",
        "global_pnl_cls": color_val(global_pnl),
        "realized_html": f"{realized:+.4f}",
        "realized_cls": color_val(realized),
        "unrealized_html": f"{unrealized:+.4f}",
        "unrealized_cls": color_val(unrealized),
        "stale_html": stale_html,
        "updated_str": updated_str,
        "perf_html": perf_html,
        "pos_html": pos_html,
        "manual_close_note": manual_close_note,
        "ord_html": ord_html,
        "exec_html": exec_html,
        "log_html": log_html,
        "active_preset": "standard_5m",
        "active_preset_name": strategy_info["name"],
        "active_preset_badge": strategy_info["badge"],
        "target_preset": "",
        "switch_btn_label": "",
    }



_config_lock = threading.Lock()


def config_view() -> dict:
    return load_config()


def save_config(payload: dict) -> dict:
    with _config_lock:
        return apply_config(payload)


def _field_control(key: str, label: str, kind: str, bounds, value: str, shared: bool) -> str:
    attr = f'data-shared="{html.escape(key)}"' if shared else f'data-k="{html.escape(key)}"'
    if kind == "choice":
        options = "".join(
            f'<option value="{html.escape(item)}"{" selected" if item == value else ""}>{html.escape(item)}</option>'
            for item in bounds
        )
        control = f"<select {attr}>{options}</select>"
    elif kind == "bool":
        current = value.lower()
        options = "".join(
            f'<option value="{item}"{" selected" if item == current else ""}>{item}</option>'
            for item in ("false", "true")
        )
        control = f"<select {attr}>{options}</select>"
    else:
        control = f'<input {attr} value="{html.escape(value)}" inputmode="decimal" autocomplete="off">'
    return (
        f'<label class="field"><span>{html.escape(label)}</span>{control}</label>'
    )


def render_config_body() -> str:
    try:
        view = load_config()
    except ConfigError as exc:
        return f'<div class="card error">⚠️ {html.escape(str(exc))}</div>'

    rows = []
    for pair in view["pairs"]:
        rows.append(
            "<tr class=\"pair-row\" data-file=\"{file}\">"
            "<td><div class=\"pair-cell\">{pair} <span class=\"pair-name\">{trading_pair}</span></div></td>"
            "<td><input class=\"tbl-input num\" data-k=\"leverage\" value=\"{leverage}\" inputmode=\"numeric\" autocomplete=\"off\"></td>"
            "<td><input class=\"tbl-input num\" data-k=\"total_amount_quote\" value=\"{amount}\" inputmode=\"decimal\" autocomplete=\"off\"></td>"
            "<td><input class=\"tbl-input num\" data-k=\"take_profit_quote\" value=\"{tp}\" inputmode=\"decimal\" autocomplete=\"off\"></td>"
            "<td><input class=\"tbl-input num\" data-k=\"fixed_unrealized_tp_quote\" value=\"{fixed_tp}\" inputmode=\"decimal\" autocomplete=\"off\" placeholder=\"例如 2.0\"></td>"
            "</tr>".format(
                file=html.escape(pair["file"]),
                pair=pair_label(pair["trading_pair"]),
                trading_pair=html.escape(pair["trading_pair"]),
                leverage=html.escape(str(pair["leverage"])),
                amount=html.escape(str(pair["total_amount_quote"])),
                tp=html.escape(str(pair["take_profit_quote"])),
                fixed_tp=html.escape(str(pair.get("fixed_unrealized_tp_quote", "2"))),
            )
        )
    shared_controls = "".join(
        _field_control(key, label, kind, bounds, view["shared"][key], shared=True)
        for key, label, kind, bounds in SHARED_FIELDS
    )
    mixed = [label for key, label, _, _ in SHARED_FIELDS if view["shared_mixed"].get(key)]
    mixed_html = ""
    if mixed:
        mixed_html = (
            '<div class="alert-box warn">⚠️ 这些参数在各币种间不一致，保存后将统一更新为下方数值：'
            + html.escape("、".join(mixed)) + "</div>"
        )

    strategy = view.get("strategy") or STRATEGY_INFO

    return f"""
        <form id="config-form">
          <div class="workbench-panel">
            <div class="workbench-panel-head">
              <h2 class="workbench-panel-title">⚙️ 当前运行策略模型 Strategy Overview</h2>
              <span class="badge green"><span class="status-dot"></span>生效中</span>
            </div>
            <div style="padding: 14px 16px;">
              <div class="strategy-meta-grid">
                <div class="meta-box">
                  <span class="meta-box-label">策略模型</span>
                  <span class="meta-box-val">{html.escape(strategy["name"])}</span>
                </div>
                <div class="meta-box">
                  <span class="meta-box-label">K 线周期</span>
                  <span class="meta-box-val">{html.escape(strategy["interval"])}</span>
                </div>
                <div class="meta-box">
                  <span class="meta-box-label">均值窗口</span>
                  <span class="meta-box-val">{strategy["mean_window"]} 根 ({strategy["mean_window"] * 5 // 60} 小时)</span>
                </div>
                <div class="meta-box">
                  <span class="meta-box-label">单笔持仓时限</span>
                  <span class="meta-box-val">{strategy["time_limit"]} 秒 ({strategy["time_limit"] // 60} 分钟)</span>
                </div>
              </div>
              <p style="font-size: 12px; color: var(--color-muted); line-height: 1.5;">{html.escape(strategy["description"])}</p>
            </div>
          </div>

          <div class="workbench-panel">
            <div class="workbench-panel-head">
              <h2 class="workbench-panel-title">⚖️ 批量与各币种仓位配置 Leverage & Allocation</h2>
              <div class="quick-bar" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
                <div style="display:inline-flex;gap:6px;align-items:center;">
                  <span style="font-size: 11px; color: var(--color-muted);">批量杠杆:</span>
                  <input id="apply-leverage" class="tbl-input mini-input" value="3" inputmode="numeric" autocomplete="off" placeholder="3">
                  <button type="button" id="apply-leverage-btn" class="btn btn-secondary">应用全部</button>
                </div>
                <div style="display:inline-flex;gap:6px;align-items:center;">
                  <span style="font-size: 11px; color: var(--color-muted);">批量固定浮盈止盈(&gt;1):</span>
                  <input id="apply-fixed-tp" class="tbl-input mini-input" value="2" inputmode="decimal" autocomplete="off" placeholder="2">
                  <button type="button" id="apply-fixed-tp-btn" class="btn btn-secondary">应用全部</button>
                </div>
              </div>
            </div>
            <div style="padding: 14px 16px;">
              <p style="font-size: 11.5px; color: var(--color-muted); margin-bottom: 12px;">开单金额为单笔最大名义本金（USDT），非保证金占用；各币种杠杆支持根据波动率单独设置（1x–5x）。止盈按单笔价格浮盈触发市价平仓；固定浮盈平仓（&gt;1 USDT）在性能矩阵浮盈达到设定值时立即平仓。</p>
              <div class="table-wrap">
                <table>
                  <thead><tr>
                    <th>交易对 PAIR</th>
                    <th>杠杆 LEVERAGE</th>
                    <th>名义仓位上限 TOTAL (USDT)</th>
                    <th>现金止盈 CASH TP (USDT)</th>
                    <th>固定浮盈平仓 FIXED TP (USDT, &gt;1)</th>
                  </tr></thead>
                  <tbody>{''.join(rows)}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="workbench-panel">
            <div class="workbench-panel-head">
              <h2 class="workbench-panel-title">🛡️ 全局核心策略与风控参数 Risk & Model Parameters</h2>
              <span class="workbench-panel-meta">统一调度参数</span>
            </div>
            <div style="padding: 14px 16px;">
              <p style="font-size: 11.5px; color: var(--color-muted);">以下参数对当前启用的全部币种生效。修改后保存将写入 conf/controllers，重启交易机器人后生效。</p>
              {mixed_html}
              <div class="field-grid">{shared_controls}</div>
            </div>
          </div>

          <div class="sticky-actions-bar">
            <button type="submit" class="btn btn-primary">💾 保存全部配置</button>
            <span id="config-msg" class="save-msg"></span>
          </div>
        </form>
        <script>
          (function() {{
            var form = document.getElementById('config-form');
            var msg = document.getElementById('config-msg');
            document.getElementById('apply-leverage-btn').onclick = function() {{
              var value = document.getElementById('apply-leverage').value || '3';
              document.querySelectorAll('[data-k="leverage"]').forEach(function(el) {{ el.value = value; }});
            }};
            document.getElementById('apply-fixed-tp-btn').onclick = function() {{
              var value = document.getElementById('apply-fixed-tp').value || '2';
              document.querySelectorAll('[data-k="fixed_unrealized_tp_quote"]').forEach(function(el) {{ el.value = value; }});
            }};
            form.onsubmit = function(event) {{
              event.preventDefault();
              var pairs = [].map.call(document.querySelectorAll('.pair-row'), function(row) {{
                var item = {{ file: row.getAttribute('data-file') }};
                row.querySelectorAll('[data-k]').forEach(function(el) {{ item[el.getAttribute('data-k')] = el.value; }});
                return item;
              }});
              var shared = {{}};
              document.querySelectorAll('[data-shared]').forEach(function(el) {{
                shared[el.getAttribute('data-shared')] = el.value;
              }});
              msg.className = 'save-msg saving';
              msg.textContent = '正在保存写入配置...';
              fetch('/api/config', {{
                method: 'POST',
                headers: {{
                  'Content-Type': 'application/json',
                  'X-Requested-With': 'OKX-Dashboard'
                }},
                body: JSON.stringify({{ pairs: pairs, shared: shared }})
              }}).then(function(response) {{
                return response.json().then(function(data) {{ return {{ ok: response.ok, data: data }}; }});
              }}).then(function(result) {{
                if (result.ok && result.data.ok) {{
                  msg.className = 'save-msg ok';
                  msg.textContent = '✅ ' + (result.data.message || '配置已成功保存！');
                }} else {{
                  msg.className = 'save-msg err';
                  msg.textContent = '❌ ' + ((result.data && result.data.message) || '保存失败');
                }}
              }}).catch(function() {{
                msg.className = 'save-msg err';
                msg.textContent = '❌ 保存请求异常，请检查网络或日志';
              }});
            }};
          }})();
        </script>"""


def markets_view(status: dict) -> dict:
    """行情页单独拉 OKX 公开报价，主面板轮询不带这份数据。"""
    err = status.get("error")
    if err:
        return {"error": str(err), "markets_html": "", "markets_meta": ""}
    html_content, meta = build_markets(strategy_pairs(status, parse_performance_table(status)))
    return {"error": None, "markets_html": html_content, "markets_meta": meta}


def render_html(status: dict, page: str = "dashboard") -> str:
    if not status:
        status = read_status()
    view = build_view(status)
    markets_data = markets_view(status)
    config_body = render_config_body()
    strategy_info = get_strategy_info()
    strategy_badge = strategy_info.get("badge", "5M MEAN REVERSION")
    
    # Active tab based on route
    active_tab = "overview"
    if page == "markets":
        active_tab = "markets"
    elif page == "config":
        active_tab = "config"

    now_str = time.strftime("%H:%M:%S")
    age_label = fmt_snapshot_age(
        (time.time() - view["updated_at"]) if view.get("updated_at") else None
    )
    stale_cls = "neg" if view.get("stale") else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OKX QUANTITATIVE TRADER // INSTITUTIONAL TERMINAL</title>
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#060709">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-canvas: #060709;
      --bg-surface: #0c0e14;
      --bg-surface-hover: #121520;
      --bg-card: #0f121a;
      --bg-elevated: #161a25;
      --border-subtle: rgba(255, 255, 255, 0.08);
      --border-focus: rgba(255, 255, 255, 0.20);
      --border-glow: rgba(56, 189, 248, 0.25);
      
      --color-ink: #f8fafc;
      --color-ink-muted: #94a3b8;
      --color-ink-faint: #64748b;
      
      --color-pos: #10b981;
      --color-pos-bg: rgba(16, 185, 129, 0.12);
      --color-neg: #f43f5e;
      --color-neg-bg: rgba(244, 63, 94, 0.12);
      --color-accent: #38bdf8;
      --color-accent-bg: rgba(56, 189, 248, 0.12);
      --color-gold: #e2b714;
      --color-gold-bg: rgba(226, 183, 20, 0.12);
      
      --font-serif: "Times New Roman", Times, "Songti SC", "SimSun", serif;
      --font-mono: "Times New Roman", Times, "Courier New", monospace;
    }}

    *, *::before, *::after {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      background-color: var(--bg-canvas);
      background-image: 
        radial-gradient(ellipse 70% 35% at 50% -10%, rgba(56, 189, 248, 0.07), transparent 70%),
        linear-gradient(to right, rgba(255, 255, 255, 0.015) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255, 255, 255, 0.015) 1px, transparent 1px);
      background-size: 100% 100%, 32px 32px, 32px 32px;
      color: var(--color-ink);
      font-family: var(--font-serif);
      font-size: 14px;
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
    }}

    /* ─── Top Institutional Header ─── */
    .site-header {{
      position: sticky;
      top: 0;
      z-index: 400;
      background: rgba(6, 7, 9, 0.88);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 24px;
      height: 56px;
    }}

    .brand-section {{
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 260px;
    }}

    .brand-logo-svg {{
      flex-shrink: 0;
      display: block;
    }}

    .brand-titles {{
      display: flex;
      flex-direction: column;
    }}

    .brand-main {{
      font-family: var(--font-serif);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.10em;
      color: var(--color-ink);
      text-transform: uppercase;
      text-decoration: none;
    }}

    .brand-sub {{
      font-size: 11px;
      color: var(--color-ink-muted);
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}

    /* ─── Tab Strip (Strictly NO EMOJIS) ─── */
    .tab-strip {{
      display: flex;
      align-items: center;
      gap: 4px;
      background: rgba(255, 255, 255, 0.03);
      padding: 3px 4px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
    }}

    .tab-btn {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 14px;
      border-radius: 4px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--color-ink-muted);
      font-family: var(--font-serif);
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      cursor: pointer;
      transition: all 0.15s ease;
      white-space: nowrap;
    }}

    .tab-btn:hover {{
      color: var(--color-ink);
      background: rgba(255, 255, 255, 0.04);
    }}

    .tab-btn.active {{
      color: var(--color-ink);
      background: var(--bg-elevated);
      border-color: rgba(255, 255, 255, 0.12);
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
    }}

    .tab-icon {{
      stroke-width: 1.8;
      opacity: 0.85;
    }}

    .tab-btn.active .tab-icon {{
      stroke: var(--color-accent);
      opacity: 1;
    }}

    /* ─── Top Controls & Status Group ─── */
    .top-status-group {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .clock-display {{
      font-family: var(--font-serif);
      font-variant-numeric: tabular-nums;
      font-size: 12px;
      letter-spacing: 0.04em;
      color: var(--color-ink-faint);
      padding: 0 4px;
    }}

    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 7px;
      border-radius: 3px;
      font-family: var(--font-serif);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}

    .badge.green {{
      background: var(--color-pos-bg);
      color: var(--color-pos);
      border-color: rgba(16, 185, 129, 0.25);
    }}

    .badge.red {{
      background: var(--color-neg-bg);
      color: var(--color-neg);
      border-color: rgba(244, 63, 94, 0.25);
    }}

    .badge.yellow {{
      background: rgba(245, 158, 11, 0.12);
      color: #f59e0b;
      border-color: rgba(245, 158, 11, 0.25);
    }}

    .badge.gray {{
      background: rgba(255, 255, 255, 0.05);
      color: var(--color-ink-muted);
      border-color: var(--border-subtle);
    }}

    .status-dot {{
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: currentColor;
    }}

    .snapshot-age-tag {{
      font-size: 11px;
      color: var(--color-ink-faint);
    }}

    .snapshot-age-tag.neg {{
      color: var(--color-neg);
      font-weight: 600;
    }}

    /* ─── Buttons ─── */
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 4px;
      font-family: var(--font-serif);
      font-size: 11.5px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.15s ease;
      white-space: nowrap;
    }}

    .btn-action-warn {{
      background: rgba(245, 158, 11, 0.08);
      color: #f59e0b;
      border-color: rgba(245, 158, 11, 0.3);
    }}

    .btn-action-warn:hover {{
      background: rgba(245, 158, 11, 0.18);
      border-color: #f59e0b;
    }}

    .btn-action-neg {{
      background: rgba(244, 63, 94, 0.08);
      color: #f43f5e;
      border-color: rgba(244, 63, 94, 0.3);
    }}

    .btn-action-neg:hover {{
      background: rgba(244, 63, 94, 0.18);
      border-color: #f43f5e;
    }}

    .btn-primary {{
      background: var(--color-pos);
      color: #060709;
      border-color: var(--color-pos);
    }}

    .btn-primary:hover {{
      background: #059669;
    }}

    .btn-secondary {{
      background: rgba(255, 255, 255, 0.06);
      color: var(--color-ink);
      border-color: var(--border-subtle);
    }}

    .btn-secondary:hover {{
      background: rgba(255, 255, 255, 0.12);
    }}

    .btn-subtle {{
      background: transparent;
      color: var(--color-ink-muted);
      border-color: var(--border-subtle);
    }}

    .btn-subtle:hover {{
      color: var(--color-ink);
      background: rgba(255, 255, 255, 0.04);
    }}

    /* ─── Main Container ─── */
    .container {{
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
      padding: 24px;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}

    /* ─── Tab Panes ─── */
    .tab-pane {{
      display: none;
      flex-direction: column;
      gap: 20px;
    }}

    .tab-pane.active {{
      display: flex;
    }}

    /* ─── KPI Stat Ribbon ─── */
    .stat-ribbon {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}

    .stat-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s ease, transform 0.2s ease;
    }}

    .stat-card:hover {{
      border-color: var(--border-focus);
    }}

    .stat-card.highlight {{
      border-color: rgba(56, 189, 248, 0.35);
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.04), transparent 70%), var(--bg-card);
    }}

    .stat-card.session-card {{
      border-color: rgba(16, 185, 129, 0.35);
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.04), transparent 70%), var(--bg-card);
    }}

    .stat-label {{
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      color: var(--color-ink-muted);
      text-transform: uppercase;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    .stat-num {{
      font-family: var(--font-serif);
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.01em;
      font-variant-numeric: tabular-nums;
      color: var(--color-ink);
    }}

    .stat-unit {{
      font-size: 12px;
      font-weight: 400;
      color: var(--color-ink-muted);
      margin-left: 2px;
    }}

    .stat-sub {{
      font-size: 11px;
      color: var(--color-ink-faint);
    }}

    /* ─── Institutional Panels ─── */
    .panel {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}

    .panel-head {{
      padding: 14px 20px;
      border-bottom: 1px solid var(--border-subtle);
      background: rgba(255, 255, 255, 0.015);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .panel-titles {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}

    .panel-title {{
      font-family: var(--font-serif);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--color-ink);
    }}

    .panel-meta {{
      font-size: 11.5px;
      color: var(--color-ink-muted);
    }}

    /* ─── Chart Specific Controls & Layout ─── */
    .chart-controls {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .chart-tab {{
      padding: 4px 10px;
      border-radius: 4px;
      border: 1px solid var(--border-subtle);
      background: rgba(255, 255, 255, 0.03);
      color: var(--color-ink-muted);
      font-family: var(--font-serif);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
      transition: all 0.15s ease;
    }}

    .chart-tab:hover {{
      color: var(--color-ink);
      border-color: var(--border-focus);
    }}

    .chart-tab.active {{
      color: var(--color-ink);
      background: var(--bg-elevated);
      border-color: var(--color-accent);
      box-shadow: 0 0 10px rgba(56, 189, 248, 0.2);
    }}

    .chart-canvas-wrap {{
      position: relative;
      width: 100%;
      height: 240px;
      background: #090b10;
      overflow: hidden;
    }}

    #equity-svg {{
      width: 100%;
      height: 100%;
      display: block;
      cursor: crosshair;
    }}

    .chart-tooltip {{
      position: absolute;
      top: 14px;
      left: 20px;
      background: rgba(12, 14, 20, 0.92);
      border: 1px solid var(--border-focus);
      border-radius: 4px;
      padding: 8px 12px;
      pointer-events: none;
      font-family: var(--font-serif);
      font-size: 11.5px;
      line-height: 1.4;
      color: var(--color-ink);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      z-index: 10;
      white-space: nowrap;
    }}

    .chart-metrics-bar {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      background: rgba(255, 255, 255, 0.015);
      border-top: 1px solid var(--border-subtle);
      padding: 10px 20px;
      gap: 16px;
    }}

    .chart-metric {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}

    .chart-metric-label {{
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--color-ink-muted);
    }}

    .chart-metric-val {{
      font-family: var(--font-serif);
      font-size: 13.5px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--color-ink);
    }}

    /* ─── Table Design ─── */
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }}

    thead th {{
      padding: 10px 16px;
      font-family: var(--font-serif);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--color-ink-muted);
      border-bottom: 1px solid var(--border-subtle);
      background: rgba(255, 255, 255, 0.02);
      white-space: nowrap;
    }}

    tbody td {{
      padding: 11px 16px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: var(--font-serif);
      font-variant-numeric: tabular-nums;
      color: var(--color-ink);
      white-space: nowrap;
      transition: background 0.1s ease;
    }}

    tbody tr:hover td {{
      background: rgba(255, 255, 255, 0.025);
    }}

    tbody tr.total-row td {{
      background: rgba(56, 189, 248, 0.04);
      font-weight: 700;
      border-top: 1px solid rgba(56, 189, 248, 0.2);
    }}

    /* Crypto Coin Icon Badge */
    .coin-badge {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-weight: 600;
    }}

    .coin-icon {{
      width: 18px;
      height: 18px;
      min-width: 18px;
      min-height: 18px;
      max-width: 18px;
      max-height: 18px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.08);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 9.5px;
      font-weight: 700;
      color: var(--color-gold);
      border: 1px solid rgba(255, 255, 255, 0.15);
      overflow: hidden;
      position: relative;
      vertical-align: middle;
      flex-shrink: 0;
    }}

    .coin-icon img {{
      width: 13px;
      height: 13px;
      max-width: 13px;
      max-height: 13px;
      object-fit: contain;
      display: block;
    }}

    .pos {{
      color: var(--color-pos);
    }}

    .neg {{
      color: var(--color-neg);
    }}

    .zero {{
      color: var(--color-ink-muted);
    }}

    /* ─── Log Viewer ─── */
    .log-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 16px;
      background: rgba(255, 255, 255, 0.02);
      border-bottom: 1px solid var(--border-subtle);
    }}

    .log-filter-input {{
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 5px 12px;
      font-family: var(--font-serif);
      font-size: 12px;
      color: var(--color-ink);
      width: 260px;
      outline: none;
      transition: border-color 0.15s ease;
    }}

    .log-filter-input:focus {{
      border-color: var(--color-accent);
    }}

    .log-tool-btn {{
      padding: 4px 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      color: var(--color-ink-muted);
      font-family: var(--font-serif);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
    }}

    .log-tool-btn:hover {{
      color: var(--color-ink);
      border-color: var(--border-focus);
    }}

    .log-tool-btn.active {{
      color: var(--color-pos);
      border-color: rgba(16, 185, 129, 0.4);
      background: var(--color-pos-bg);
    }}

    .log-box {{
      height: 480px;
      overflow-y: auto;
      padding: 14px 16px;
      background: #050608;
      font-family: "JetBrains Mono", "Courier New", monospace;
      font-size: 11.5px;
      line-height: 1.6;
      color: #cbd5e1;
    }}

    .log-line {{
      white-space: pre-wrap;
      word-break: break-all;
    }}

    .log-line.log-warn {{
      color: #f59e0b;
    }}

    /* ─── Modal Dialog ─── */
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      z-index: 200;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(8px);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}

    .modal-backdrop[hidden] {{
      display: none !important;
    }}

    .modal-backdrop.is-open:not([hidden]) {{
      display: flex;
    }}

    .modal-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-focus);
      border-radius: 8px;
      width: 100%;
      max-width: 460px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
    }}

    .modal-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    .modal-close-x {{
      background: transparent;
      border: none;
      color: var(--color-ink-muted);
      font-size: 20px;
      cursor: pointer;
    }}

    .input-unit-group {{
      display: flex;
      align-items: center;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 2px 10px;
    }}

    .input-unit-group input {{
      flex: 1;
      background: transparent;
      border: none;
      color: var(--color-ink);
      font-family: var(--font-serif);
      font-size: 14px;
      padding: 8px 0;
      outline: none;
    }}

    .unit-tag {{
      font-size: 12px;
      color: var(--color-ink-muted);
    }}

    /* Config Page Embedded Styles */
    .strategy-meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}

    .meta-box {{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}

    .meta-box-label {{
      font-size: 10.5px;
      color: var(--color-ink-muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}

    .meta-box-val {{
      font-size: 13.5px;
      font-weight: 700;
      color: var(--color-ink);
    }}

    .field-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }}

    .field-card {{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .field-label {{
      font-size: 11px;
      font-weight: 600;
      color: var(--color-ink-muted);
      text-transform: uppercase;
    }}

    .tbl-input {{
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 6px 10px;
      color: var(--color-ink);
      font-family: var(--font-serif);
      font-size: 13px;
      outline: none;
      width: 100%;
    }}

    .tbl-input:focus {{
      border-color: var(--color-accent);
    }}

    .sticky-actions-bar {{
      position: sticky;
      bottom: 20px;
      margin-top: 10px;
      padding: 12px 20px;
      background: rgba(12, 14, 20, 0.95);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border-focus);
      border-radius: 6px;
      display: flex;
      align-items: center;
      gap: 16px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
    }}

    /* ─── Site Footer ─── */
    .site-footer {{
      margin-top: auto;
      border-top: 1px solid var(--border-subtle);
      background: rgba(6, 7, 9, 0.9);
      padding: 12px 24px;
      font-size: 11px;
      letter-spacing: 0.04em;
      color: var(--color-ink-faint);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    @media (max-width: 900px) {{
      .stat-ribbon {{
        grid-template-columns: repeat(2, 1fr);
      }}
      .site-header {{
        height: auto;
        padding: 10px 16px;
        flex-direction: column;
        align-items: stretch;
      }}
      .tab-strip {{
        overflow-x: auto;
      }}
    }}
  </style>
  <script>
    (function() {{
      var POLL_MS = 2000;
      var MARKETS_POLL_MS = 4000;
      var chartMode = 'bot'; // 'bot' | 'assets' | 'dual'
      var lastHistory = [];
      var autoScroll = true;
      var lastSnapshotAt = {view.get("updated_at", 0)};

      function $(id) {{ return document.getElementById(id); }}

      function updateSnapshotAge() {{
        var el = $('snapshot-age');
        if (!el || !lastSnapshotAt || lastSnapshotAt <= 0) return;
        var age = Math.max(0, Math.floor(Date.now() / 1000 - lastSnapshotAt));
        el.textContent = age < 60 ? (age + 's ago') : ((age / 60).toFixed(1) + 'm ago');
        if (age > 15) {{
          el.className = 'snapshot-age-tag neg';
        }} else {{
          el.className = 'snapshot-age-tag';
        }}
      }}

      window.switchTab = function(tabName) {{
        closeDialog();
        var tabs = document.querySelectorAll('.tab-btn');
        var panes = document.querySelectorAll('.tab-pane');
        tabs.forEach(function(b) {{ b.classList.toggle('active', b.getAttribute('data-tab') === tabName); }});
        panes.forEach(function(p) {{ p.classList.toggle('active', p.id === 'tab-' + tabName); }});
        try {{
          window.location.hash = '#' + tabName;
          localStorage.setItem('okx_active_tab', tabName);
        }} catch(e) {{}}
        if (tabName === 'overview') {{
          renderChart(lastHistory, chartMode);
        }} else if (tabName === 'markets') {{
          pollMarkets();
        }}
      }};

      window.setChartMode = function(mode) {{
        chartMode = mode;
        document.querySelectorAll('.chart-tab').forEach(function(btn) {{
          btn.classList.toggle('active', btn.getAttribute('data-mode') === mode);
        }});
        renderChart(lastHistory, chartMode);
      }};

      window.toggleAutoScroll = function() {{
        autoScroll = !autoScroll;
        var btn = $('autoscroll-btn');
        if (btn) {{
          btn.classList.toggle('active', autoScroll);
          btn.textContent = 'AUTO-SCROLL: ' + (autoScroll ? 'ON' : 'OFF');
        }}
      }};

      window.filterLogs = function(query) {{
        query = (query || '').toLowerCase();
        var lines = document.querySelectorAll('#logbox .log-line');
        lines.forEach(function(line) {{
          var txt = line.textContent.toLowerCase();
          line.style.display = (!query || txt.indexOf(query) !== -1) ? 'block' : 'none';
        }});
      }};

      window.clearLogs = function() {{
        var lb = $('logbox');
        if (lb) lb.innerHTML = '<div class="log-line zero">[Display Cleared by User]</div>';
      }};

      window.copyLogs = function() {{
        var lb = $('logbox');
        if (lb) {{
          navigator.clipboard.writeText(lb.textContent || '').then(function() {{
            alert('Logs copied to clipboard.');
          }});
        }}
      }};

      function renderChart(history, mode) {{
        if (!history || history.length < 2) return;
        var svg = $('equity-svg');
        if (!svg) return;
        var width = 1000;
        var height = 240;
        var padTop = 30;
        var padBottom = 30;
        var plotH = height - padTop - padBottom;

        // Stats tracking
        var botPnls = history.map(function(h) {{ return h.bot_pnl; }});
        var assets = history.map(function(h) {{ return h.assets; }});
        var curBot = botPnls[botPnls.length - 1];
        var curAssets = assets[assets.length - 1];
        var peakBot = Math.max.apply(null, botPnls);

        if ($('chart-stat-bot')) $('chart-stat-bot').textContent = (curBot >= 0 ? '+' : '') + curBot.toFixed(4) + ' USDT';
        if ($('chart-stat-peak')) $('chart-stat-peak').textContent = (peakBot >= 0 ? '+' : '') + peakBot.toFixed(4) + ' USDT';
        if ($('chart-stat-assets')) $('chart-stat-assets').textContent = '$' + curAssets.toFixed(2) + ' USDT';
        if ($('chart-stat-ticks')) $('chart-stat-ticks').textContent = history.length;

        // Scaling calculations
        var minVal, maxVal;
        if (mode === 'bot') {{
          minVal = Math.min.apply(null, botPnls);
          maxVal = Math.max.apply(null, botPnls);
          var spread = Math.max(Math.abs(minVal), Math.abs(maxVal), 0.1);
          minVal = -spread * 1.15;
          maxVal = spread * 1.15;
        }} else if (mode === 'assets') {{
          minVal = Math.min.apply(null, assets);
          maxVal = Math.max.apply(null, assets);
          if (minVal === maxVal) {{ minVal -= 1; maxVal += 1; }}
          var padA = (maxVal - minVal) * 0.15;
          minVal -= padA;
          maxVal += padA;
        }} else {{ // dual
          minVal = Math.min.apply(null, botPnls);
          maxVal = Math.max.apply(null, botPnls);
          var sp = Math.max(Math.abs(minVal), Math.abs(maxVal), 0.1);
          minVal = -sp * 1.15;
          maxVal = sp * 1.15;
        }}

        function getX(i) {{
          return (i / (history.length - 1)) * width;
        }}
        function getY(v, mn, mx) {{
          return padTop + plotH - ((v - mn) / (mx - mn)) * plotH;
        }}

        // Zero line calculation
        var zeroY = getY(0, minVal, maxVal);
        var zLine = $('chart-zero-line');
        if (zLine) {{
          if (mode === 'assets') {{
            zLine.style.display = 'none';
          }} else {{
            zLine.style.display = 'block';
            zLine.setAttribute('y1', zeroY);
            zLine.setAttribute('y2', zeroY);
          }}
        }}

        // Build Paths
        function buildPath(dataKey, mn, mx) {{
          var pts = history.map(function(h, idx) {{
            return [getX(idx), getY(h[dataKey], mn, mx)];
          }});
          var d = 'M ' + pts[0][0] + ' ' + pts[0][1];
          for (var i = 1; i < pts.length; i++) {{
            var prev = pts[i - 1];
            var cur = pts[i];
            var midX = (prev[0] + cur[0]) / 2;
            d += ' C ' + midX + ' ' + prev[1] + ', ' + midX + ' ' + cur[1] + ', ' + cur[0] + ' ' + cur[1];
          }}
          return {{ line: d, pts: pts }};
        }}

        var botPath = buildPath('bot_pnl', minVal, maxVal);
        var botArea = botPath.line + ' L ' + width + ' ' + zeroY + ' L 0 ' + zeroY + ' Z';
        
        var minAst = Math.min.apply(null, assets);
        var maxAst = Math.max.apply(null, assets);
        if (minAst === maxAst) {{ minAst -= 1; maxAst += 1; }}
        var padAst = (maxAst - minAst) * 0.15;
        minAst -= padAst; maxAst += padAst;
        var astPath = buildPath('assets', minAst, maxAst);
        var astArea = astPath.line + ' L ' + width + ' ' + height + ' L 0 ' + height + ' Z';

        var pLine = $('chart-line');
        var pArea = $('chart-area');
        var pAstLine = $('chart-assets-line');

        if (mode === 'bot') {{
          pLine.setAttribute('d', botPath.line);
          pLine.setAttribute('stroke', curBot >= 0 ? '#10b981' : '#f43f5e');
          pArea.setAttribute('d', botArea);
          pArea.setAttribute('fill', curBot >= 0 ? 'url(#pnlGradPos)' : 'url(#pnlGradNeg)');
          pArea.style.display = 'block';
          if (pAstLine) pAstLine.style.display = 'none';
        }} else if (mode === 'assets') {{
          pLine.setAttribute('d', astPath.line);
          pLine.setAttribute('stroke', '#38bdf8');
          pArea.setAttribute('d', astArea);
          pArea.setAttribute('fill', 'url(#assetsGrad)');
          pArea.style.display = 'block';
          if (pAstLine) pAstLine.style.display = 'none';
        }} else {{ // dual
          pLine.setAttribute('d', botPath.line);
          pLine.setAttribute('stroke', '#10b981');
          pArea.style.display = 'none';
          if (pAstLine) {{
            pAstLine.setAttribute('d', astPath.line);
            pAstLine.setAttribute('stroke', '#38bdf8');
            pAstLine.style.display = 'block';
          }}
        }}

        // Interactive mouse hover crosshair
        svg.onmousemove = function(ev) {{
          var rect = svg.getBoundingClientRect();
          var mouseX = (ev.clientX - rect.left) / rect.width * width;
          var idx = Math.round((mouseX / width) * (history.length - 1));
          idx = Math.max(0, Math.min(history.length - 1, idx));
          var item = history[idx];
          var hX = getX(idx);
          var hY = getY(item.bot_pnl, minVal, maxVal);

          var ch = $('chart-crosshair');
          var dot = $('chart-hover-dot');
          var tt = $('chart-tooltip');

          if (ch) {{
            ch.setAttribute('x1', hX);
            ch.setAttribute('x2', hX);
            ch.style.display = 'block';
          }}
          if (dot) {{
            dot.setAttribute('cx', hX);
            dot.setAttribute('cy', hY);
            dot.style.display = 'block';
          }}
          if (tt) {{
            var timeStr = new Date(item.t * 1000).toLocaleTimeString();
            tt.innerHTML = '<strong>' + timeStr + '</strong><br>' +
              '本机挂单操作盈亏: <span style="color:' + (item.bot_pnl >= 0 ? '#10b981' : '#f43f5e') + '">' +
              (item.bot_pnl >= 0 ? '+' : '') + item.bot_pnl.toFixed(4) + ' USDT</span><br>' +
              '总账户资产: <span style="color:#38bdf8">$' + item.assets.toFixed(2) + ' USDT</span>';
            tt.style.display = 'block';
            var leftPct = (hX / width) * 100;
            if (leftPct > 70) {{
              tt.style.left = 'auto';
              tt.style.right = (100 - leftPct + 2) + '%';
            }} else {{
              tt.style.right = 'auto';
              tt.style.left = (leftPct + 2) + '%';
            }}
          }}
        }};

        svg.onmouseleave = function() {{
          var ch = $('chart-crosshair');
          var dot = $('chart-hover-dot');
          var tt = $('chart-tooltip');
          if (ch) ch.style.display = 'none';
          if (dot) dot.style.display = 'none';
          if (tt) tt.style.display = 'none';
        }};
      }}

      function patch(id, val) {{
        var el = $(id);
        if (el && val !== undefined && el.innerHTML !== val) {{
          el.innerHTML = val;
        }}
      }}

      function poll() {{
        fetch('/api/view', {{ cache: 'no-store' }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (!data) return;
            if (data.updated_at) {{
              lastSnapshotAt = data.updated_at;
              updateSnapshotAge();
            }}
            patch('status-badge', data.status_html);
            patch('snapshot-badge', data.snapshot_badge_html);
            patch('usdt-bal', data.usdt_bal);
            patch('global-pnl', data.global_pnl_html);
            var gp = $('global-pnl');
            if (gp) gp.className = 'stat-num ' + (data.global_pnl_cls || '');
            patch('realized', data.realized_html);
            var rEl = $('realized');
            if (rEl && data.realized_cls) rEl.className = data.realized_cls;
            patch('unrealized', data.unrealized_html);
            var uEl = $('unrealized');
            if (uEl && data.unrealized_cls) uEl.className = data.unrealized_cls;
            patch('uptime', data.uptime_str || data.uptime);
            patch('stale-banner', data.stale_html || '');
            patch('perf-body', data.perf_html);
            patch('pos-body', data.pos_html);
            patch('manual-close-note', data.manual_close_note || '');
            patch('ord-body', data.ord_html);
            patch('exec-body', data.exec_html);
            patch('logbox', data.log_html);

            if (autoScroll) {{
              var lb = $('logbox');
              if (lb) lb.scrollTop = lb.scrollHeight;
            }}

            if (data.history && data.history.length > 0) {{
              lastHistory = data.history;
              renderChart(lastHistory, chartMode);
            }}
          }})
          .catch(function(err) {{}});
      }}

      function pollMarkets() {{
        fetch('/api/markets', {{ cache: 'no-store' }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (!data) return;
            patch('markets-body', data.markets_html);
            patch('markets-meta', data.markets_meta);
          }})
          .catch(function(err) {{}});
      }}

      window.restartStrategy = function() {{
        if (!confirm('确认重新启动交易策略？重启后本机挂单盈亏将立即自动归零。')) return;
        var btn = $('restart-btn');
        if (btn) btn.disabled = true;
        fetch('/api/restart', {{
          method: 'POST',
          headers: {{ 'X-Requested-With': 'OKX-Dashboard' }}
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            alert(data.message || '重启指令已下发');
            if (btn) btn.disabled = false;
            poll();
          }}).catch(function() {{
            alert('重启请求发送异常');
            if (btn) btn.disabled = false;
          }});
      }};

      window.stopStrategy = function() {{
        if (!confirm('确定要停止交易策略并撤回运行吗？')) return;
        var btn = $('stop-btn');
        if (btn) btn.disabled = true;
        fetch('/api/stop', {{
          method: 'POST',
          headers: {{ 'X-Requested-With': 'OKX-Dashboard' }}
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            alert(data.message || '停止指令已下发');
            if (btn) btn.disabled = false;
            poll();
          }}).catch(function() {{
            alert('停止请求发送异常');
            if (btn) btn.disabled = false;
          }});
      }};

      // Manual Close Helpers
      var manualTarget = {{ pair: '', side: '', amount: 0, breakeven: 0 }};
      window.closeDialog = function() {{
        var dialog = $('manual-close-dialog');
        if (!dialog) return;
        dialog.classList.remove('is-open');
        dialog.hidden = true;
      }};
      window.openManualClose = function(pair, side, amount, breakeven) {{
        manualTarget = {{ pair: pair, side: side, amount: amount, breakeven: breakeven }};
        $('manual-close-title').textContent = '手工平仓盈亏录入: ' + pair + ' (' + side + ')';
        $('manual-close-pnl').value = '0.00';
        var dialog = $('manual-close-dialog');
        dialog.hidden = false;
        dialog.classList.add('is-open');
        var input = $('manual-close-pnl');
        if (input) input.focus();
      }};
      window.saveManualClose = function(pnlVal) {{
        var val = (pnlVal !== undefined) ? pnlVal : parseFloat($('manual-close-pnl').value || '0');
        fetch('/api/manual-close', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Requested-With': 'OKX-Dashboard'
          }},
          body: JSON.stringify({{
            pair: manualTarget.pair,
            side: manualTarget.side,
            amount: manualTarget.amount,
            breakeven: manualTarget.breakeven,
            pnl: val
          }})
        }}).then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (data && !data.ok && data.message) alert(data.message);
            closeDialog();
            poll();
          }}).catch(function() {{
            alert('手工平仓提交失败');
            closeDialog();
          }});
      }};

      window.requestPositionClose = function(controller) {{
        if (!confirm('确认市价平掉 [' + controller + '] 当前仓位并撤销开仓挂单？')) return;
        fetch('/api/close-position', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Requested-With': 'OKX-Dashboard'
          }},
          body: JSON.stringify({{ controller: controller }})
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            alert(data.message || '平仓指令已下发');
            poll();
          }})
          .catch(function() {{ alert('平仓请求发送异常'); }});
      }};

      window.addEventListener('load', function() {{
        var hashTab = (window.location.hash || '').replace('#', '');
        var savedTab = '';
        try {{ savedTab = localStorage.getItem('okx_active_tab') || ''; }} catch(e) {{}}
        var init = hashTab || savedTab || '{active_tab}';
        if (['overview', 'positions', 'markets', 'logs', 'config'].indexOf(init) !== -1) {{
          switchTab(init);
        }}
        poll();
        setInterval(poll, POLL_MS);
        setInterval(function() {{
          var c = $('clock');
          if (c) c.textContent = new Date().toLocaleTimeString();
          updateSnapshotAge();
        }}, 1000);

        var mCancel = $('manual-close-cancel');
        if (mCancel) mCancel.onclick = closeDialog;
        var mZero = $('manual-close-zero');
        if (mZero) mZero.onclick = function() {{ saveManualClose(0); }};
        var mSave = $('manual-close-save');
        if (mSave) mSave.onclick = function() {{ saveManualClose(); }};
        var mDialog = $('manual-close-dialog');
        if (mDialog) {{
          mDialog.addEventListener('click', function(e) {{
            if (e.target === mDialog) closeDialog();
          }});
        }}
        document.addEventListener('keydown', function(e) {{
          if (e.key === 'Escape') closeDialog();
        }});

        document.addEventListener('click', function(e) {{
          var target = e.target;
          if (!target || !target.closest) return;
          var manual = target.closest('.manual-close');
          if (manual) {{
            window.openManualClose(
              manual.getAttribute('data-pair'),
              manual.getAttribute('data-side'),
              parseFloat(manual.getAttribute('data-amount')),
              parseFloat(manual.getAttribute('data-breakeven'))
            );
            return;
          }}
          var closeBtn = target.closest('.close-position');
          if (closeBtn) {{
            var ctrl = closeBtn.getAttribute('data-controller');
            if (ctrl) window.requestPositionClose(ctrl);
          }}
        }});
      }});
    }})();
  </script>
</head>
<body>
  <!-- ─── Institutional Header ─── -->
  <header class="site-header">
    <div class="brand-section">
      <svg class="brand-logo-svg" width="28" height="28" viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="6" fill="#0f131a" stroke="rgba(255,255,255,0.12)"/>
        <path d="M16 6L26 16L16 26L6 16L16 6Z" stroke="#e2b714" stroke-width="1.6"/>
        <circle cx="16" cy="16" r="3.2" fill="#38bdf8"/>
        <path d="M16 3V6M16 26V29M3 16H6M26 16H29" stroke="rgba(226,183,20,0.6)" stroke-width="1.2"/>
      </svg>
      <div class="brand-titles">
        <a href="/" class="brand-main">OKX QUANT TRADER</a>
        <span class="brand-sub">5M MEAN REVERSION · PRO TERMINAL</span>
      </div>
    </div>

    <!-- ─── Tab Strip (No Emoji) ─── -->
    <nav class="tab-strip" role="tablist">
      <button type="button" class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">
        <svg class="tab-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
        <span>OVERVIEW</span>
      </button>
      <button type="button" class="tab-btn" data-tab="positions" onclick="switchTab('positions')">
        <svg class="tab-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
        <span>POSITIONS &amp; ORDERS</span>
      </button>
      <button type="button" class="tab-btn" data-tab="markets" onclick="switchTab('markets')">
        <svg class="tab-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        <span>MARKETS</span>
      </button>
      <button type="button" class="tab-btn" data-tab="logs" onclick="switchTab('logs')">
        <svg class="tab-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
        <span>LOGS</span>
      </button>
      <button type="button" class="tab-btn" data-tab="config" onclick="switchTab('config')">
        <svg class="tab-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        <span>CONFIG</span>
      </button>
    </nav>

    <!-- ─── Controls ─── -->
    <div class="top-status-group">
      <span id="status-badge">{view["status_html"]}</span>
      <span id="snapshot-badge">{view["snapshot_badge_html"]}</span>
      <span id="snapshot-age" class="snapshot-age-tag {stale_cls}">{age_label}</span>
      <span id="clock" class="clock-display">{now_str}</span>
      <button type="button" id="restart-btn" class="btn btn-action-warn" onclick="restartStrategy()">RESTART</button>
      <button type="button" id="stop-btn" class="btn btn-action-neg" onclick="stopStrategy()">STOP</button>
    </div>
  </header>

  <!-- ─── Main Content ─── -->
  <main class="container">
    <div id="stale-banner">{view.get("stale_html", "")}</div>

    <!-- ══════════════ TAB 1: OVERVIEW ══════════════ -->
    <div class="tab-pane active" id="tab-overview">
      <!-- 5-Column Metric Ribbon -->
      <div class="stat-ribbon">
        <div class="stat-card highlight">
          <div class="stat-label">
            <span>TOTAL ASSETS (总资产)</span>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--color-accent)" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 6v2m0 8v2"/></svg>
          </div>
          <div class="stat-num" id="usdt-bal">${view["usdt_bal"]}<span class="stat-unit">USDT</span></div>
          <div class="stat-sub">OKX 永续合约保证金总权益</div>
        </div>

        <div class="stat-card">
          <div class="stat-label">
            <span>GLOBAL PNL (总盈亏)</span>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
          </div>
          <div class="stat-num {view['global_pnl_cls']}" id="global-pnl">{view['global_pnl_html']}</div>
          <div class="stat-sub">REALIZED + UNREALIZED 实时求和</div>
        </div>

        <div class="stat-card">
          <div class="stat-label">
            <span>REALIZED / UNREALIZED</span>
            <span class="badge green">重启清零</span>
          </div>
          <div class="stat-num" style="font-size: 17px;">
            <span id="realized" class="{view['realized_cls']}">{view['realized_html']}</span>
            <span style="color: var(--color-ink-faint); margin: 0 4px;">/</span>
            <span id="unrealized" class="{view['unrealized_cls']}">{view['unrealized_html']}</span>
          </div>
          <div class="stat-sub">已实现落袋 / 浮动未实现 (USDT)</div>
        </div>

        <div class="stat-card">
          <div class="stat-label">
            <span>ENGINE RUNTIME</span>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          </div>
          <div class="stat-num" id="uptime">{fmt_uptime(view["uptime_s"])}</div>
          <div class="stat-sub">{view["strategy"]}</div>
        </div>
      </div>

      <!-- Real-time Trajectory Chart Panel -->
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">PORTFOLIO ASSETS &amp; BOT OPERATION PERFORMANCE</h2>
            <span class="panel-meta">可视化分别展示总资产和本机挂单盈亏 · 本机操作盈亏每次重启自动归零起算</span>
          </div>
          <div class="chart-controls">
            <button type="button" class="chart-tab active" data-mode="bot" onclick="setChartMode('bot')">BOT OPERATION PNL (挂单盈亏)</button>
            <button type="button" class="chart-tab" data-mode="assets" onclick="setChartMode('assets')">TOTAL ASSETS (总资产)</button>
            <button type="button" class="chart-tab" data-mode="dual" onclick="setChartMode('dual')">DUAL VIEW (双轨对比)</button>
          </div>
        </div>
        <div class="chart-canvas-wrap">
          <svg id="equity-svg" viewBox="0 0 1000 240" preserveAspectRatio="none">
            <defs>
              <linearGradient id="pnlGradPos" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#10b981" stop-opacity="0.35"/>
                <stop offset="100%" stop-color="#10b981" stop-opacity="0.00"/>
              </linearGradient>
              <linearGradient id="pnlGradNeg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#f43f5e" stop-opacity="0.35"/>
                <stop offset="100%" stop-color="#f43f5e" stop-opacity="0.00"/>
              </linearGradient>
              <linearGradient id="assetsGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.30"/>
                <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.00"/>
              </linearGradient>
            </defs>
            <path id="chart-area" d="" fill="url(#pnlGradPos)"/>
            <path id="chart-line" d="" fill="none" stroke="#10b981" stroke-width="2.2"/>
            <path id="chart-assets-line" d="" fill="none" stroke="#38bdf8" stroke-width="2.0" style="display:none;"/>
            <line id="chart-zero-line" x1="0" y1="120" x2="1000" y2="120" stroke="rgba(255,255,255,0.18)" stroke-dasharray="3,3"/>
            <line id="chart-crosshair" x1="0" y1="0" x2="0" y2="240" stroke="rgba(56,189,248,0.7)" stroke-width="1.2" stroke-dasharray="2,2" style="display:none;"/>
            <circle id="chart-hover-dot" cx="0" cy="0" r="4.5" fill="#38bdf8" stroke="#ffffff" stroke-width="1.8" style="display:none;"/>
          </svg>
          <div id="chart-tooltip" class="chart-tooltip" style="display:none;"></div>
        </div>
        <div class="chart-metrics-bar">
          <div class="chart-metric">
            <span class="chart-metric-label">BOT SESSION RETURN (本次)</span>
            <span class="chart-metric-val {view['bot_session_pnl_cls']}" id="chart-stat-bot">{view['bot_session_pnl_html']} USDT</span>
          </div>
          <div class="chart-metric">
            <span class="chart-metric-label">SESSION PEAK GAIN</span>
            <span class="chart-metric-val pos" id="chart-stat-peak">--</span>
          </div>
          <div class="chart-metric">
            <span class="chart-metric-label">TOTAL ACCOUNT ASSETS</span>
            <span class="chart-metric-val" id="chart-stat-assets">${view['usdt_bal']} USDT</span>
          </div>
          <div class="chart-metric">
            <span class="chart-metric-label">RUNTIME DATA TICKS</span>
            <span class="chart-metric-val" id="chart-stat-ticks">0</span>
          </div>
        </div>
      </div>

      <!-- Performance Summary Table -->
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">STRATEGY CONTROLLER PERFORMANCE MATRIX</h2>
            <span class="panel-meta">|Z| ≥ 2.0 偏离触发建仓 · 动态 ATR 止损与现金止盈 · 真实扣费结算</span>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>CONTROLLER</th><th>ACTION</th><th>Z-SCORE DEVIATION</th><th>REALIZED (USDT)</th><th>UNREALIZED (USDT)</th>
              <th>GLOBAL PNL</th><th>RETURN%</th><th>VOLUME TRADED (USDT)</th>
            </tr></thead>
            <tbody id="perf-body">{view["perf_html"]}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══════════════ TAB 2: POSITIONS & ORDERS ══════════════ -->
    <div class="tab-pane" id="tab-positions">
      <!-- Active Positions -->
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">ACTIVE MARGIN POSITIONS</h2>
            <span class="panel-meta" id="manual-close-note">{html.escape(view.get("manual_close_note") or "")}</span>
          </div>
        </div>
        
        <!-- Manual Close Dialog -->
        <div id="manual-close-dialog" class="modal-backdrop" hidden>
          <div class="modal-card">
            <div class="modal-header">
              <h3 id="manual-close-title">MANUAL POSITION CLOSE SETTLEMENT</h3>
              <button type="button" class="modal-close-x" onclick="closeDialog()" aria-label="关闭">&times;</button>
            </div>
            <div class="modal-body">
              <p style="font-size: 12px; color: var(--color-ink-muted); margin-bottom: 12px;">
                若已在 OKX 交易所手动市价平仓该仓位，请录入结算净盈亏金额，面板将自动同步并记入已实现盈亏统计。
              </p>
              <div class="input-unit-group">
                <input id="manual-close-pnl" inputmode="decimal" autocomplete="off" placeholder="例如 1.25 或 -0.40">
                <span class="unit-tag">USDT</span>
              </div>
            </div>
            <div style="display:flex; justify-content:flex-end; gap:8px;">
              <button type="button" id="manual-close-cancel" class="btn btn-secondary">CANCEL</button>
              <button type="button" id="manual-close-zero" class="btn btn-subtle">MARK AS 0</button>
              <button type="button" id="manual-close-save" class="btn btn-primary">CONFIRM PNL</button>
            </div>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>TRADING PAIR</th><th>MANUAL RECORD</th><th>SIDE</th><th>AMOUNT</th><th>NOTIONAL VALUE</th>
              <th>BREAKEVEN PRICE</th><th>UNREALIZED PNL</th><th>REALIZED PNL</th><th>FEES</th>
            </tr></thead>
            <tbody id="pos-body">{view["pos_html"]}</tbody>
          </table>
        </div>
      </div>

      <!-- Active Open Orders -->
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">OPEN LIMIT ORDERS</h2>
            <span class="panel-meta">待成交限价入场或回归平仓订单</span>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>TRADING PAIR</th><th>SIDE</th><th>PRICE</th><th>AMOUNT</th><th>ORDER AGE</th>
            </tr></thead>
            <tbody id="ord-body">{view["ord_html"]}</tbody>
          </table>
        </div>
      </div>

      <!-- Position Executors Lifecycle -->
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">POSITION EXECUTORS MONITOR</h2>
            <span class="panel-meta">入场、ATR 动态止损与收益锁定生命周期监控</span>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>CONTROLLER</th><th>SIDE</th><th>STATUS</th><th>NET PNL</th>
              <th>PNL%</th><th>VOLUME</th><th>LIVE</th><th>CLOSE TYPE</th><th>EXECUTOR AGE</th>
            </tr></thead>
            <tbody id="exec-body">{view["exec_html"]}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══════════════ TAB 3: MARKETS ══════════════ -->
    <div class="tab-pane" id="tab-markets">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">OKX PERPETUAL SWAP MARKET OVERVIEW</h2>
            <span class="panel-meta" id="markets-meta">{markets_data["markets_meta"]}</span>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>TRADING PAIR</th><th>LAST PRICE</th><th>24H CHANGE</th><th>BID PRICE</th><th>ASK PRICE</th>
              <th>SPREAD</th><th>24H HIGH</th><th>24H LOW</th><th>24H VOLUME (USDT)</th>
            </tr></thead>
            <tbody id="markets-body">{markets_data["markets_html"]}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══════════════ TAB 4: LOGS ══════════════ -->
    <div class="tab-pane" id="tab-logs">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-titles">
            <h2 class="panel-title">PROCESS CONSOLE LOG STREAM</h2>
            <span class="panel-meta">实时终端进程输出 (Latest 60 lines)</span>
          </div>
          <div class="log-toolbar" style="border:none; padding:0; background:transparent;">
            <button type="button" id="autoscroll-btn" class="log-tool-btn active" onclick="toggleAutoScroll()">AUTO-SCROLL: ON</button>
            <input id="log-filter" class="log-filter-input" placeholder="Filter log lines..." oninput="filterLogs(this.value)">
            <button type="button" class="log-tool-btn" onclick="clearLogs()">CLEAR</button>
            <button type="button" class="log-tool-btn" onclick="copyLogs()">COPY</button>
          </div>
        </div>
        <div class="log-box" id="logbox">{view["log_html"]}</div>
      </div>
    </div>

    <!-- ══════════════ TAB 5: CONFIG ══════════════ -->
    <div class="tab-pane" id="tab-config">
      {config_body}
    </div>
  </main>

  <!-- ─── Institutional Footer ─── -->
  <footer class="site-footer">
    <span>OKX QUANTITATIVE TRADER // PRO INSTITUTIONAL TERMINAL · 5M MEAN REVERSION</span>
    <span>STATUS: <span style="color:var(--color-pos)">ENGINE ONLINE</span> · LATENCY {age_label}</span>
  </footer>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默日志

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/api/status", "/api/view", "/api/markets", "/api/config", "/api/presets"):
                if path == "/api/config":
                    try:
                        data = config_view()
                    except ConfigError as exc:
                        self._send_json(400, {"ok": False, "message": str(exc)})
                        return
                elif path == "/api/presets":
                    data = get_presets_info()
                else:
                    status = read_status()
                    if path == "/api/status":
                        data = status
                    elif path == "/api/markets":
                        data = markets_view(status)
                    else:
                        data = build_view(status)
                self._send_json(200, data)

            elif path in ("/", "/index.html", "/markets", "/config"):
                status = {} if path == "/config" else read_status()
                page = {"/markets": "markets", "/config": "config"}.get(path, "dashboard")
                page_html = render_html(status, page).encode("utf-8")
                self._send(200, page_html, "text/html; charset=utf-8")
            else:
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
                if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    self._send_json(403, {"ok": False, "message": "只允许从本机控制策略。"})
                    return
                if (self.headers.get("X-Requested-With") or "").strip().lower() != "okx-dashboard":
                    self._send_json(403, {"ok": False, "message": "请求校验失败。"})
                    return
                control = restart_strategy if path == "/api/restart" else stop_strategy
                started, message = control()
                self._send_json(202 if started else 409, {"ok": started, "message": message})
                return

            if path == "/api/close-position":
                if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    self._send_json(403, {"ok": False, "message": "只允许从本机平仓。"})
                    return
                if (self.headers.get("X-Requested-With") or "").strip().lower() != "okx-dashboard":
                    self._send_json(403, {"ok": False, "message": "请求校验失败。"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length < 0 or length > 4_000:
                    self._send_json(413, {"ok": False, "message": "请求过大"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    message = request_position_close(str(payload.get("controller") or ""))
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "message": "请求不是 JSON"})
                    return
                self._send_json(200, {"ok": True, "message": message})
                return
            if path == "/api/manual-close":
                if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    self._send_json(403, {"ok": False, "message": "只允许从本机标记手动平仓。"})
                    return
                if (self.headers.get("X-Requested-With") or "").strip().lower() != "okx-dashboard":
                    self._send_json(403, {"ok": False, "message": "请求校验失败。"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length < 0 or length > 8_000:
                    self._send_json(413, {"ok": False, "message": "请求过大"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    pair = str(payload.get("pair") or "")
                    closes = mark_manual_close(
                        pair,
                        str(payload.get("side") or ""),
                        payload.get("amount"),
                        payload.get("breakeven"),
                        payload.get("pnl"),
                    )
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "message": "请求不是 JSON"})
                    return
                self._send_json(200, {
                    "ok": True,
                    "closes": closes,
                    "message": f"{pair.strip().upper()} 已记入手动平仓盈亏。",
                })
                return
            if path != "/api/config":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > 256_000:
                self._send_json(413, {"ok": False, "message": "请求过大"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                written = save_config(payload)
            except ConfigError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
                return
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "message": "请求不是 JSON"})
                return
            self._send_json(200, {
                "ok": True,
                "files": written["files"],
                "message": "已写入 conf/controllers。请停止并重新启动策略后生效。",
            })
        except BrokenPipeError:
            return
        except Exception as exc:
            _log(f"request {self.path} failed: {type(exc).__name__}: {exc}")
            try:
                self._send_json(500, {"ok": False, "message": "保存失败"})
            except Exception:
                pass


def main():
    print(f"📊 OKX Quant Dashboard 启动中...", flush=True)
    print(f"   状态文件 : {STATUS_FILE}", flush=True)
    print(f"   日志文件 : {LOG_FILE}", flush=True)
    print(f"   访问地址 : http://127.0.0.1:{PORT}", flush=True)
    print(f"   按 Ctrl+C 停止", flush=True)
    server = ThreadingHTTPServer((os.environ.get("DASHBOARD_HOST", "127.0.0.1"), PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)


if __name__ == "__main__":
    main()
