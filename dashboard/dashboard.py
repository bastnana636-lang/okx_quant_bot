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
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from strategy_config import SHARED_FIELDS, ConfigError, apply_config, load_config
except ImportError:  # imported as dashboard.dashboard
    from dashboard.strategy_config import SHARED_FIELDS, ConfigError, apply_config, load_config

# ─── 路径配置 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
STATUS_FILE = BASE_DIR / "data" / "bot" / "status.json"
LOG_FILE    = BASE_DIR / "logs" / "logs_conf_okx_multi.log"
PORT        = 8888
STALE_AFTER_S = 20  # 引擎每 5 秒写一次快照；超过该秒数视为过期
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
TICKER_TTL_S = 5
_ticker_lock = threading.Lock()
_ticker_cache: dict = {"fetched_at": 0.0, "by_inst": {}, "error": None}


def _log(msg: str) -> None:
    print(f"[dashboard] {msg}", flush=True)


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
    "BTC":  "₿  BTC",
    "ETH":  "Ξ  ETH",
    "SOL":  "◎  SOL",
    "XRP":  "✕  XRP",
    "DOGE": "Ð  DOGE",
    "SUI":  "◈  SUI",
    "SNDK": "💾  SNDK",
    "ONE":  "✦  ONE",
    "ZEC":  "ℤ  ZEC",
    "UNI":  "🦄 UNI",
    "ADA":  "₳  ADA",
    "BNB":  "◆  BNB",
    "LTC":  "Ł  LTC",
    "AVAX": "▲  AVAX",
    "MATIC":"⬡  MATIC",
    "DOT":  "●  DOT",
    "LINK": "⬡  LINK",
    "TRX":  "♦  TRX",
    "ATOM": "⚛  ATOM",
    "OP":   "◉  OP",
    "ARB":  "◈  ARB",
}

def coin_label(controller: str) -> str:
    """将 okx_pmm_btc 之类的 controller 名转换为 '₿ BTC' 格式。
    GLOBAL TOTAL 原样保留。未知币种自动大写展示。"""
    if controller == "GLOBAL TOTAL":
        return "∑  TOTAL"
    # 提取末尾的币种名，如 okx_pmm_btc → btc → BTC
    symbol = controller.split("_")[-1].upper()
    return COIN_ICONS.get(symbol, f"◆  {symbol}")


def pair_label(pair: str) -> str:
    symbol = pair.split("-")[0].upper()
    return COIN_ICONS.get(symbol, f"◆  {symbol}")


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
                + '<td colspan="8" class="empty">-- NO QUOTE --</td></tr>'
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
        rows.append('<tr><td colspan="9" class="empty">-- NO MARKETS --</td></tr>')
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


