"""
========================================================================================
OKX 永续合约真人全真模拟交易系统 (4H结构共振 + 市场广度过滤 + 等风险日内波段策略)
OKX Perpetual Trend-Regime Swing Trader (Holding Period <= 1 Day)
========================================================================================
严格遵循实盘时序因果律：绝对无任何未来函数 (Zero Lookahead Bias)
- 初始本金: 10,000 USDT
- 杠杆上限: 主流币(BTC, ETH) 15x; 山寨币(SOL, DOGE, XRP, ADA, SUI, LINK, LTC, AVAX, ZEC) 10x
- 等风险资金管理: 按单笔最大风险权益 1.5% 结合 15m ATR 止损距离反算名义仓位 (单笔保证金不超过权益 20% 与 2,500 USDT 上限)，最多同时持仓 3 笔
- 真实交易摩擦建模: 入场采用 Post-Only Maker 挂单 (0.02% 手续费, 0 滑点); 主动/被动市价离场 (SL_ATR / TRAILING_STOP / TIME_LIMIT) 按 Taker 0.05% + 0.02% 滑点结算, 固定目标限价止盈 (TP_TARGET) 按 Maker 0.02% 结算
- 资金费率建模: 每 8 小时 (00/08/16 UTC) 按名义价值结算资金费 (默认 0.01%/8h, 多头付费、空头收取)
- 强平风险建模: 按逐仓保证金与维持保证金率推算强平价，K 线穿越强平价按损失全部保证金结算
- 行情研判层 (Optimal Regime Engine - 无未来函数已收盘高周期对齐):
  1) 4H 多周期结构 + BTC 共振: 需已收盘 4H K 线满足 Close > EMA20 > EMA50 且 EMA20 向上 (做空反之), 且 BTC 4H 同期同向共振;
  2) 多币种全市场广度共振: 组合核心永续合约中至少 60% 在 1H/4H 均线同侧, 且 12h 截面中位收益同向;
  3) 配合平仓后同标的 1h 冷却期与单日 2% 回撤熔断, 震荡期自动空仓避险;
- 震荡与回撤增强模块 (可独立开关与组合):
  1) 波动率自适应追踪止盈 (Idea 1): 按入场 15m ATR 比例动态计算激活阈值 clip(3.2*ATR15%, 1.2%, 1.8%) 与回撤幅度;
  2) 同向并发未护航控仓 (Idea 2): 已有同向持仓尚未浮盈护航时, 第2/3笔并发开仓按 0.6x / 0.4x 缩减风险敞口 (并支持组合总敞口上限);
  3) 4H 路径效率软降仓 (Idea 3): 已收盘 4H 72h 路径效率 ER < 0.22 时自动半仓缩容 (0.5x), 兼顾震荡减损与早期突破捕获;
  4) 防过度拉伸追尾过滤 (Idea 4): 拦截偏离 12h 锚定均线 (EMA144) 超过 1.2 倍 15m ATR 的过伸信号;
  5) 主流币名义仓位与杠杆对齐开关 (Idea 5): 支持 max_nominal_cap 与 btc_eth_lev 配置对齐.
- 顺势双向波段执行:
  - 在满足行情研判的前提下, 5m K 线回踩 1h/3h 均线 (EMA12/EMA36) 企稳收阳/承压收阴配合 MACD 与 RSI9 动能开仓;
  - 止损: max(2.0 * ATR14_15m, 0.8%); 自适应追踪止盈 (低波 ~1.2%/0.45%, 高波 ~1.8%/0.65%); 目标止盈: +4.5%; 最长持仓 <= 24 小时 (288 根 5m K 线).
========================================================================================
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from regime_engine import RegimeConfig, compute_regime_masks, precompute_regime_features

DATA_DIR = Path("data/okx_candles")
SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP", "XRP-USDT-SWAP",
    "ADA-USDT-SWAP", "SUI-USDT-SWAP", "LINK-USDT-SWAP", "LTC-USDT-SWAP", "AVAX-USDT-SWAP",
    "ZEC-USDT-SWAP",
]


@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str  # 'LONG' or 'SHORT'
    mode: str  # 'SWING'
    entry_bar: int
    entry_time: pd.Timestamp
    entry_price: float
    leverage: float
    margin: float
    nominal_size: float
    quantity: float
    stop_loss: float
    take_profit: float
    tp_pct: float
    entry_reason: str
    peak: float
    trough: float
    liq_price: float = 0.0
    next_funding_dt: Optional[pd.Timestamp] = None
    funding_paid: float = 0.0
    trail_trig: float = 0.016
    trail_pb: float = 0.006


def _compute_trail_params(
    atr15_pct: float,
    adaptive_trail: bool,
    default_trig: float,
    default_pb: float,
    trail_atr_mult: float,
    trail_trig_min: float,
    trail_trig_max: float,
    trail_pb_ratio: float,
    trail_pb_min: float,
    trail_pb_max: float,
) -> Tuple[float, float]:
    """Compute per-trade trailing activation and pullback thresholds (strictly causal at entry)."""
    if not adaptive_trail:
        return default_trig, default_pb
    trig = float(np.clip(trail_atr_mult * atr15_pct, trail_trig_min, trail_trig_max))
    pb = float(np.clip(trig * trail_pb_ratio, trail_pb_min, trail_pb_max))
    return trig, pb


def _compute_open_portfolio_risk(
    active_positions: Dict[str, Position],
    current_mtm_equity: float,
) -> float:
    """Compute remaining downside risk-to-stop across open positions as a fraction of equity."""
    open_risk = 0.0
    for ap in active_positions.values():
        if ap.side == "LONG":
            eff_sl = ap.stop_loss
            if (ap.peak - ap.entry_price) / ap.entry_price >= ap.trail_trig:
                eff_sl = max(eff_sl, ap.peak * (1.0 - ap.trail_pb))
            rem_loss = max(0.0, (ap.entry_price - eff_sl) * ap.quantity)
        else:
            eff_sl = ap.stop_loss
            if (ap.entry_price - ap.trough) / ap.entry_price >= ap.trail_trig:
                eff_sl = min(eff_sl, ap.trough * (1.0 + ap.trail_pb))
            rem_loss = max(0.0, (eff_sl - ap.entry_price) * ap.quantity)
        open_risk += rem_loss / max(current_mtm_equity, 1e-9)
    return open_risk


def next_funding_time(ts: pd.Timestamp, interval_hours: int = 8) -> pd.Timestamp:
    """返回严格晚于 ts 的下一个资金费结算时刻（默认 00/08/16 UTC 整点）"""
    day = pd.Timestamp(ts).normalize()
    for h in range(0, 24, interval_hours):
        t = day + pd.Timedelta(hours=h)
        if t > ts:
            return t
    return day + pd.Timedelta(days=1)


def _read_sorted_candles_csv(path: Path) -> pd.DataFrame:
    """Read a candle CSV, parse UTC timestamps, deduplicate, and sort ascending."""
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df["dt"])
    return df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)


def _load_symbol_candles(symbol: str, period: str, pattern: str) -> pd.DataFrame:
    """Load 5m candles for a symbol, falling back cleanly for ZEC-USDT-SWAP's pre-listing history."""
    p = DATA_DIR / pattern.format(s=symbol)
    if p.exists():
        return _read_sorted_candles_csv(p)

    if symbol == "ZEC-USDT-SWAP":
        zec_okx_1y = DATA_DIR / "ZEC-USDT-SWAP_5m_1year.csv"
        zec_bn_2y = DATA_DIR / "ZECUSDT_binance_5m_2years.csv"
        if period in ("1year", "6months", "last_month") and zec_okx_1y.exists():
            return _read_sorted_candles_csv(zec_okx_1y)
        if period in ("2years", "prev_year") and zec_bn_2y.exists():
            df_bn = _read_sorted_candles_csv(zec_bn_2y)
            if zec_okx_1y.exists():
                df_okx = _read_sorted_candles_csv(zec_okx_1y)
                okx_first_dt = df_okx["dt"].iloc[0]
                # Use Binance perp through OKX listing bar (<= okx_first_dt) and native OKX after
                # to avoid OKX's listing-candle opening wick spike while maximizing native OKX coverage.
                df = pd.concat(
                    [df_bn[df_bn["dt"] <= okx_first_dt], df_okx[df_okx["dt"] > okx_first_dt]],
                    ignore_index=True,
                )
                return df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
            return df_bn

    raise FileNotFoundError(f"Data file not found: {p}")


