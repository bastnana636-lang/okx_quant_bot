"""Unit tests for the retained optimal 4H+BTC+Breadth regime engine and simulation wiring."""

from __future__ import annotations

import numpy as np
import pandas as pd

from regime_engine import RegimeConfig, compute_regime_masks, precompute_regime_features


def _make_synthetic_market(n_bars: int = 2500, seed: int = 42):
    rng = np.random.default_rng(seed)
    ts = list(pd.date_range("2026-01-01 00:00:00+00:00", periods=n_bars, freq="5min"))
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    data = {}
    for idx, sym in enumerate(symbols):
        ret = rng.normal(0.0001 * (1 if idx == 0 else 0.5), 0.0025, size=n_bars)
        c = 100.0 * np.exp(np.cumsum(ret))
        o = np.roll(c, 1)
        o[0] = c[0]
        spread = np.abs(rng.normal(0.0015, 0.0005, size=n_bars)) * c
        h = np.maximum(o, c) + spread
        l = np.minimum(o, c) - spread
        v = rng.uniform(100.0, 1000.0, size=n_bars)
        ema12 = pd.Series(c).ewm(span=12, adjust=False).mean().to_numpy()
        ema36 = pd.Series(c).ewm(span=36, adjust=False).mean().to_numpy()
        ema144 = pd.Series(c).ewm(span=144, adjust=False).mean().to_numpy()
        ema288 = pd.Series(c).ewm(span=288, adjust=False).mean().to_numpy()
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr14 = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        data[sym] = {
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "v": v,
            "ema12": ema12,
            "ema36": ema36,
            "ema144": ema144,
            "ema288": ema288,
            "atr14": atr14,
        }
    return ts, symbols, data


def test_zero_lookahead_regime_features():
    """Perturbing bars strictly after cut MUST NOT change any feature or mask at <= cut."""
    ts, symbols, data_orig = _make_synthetic_market(n_bars=2600, seed=123)
    feat_orig = precompute_regime_features(ts, data_orig, symbols)
    cfg = RegimeConfig(require_btc_4h=True, min_breadth_frac=0.60)
    masks_orig = compute_regime_masks(feat_orig, symbols, cfg)

    for cut in [1500, 1726, 1727, 1728]:
        data_pert = {}
        for sym in symbols:
            sd = {k: v.copy() for k, v in data_orig[sym].items()}
            for k in ("o", "h", "l", "c"):
                sd[k][cut + 1 :] *= 5.0
            sd["h"][cut + 1 :] *= 1.5
            sd["l"][cut + 1 :] *= 0.5
            data_pert[sym] = sd

        feat_pert = precompute_regime_features(ts, data_pert, symbols)
        masks_pert = compute_regime_masks(feat_pert, symbols, cfg)

        for sym in symbols:
            for key, arr_o in feat_orig[sym].items():
                arr_p = feat_pert[sym][key]
                assert np.array_equal(arr_o[: cut + 1], arr_p[: cut + 1], equal_nan=True), (
                    f"Lookahead detected at cut={cut}, sym={sym}, key={key}"
                )
            lo_o, sh_o = masks_orig[sym]
            lo_p, sh_p = masks_pert[sym]
            assert np.array_equal(lo_o[: cut + 1], lo_p[: cut + 1]), f"Long mask leak at cut={cut}"
            assert np.array_equal(sh_o[: cut + 1], sh_p[: cut + 1]), f"Short mask leak at cut={cut}"


def test_optimal_regime_gates():
    """Verify 4H + BTC alignment and 60% breadth gate jointly control entry permission."""
    ts, symbols, data = _make_synthetic_market(n_bars=1200, seed=7)
    feat = precompute_regime_features(ts, data, symbols)
    sym = "ETH-USDT-SWAP"

    # Case 1: all conditions satisfied at bar 1000
    feat[sym]["bull_4h"][1000] = True
    feat[sym]["btc_bull_4h"][1000] = True
    feat[sym]["univ_bull_frac"][1000] = 0.70
    feat[sym]["univ_med_ret12"][1000] = 0.01

    # Case 2: BTC 4H not aligned at bar 1001
    feat[sym]["bull_4h"][1001] = True
    feat[sym]["btc_bull_4h"][1001] = False
    feat[sym]["univ_bull_frac"][1001] = 0.70
    feat[sym]["univ_med_ret12"][1001] = 0.01

    # Case 3: breadth below 60% at bar 1002
    feat[sym]["bull_4h"][1002] = True
    feat[sym]["btc_bull_4h"][1002] = True
    feat[sym]["univ_bull_frac"][1002] = 0.50
    feat[sym]["univ_med_ret12"][1002] = 0.01

    masks = compute_regime_masks(feat, symbols, RegimeConfig(require_btc_4h=True, min_breadth_frac=0.60))
    l_mask, _ = masks[sym]
    assert bool(l_mask[1000]) is True
    assert bool(l_mask[1001]) is False
    assert bool(l_mask[1002]) is False

    feat[sym]["er4h_18"][1000] = 0.15
    masks_er = compute_regime_masks(
        feat, symbols, RegimeConfig(require_btc_4h=True, min_breadth_frac=0.60, min_er4h_18=0.22)
    )
    assert bool(masks_er[sym][0][1000]) is False


