"""Live causal signal & sizing engine aligned with okx_human_trader.py + regime_engine.py."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from controllers.market_making.mean_reversion import MeanReversionSettings


@dataclass(frozen=True)
class HumanSwingSnapshot:
    candle_timestamp: float
    signal: int  # +1 LONG, -1 SHORT, 0 NONE
    stop_pct: Decimal
    trail_trig: Decimal
    trail_pb: Decimal
    atr15: Decimal
    rsi9: Decimal
    er4h: Decimal
    eff_risk_frac: Decimal
    regime_dir: int  # +1 bull, -1 bear, 0 neutral
    reason: str

    @property
    def closed_z_score(self) -> Decimal:
        return self.rsi9

    @property
    def z_score(self) -> Decimal:
        return self.rsi9


def _completed_frame(candles: pd.DataFrame, interval_s: int, now: float, required: int) -> pd.DataFrame:
    if candles is None or len(candles) < required:
        raise ValueError("Warming up candles")
    frame = candles.loc[:, ["timestamp", "open", "high", "low", "close"]].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame["timestamp"].to_numpy(dtype=float)).all():
        raise ValueError("Invalid candle timestamp")
    frame = frame.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    frame = frame[frame["timestamp"] + interval_s <= now]
    if len(frame) < required:
        raise ValueError("Warming up completed candles")
    if not np.isfinite(frame[["open", "high", "low", "close"]].to_numpy(dtype=float)).all():
        raise ValueError("Invalid candle prices")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Invalid candle prices")
    return frame.reset_index(drop=True)


def _wilder_atr_last(frame: pd.DataFrame, period: int = 14) -> float:
    prev_c = frame["close"].shift(1)
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_c).abs(),
            (frame["low"] - prev_c).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    val = float(atr.iloc[-1])
    if not np.isfinite(val) or val <= 0:
        raise ValueError("Invalid 15m ATR")
    return val


def _wilder_rsi_last(close: pd.Series, period: int = 9) -> float:
    diff = close.diff()
    up = diff.clip(lower=0.0)
    down = -diff.clip(upper=0.0)
    ma_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    ma_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = ma_up / (ma_down + 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi.iloc[-1])


def _four_hour_structure(df4h: pd.DataFrame) -> Tuple[bool, bool, float, float]:
    """Return (bull_4h, bear_4h, close_gt_ema20, er4h_18) on completed 4H bars."""
    c = df4h["close"]
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    bull = bool(c.iloc[-1] > ema20.iloc[-1] and ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-1] > ema20.iloc[-2])
    bear = bool(c.iloc[-1] < ema20.iloc[-1] and ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-1] < ema20.iloc[-2])
    er_net = abs(float(c.iloc[-1] - c.iloc[-19])) if len(c) >= 19 else 0.0
    er_path = float(c.diff().abs().tail(18).sum()) if len(c) >= 19 else 0.0
    er4h_18 = (er_net / er_path) if er_path > 1e-12 else 0.0
    above_ema20 = float(c.iloc[-1] > ema20.iloc[-1])
    below_ema20 = float(c.iloc[-1] < ema20.iloc[-1])
    return bull, bear, above_ema20 - below_ema20, er4h_18


def _compute_breadth(
    peer_feeds: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]],
    now: float,
) -> Tuple[float, float, float]:
    """Compute (bull_frac, bear_frac, median_ret_12h) across available 1h/4h peer feeds."""
    if not peer_feeds:
        return 1.0, 1.0, 0.0
    bull_votes = []
    bear_votes = []
    ret12_vals = []
    for df1h_raw, df4h_raw in peer_feeds.values():
        try:
            df1h = _completed_frame(df1h_raw, 3600, now, 21)
            df4h = _completed_frame(df4h_raw, 14400, now, 21)
        except ValueError:
            continue
        c1 = df1h["close"]
        c4 = df4h["close"]
        e20_1h = float(c1.ewm(span=20, adjust=False).mean().iloc[-1])
        e20_4h = float(c4.ewm(span=20, adjust=False).mean().iloc[-1])
        bull_votes.append(1.0 if (float(c1.iloc[-1]) > e20_1h and float(c4.iloc[-1]) > e20_4h) else 0.0)
        bear_votes.append(1.0 if (float(c1.iloc[-1]) < e20_1h and float(c4.iloc[-1]) < e20_4h) else 0.0)
        if len(c1) >= 13 and float(c1.iloc[-13]) > 0:
            ret12_vals.append(float(c1.iloc[-1]) / float(c1.iloc[-13]) - 1.0)
    if not bull_votes:
        return 0.0, 0.0, 0.0
    med_ret = float(np.median(ret12_vals)) if ret12_vals else 0.0
    return float(np.mean(bull_votes)), float(np.mean(bear_votes)), med_ret


def human_swing_snapshot(
    candles_5m: pd.DataFrame,
    candles_15m: pd.DataFrame,
    candles_1h: pd.DataFrame,
    candles_4h: pd.DataFrame,
    now: float,
    mid: Decimal,
    settings: MeanReversionSettings,
    *,
    btc_candles_4h: Optional[pd.DataFrame] = None,
    peer_feeds: Optional[Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]] = None,
    same_side_unprotected_count: int = 0,
) -> HumanSwingSnapshot:
    """Evaluate causal 5m swing entry + 4H/BTC/Breadth regime + 15m ATR stop & adaptive trail."""
    if not mid.is_finite() or mid <= 0:
        raise ValueError("Invalid mid price")

    df5 = _completed_frame(candles_5m, 300, now, 290)
    df15 = _completed_frame(candles_15m, 900, now, max(settings.atr_length + 2, 16))
    _ = _completed_frame(candles_1h, 3600, now, 22)
    df4 = _completed_frame(candles_4h, 14400, now, 52)

    last_ts = float(df5["timestamp"].iloc[-1])
    if now - (last_ts + 300) > 330:
        raise ValueError("Stale 5m candles")

    c5 = df5["close"]
    o5 = df5["open"]
    h5 = df5["high"]
    l5 = df5["low"]

    ema12 = c5.ewm(span=12, adjust=False).mean()
    ema36 = c5.ewm(span=36, adjust=False).mean()
    ema144 = c5.ewm(span=144, adjust=False).mean()
    ema288 = c5.ewm(span=288, adjust=False).mean()
    rsi9 = _wilder_rsi_last(c5, period=9)

    macd_line = c5.ewm(span=12, adjust=False).mean() - c5.ewm(span=26, adjust=False).mean()
    macd_sig = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = float((macd_line - macd_sig).iloc[-1])

    c_last = float(c5.iloc[-1])
    o_last = float(o5.iloc[-1])
    h_last = float(h5.iloc[-1])
    l_last = float(l5.iloc[-1])
    e12_last = float(ema12.iloc[-1])
    e36_last = float(ema36.iloc[-1])
    e144_last = float(ema144.iloc[-1])
    e288_last = float(ema288.iloc[-1])

    # Raw 5m swing signals
    macro_bull = c_last > e144_last > e288_last
    tested_sup = (l_last <= e12_last * 1.002) or (l_last <= e36_last * 1.002)
    closed_strong = c_last > o_last and c_last > e12_last
    mom_bull = macd_hist > 0.0 and 32.0 <= rsi9 <= 50.0
    raw_long = macro_bull and tested_sup and closed_strong and mom_bull

    macro_bear = c_last < e144_last < e288_last
    tested_res = (h_last >= e12_last * 0.998) or (h_last >= e36_last * 0.998)
    closed_weak = c_last < o_last and c_last < e12_last
    mom_bear = macd_hist < 0.0 and 50.0 <= rsi9 <= 68.0
    raw_short = macro_bear and tested_res and closed_weak and mom_bear

    # 15m ATR stop & adaptive trail
    atr15 = _wilder_atr_last(df15, period=settings.atr_length)
    mid_f = float(mid)
    atr15_pct = atr15 / mid_f
    stop_pct_f = float(settings.atr_stop_multiplier) * atr15_pct
    stop_pct_f = max(float(settings.min_stop_loss), stop_pct_f)
    if stop_pct_f > float(settings.max_stop_loss):
        raise ValueError("Required volatility stop exceeds the risk cap")

    if settings.adaptive_trail:
        trig_f = float(np.clip(float(settings.trail_atr_mult) * atr15_pct,
                               float(settings.trail_trig_min), float(settings.trail_trig_max)))
        pb_f = float(np.clip(trig_f * float(settings.trail_pb_ratio),
                             float(settings.trail_pb_min), float(settings.trail_pb_max)))
    else:
        trig_f = float(settings.trail_trig_max)
        pb_f = float(settings.trail_pb_max)

    # 4H Structure + BTC resonance + 11-coin market breadth
    sym_bull_4h, sym_bear_4h, _, er4h_18 = _four_hour_structure(df4)
    if settings.require_btc_4h and btc_candles_4h is not None:
        df_btc4 = _completed_frame(btc_candles_4h, 14400, now, 52)
        btc_bull_4h, btc_bear_4h, _, _ = _four_hour_structure(df_btc4)
    else:
        btc_bull_4h = sym_bull_4h
        btc_bear_4h = sym_bear_4h

    bull_frac, bear_frac, med_ret12 = _compute_breadth(peer_feeds or {}, now)
    min_breadth = float(settings.min_breadth_frac)
    long_regime_ok = sym_bull_4h and btc_bull_4h and bull_frac >= min_breadth and med_ret12 > 0.0
    short_regime_ok = sym_bear_4h and btc_bear_4h and bear_frac >= min_breadth and med_ret12 < 0.0
    regime_dir = 1 if long_regime_ok else (-1 if short_regime_ok else 0)

    signal = 0
    reason = "outside_swing_setup"
    if raw_long:
        if not long_regime_ok:
            reason = "regime_filtered_long"
        else:
            dist = (c_last - e144_last) / max(atr15, 1e-9)
            if float(settings.max_dist_e144_atr15) > 0 and dist > float(settings.max_dist_e144_atr15):
                reason = "overextended_ema144"
            else:
                signal = 1
                reason = "human_swing_long"
    elif raw_short:
        if settings.long_only:
            reason = "long_only_blocked_short"
        elif not short_regime_ok:
            reason = "regime_filtered_short"
        else:
            dist = (e144_last - c_last) / max(atr15, 1e-9)
            if float(settings.max_dist_e144_atr15) > 0 and dist > float(settings.max_dist_e144_atr15):
                reason = "overextended_ema144"
            else:
                signal = -1
                reason = "human_swing_short"
    elif regime_dir == 0:
        reason = "regime_neutral"

    eff_risk = float(settings.risk_frac)
    if float(settings.er4h_soft_thresh) > 0 and er4h_18 < float(settings.er4h_soft_thresh):
        eff_risk *= float(settings.er4h_soft_scale)
    if same_side_unprotected_count == 1:
        eff_risk *= float(settings.unprot_2nd_scale)
    elif same_side_unprotected_count >= 2:
        eff_risk *= float(settings.unprot_3rd_scale)

    return HumanSwingSnapshot(
        candle_timestamp=last_ts,
        signal=signal,
        stop_pct=Decimal(f"{stop_pct_f:.6f}"),
        trail_trig=Decimal(f"{trig_f:.6f}"),
        trail_pb=Decimal(f"{pb_f:.6f}"),
        atr15=Decimal(f"{atr15:.8f}"),
        rsi9=Decimal(f"{rsi9:.2f}"),
        er4h=Decimal(f"{er4h_18:.4f}"),
        eff_risk_frac=Decimal(f"{eff_risk:.6f}"),
        regime_dir=regime_dir,
        reason=reason,
    )


def human_swing_notional(
    cap: Decimal,
    equity: Decimal,
    leverage: int,
    eff_risk_frac: Decimal,
    stop_pct: Decimal,
    margin_ratio: Decimal,
    max_margin_cap: Decimal,
) -> Decimal:
    """Compute equal-risk notional bounded by live equity margin cap and controller notional cap."""
    if stop_pct <= 0 or cap <= 0 or eff_risk_frac <= 0 or equity <= 0 or leverage <= 0:
        return Decimal("0")
    risk_notional = (equity * eff_risk_frac) / stop_pct
    max_margin = min(equity * margin_ratio, max_margin_cap)
    margin_notional = max_margin * Decimal(leverage)
    return min(cap, risk_notional, margin_notional)
