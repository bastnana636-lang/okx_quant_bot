#!/usr/bin/env python3
"""
OKX Quant Trader — Real-time Dashboard
=======================================
读取 ../hummingbot/data/bot/status.json，以 HTTP 服务方式暴露实时面板。
完全独立，不修改任何现有文件，不需要额外安装任何依赖。

用法:
    python dashboard/dashboard.py
    # 然后在浏览器打开 http://localhost:8888
"""

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ─── 路径配置 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent / "hummingbot"
STATUS_FILE = BASE_DIR / "data" / "bot" / "status.json"
LOG_FILE    = BASE_DIR / "logs" / "logs_conf_okx_multi.log"
PORT        = 8888
STALE_AFTER_S = 20  # 引擎每 5 秒写一次快照；超过该秒数视为过期


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


def render_html(status: dict) -> str:
    view = build_view(status)
    if view.get("error"):
        body = f'<div class="card error">⚠️ 无法读取状态文件: {view["error"]}</div>'
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
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OKX QUANT // DASHBOARD</title>
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
        fetch('/api/view', {{ cache: 'no-store' }})
          .then(function(r) {{ return r.json(); }})
          .then(apply)
          .catch(function() {{}});
      }}
      window.addEventListener('load', function() {{
        ['status-badge', 'snapshot-badge', 'stale-banner', 'perf-body',
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
    <h1>&gt; OKX_QUANT // LIVE DASHBOARD<span class="cursor">_</span></h1>
    <div class="refresh-info">LIVE PATCH &nbsp;|&nbsp; <span id="clock">{now_str}</span></div>
  </header>
  <div class="container" id="app">
    {body}
  </div>
  <footer>[ READ-ONLY MONITOR // NO TRADE SIDE EFFECTS ]</footer>
</body>
</html>"""


# ─── HTTP 服务 ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默日志

    def do_GET(self):
        try:
            if self.path in ("/api/status", "/api/view"):
                data = read_status() if self.path == "/api/status" else build_view(read_status())
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/", "/index.html"):
                status = read_status()
                html   = render_html(status).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
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