def build_view(status: dict) -> dict:
    """把 status.json 收成面板可原地补丁的字段（数字 + 表格 HTML）。"""
    err = status.get("error")
    if err:
        return {"error": str(err)}

    engine    = status.get("engine", {}) or {}
    balances  = (status.get("balances", {}) or {}).get("okx_perpetual", {}) or {}
    perf_rows = parse_performance_table(status)
    positions = parse_positions(status)
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
    global_row = next((r for r in perf_rows if r["controller"] == "GLOBAL TOTAL"), None)
    global_pnl = global_row["global"] if global_row else 0
    glob_pct   = global_row["global_pct"] if global_row else "-"
    realized   = global_row["realized"] if global_row else 0
    unrealized = global_row["unrealized"] if global_row else 0

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

        perf_html += (
            f'<tr class="{cls}">'
            f'<td>{coin_label(ctrl)}</td>'
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
        pos_html += (
            f"<tr>"
            f"<td>{p['pair']}</td>"
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
        pos_html = '<tr><td colspan="8" class="empty">-- NO POSITIONS --</td></tr>'

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
        ord_html = '<tr><td colspan="5" class="empty">-- NO ORDERS --</td></tr>'

    exec_html = ""
    for e in executors:
        side_cls = "pos" if e["side"] == "BUY" else "neg"
        badge_cls = {"RUNNING": "badge green", "TERMINATED": "badge gray"}.get(e["status"], "badge gray")
        trading_ind = "[*]" if e["is_trading"] else "[ ]"
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
        exec_html = '<tr><td colspan="9" class="empty">-- NO EXECUTORS --</td></tr>'

    log_html = "\n".join(
        f'<div class="log-line {"log-warn" if "WARNING" in line or "ERROR" in line else ""}">{line}</div>'
        for line in tail_log(60)
    )
    stale_html = (
        f'<div class="card error">⚠️ 快照停留在 {updated_str}，挂单可能与 OKX 不一致</div>'
        if stale else ""
    )

    return {
        "error": None,
        "updated_at": updated_at,
        "uptime_s": uptime_s,
        "running": running,
        "stale": stale,
        "strategy": engine.get("strategy_file_name", "-") or "-",
        "status_html": (
            '<span class="badge green">ON</span>' if running
            else '<span class="badge red">OFF</span>'
        ),
        "snapshot_badge_html": (
            '<span class="badge green">LIVE</span>' if not stale
            else '<span class="badge red">STALE</span>'
        ),
        "usdt_bal": f"{usdt_bal:,.2f}",
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
        "ord_html": ord_html,
        "exec_html": exec_html,
        "log_html": log_html,
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
            "<td>{pair}</td>"
            "<td><input data-k=\"leverage\" value=\"{leverage}\" inputmode=\"numeric\" autocomplete=\"off\"></td>"
            "<td><input data-k=\"total_amount_quote\" value=\"{amount}\" inputmode=\"decimal\" autocomplete=\"off\"></td>"
            "<td><input data-k=\"take_profit_quote\" value=\"{tp}\" inputmode=\"decimal\" autocomplete=\"off\"></td>"
            "</tr>".format(
                file=html.escape(pair["file"]),
                pair=html.escape(pair_label(pair["trading_pair"]) + "  " + pair["trading_pair"]),
                leverage=html.escape(pair["leverage"]),
                amount=html.escape(pair["total_amount_quote"]),
                tp=html.escape(pair["take_profit_quote"]),
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
            '<p class="hint warn">这些参数在各币种间不一致，保存后会写成同一个值：'
            + html.escape("、".join(mixed)) + "</p>"
        )
    return f"""
        <form id="config-form">
          <div class="card">
            <h2>// LEVERAGE</h2>
            <p class="hint">杠杆默认 3，允许 1 到 5。填好后点「应用到全部币种」，或在下表逐个修改。</p>
            <div class="inline-form">
              <label class="field"><span>统一杠杆</span>
                <input id="apply-leverage" value="3" inputmode="numeric" autocomplete="off">
              </label>
              <button type="button" id="apply-leverage-btn">应用到全部币种</button>
            </div>
          </div>
          <div class="card">
            <h2>// ORDER SIZE</h2>
            <p class="hint">开单金额是单笔最大名义本金（USDT），不是保证金。止盈是单笔锁定的现金利润。</p>
            <table>
              <thead><tr>
                <th>PAIR</th><th>LEVERAGE</th><th>NOTIONAL USDT</th><th>CASH TP</th>
              </tr></thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
          </div>
          <div class="card">
            <h2>// STRATEGY PARAMS</h2>
            <p class="hint">下面的参数对当前启用的全部币种生效。保存写入 conf/controllers，重启策略后才会用于实盘。</p>
            {mixed_html}
            <div class="field-grid">{shared_controls}</div>
          </div>
          <div class="save-bar">
            <button type="submit" class="save">保存配置</button>
            <span id="config-msg" class="hint"></span>
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
              msg.className = 'hint';
              msg.textContent = '保存中...';
              fetch('/api/config', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ pairs: pairs, shared: shared }})
              }}).then(function(response) {{
                return response.json().then(function(data) {{ return {{ ok: response.ok, data: data }}; }});
              }}).then(function(result) {{
                msg.className = result.ok && result.data.ok ? 'hint ok' : 'hint warn';
                msg.textContent = (result.data && result.data.message) || '保存失败';
              }}).catch(function() {{
                msg.className = 'hint warn';
                msg.textContent = '保存失败';
              }});
            }};
          }})();
        </script>"""


def markets_view(status: dict) -> dict:
    """行情页单独拉 OKX 公开报价，主面板轮询不带这份数据。"""
    err = status.get("error")
    if err:
        return {"error": str(err), "markets_html": "", "markets_meta": ""}
    html, meta = build_markets(strategy_pairs(status, parse_performance_table(status)))
    return {"error": None, "markets_html": html, "markets_meta": meta}


def render_html(status: dict, page: str = "dashboard") -> str:
    if page == "config":
        view = {}
        body = render_config_body()
    else:
        view = markets_view(status) if page == "markets" else build_view(status)
    if page != "config" and view.get("error"):
        body = f'<div class="card error">⚠️ 无法读取状态文件: {view["error"]}</div>'
    elif page == "config":
        pass
    elif page == "markets":
        body = f"""
        <div class="card">
          <h2>// MARKETS <span class="section-meta" id="markets-meta">{view["markets_meta"]}</span></h2>
          <table>
            <thead><tr>
              <th>PAIR</th><th>LAST</th><th>24H</th><th>BID</th><th>ASK</th>
              <th>SPREAD</th><th>HIGH 24H</th><th>LOW 24H</th><th>VOL 24H</th>
            </tr></thead>
            <tbody id="markets-body">{view["markets_html"]}</tbody>
          </table>
        </div>"""
    else:
        age_label = fmt_snapshot_age(
            (time.time() - view["updated_at"]) if view["updated_at"] else None
        )
        stale_cls = "neg" if view["stale"] else ""
        body = f"""
        <div id="stale-banner">{view["stale_html"]}</div>
        <div class="info-bar">
          <div class="info-card">
            <div class="label">STATUS</div>
            <div class="value" id="status-badge">{view["status_html"]}</div>
          </div>
          <div class="info-card">
            <div class="label">STRATEGY</div>
            <div class="value" id="strategy">{view["strategy"]}</div>
          </div>
          <div class="info-card">
            <div class="label">UPTIME</div>
            <div class="value" id="uptime">{fmt_uptime(view["uptime_s"])}</div>
          </div>
          <div class="info-card">
            <div class="label">USDT BAL</div>
            <div class="value" id="usdt-bal">{view["usdt_bal"]}</div>
          </div>
          <div class="info-card">
            <div class="label">GLOBAL PNL</div>
            <div class="value {view["global_pnl_cls"]}" id="global-pnl">{view["global_pnl_html"]}</div>
          </div>
          <div class="info-card">
            <div class="label">REAL / UNREAL</div>
            <div class="value">
              <span id="realized" class="{view["realized_cls"]}">{view["realized_html"]}</span> /
              <span id="unrealized" class="{view["unrealized_cls"]}">{view["unrealized_html"]}</span>
            </div>
          </div>
          <div class="info-card">
            <div class="label">SNAPSHOT <span id="snapshot-badge">{view["snapshot_badge_html"]}</span></div>
            <div class="value {stale_cls}" id="snapshot-age">{age_label}</div>
          </div>
        </div>
        <div class="card">
          <h2>// PERFORMANCE</h2>
          <table>
            <thead><tr>
              <th>CONTROLLER</th><th>Z-SCORE</th><th>REALIZED</th><th>UNREALIZED</th>
              <th>GLOBAL PNL</th><th>PNL%</th><th>VOLUME(USDT)</th>
            </tr></thead>
            <tbody id="perf-body">{view["perf_html"]}</tbody>
          </table>
        </div>
        <div class="card">
          <h2>// POSITIONS</h2>
          <table>
            <thead><tr>
              <th>PAIR</th><th>SIDE</th><th>AMT</th><th>VALUE</th>
              <th>BREAKEVEN</th><th>UNREAL PNL</th><th>REAL PNL</th><th>FEE</th>
            </tr></thead>
            <tbody id="pos-body">{view["pos_html"]}</tbody>
          </table>
        </div>
        <div class="card">
          <h2>// OPEN ORDERS</h2>
          <table>
            <thead><tr>
              <th>PAIR</th><th>SIDE</th><th>PRICE</th><th>AMT</th><th>AGE</th>
            </tr></thead>
            <tbody id="ord-body">{view["ord_html"]}</tbody>
          </table>
        </div>
        <div class="card">
          <h2>// EXECUTORS</h2>
          <table>
            <thead><tr>
              <th>CTRL</th><th>SIDE</th><th>STATUS</th><th>NET PNL</th>
              <th>PNL%</th><th>VOLUME</th><th>LIVE</th><th>CLOSE TYPE</th><th>AGE</th>
            </tr></thead>
            <tbody id="exec-body">{view["exec_html"]}</tbody>
          </table>
        </div>
        <div class="card">
          <h2>// LOG TAIL (60)</h2>
          <div class="log-box" id="logbox">{view["log_html"]}</div>
        </div>"""

    now_str = time.strftime("%H:%M:%S")
    titles = {
        "markets": ("OKX QUANT // MARKETS", "MARKETS"),
        "config": ("OKX QUANT // CONFIG", "CONFIG"),
    }
    page_title, heading = titles.get(page, ("OKX QUANT // DASHBOARD", "LIVE DASHBOARD"))
    nav_links = []
    for key, href, label in (
        ("dashboard", "/", "DASHBOARD"),
        ("markets", "/markets", "MARKETS"),
        ("config", "/config", "CONFIG"),
    ):
        active = "active" if page == key else ""
        nav_links.append(f'<a href="{href}" class="{active}">{label}</a>')
    nav_html = "\n      ".join(nav_links)
    footer = (
        "[ LOCAL CONFIG // RESTART STRATEGY TO APPLY ]"
        if page == "config"
        else "[ READ-ONLY MONITOR // NO TRADE SIDE EFFECTS ]"
    )
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=VT323&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:     #000000;
      --panel:  #0a0a0a;
      --border: #1a1a1a;
      --text:   #c8c8c8;
      --muted:  #555555;
      --pos:    #00ff41;
      --neg:    #ff2222;
      --zero:   #444444;
      --accent: #00e5ff;
      --warn:   #ffaa00;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'VT323', monospace;
      font-size: 18px;
      line-height: 1.5;
      letter-spacing: 0.04em;
    }}
    /* scanline overlay */
    body::after {{
      content: '';
      position: fixed; inset: 0; pointer-events: none; z-index: 9999;
      background: repeating-linear-gradient(
        to bottom,
        transparent 0px, transparent 3px,
        rgba(0,0,0,0.12) 3px, rgba(0,0,0,0.12) 4px
      );
    }}
    /* ── header ── */
    header {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 20px;
      border-bottom: 2px solid var(--accent);
      background: var(--bg);
    }}
    header h1 {{
      font-size: 26px; color: var(--accent);
      text-shadow: 0 0 10px var(--accent), 0 0 2px #fff;
      letter-spacing: 0.08em;
    }}
    header h1 a {{ color: inherit; text-decoration: none; }}
    .nav {{ display: flex; gap: 8px; align-items: center; }}
    .nav a {{
      color: var(--muted); text-decoration: none;
      border: 1px solid var(--border); padding: 2px 12px;
      letter-spacing: 0.08em;
    }}
    .nav a:hover, .nav a.active {{ color: var(--accent); border-color: var(--accent); }}
    .hint {{ color: var(--muted); font-size: 15px; margin: 0 0 10px; }}
    .hint.ok {{ color: var(--pos); }}
    .hint.warn {{ color: var(--warn); }}
    .field-grid {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px;
    }}
    .field span {{
      display: block; color: var(--muted); font-size: 13px;
      letter-spacing: 0.08em; text-transform: uppercase;
    }}
    .field input, .field select, td input {{
      width: 100%; background: #000; color: var(--text);
      border: 1px solid var(--border); font-family: inherit; font-size: 18px;
      padding: 4px 8px;
    }}
    td input {{ width: 8rem; }}
    .inline-form {{ display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }}
    .inline-form .field {{ width: 8rem; }}
    button, .save {{
      background: #000; color: var(--accent); border: 1px solid var(--accent);
      font-family: inherit; font-size: 18px; padding: 6px 14px; cursor: pointer;
    }}
    button:hover, .save:hover {{ background: #041418; }}
    .save-bar {{ display: flex; gap: 14px; align-items: center; margin: 4px 0 12px; }}
    .refresh-info {{ color: var(--muted); font-size: 15px; letter-spacing: 0.06em; }}
    /* ── layout ── */
    .container {{ max-width: 1600px; margin: 0 auto; padding: 14px 18px; }}
    /* ── info bar ── */
    .info-bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
    .info-card {{
      flex: 1 1 150px;
      background: var(--panel);
      border: 1px solid var(--border);
      padding: 10px 14px;
    }}
    .info-card .label {{
      color: var(--muted); font-size: 13px; margin-bottom: 4px;
      text-transform: uppercase; letter-spacing: 0.1em;
    }}
    .info-card .value {{ font-size: 20px; color: var(--text); }}
    /* ── cards ── */
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      padding: 12px 16px;
      margin-bottom: 10px;
      overflow-x: auto;
    }}
    .card.error {{ color: var(--warn); border-color: var(--warn); }}
    .card h2 {{
      font-size: 20px; color: var(--accent); margin-bottom: 10px;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border);
      text-transform: uppercase; letter-spacing: 0.1em;
    }}
    .section-meta {{
      color: var(--muted); font-size: 14px; letter-spacing: 0.06em;
      text-transform: none; margin-left: 8px;
    }}
    /* ── tables ── */
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      color: var(--muted); font-size: 14px; text-align: left;
      padding: 5px 10px; border-bottom: 1px solid #222;
      white-space: nowrap; text-transform: uppercase; letter-spacing: 0.06em;
    }}
    td {{
      font-size: 16px; padding: 4px 10px;
      border-bottom: 1px solid #111; white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #0d0d0d; }}
    .total-row td {{ background: #0c0c0c; color: var(--accent); }}
    /* ── colours ── */
    .pos  {{ color: var(--pos);  text-shadow: 0 0 6px var(--pos);  }}
    .neg  {{ color: var(--neg);  text-shadow: 0 0 6px var(--neg);  }}
    .zero {{ color: var(--zero); }}
    .center {{ text-align: center; }}
    .empty {{ color: var(--muted); text-align: center; padding: 12px 0; font-size: 15px; }}
    /* ── badges ── */
    .badge {{
      display: inline-block; padding: 1px 7px;
      font-size: 15px; border: 1px solid currentColor;
    }}
    .badge.green  {{ color: var(--pos);   border-color: var(--pos); }}
    .badge.red    {{ color: var(--neg);   border-color: var(--neg); }}
    .badge.yellow {{ color: var(--warn);  border-color: var(--warn); }}
    .badge.gray   {{ color: var(--muted); border-color: var(--muted); }}
    /* ── log ── */
    .log-box {{
      background: #000; border: 1px solid #1a1a1a;
      padding: 10px; max-height: 340px; overflow-y: auto;
      font-family: 'VT323', monospace; font-size: 14px; line-height: 1.6;
    }}
    .log-line {{ color: #3a3a3a; word-break: break-all; }}
    .log-warn  {{ color: var(--warn); text-shadow: 0 0 4px var(--warn); }}
    /* ── footer ── */
    footer {{
      text-align: center; padding: 10px;
      color: var(--muted); font-size: 13px;
      border-top: 1px solid var(--border);
    }}
    /* ── cursor blink ── */
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
    .cursor {{ animation: blink 1s step-end infinite; color: var(--accent); }}
    /* ── scrollbar ── */
    ::-webkit-scrollbar {{ width: 4px; height: 4px; background: #000; }}
    ::-webkit-scrollbar-thumb {{ background: #222; }}
  </style>
  <script>
    (function() {{
      var POLL_MS = 3000;
      var PAGE = "{page}";
      var last = {{}};
      var meta = {{ updatedAt: 0, uptimeS: 0, fetchedAt: Date.now() }};

      function $(id) {{ return document.getElementById(id); }}
      function pad(n) {{ return String(n).padStart(2, '0'); }}
      function fmtClock(d) {{
        return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
      }}
      function fmtUptime(s) {{
        s = Math.max(0, Math.floor(s));
        return pad(Math.floor(s / 3600)) + 'h ' + pad(Math.floor((s % 3600) / 60)) + 'm ' + pad(s % 60) + 's';
      }}
      function fmtAge(age) {{
        if (age == null || isNaN(age)) return 'no snapshot';
        if (age < 60) return Math.floor(age) + 's ago';
        return (age / 60).toFixed(1) + 'm ago';
      }}
      function setHtml(id, html) {{
        if (last[id] === html) return false;
        last[id] = html;
        var el = $(id);
        if (el) el.innerHTML = html;
        return true;
      }}
      function setText(id, text) {{
        var el = $(id);
        if (el && el.textContent !== text) el.textContent = text;
      }}
      function setClass(id, cls) {{
        var el = $(id);
        if (el) el.className = cls;
      }}
      function tickClock() {{
        var now = Date.now();
        setText('clock', fmtClock(new Date(now)));
        if (!meta.updatedAt) return;
        var age = (now / 1000) - meta.updatedAt;
        var liveUptime = meta.uptimeS + (now - meta.fetchedAt) / 1000;
        setText('uptime', fmtUptime(liveUptime));
        setText('snapshot-age', fmtAge(age));
        var stale = age > {STALE_AFTER_S};
        setClass('snapshot-age', stale ? 'value neg' : 'value');
      }}
      function apply(data) {{
        if (data.error) {{
          var box = $('app');
          if (box) box.innerHTML = '<div class="card error">⚠️ 无法读取状态文件: ' + data.error + '</div>';
          last = {{}};
          return;
        }}
        if (PAGE === 'config') return;
        if (PAGE === 'markets') {{
          setText('markets-meta', data.markets_meta || '');
          setHtml('markets-body', data.markets_html);
          tickClock();
          return;
        }}
        meta.updatedAt = data.updated_at || 0;
        meta.uptimeS = data.uptime_s || 0;
        meta.fetchedAt = Date.now();
        setHtml('status-badge', data.status_html);
        setText('strategy', data.strategy);
        setText('usdt-bal', data.usdt_bal);
        setHtml('global-pnl', data.global_pnl_html);
        setClass('global-pnl', 'value ' + (data.global_pnl_cls || ''));
        setText('realized', data.realized_html);
        setClass('realized', data.realized_cls || '');
        setText('unrealized', data.unrealized_html);
        setClass('unrealized', data.unrealized_cls || '');
        setHtml('snapshot-badge', data.snapshot_badge_html);
        setHtml('stale-banner', data.stale_html || '');
        setHtml('perf-body', data.perf_html);
        setHtml('pos-body', data.pos_html);
        setHtml('ord-body', data.ord_html);
        setHtml('exec-body', data.exec_html);
        var lb = $('logbox');
        if (lb && last['logbox'] !== data.log_html) {{
          var atBottom = lb.scrollHeight - lb.scrollTop - lb.clientHeight < 24;
          lb.innerHTML = data.log_html;
          last['logbox'] = data.log_html;
          if (atBottom) lb.scrollTop = lb.scrollHeight;
        }}
        tickClock();
      }}
      function poll() {{
        if (PAGE === 'config') return;
        var url = PAGE === 'markets' ? '/api/markets' : '/api/view';
        fetch(url, {{ cache: 'no-store' }})
          .then(function(r) {{ return r.json(); }})
          .then(apply)
          .catch(function() {{}});
      }}
      window.addEventListener('load', function() {{
        ['status-badge', 'snapshot-badge', 'stale-banner', 'markets-body', 'perf-body',
         'pos-body', 'ord-body', 'exec-body', 'logbox'].forEach(function(id) {{
          var el = $(id);
          if (el) last[id] = el.innerHTML;
        }});
        var lb = $('logbox');
        if (lb) lb.scrollTop = lb.scrollHeight;
        poll();
        setInterval(poll, POLL_MS);
        setInterval(tickClock, 1000);
      }});
    }})();
  </script>
</head>
<body>
  <header>
    <h1><a href="/">&gt; OKX_QUANT // {heading}<span class="cursor">_</span></a></h1>
    <nav class="nav">
      {nav_html}
    </nav>
    <div class="refresh-info">LIVE PATCH &nbsp;|&nbsp; <span id="clock">{now_str}</span></div>
  </header>
  <div class="container" id="app">
    {body}
  </div>
  <footer>{footer}</footer>
</body>
</html>"""


# ─── HTTP 服务 ───────────────────────────────────────────────────────────────
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
            if path in ("/api/status", "/api/view", "/api/markets", "/api/config"):
                if path == "/api/config":
                    try:
                        data = config_view()
                    except ConfigError as exc:
                        self._send_json(400, {"ok": False, "message": str(exc)})
                        return
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
                "message": "已写入 conf/controllers。请执行 make stop && make start 后生效。",
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)


if __name__ == "__main__":
    main()