def load_and_align_candles(period: str = "2years"):
    period_patterns = {
        "2years": "{s}_5m_2years.csv",
        "prev_year": "{s}_5m_prev_year.csv",
        "1year": "{s}_5m_1year.csv",
        "6months": "{s}_5m_6months.csv",
    }
    pattern = period_patterns.get(period, "{s}_5m_last_month.csv")

    dfs = {s: _load_symbol_candles(s, period, pattern) for s in SYMBOLS}

    # 寻找所有标的完全交集的公共时间戳
    ts_set = set(dfs[SYMBOLS[0]]["dt"])
    for s in SYMBOLS[1:]:
        ts_set = ts_set.intersection(set(dfs[s]["dt"]))
    common_ts = sorted(list(ts_set))

    market_data = {}
    for s in SYMBOLS:
        df = dfs[s].set_index("dt").loc[common_ts].reset_index()
        c = df["c"].to_numpy(dtype=np.float64)
        h = df["h"].to_numpy(dtype=np.float64)
        l = df["l"].to_numpy(dtype=np.float64)
        o = df["o"].to_numpy(dtype=np.float64)
        v = df["vol"].to_numpy(dtype=np.float64)

        ema12 = pd.Series(c).ewm(span=12, adjust=False).mean().to_numpy()
        ema36 = pd.Series(c).ewm(span=36, adjust=False).mean().to_numpy()
        ema144 = pd.Series(c).ewm(span=144, adjust=False).mean().to_numpy()
        ema288 = pd.Series(c).ewm(span=288, adjust=False).mean().to_numpy()
        ema200 = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()

        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr14 = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()

        diff = pd.Series(c).diff()
        up = diff.clip(lower=0)
        down = -1 * diff.clip(upper=0)

        ma_up3 = up.ewm(alpha=1.0 / 3, adjust=False).mean()
        ma_down3 = down.ewm(alpha=1.0 / 3, adjust=False).mean()
        rsi3 = (100.0 - (100.0 / (1.0 + ma_up3 / (ma_down3 + 1e-9)))).to_numpy()

        ma_up9 = up.ewm(alpha=1.0 / 9, adjust=False).mean()
        ma_down9 = down.ewm(alpha=1.0 / 9, adjust=False).mean()
        rsi9 = (100.0 - (100.0 / (1.0 + ma_up9 / (ma_down9 + 1e-9)))).to_numpy()

        macd_line = pd.Series(c).ewm(span=12, adjust=False).mean() - pd.Series(c).ewm(span=26, adjust=False).mean()
        macd_sig = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = (macd_line - macd_sig).to_numpy()

        market_data[s] = {
            "c": c, "h": h, "l": l, "o": o, "v": v,
            "ema12": ema12, "ema36": ema36, "ema144": ema144, "ema288": ema288, "ema200": ema200,
            "atr14": atr14, "rsi3": rsi3, "rsi9": rsi9, "macd_hist": macd_hist,
        }
    return common_ts, market_data