def test_adaptive_trail_and_portfolio_risk_helpers():
    """Verify Idea 1 adaptive trailing thresholds and Idea 2 open portfolio risk helper."""
    from okx_human_trader import Position, _compute_open_portfolio_risk, _compute_trail_params

    trig_low, pb_low = _compute_trail_params(0.0025, True, 0.016, 0.006, 3.2, 0.012, 0.018, 0.375, 0.0045, 0.0065)
    assert abs(trig_low - 0.012) < 1e-9 and abs(pb_low - 0.0045) < 1e-9

    trig_high, pb_high = _compute_trail_params(0.0060, True, 0.016, 0.006, 3.2, 0.012, 0.018, 0.375, 0.0045, 0.0065)
    assert abs(trig_high - 0.018) < 1e-9 and abs(pb_high - 0.0065) < 1e-9

    trig_off, pb_off = _compute_trail_params(0.0025, False, 0.016, 0.006, 3.2, 0.012, 0.018, 0.375, 0.0045, 0.0065)
    assert trig_off == 0.016 and pb_off == 0.006

    pos = Position(
        trade_id=1, symbol="BTC-USDT-SWAP", side="LONG", mode="SWING",
        entry_bar=10, entry_time=pd.Timestamp("2026-01-01 01:00:00+00:00"),
        entry_price=100.0, leverage=10.0, margin=1000.0, nominal_size=10000.0,
        quantity=100.0, stop_loss=98.5, take_profit=104.5, tp_pct=0.045,
        entry_reason="test", peak=100.0, trough=100.0, trail_trig=0.012, trail_pb=0.0045,
    )
    assert abs(_compute_open_portfolio_risk({"BTC-USDT-SWAP": pos}, 10000.0) - 0.015) < 1e-9
    pos.peak = 102.0  # trailing activated -> eff_sl = 102*(1-0.0045) = 101.541 > entry_price
    assert _compute_open_portfolio_risk({"BTC-USDT-SWAP": pos}, 10000.0) == 0.0


def test_simulation_integration_optimal_strategy():
    """Verify run_human_trading_simulation runs end-to-end with retained optimal defaults."""
    from okx_human_trader import SYMBOLS, run_human_trading_simulation

    ts, _, base_data = _make_synthetic_market(n_bars=1200, seed=99)
    full_data = {}
    for idx, sym in enumerate(SYMBOLS):
        src = base_data[list(base_data.keys())[idx % len(base_data)]]
        full_data[sym] = {k: v.copy() for k, v in src.items()}
        c = pd.Series(full_data[sym]["c"])
        diff = c.diff()
        up = diff.clip(lower=0)
        dn = -diff.clip(upper=0)
        ma_up = up.ewm(alpha=1.0 / 9, adjust=False).mean()
        ma_dn = dn.ewm(alpha=1.0 / 9, adjust=False).mean()
        full_data[sym]["rsi9"] = (100.0 - (100.0 / (1.0 + ma_up / (ma_dn + 1e-9)))).to_numpy()
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        full_data[sym]["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()).to_numpy()
        full_data[sym]["ema200"] = c.ewm(span=200, adjust=False).mean().to_numpy()

    cash, trades, equity = run_human_trading_simulation(
        period="2years",
        preloaded_data=(ts, full_data),
    )
    assert np.isfinite(cash)
    assert len(equity) > 0
    assert {"net_pnl", "symbol", "side"}.issubset(trades.columns)


def test_zec_retained_in_portfolio_and_loader_splicing(tmp_path, monkeypatch):
    """Verify ZEC-USDT-SWAP is in SYMBOLS and splices Binance pre-listing + OKX native data."""
    import okx_human_trader as oht

    assert "ZEC-USDT-SWAP" in oht.SYMBOLS
    assert len(oht.SYMBOLS) == 11

    monkeypatch.setattr(oht, "DATA_DIR", tmp_path)
    dt_bn = pd.date_range("2025-11-06 03:05:00+00:00", periods=4, freq="5min")
    dt_okx = pd.date_range("2025-11-06 03:15:00+00:00", periods=3, freq="5min")
    pd.DataFrame({"dt": dt_bn, "o": [1, 2, 3, 4], "h": 1, "l": 1, "c": [10, 20, 30, 40], "vol": 1.0}).to_csv(
        tmp_path / "ZECUSDT_binance_5m_2years.csv", index=False
    )
    pd.DataFrame({"dt": dt_okx, "o": [999, 5, 6], "h": 1, "l": 1, "c": [999, 50, 60], "vol": 1.0}).to_csv(
        tmp_path / "ZEC-USDT-SWAP_5m_1year.csv", index=False
    )

    df_2y = oht._load_symbol_candles("ZEC-USDT-SWAP", "2years", "{s}_5m_2years.csv")
    assert len(df_2y) == 5
    # <= 03:15 comes from Binance (c=30 at 03:15, skipping OKX's 999 auction bar), > 03:15 from OKX
    assert list(df_2y["c"]) == [10, 20, 30, 50, 60]

    df_1y = oht._load_symbol_candles("ZEC-USDT-SWAP", "1year", "{s}_5m_1year.csv")
    assert len(df_1y) == 3
    assert list(df_1y["c"]) == [999, 50, 60]
