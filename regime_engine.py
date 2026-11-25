"""Causal multi-timeframe regime detection engine for OKX perpetual swing trading.

Zero-Lookahead Guarantee:
All higher-timeframe (15m / 1h / 4h) candles are labeled by their *completion*
timestamp (`label='right', closed='left'`) and aligned onto 5m bars using the
5m bar close time (`bar_end_dt = bar_start_dt + 5m`) via forward-fill (`ffill`).
Thus, at the close of 5m bar `i`, only higher-timeframe bars whose completion
timestamp is `<= bar_end_dt` are ever visible.

Retained Optimal Regime Combination:
1. 4H Structural Alignment + BTC Resonance:
   - Bullish: 4H Close > EMA20_4h > EMA50_4h and EMA20_4h rising, for both symbol and BTC.
   - Bearish: 4H Close < EMA20_4h < EMA50_4h and EMA20_4h falling, for both symbol and BTC.
2. Cross-Sectional Market Breadth (multi-asset universe on completed 1H/4H bars):
   - Bullish breadth >= 60% (Close_1h > EMA20_1h and Close_4h > EMA20_4h) & median 12h return > 0.
   - Bearish breadth >= 60% (Close_1h < EMA20_1h and Close_4h < EMA20_4h) & median 12h return < 0.
3. 15m Wilder ATR(14) exported for stop-loss volatility scaling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeConfig:
    """Parameters for the optimal 4H + BTC + Market Breadth regime engine."""

    require_btc_4h: bool = True
    min_breadth_frac: float = 0.60
    min_median_ret_12h: float = 0.0
    min_er4h_18: float = 0.0


def _wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Compute causal Wilder ATR."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    atr.iloc[:period] = np.nan
    return atr


def _resample_completed(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    bar_start_idx: pd.DatetimeIndex,
    rule: str,
) -> pd.DataFrame:
    """Resample 5m bars into completed higher-TF bars indexed by completion timestamp."""
    df = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "v": v}, index=bar_start_idx)
    res = df.resample(rule, origin="epoch", closed="left", label="right").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    )
    counts = df["c"].resample(rule, origin="epoch", closed="left", label="right").count()
    res = res[counts > 0].dropna()
    final_end = bar_start_idx[-1] + pd.Timedelta(minutes=5)
    return res[res.index <= final_end]


def precompute_regime_features(
    common_ts: List[pd.Timestamp],
    market_data: Dict[str, Dict[str, np.ndarray]],
    symbols: List[str],
) -> Dict[str, Dict[str, np.ndarray]]:
    """Precompute strictly causal 15m ATR, 4H structure, and 10-coin breadth features."""
    bar_start_idx = pd.DatetimeIndex(common_ts)
    bar_end_idx = bar_start_idx + pd.Timedelta(minutes=5)
    n_bars = len(common_ts)

    def _align(series: pd.Series, fill_val=np.nan, dtype=np.float64) -> np.ndarray:
        aligned = series.reindex(bar_end_idx, method="ffill")
        if fill_val is not None and not (isinstance(fill_val, float) and np.isnan(fill_val)):
            aligned = aligned.fillna(fill_val)
        return aligned.to_numpy(dtype=dtype, copy=True)

    sym_1h: Dict[str, pd.DataFrame] = {}
    sym_4h: Dict[str, pd.DataFrame] = {}
    features: Dict[str, Dict[str, np.ndarray]] = {}

    for s in symbols:
        sd = market_data[s]
        df15 = _resample_completed(sd["o"], sd["h"], sd["l"], sd["c"], sd["v"], bar_start_idx, "15min")
        df1h = _resample_completed(sd["o"], sd["h"], sd["l"], sd["c"], sd["v"], bar_start_idx, "1h")
        df4h = _resample_completed(sd["o"], sd["h"], sd["l"], sd["c"], sd["v"], bar_start_idx, "4h")

        atr15 = _wilder_atr(df15["h"], df15["l"], df15["c"], period=14)

        ema20_1h = df1h["c"].ewm(span=20, adjust=False).mean()
        df1h["ema20"] = ema20_1h
        df1h["ret12"] = df1h["c"] / df1h["c"].shift(12) - 1.0
        sym_1h[s] = df1h

        ema20_4h = df4h["c"].ewm(span=20, adjust=False).mean()
        ema50_4h = df4h["c"].ewm(span=50, adjust=False).mean()
        bull_4h = (df4h["c"] > ema20_4h) & (ema20_4h > ema50_4h) & (ema20_4h > ema20_4h.shift(1))
        bear_4h = (df4h["c"] < ema20_4h) & (ema20_4h < ema50_4h) & (ema20_4h < ema20_4h.shift(1))
        er4h_net = (df4h["c"] - df4h["c"].shift(18)).abs()
        er4h_path = df4h["c"].diff().abs().rolling(18, min_periods=18).sum().replace(0.0, np.nan)
        er4h_18 = er4h_net / er4h_path
        df4h["ema20"] = ema20_4h
        sym_4h[s] = df4h

        features[s] = {
            "atr15m": _align(atr15),
            "bull_4h": _align(bull_4h.astype(float), fill_val=0.0, dtype=np.float64) > 0.5,
            "bear_4h": _align(bear_4h.astype(float), fill_val=0.0, dtype=np.float64) > 0.5,
            "er4h_18": _align(er4h_18, fill_val=0.0, dtype=np.float64),
        }

    # Cross-sectional 10-symbol breadth on aligned completed 1H grid
    common_1h_idx = sym_1h[symbols[0]].index
    for s in symbols[1:]:
        common_1h_idx = common_1h_idx.intersection(sym_1h[s].index)

    bull_mat = np.zeros((len(symbols), len(common_1h_idx)), dtype=np.float64)
    bear_mat = np.zeros((len(symbols), len(common_1h_idx)), dtype=np.float64)
    ret12_mat = np.full((len(symbols), len(common_1h_idx)), np.nan, dtype=np.float64)

    for idx_s, s in enumerate(symbols):
        d1 = sym_1h[s].reindex(common_1h_idx)
        d4 = sym_4h[s].reindex(common_1h_idx, method="ffill")
        bull_mat[idx_s] = ((d1["c"] > d1["ema20"]) & (d4["c"] > d4["ema20"])).fillna(False).to_numpy(dtype=np.float64)
        bear_mat[idx_s] = ((d1["c"] < d1["ema20"]) & (d4["c"] < d4["ema20"])).fillna(False).to_numpy(dtype=np.float64)
        ret12_mat[idx_s] = d1["ret12"].to_numpy(dtype=np.float64)

    bull_frac_1h = pd.Series(np.mean(bull_mat, axis=0), index=common_1h_idx)
    bear_frac_1h = pd.Series(np.mean(bear_mat, axis=0), index=common_1h_idx)
    valid_ret_cols = np.any(np.isfinite(ret12_mat), axis=0)
    med_vals = np.zeros(len(common_1h_idx), dtype=np.float64)
    if np.any(valid_ret_cols):
        med_vals[valid_ret_cols] = np.nanmedian(ret12_mat[:, valid_ret_cols], axis=0)
    med_ret12_1h = pd.Series(med_vals, index=common_1h_idx)

    bull_frac_5m = _align(bull_frac_1h, fill_val=0.0, dtype=np.float64)
    bear_frac_5m = _align(bear_frac_1h, fill_val=0.0, dtype=np.float64)
    med_ret12_5m = _align(med_ret12_1h, fill_val=0.0, dtype=np.float64)

    btc_sym = "BTC-USDT-SWAP" if "BTC-USDT-SWAP" in features else symbols[0]
    btc_bull_4h = features[btc_sym]["bull_4h"]
    btc_bear_4h = features[btc_sym]["bear_4h"]

    for s in symbols:
        features[s]["btc_bull_4h"] = btc_bull_4h
        features[s]["btc_bear_4h"] = btc_bear_4h
        features[s]["univ_bull_frac"] = bull_frac_5m
        features[s]["univ_bear_frac"] = bear_frac_5m
        features[s]["univ_med_ret12"] = med_ret12_5m

    assert len(bull_frac_5m) == n_bars
    return features


def compute_regime_masks(
    regime_features: Dict[str, Dict[str, np.ndarray]],
    symbols: List[str],
    cfg: RegimeConfig,
) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """Compute per-bar boolean masks (long_allowed, short_allowed) for each symbol."""
    masks: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for s in symbols:
        rf = regime_features[s]
        long_ok = rf["bull_4h"] & (rf["univ_bull_frac"] >= cfg.min_breadth_frac) & (
            rf["univ_med_ret12"] > cfg.min_median_ret_12h
        )
        short_ok = rf["bear_4h"] & (rf["univ_bear_frac"] >= cfg.min_breadth_frac) & (
            rf["univ_med_ret12"] < -cfg.min_median_ret_12h
        )
        if cfg.require_btc_4h:
            long_ok = long_ok & rf["btc_bull_4h"]
            short_ok = short_ok & rf["btc_bear_4h"]
        if cfg.min_er4h_18 > 0.0 and "er4h_18" in rf:
            er_ok = rf["er4h_18"] >= cfg.min_er4h_18
            long_ok = long_ok & er_ok
            short_ok = short_ok & er_ok
        masks[s] = (long_ok, short_ok)
    return masks