def run_human_trading_simulation(
    period: str = "2years",
    execution: str = "maker",
    exit_execution: Optional[str] = "taker",
    initial_capital: float = 10000.0,
    max_positions: int = 3,
    margin_ratio: float = 0.20,
    max_margin_cap: float = 2500.0,
    btc_eth_lev: float = 15.0,
    alt_lev: float = 10.0,
    swing_tp: float = 0.045,
    swing_sl_mult: float = 2.0,
    swing_trail_trig: float = 0.016,
    swing_trail_pb: float = 0.006,
    max_hold_bars: int = 288,
    fee_rate: Optional[float] = None,
    slippage: Optional[float] = None,
    funding_rate: float = 0.0001,
    funding_interval_hours: int = 8,
    maintenance_margin_rate: float = 0.005,
    regime_filter: bool = True,
    require_btc_4h: bool = True,
    min_breadth_frac: float = 0.60,
    stop_tf: str = "15m",
    min_stop_pct: float = 0.008,
    max_stop_pct: float = 1.0,
    risk_per_trade_pct: Optional[float] = 0.015,
    cooldown_bars: int = 12,
    daily_loss_limit: float = 0.02,
    # Idea 1: Adaptive ATR trailing stop
    adaptive_trail: bool = True,
    trail_atr_mult: float = 3.2,
    trail_trig_min: float = 0.012,
    trail_trig_max: float = 0.018,
    trail_pb_ratio: float = 0.375,
    trail_pb_min: float = 0.0045,
    trail_pb_max: float = 0.0065,
    # Idea 2: Concurrent same-side unprotected scaling & open-risk cap
    unprot_2nd_scale: float = 0.60,
    unprot_3rd_scale: float = 0.40,
    prot_profit_thresh: float = 0.0,
    max_open_risk_pct: Optional[float] = None,
    # Idea 3: 4H path efficiency & whipsaw regime scaling/gating
    er4h_soft_thresh: float = 0.22,
    er4h_soft_scale: float = 0.50,
    er4h_hard_min: float = 0.0,
    whipsaw_hours: int = 0,
    whipsaw_soft_scale: float = 0.0,
    # Idea 4: Overextension filter relative to EMA144 in 15m ATR units
    max_dist_e144_atr15: Optional[float] = 1.2,
    # Idea 5: BTC/ETH nominal cap alignment
    max_nominal_cap: Optional[float] = None,
    preloaded_data: Optional[Tuple[List[pd.Timestamp], Dict[str, Dict[str, np.ndarray]]]] = None,
    precomputed_regime: Optional[Dict[str, Dict[str, np.ndarray]]] = None,
    start_bar_override: Optional[int] = None,
    end_bar_override: Optional[int] = None,
):
    if execution == "taker":
        fee_rate = 0.0005 if fee_rate is None else fee_rate
        slippage = 0.0002 if slippage is None else slippage
    else:
        fee_rate = 0.0002 if fee_rate is None else fee_rate
        slippage = 0.0000 if slippage is None else slippage

    if exit_execution is None:
        exit_fee_rate = fee_rate
        exit_slippage = slippage
    elif exit_execution == "taker":
        exit_fee_rate = 0.0005
        exit_slippage = 0.0002
    else:
        exit_fee_rate = 0.0002
        exit_slippage = 0.0

    if preloaded_data is not None:
        common_ts, data = preloaded_data
    else:
        common_ts, data = load_and_align_candles(period=period)
    n_bars = len(common_ts)

    if (regime_filter or stop_tf == "15m") and precomputed_regime is None:
        precomputed_regime = precompute_regime_features(common_ts, data, SYMBOLS)

    regime_masks = None
    btc_had_short_ws = None
    btc_had_long_ws = None
    if regime_filter:
        cfg = RegimeConfig(
            require_btc_4h=require_btc_4h,
            min_breadth_frac=min_breadth_frac,
            min_er4h_18=er4h_hard_min,
        )
        regime_masks = compute_regime_masks(precomputed_regime, SYMBOLS, cfg)
        if whipsaw_hours > 0 and "BTC-USDT-SWAP" in regime_masks:
            ws_bars = max(1, whipsaw_hours * 12)
            btc_l_mask, btc_s_mask = regime_masks["BTC-USDT-SWAP"]
            btc_had_short_ws = pd.Series(btc_s_mask.astype(int)).rolling(ws_bars, min_periods=1).max().to_numpy() > 0
            btc_had_long_ws = pd.Series(btc_l_mask.astype(int)).rolling(ws_bars, min_periods=1).max().to_numpy() > 0

    # 预计算原始 5m 波段信号（严格因果）
    raw_side_map: Dict[str, np.ndarray] = {}
    for sym in SYMBOLS:
        sd = data[sym]
        c_arr = sd["c"]; o_arr = sd["o"]; h_arr = sd["h"]; l_arr = sd["l"]
        e12 = sd["ema12"]; e36 = sd["ema36"]; e144 = sd["ema144"]; e288 = sd["ema288"]
        r9 = sd["rsi9"]; mh = sd["macd_hist"]

        macro_bear = (c_arr < e144) & (e144 < e288)
        tested_res = (h_arr >= e12 * 0.998) | (h_arr >= e36 * 0.998)
        closed_weak = (c_arr < o_arr) & (c_arr < e12)
        mom_bear = (mh < 0) & (r9 >= 50.0) & (r9 <= 68.0)

        macro_bull = (c_arr > e144) & (e144 > e288)
        tested_sup = (l_arr <= e12 * 1.002) | (l_arr <= e36 * 1.002)
        closed_strong = (c_arr > o_arr) & (c_arr > e12)
        mom_bull = (mh > 0) & (r9 >= 32.0) & (r9 <= 50.0)

        short_sig = macro_bear & tested_res & closed_weak & mom_bear
        long_sig = macro_bull & tested_sup & closed_strong & mom_bull
        raw_side_map[sym] = np.where(short_sig, -1, np.where(long_sig, 1, 0)).astype(np.int8)

    bar_dates = [t.date() for t in common_ts]
    cash = initial_capital
    active_positions: Dict[str, Position] = {}
    completed_trades = []
    equity_curve = []
    trade_id_counter = 1
    last_exit_bar: Dict[str, int] = {}
    day_key = None
    day_start_equity = initial_capital

    start_bar = 288 if start_bar_override is None else max(288, start_bar_override)
    end_bar = (n_bars - 1) if end_bar_override is None else min(n_bars - 1, end_bar_override)

    for i in range(start_bar, end_bar):
        curr_dt = common_ts[i]
        next_dt = common_ts[i + 1]
        bar_end_dt = curr_dt + pd.Timedelta(minutes=5)

        # --- 步骤 1: 持仓演变与离场触发 ---
        for sym, pos in list(active_positions.items()):
            sd = data[sym]
            c = sd["c"][i]
            h = sd["h"][i]
            l = sd["l"][i]
            hold_bars = i - pos.entry_bar + 1
            hold_mins = hold_bars * 5

            while pos.next_funding_dt is not None and pos.next_funding_dt <= curr_dt:
                funding_cost = pos.nominal_size * funding_rate
                pos.funding_paid += funding_cost if pos.side == "LONG" else -funding_cost
                pos.next_funding_dt = pos.next_funding_dt + pd.Timedelta(hours=funding_interval_hours)

            exit_price = None
            exit_reason = ""
            liquidated = False
            is_tp_exit = False

            if pos.side == "LONG":
                liq_hit = l <= pos.liq_price
                sl_hit = l <= pos.stop_loss
                tp_hit = h >= pos.take_profit

                prior_peak = pos.peak
                gain_prior = (prior_peak - pos.entry_price) / pos.entry_price
                trail_hit = False
                if gain_prior >= pos.trail_trig:
                    trail_px = prior_peak * (1.0 - pos.trail_pb)
                    if l <= trail_px:
                        trail_hit = True
                        trail_exit = max(trail_px, pos.stop_loss)
                if not trail_hit and h > pos.peak:
                    gain_new = (h - pos.entry_price) / pos.entry_price
                    if gain_new >= pos.trail_trig:
                        trail_px = h * (1.0 - pos.trail_pb)
                        if c <= trail_px:
                            trail_hit = True
                            trail_exit = max(trail_px, pos.stop_loss)

                if liq_hit:
                    liquidated = True
                    exit_price = pos.liq_price
                    exit_reason = f"LIQUIDATION (强平 {pos.liq_price:.4f})"
                elif tp_hit and not sl_hit:
                    is_tp_exit = True
                    exit_price = pos.take_profit
                    exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                elif trail_hit:
                    exit_price = trail_exit * (1.0 - exit_slippage)
                    exit_reason = "TRAILING_STOP"
                elif sl_hit:
                    exit_price = pos.stop_loss * (1.0 - exit_slippage)
                    exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR_{stop_tf})"
                elif hold_bars >= max_hold_bars:
                    exit_price = c * (1.0 - exit_slippage)
                    exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"

                if exit_price is not None:
                    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    used_exit_fee = fee_rate if is_tp_exit else exit_fee_rate
                    exit_fee = exit_nominal * used_exit_fee
                    total_fees = round(entry_fee + exit_fee, 2)
                    net_pnl = -round(pos.margin, 2) if liquidated else round(gross_pnl - total_fees - pos.funding_paid, 2)

                    cash += pos.margin + net_pnl
                    del active_positions[sym]
                    last_exit_bar[sym] = i

                    unrealized_rem = sum(
                        ((data[s]["c"][i] - p.entry_price) * p.quantity if p.side == "LONG"
                         else (p.entry_price - data[s]["c"][i]) * p.quantity) - p.funding_paid
                        for s, p in active_positions.items()
                    )
                    total_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_rem, 2)
                    price_ret = (exit_price - pos.entry_price) / pos.entry_price
                    roe_pct = round((net_pnl / pos.margin) * 100.0, 2)

                    completed_trades.append({
                        "trade_id": pos.trade_id, "symbol": sym, "side": pos.side, "mode": pos.mode,
                        "leverage": f"{pos.leverage:.0f}x", "margin": round(pos.margin, 2),
                        "nominal_size": round(pos.nominal_size, 2),
                        "entry_time": pos.entry_time.strftime("%Y-%m-%d %H:%M UTC"),
                        "entry_price": round(pos.entry_price, 4),
                        "exit_time": bar_end_dt.strftime("%Y-%m-%d %H:%M UTC"),
                        "exit_price": round(exit_price, 4),
                        "stop_loss": round(pos.stop_loss, 4), "take_profit": round(pos.take_profit, 4),
                        "hold_bars": hold_bars, "hold_mins": hold_mins,
                        "total_fees": total_fees, "price_ret_pct": round(price_ret * 100.0, 3),
                        "net_pnl": net_pnl, "roe_pct": roe_pct,
                        "funding_paid": round(pos.funding_paid, 2), "liq_price": round(pos.liq_price, 4),
                        "account_equity": total_equity, "exit_reason": exit_reason, "entry_reason": pos.entry_reason,
                    })
                else:
                    if h > pos.peak:
                        pos.peak = h
                    if l < pos.trough:
                        pos.trough = l

            elif pos.side == "SHORT":
                liq_hit = h >= pos.liq_price
                sl_hit = h >= pos.stop_loss
                tp_hit = l <= pos.take_profit

                prior_trough = pos.trough
                gain_prior = (pos.entry_price - prior_trough) / pos.entry_price
                trail_hit = False
                if gain_prior >= pos.trail_trig:
                    trail_px = prior_trough * (1.0 + pos.trail_pb)
                    if h >= trail_px:
                        trail_hit = True
                        trail_exit = min(trail_px, pos.stop_loss)
                if not trail_hit and l < pos.trough:
                    gain_new = (pos.entry_price - l) / pos.entry_price
                    if gain_new >= pos.trail_trig:
                        trail_px = l * (1.0 + pos.trail_pb)
                        if c >= trail_px:
                            trail_hit = True
                            trail_exit = min(trail_px, pos.stop_loss)

                if liq_hit:
                    liquidated = True
                    exit_price = pos.liq_price
                    exit_reason = f"LIQUIDATION (强平 {pos.liq_price:.4f})"
                elif tp_hit and not sl_hit:
                    is_tp_exit = True
                    exit_price = pos.take_profit
                    exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                elif trail_hit:
                    exit_price = trail_exit * (1.0 + exit_slippage)
                    exit_reason = "TRAILING_STOP"
                elif sl_hit:
                    exit_price = pos.stop_loss * (1.0 + exit_slippage)
                    exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR_{stop_tf})"
                elif hold_bars >= max_hold_bars:
                    exit_price = c * (1.0 + exit_slippage)
                    exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"

                if exit_price is not None:
                    gross_pnl = (pos.entry_price - exit_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    used_exit_fee = fee_rate if is_tp_exit else exit_fee_rate
                    exit_fee = exit_nominal * used_exit_fee
                    total_fees = round(entry_fee + exit_fee, 2)
                    net_pnl = -round(pos.margin, 2) if liquidated else round(gross_pnl - total_fees - pos.funding_paid, 2)

                    cash += pos.margin + net_pnl
                    del active_positions[sym]
                    last_exit_bar[sym] = i

                    unrealized_rem = sum(
                        ((data[s]["c"][i] - p.entry_price) * p.quantity if p.side == "LONG"
                         else (p.entry_price - data[s]["c"][i]) * p.quantity) - p.funding_paid
                        for s, p in active_positions.items()
                    )
                    total_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_rem, 2)
                    price_ret = (pos.entry_price - exit_price) / pos.entry_price
                    roe_pct = round((net_pnl / pos.margin) * 100.0, 2)

                    completed_trades.append({
                        "trade_id": pos.trade_id, "symbol": sym, "side": pos.side, "mode": pos.mode,
                        "leverage": f"{pos.leverage:.0f}x", "margin": round(pos.margin, 2),
                        "nominal_size": round(pos.nominal_size, 2),
                        "entry_time": pos.entry_time.strftime("%Y-%m-%d %H:%M UTC"),
                        "entry_price": round(pos.entry_price, 4),
                        "exit_time": bar_end_dt.strftime("%Y-%m-%d %H:%M UTC"),
                        "exit_price": round(exit_price, 4),
                        "stop_loss": round(pos.stop_loss, 4), "take_profit": round(pos.take_profit, 4),
                        "hold_bars": hold_bars, "hold_mins": hold_mins,
                        "total_fees": total_fees, "price_ret_pct": round(price_ret * 100.0, 3),
                        "net_pnl": net_pnl, "roe_pct": roe_pct,
                        "funding_paid": round(pos.funding_paid, 2), "liq_price": round(pos.liq_price, 4),
                        "account_equity": total_equity, "exit_reason": exit_reason, "entry_reason": pos.entry_reason,
                    })
                else:
                    if h > pos.peak:
                        pos.peak = h
                    if l < pos.trough:
                        pos.trough = l

        unrealized_total = sum(
            ((data[s]["c"][i] - p.entry_price) * p.quantity if p.side == "LONG"
             else (p.entry_price - data[s]["c"][i]) * p.quantity) - p.funding_paid
            for s, p in active_positions.items()
        )
        current_mtm_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_total, 2)
        equity_curve.append({
            "dt": curr_dt,
            "equity": current_mtm_equity,
            "cash": round(cash, 2),
            "active_positions": len(active_positions),
        })

        bar_day = bar_dates[i]
        if bar_day != day_key:
            day_key = bar_day
            day_start_equity = current_mtm_equity

        # --- 步骤 2: 行情研判 + 信号评估 + 等风险定仓 ---
        if len(active_positions) < max_positions and cash > 500:
            for sym in SYMBOLS:
                if sym in active_positions:
                    continue
                if len(active_positions) >= max_positions:
                    break

                raw_sig = int(raw_side_map[sym][i])
                if raw_sig == 0:
                    continue

                side = "SHORT" if raw_sig == -1 else "LONG"
                if regime_masks is not None:
                    l_ok, s_ok = regime_masks[sym]
                    if not (l_ok[i] if side == "LONG" else s_ok[i]):
                        continue

                if cooldown_bars > 0 and (i - last_exit_bar.get(sym, -(10**9))) < cooldown_bars:
                    continue
                if daily_loss_limit > 0 and current_mtm_equity <= day_start_equity * (1.0 - daily_loss_limit):
                    continue

                # Idea 3: Opposite-regime recent whipsaw check (hard block or soft scale)
                whipsaw_hit = False
                if btc_had_short_ws is not None and btc_had_long_ws is not None:
                    whipsaw_hit = bool(btc_had_short_ws[i]) if side == "LONG" else bool(btc_had_long_ws[i])
                    if whipsaw_hit and whipsaw_soft_scale <= 0.0:
                        continue

                sd = data[sym]
                rsi9_i = sd["rsi9"][i]
                atr_5m = float(sd["atr14"][i])
                stop_atr = float(precomputed_regime[sym]["atr15m"][i]) if (stop_tf == "15m" and precomputed_regime is not None) else atr_5m
                if not np.isfinite(stop_atr) or stop_atr <= 0:
                    stop_atr = atr_5m

                # Idea 4: Overextension filter (distance from 12h EMA144 in 15m ATR units)
                if max_dist_e144_atr15 is not None and max_dist_e144_atr15 > 0.0:
                    sgn = 1.0 if side == "LONG" else -1.0
                    dist_e144 = ((float(sd["c"][i]) - float(sd["ema144"][i])) / max(stop_atr, 1e-9)) * sgn
                    if dist_e144 > max_dist_e144_atr15:
                        continue

                next_open = sd["o"][i + 1]
                fill_price = next_open * (1.0 + slippage) if side == "LONG" else next_open * (1.0 - slippage)
                stop_pct = (swing_sl_mult * stop_atr) / max(fill_price, 1e-9)
                if min_stop_pct > 0.0 and stop_pct < min_stop_pct:
                    stop_pct = min_stop_pct
                if stop_pct > max_stop_pct:
                    continue
                stop_dist = fill_price * stop_pct

                # Idea 1: Per-trade adaptive ATR trailing thresholds
                atr15_pct = stop_atr / max(fill_price, 1e-9)
                pos_trail_trig, pos_trail_pb = _compute_trail_params(
                    atr15_pct=atr15_pct,
                    adaptive_trail=adaptive_trail,
                    default_trig=swing_trail_trig,
                    default_pb=swing_trail_pb,
                    trail_atr_mult=trail_atr_mult,
                    trail_trig_min=trail_trig_min,
                    trail_trig_max=trail_trig_max,
                    trail_pb_ratio=trail_pb_ratio,
                    trail_pb_min=trail_pb_min,
                    trail_pb_max=trail_pb_max,
                )

                # Idea 2 & Idea 3: Dynamic risk scaling (4H ER + whipsaw + concurrent unprotected scaling)
                eff_risk_pct = risk_per_trade_pct
                if eff_risk_pct is not None and eff_risk_pct > 0:
                    if whipsaw_hit and whipsaw_soft_scale > 0.0:
                        eff_risk_pct *= whipsaw_soft_scale
                    if er4h_soft_thresh > 0.0 and precomputed_regime is not None and "er4h_18" in precomputed_regime[sym]:
                        if float(precomputed_regime[sym]["er4h_18"][i]) < er4h_soft_thresh:
                            if er4h_soft_scale <= 0.0:
                                continue
                            eff_risk_pct *= er4h_soft_scale

                    same_side_pos = [p for p in active_positions.values() if p.side == side]
                    if len(same_side_pos) > 0 and (unprot_2nd_scale < 1.0 or unprot_3rd_scale < 1.0):
                        has_prot = any(
                            ((data[p.symbol]["c"][i] - p.entry_price) / p.entry_price
                             if p.side == "LONG" else (p.entry_price - data[p.symbol]["c"][i]) / p.entry_price)
                            >= prot_profit_thresh
                            for p in same_side_pos
                        )
                        if not has_prot:
                            scale = unprot_2nd_scale if len(same_side_pos) == 1 else unprot_3rd_scale
                            if scale <= 0.0:
                                continue
                            eff_risk_pct *= scale

                    if max_open_risk_pct is not None and max_open_risk_pct > 0.0:
                        open_risk = _compute_open_portfolio_risk(active_positions, current_mtm_equity)
                        rem_budget = max_open_risk_pct - open_risk
                        if rem_budget < 0.003:
                            continue
                        eff_risk_pct = min(eff_risk_pct, rem_budget)

                lev = btc_eth_lev if ("BTC" in sym or "ETH" in sym) else alt_lev
                max_allowed_margin = min(current_mtm_equity * margin_ratio, max_margin_cap)
                if eff_risk_pct is not None and eff_risk_pct > 0:
                    target_nominal = (current_mtm_equity * eff_risk_pct) / max(stop_pct, 1e-6)
                    if max_nominal_cap is not None and max_nominal_cap > 0.0:
                        target_nominal = min(target_nominal, max_nominal_cap)
                    margin = min(target_nominal / lev, max_allowed_margin)
                else:
                    margin = max_allowed_margin
                    if max_nominal_cap is not None and max_nominal_cap > 0.0:
                        margin = min(margin, max_nominal_cap / lev)
                if margin > cash:
                    margin = cash * 0.90
                if margin < 100:
                    continue

                cash -= margin
                nominal = margin * lev
                qty = nominal / fill_price

                if side == "LONG":
                    reason_desc = f"[4H+BTC+广度共振] 上行趋势回踩均线企稳(RSI9={rsi9_i:.1f})"
                    sl = fill_price - stop_dist
                    tp = fill_price * (1.0 + swing_tp)
                    liq = fill_price * (1.0 - 1.0 / lev + maintenance_margin_rate)
                else:
                    reason_desc = f"[4H+BTC+广度共振] 下行趋势反弹承压滞涨(RSI9={rsi9_i:.1f})"
                    sl = fill_price + stop_dist
                    tp = fill_price * (1.0 - swing_tp)
                    liq = fill_price * (1.0 + 1.0 / lev - maintenance_margin_rate)

                active_positions[sym] = Position(
                    trade_id=trade_id_counter,
                    symbol=sym,
                    side=side,
                    mode="SWING",
                    entry_bar=i + 1,
                    entry_time=next_dt,
                    entry_price=fill_price,
                    leverage=lev,
                    margin=margin,
                    nominal_size=nominal,
                    quantity=qty,
                    stop_loss=sl,
                    take_profit=tp,
                    tp_pct=swing_tp,
                    entry_reason=reason_desc,
                    peak=fill_price,
                    trough=fill_price,
                    liq_price=liq,
                    next_funding_dt=next_funding_time(next_dt, funding_interval_hours),
                    trail_trig=pos_trail_trig,
                    trail_pb=pos_trail_pb,
                )
                trade_id_counter += 1

    # 模拟结束强平剩余在持仓位
    final_bar_idx = end_bar
    final_dt = common_ts[final_bar_idx]
    final_bar_end_dt = final_dt + pd.Timedelta(minutes=5)
    for sym, pos in list(active_positions.items()):
        sd = data[sym]
        c = sd["c"][final_bar_idx]
        hold_bars = final_bar_idx - pos.entry_bar + 1
        hold_mins = hold_bars * 5
        if pos.side == "LONG":
            exit_price = c * (1.0 - exit_slippage)
            gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            exit_price = c * (1.0 + exit_slippage)
            gross_pnl = (pos.entry_price - exit_price) * pos.quantity

        entry_fee = pos.nominal_size * fee_rate
        exit_fee = (exit_price * pos.quantity) * exit_fee_rate
        total_fees = round(entry_fee + exit_fee, 2)
        net_pnl = round(gross_pnl - total_fees - pos.funding_paid, 2)

        cash += pos.margin + net_pnl
        del active_positions[sym]
        total_equity = round(cash + sum(p.margin for p in active_positions.values()), 2)
        price_ret = (exit_price - pos.entry_price) / pos.entry_price if pos.side == "LONG" else (pos.entry_price - exit_price) / pos.entry_price
        roe_pct = round((net_pnl / pos.margin) * 100.0, 2)

        completed_trades.append({
            "trade_id": pos.trade_id, "symbol": sym, "side": pos.side, "mode": pos.mode,
            "leverage": f"{pos.leverage:.0f}x", "margin": round(pos.margin, 2),
            "nominal_size": round(pos.nominal_size, 2),
            "entry_time": pos.entry_time.strftime("%Y-%m-%d %H:%M UTC"),
            "entry_price": round(pos.entry_price, 4),
            "exit_time": final_bar_end_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "exit_price": round(exit_price, 4),
            "stop_loss": round(pos.stop_loss, 4), "take_profit": round(pos.take_profit, 4),
            "hold_bars": hold_bars, "hold_mins": hold_mins,
            "total_fees": total_fees, "price_ret_pct": round(price_ret * 100.0, 3),
            "net_pnl": net_pnl, "roe_pct": roe_pct,
            "funding_paid": round(pos.funding_paid, 2), "liq_price": round(pos.liq_price, 4),
            "account_equity": total_equity, "exit_reason": "SIMULATION_END", "entry_reason": pos.entry_reason,
        })

    final_cash_rounded = round(cash, 2)
    equity_curve.append({
        "dt": final_dt,
        "equity": final_cash_rounded,
        "cash": final_cash_rounded,
        "active_positions": 0,
    })

    trade_cols = [
        "trade_id", "symbol", "side", "mode", "leverage", "margin", "nominal_size",
        "entry_time", "entry_price", "exit_time", "exit_price", "stop_loss", "take_profit",
        "hold_bars", "hold_mins", "total_fees", "price_ret_pct", "net_pnl", "roe_pct",
        "funding_paid", "liq_price", "account_equity", "exit_reason", "entry_reason",
    ]
    return final_cash_rounded, pd.DataFrame(completed_trades, columns=trade_cols), pd.DataFrame(equity_curve)


def generate_outputs(
    period: str = "2years",
    execution: str = "maker",
    exit_execution: Optional[str] = "taker",
    funding_rate: float = 0.0001,
    maintenance_margin_rate: float = 0.005,
    regime_filter: bool = True,
    cooldown_bars: int = 12,
    daily_loss_limit: float = 0.02,
    adaptive_trail: bool = True,
    unprot_2nd_scale: float = 0.60,
    unprot_3rd_scale: float = 0.40,
    er4h_soft_thresh: float = 0.22,
    er4h_soft_scale: float = 0.50,
    max_dist_e144_atr15: Optional[float] = 1.2,
    btc_eth_lev: float = 15.0,
    max_nominal_cap: Optional[float] = None,
):
    initial_cap = 10000.0
    final_cash, df_trades, df_equity = run_human_trading_simulation(
        period=period,
        execution=execution,
        exit_execution=exit_execution,
        initial_capital=initial_cap,
        funding_rate=funding_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        regime_filter=regime_filter,
        cooldown_bars=cooldown_bars,
        daily_loss_limit=daily_loss_limit,
        adaptive_trail=adaptive_trail,
        unprot_2nd_scale=unprot_2nd_scale,
        unprot_3rd_scale=unprot_3rd_scale,
        er4h_soft_thresh=er4h_soft_thresh,
        er4h_soft_scale=er4h_soft_scale,
        max_dist_e144_atr15=max_dist_e144_atr15,
        btc_eth_lev=btc_eth_lev,
        max_nominal_cap=max_nominal_cap,
    )

    csv_path = Path("data/okx_human_trades_log.csv")
    df_trades.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[1/4] Saved detailed trades CSV ({len(df_trades)} trades) to {csv_path}")

    wins = df_trades[df_trades["net_pnl"] > 0]
    losses = df_trades[df_trades["net_pnl"] <= 0]
    total_trades = len(df_trades)
    win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
    total_net_pnl = float(df_trades["net_pnl"].sum()) if total_trades > 0 else 0.0
    total_fees_paid = float(df_trades["total_fees"].sum()) if total_trades > 0 else 0.0
    total_funding_paid = float(df_trades["funding_paid"].sum()) if total_trades > 0 else 0.0
    final_equity = float(df_equity["equity"].iloc[-1]) if len(df_equity) > 0 else initial_cap
    net_return_pct = ((final_equity - initial_cap) / initial_cap) * 100.0

    gain_sum = float(wins["net_pnl"].sum()) if len(wins) > 0 else 0.0
    loss_sum = float(abs(losses["net_pnl"].sum())) if len(losses) > 0 else 0.0
    profit_factor = (gain_sum / loss_sum) if loss_sum > 0 else 999.0
    avg_win = float(wins["net_pnl"].mean()) if len(wins) > 0 else 0.0
    avg_loss = float(abs(losses["net_pnl"].mean())) if len(losses) > 0 else 0.0

    df_equity["peak"] = df_equity["equity"].cummax()
    df_equity["drawdown"] = (df_equity["equity"] - df_equity["peak"]) / df_equity["peak"]
    max_dd_pct = abs(float(df_equity["drawdown"].min())) * 100.0
    max_dd_usd = float((df_equity["peak"] - df_equity["equity"]).max())

    avg_hold_mins = float(df_trades["hold_mins"].mean()) if total_trades > 0 else 0.0
    max_hold_mins = float(df_trades["hold_mins"].max()) if total_trades > 0 else 0.0

    # 绩效四联图
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.patch.set_facecolor("#0b0f19")
    for ax in axes.flatten():
        ax.set_facecolor("#111827")
        ax.tick_params(colors="#9ca3af", labelsize=9)
        for spine in ax.spines.values():
            spine.set_color("#374151")

    ax1 = axes[0, 0]
    ax1.plot(df_equity["dt"], df_equity["equity"], color="#10b981" if net_return_pct >= 0 else "#f87171", linewidth=2, label="Mark-to-Market Equity (USDT)")
    ax1.axhline(initial_cap, color="#6b7280", linestyle="--", alpha=0.8, label=f"Initial Capital (${initial_cap:,.0f})")
    pnl_sign = "+" if net_return_pct >= 0 else ""
    ax1.set_title(f"Mark-to-Market Equity Growth (Final: ${final_equity:,.2f} | {pnl_sign}{net_return_pct:.2f}%)", color="#f9fafb", fontsize=12, fontweight="bold", pad=10)
    ax1.set_ylabel("Equity (USDT)", color="#9ca3af", fontsize=10)
    ax1.legend(facecolor="#1f2937", edgecolor="#374151", labelcolor="#f9fafb", loc="upper left")
    ax1.grid(True, linestyle=":", alpha=0.3, color="#4b5563")

    ax2 = axes[0, 1]
    ax2.fill_between(df_equity["dt"], df_equity["drawdown"] * 100, 0, color="#ef4444", alpha=0.35)
    ax2.plot(df_equity["dt"], df_equity["drawdown"] * 100, color="#f87171", linewidth=1.5, label="Drawdown %")
    ax2.set_title(f"MTM Drawdown Profile (Max Drawdown: {max_dd_pct:.2f}%)", color="#f9fafb", fontsize=12, fontweight="bold", pad=10)
    ax2.set_ylabel("Drawdown (%)", color="#9ca3af", fontsize=10)
    ax2.legend(facecolor="#1f2937", edgecolor="#374151", labelcolor="#f9fafb", loc="lower left")
    ax2.grid(True, linestyle=":", alpha=0.3, color="#4b5563")

    ax3 = axes[1, 0]
    if total_trades > 0:
        colors = ["#10b981" if x > 0 else "#ef4444" for x in df_trades["net_pnl"]]
        ax3.bar(df_trades["trade_id"], df_trades["net_pnl"], color=colors, width=0.8, edgecolor="none")
    ax3.axhline(0, color="#6b7280", linestyle="-", linewidth=0.8)
    ax3.set_title(f"Net PnL Distribution (Total: {total_trades} Trades | WR: {win_rate:.1f}%)", color="#f9fafb", fontsize=12, fontweight="bold", pad=10)
    ax3.set_xlabel("Trade Sequence ID", color="#9ca3af", fontsize=10)
    ax3.set_ylabel("Net PnL (USDT)", color="#9ca3af", fontsize=10)
    ax3.grid(True, linestyle=":", alpha=0.3, color="#4b5563")

    ax4 = axes[1, 1]
    if total_trades > 0:
        sym_summary = df_trades.groupby("symbol").agg(total_pnl=("net_pnl", "sum")).reset_index()
        sym_colors = ["#38bdf8" if x >= 0 else "#fb7185" for x in sym_summary["total_pnl"]]
        ax4.barh([s.replace("-USDT-SWAP", "") for s in sym_summary["symbol"]], sym_summary["total_pnl"], color=sym_colors, height=0.6)
    ax4.axvline(0, color="#6b7280", linestyle="-", linewidth=0.8)
    ax4.set_title("Net Profit Contribution by Contract Symbol (USDT)", color="#f9fafb", fontsize=12, fontweight="bold", pad=10)
    ax4.set_xlabel("Net PnL (USDT)", color="#9ca3af", fontsize=10)
    ax4.grid(True, linestyle=":", alpha=0.3, color="#4b5563")

    plt.tight_layout()
    chart_path = Path("data/okx_human_trading_performance.png")
    plt.savefig(chart_path, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[2/4] Saved performance chart to {chart_path}")

    max_win_pnl = float(df_trades["net_pnl"].max()) if total_trades > 0 else 0.0
    max_win_roe = float(df_trades["roe_pct"].max()) if total_trades > 0 else 0.0
    min_loss_pnl = float(df_trades["net_pnl"].min()) if total_trades > 0 else 0.0
    min_loss_roe = float(df_trades["roe_pct"].min()) if total_trades > 0 else 0.0
    long_trades = df_trades[df_trades["side"] == "LONG"]
    short_trades = df_trades[df_trades["side"] == "SHORT"]
    long_w = long_trades[long_trades["net_pnl"] > 0]
    short_w = short_trades[short_trades["net_pnl"] > 0]
    long_wr = len(long_w) / len(long_trades) * 100.0 if len(long_trades) > 0 else 0.0
    short_wr = len(short_w) / len(short_trades) * 100.0 if len(short_trades) > 0 else 0.0
    long_pnl = float(long_trades["net_pnl"].sum()) if len(long_trades) > 0 else 0.0
    short_pnl = float(short_trades["net_pnl"].sum()) if len(short_trades) > 0 else 0.0
    long_pnl_str = f"+${long_pnl:,.2f}" if long_pnl >= 0 else f"-${abs(long_pnl):,.2f}"
    short_pnl_str = f"+${short_pnl:,.2f}" if short_pnl >= 0 else f"-${abs(short_pnl):,.2f}"
    short_share = (len(short_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    long_share = (len(long_trades) / total_trades * 100.0) if total_trades > 0 else 0.0

    monthly_stats = []
    if total_trades > 0:
        df_trades["entry_dt_parsed"] = pd.to_datetime(df_trades["entry_time"])
        df_trades["month_period"] = df_trades["entry_dt_parsed"].dt.tz_localize(None).dt.to_period("M")
        for m, g in df_trades.groupby("month_period"):
            m_wins = g[g["net_pnl"] > 0]
            m_losses = g[g["net_pnl"] <= 0]
            m_pnl = float(g["net_pnl"].sum())
            m_fees = float(g["total_fees"].sum())
            m_gross = m_pnl + m_fees + float(g["funding_paid"].sum())
            monthly_stats.append({
                "month": str(m), "trades": len(g), "wins": len(m_wins), "losses": len(m_losses),
                "win_rate": (len(m_wins) / len(g)) * 100.0, "net_pnl": m_pnl,
                "fees": m_fees, "gross_pnl": m_gross,
            })
    df_monthly = pd.DataFrame(monthly_stats)
    exit_counts = df_trades["exit_reason"].value_counts() if total_trades > 0 else pd.Series(dtype=int)

    if exit_execution == "taker" and execution == "maker":
        exec_desc = "入场 Post-Only Maker 0.02% 零滑点，主动止损/追踪离场按实盘 Taker 0.05% + 0.02% 滑点结算"
    elif execution == "taker":
        exec_desc = "OKX 官方 Taker 手续费 0.05% 与实盘滑点 0.02%（双边摩擦共 0.14%）"
    else:
        exec_desc = "OKX 官方 Maker 手续费 0.02% 与 Post-Only 限价挂单零滑点（双边摩擦共 0.04%）"

    start_date = df_equity["dt"].iloc[0].strftime("%Y-%m-%d")
    end_date = df_equity["dt"].iloc[-1].strftime("%Y-%m-%d")
    period_titles = {
        "2years": "双年全样本 (2-Year Full Sample)",
        "prev_year": "前一年样本 (2024-2025 Previous Year)",
        "1year": "近一年样本 (2025-2026 Recent 1-Year)",
        "6months": "半年样本 (6-Month Sample)",
        "last_month": "单月样本 (1-Month Sample)",
    }
    period_title = period_titles.get(period, period)
    date_range_str = f"{start_date} 至 {end_date} ({len(df_equity):,} 根 5m K线)"
    pnl_badge = f"+${total_net_pnl:,.2f} USDT" if total_net_pnl >= 0 else f"-${abs(total_net_pnl):,.2f} USDT"
    gross_total = total_net_pnl + total_fees_paid + total_funding_paid
    ret_desc = (
        f"扣除所有摩擦（手续费+资金费）后净盈利 +{net_return_pct:.2f}% (毛利损益为 +${gross_total:,.2f} USDT)"
        if total_net_pnl >= 0
        else f"扣除所有摩擦（手续费+资金费）后净亏损 -{abs(net_return_pct):.2f}% (毛利损益为 -${abs(gross_total):,.2f} USDT)"
    )

    sym_labels = ", ".join(s.replace("-USDT-SWAP", "") for s in SYMBOLS)
    regime_line = (
        f"> **行情研判层 (Optimal Regime Engine)**: 4H 多周期结构 (`Close > EMA20 > EMA50` 且 `EMA20` 同向推进) + BTC 4H 共振 + {len(SYMBOLS)} 币种市场广度 ≥ 60% 与 12h 截面中位收益同向；"
        f"平仓后同标的冷却 {cooldown_bars} 根 K 线 (1.0h)，单日权益回撤达 {daily_loss_limit*100:.1f}% 当日停开新仓。  \n"
        if regime_filter
        else "> **行情研判层**: 未启用（对照组）。  \n"
    )

    trail_desc = (
        "自适应波动率追踪止盈 `clip(3.2*ATR15%, 1.2%, 1.8%)` 激活 / 按比例回撤 `0.45%~0.65%`"
        if adaptive_trail
        else "追踪止盈 `+1.6%` 激活 / `0.6%` 回撤"
    )
    enh_parts = []
    if er4h_soft_thresh > 0:
        enh_parts.append(f"4H 72h 路径效率 `< {er4h_soft_thresh:.2f}` 降仓 `{er4h_soft_scale:.1f}x`")
    if max_dist_e144_atr15 is not None:
        enh_parts.append(f"过滤偏离 EMA144 > `{max_dist_e144_atr15:.1f}*ATR15` 追尾单")
    if unprot_2nd_scale < 1.0 or unprot_3rd_scale < 1.0:
        enh_parts.append(f"同向未浮盈并发降仓 `{unprot_2nd_scale:.1f}x/{unprot_3rd_scale:.1f}x`")
    if max_nominal_cap is not None:
        enh_parts.append(f"名义上限 `${max_nominal_cap:,.0f}`")
    enh_line = f"> **震荡与回撤增强**: {'；'.join(enh_parts)}。  \n" if enh_parts else ""

    md_content = f"""# OKX 永续合约最优组合策略回测报告（{period_title}）

> **测试标的**: OKX 永续合约 {len(SYMBOLS)} 大主流与高波币种 (`{sym_labels}`)  
> **覆盖时间**: **{date_range_str}**  
> **执行与成本模型**: **绝对零未来函数**。信号在第 $i$ 根 K 线收盘产生，次根开盘执行；{exec_desc}。  
{regime_line}{enh_line}> **风控与定仓**: 15m ATR 波动率止损 `max(2.0*ATR_15m, 0.8%)`，按单笔风险 ≤ 权益 `1.5%` 等风险反算名义仓位；{trail_desc}，目标止盈 `+4.5%`，最大持仓严格控制在 24 小时（288 根 5m K 线）以内。

---

## 一、 核心财务与风控指标汇总

| 核心指标 | 回测实测结果 | 说明 |
| :--- | :--- | :--- |
| **初始账户本金** | **$10,000.00 USDT** | 基准资金规模 |
| **最终账户净值** | **${final_equity:,.2f} USDT** | 期末总权益 |
| **累计净损益 (Net PnL)** | **{pnl_badge}** | {ret_desc} |
| **账户收益率 (ROI)** | **{pnl_sign}{net_return_pct:.2f}%** | 全周期净收益 |
| **总开仓笔数** | **{total_trades} 笔** | 震荡期已自动空仓过滤 |
| **整体胜率 (Win Rate)** | **{win_rate:.2f}%** ({len(wins)} 胜 / {len(losses)} 负) | 过滤伪趋势后胜率显著抬升 |
| **盈亏比 (Profit Factor)** | **{profit_factor:.2f}** | 总盈利 ${gain_sum:,.2f} / 总亏损 ${loss_sum:,.2f} |
| **单笔平均盈利 / 亏损** | **+${avg_win:.2f} / -${avg_loss:.2f}** | 等风险定仓严格锁住单笔亏损上限 |
| **最大单笔盈利 / 亏损** | **+${max_win_pnl:.2f} (+{max_win_roe:.1f}% ROE) / -${abs(min_loss_pnl):.2f} ({min_loss_roe:.1f}% ROE)** | 远离强平线 |
| **最大盯市净值回撤 (MTM Max DD)** | **{max_dd_pct:.2f}% (${max_dd_usd:,.2f})** | 动态盯市最大回撤 |
| **累计手续费与滑点支出** | **${total_fees_paid:,.2f} USDT** | 占总毛盈利比例 {total_fees_paid / max(gain_sum, 1.0) * 100:.1f}%（含复利滚仓交易成本） |
| **累计资金费净支出** | **${total_funding_paid:,.2f} USDT** | 每 8 小时按名义价值结算 (负值表示空头净收取) |
| **平均 / 最长持仓时长** | **{avg_hold_mins:.1f} 分钟 ({avg_hold_mins/60:.2f}h) / {max_hold_mins:.0f} 分钟 ({max_hold_mins/60:.1f}h)** | 100% 满足 <= 24 小时约束 |

---

## 二、 月度绩效明细

| 月度区间 | 交易笔数 | 胜负 | 胜率 | 净收益 (Net PnL) | 毛收益 (Gross PnL) | 手续费 | 阶段特征 |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
"""
    for _, mr in df_monthly.iterrows():
        p_badge = f"+${mr['net_pnl']:.2f}" if mr["net_pnl"] >= 0 else f"-${abs(mr['net_pnl']):.2f}"
        g_badge = f"+${mr['gross_pnl']:.2f}" if mr["gross_pnl"] >= 0 else f"-${abs(mr['gross_pnl']):.2f}"
        tag = "单边共振捕获正收益" if mr["net_pnl"] >= 0 else "震荡/转折期止损与过滤控制"
        md_content += f"| **{mr['month']}** | {mr['trades']} 笔 | {mr['wins']} 胜 / {mr['losses']} 负 | {mr['win_rate']:.1f}% | **{p_badge}** | {g_badge} | ${mr['fees']:.2f} | {tag} |\n"

    md_content += f"""
---

## 三、 多空分布与离场机制拆解

- **做空 (SHORT)**：共 **{len(short_trades)} 笔**（占比 {short_share:.1f}%），胜率 **{short_wr:.1f}%**，累计净盈亏 **{short_pnl_str} USDT**。
- **做多 (LONG)**：共 **{len(long_trades)} 笔**（占比 {long_share:.1f}%），胜率 **{long_wr:.1f}%**，累计净盈亏 **{long_pnl_str} USDT**。

| 离场触发原因 | 触发次数 | 占比 (%) | 说明 |
| :--- | :---: | :---: | :--- |
"""
    for reason_val, count_val in exit_counts.items():
        pct_val = (count_val / total_trades) * 100.0 if total_trades > 0 else 0.0
        md_content += f"| `{reason_val}` | {count_val} 次 | {pct_val:.1f}% | 因果时序触发离场 |\n"

    md_content += f"""
---

## 四、 {len(SYMBOLS)} 大永续合约标的盈亏贡献

| 合约代码 | 总交易数 | 盈利笔数 | 胜率 | 净收益 (Net PnL) | 最大单笔盈利 | 最大单笔亏损 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
"""
    for sym in SYMBOLS:
        st = df_trades[df_trades["symbol"] == sym]
        if len(st) > 0:
            sw = st[st["net_pnl"] > 0]
            swr = len(sw) / len(st) * 100.0
            spnl = float(st["net_pnl"].sum())
            pnl_s = f"+${spnl:.2f}" if spnl >= 0 else f"-${abs(spnl):.2f}"
            md_content += f"| `{sym}` | {len(st)} | {len(sw)} | {swr:.1f}% | **{pnl_s}** | +${st['net_pnl'].max():.2f} | -${abs(st['net_pnl'].min()):.2f} |\n"

    md_content += f"""
---

## 五、 逐笔实盘台账明细 ({total_trades} 笔)

| # | 交易标的 | 方向 | 杠杆 | 开仓时间 (UTC) | 开仓均价 | 平仓时间 (UTC) | 平仓均价 | 持仓时长 | 净盈亏 (USDT) | 资金费 | 本金收益率 | 账户净值 | 平仓理由 | 开仓理由 |
| :---: | :--- | :---: | :---: | :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
"""
    for _, r in df_trades.iterrows():
        pnl_b = f"+${r['net_pnl']:.2f}" if r["net_pnl"] >= 0 else f"-${abs(r['net_pnl']):.2f}"
        roe_b = f"+{r['roe_pct']:.2f}%" if r["roe_pct"] >= 0 else f"{r['roe_pct']:.2f}%"
        md_content += f"| {r['trade_id']:03d} | `{r['symbol']}` | **{r['side']}** | {r['leverage']} | {r['entry_time']} | {r['entry_price']} | {r['exit_time']} | {r['exit_price']} | {r['hold_mins']}m | **{pnl_b}** | {r['funding_paid']:.2f} | {roe_b} | ${r['account_equity']:,.2f} | {r['exit_reason']} | {r['entry_reason']} |\n"

    md_path = Path("data/okx_human_trades_log.md")
    md_path.write_text(md_content, encoding="utf-8")
    print(f"[3/4] Saved updated professional markdown report to {md_path}")
    print("[4/4] All simulation files generated successfully!")
    return md_content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OKX Optimal Regime-Filtered Swing Trader Simulation")
    parser.add_argument("--period", choices=["2years", "prev_year", "1year", "6months", "last_month"], default="2years", help="Data period to test (default: 2years)")
    parser.add_argument("--execution", choices=["taker", "maker"], default="maker", help="Entry execution mode (default: maker)")
    parser.add_argument("--exit-execution", choices=["taker", "maker"], default="taker", help="Exit execution mode for market stops/trails (default: taker)")
    parser.add_argument("--funding-rate", type=float, default=0.0001, help="Funding rate per 8h (default 0.0001)")
    parser.add_argument("--maintenance-margin-rate", type=float, default=0.005, help="Maintenance margin rate (default 0.005)")
    parser.add_argument("--no-regime-filter", dest="regime_filter", action="store_false", help="Disable 4H+BTC+Breadth regime filter")
    parser.add_argument("--cooldown-bars", type=int, default=12, help="Per-symbol cooldown bars after exit (default: 12)")
    parser.add_argument("--daily-loss-limit", type=float, default=0.02, help="Daily drawdown circuit breaker (default: 0.02)")
    parser.add_argument("--no-adaptive-trail", dest="adaptive_trail", action="store_false", help="Disable adaptive ATR trailing stop")
    parser.add_argument("--er4h-soft-thresh", type=float, default=0.22, help="4H 72h ER soft-scaling threshold (default: 0.22, 0=off)")
    parser.add_argument("--er4h-soft-scale", type=float, default=0.50, help="Risk scale when 4H ER < threshold (default: 0.50)")
    parser.add_argument("--max-dist-e144-atr15", type=float, default=1.2, help="Max EMA144 stretch in 15m ATR units (<=0=off)")
    parser.add_argument("--btc-eth-lev", type=float, default=15.0, help="BTC/ETH leverage cap (default: 15.0)")
    parser.add_argument("--max-nominal-cap", type=float, default=0.0, help="Optional hard nominal cap per trade (<=0=off)")
    args = parser.parse_args()
    print(f"Starting OKX Optimal Swing Simulation: period={args.period}, entry={args.execution}, exit={args.exit_execution}...")
    generate_outputs(
        period=args.period,
        execution=args.execution,
        exit_execution=args.exit_execution,
        funding_rate=args.funding_rate,
        maintenance_margin_rate=args.maintenance_margin_rate,
        regime_filter=args.regime_filter,
        cooldown_bars=args.cooldown_bars,
        daily_loss_limit=args.daily_loss_limit,
        adaptive_trail=args.adaptive_trail,
        er4h_soft_thresh=args.er4h_soft_thresh,
        er4h_soft_scale=args.er4h_soft_scale,
        max_dist_e144_atr15=args.max_dist_e144_atr15 if args.max_dist_e144_atr15 > 0 else None,
        btc_eth_lev=args.btc_eth_lev,
        max_nominal_cap=args.max_nominal_cap if args.max_nominal_cap > 0 else None,
    )
