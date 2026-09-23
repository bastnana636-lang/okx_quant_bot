import json

from dashboard import dashboard as view


def _perf():
    return [
        {"controller": "okx_pmm_zec", "realized": 0.0, "unrealized": -6.5, "global": -6.5,
         "global_pct": "-6.05%", "volume": 107.48},
        {"controller": "okx_pmm_eth", "realized": 1.0, "unrealized": 1.25, "global": 2.25,
         "global_pct": "0.70%", "volume": 320.0},
        {"controller": "GLOBAL TOTAL", "realized": 1.0, "unrealized": -5.25, "global": -4.25,
         "global_pct": "-0.10%", "volume": 427.48},
    ]


def test_manual_close_adds_final_pnl_to_realized_and_global(tmp_path, monkeypatch):
    store = tmp_path / "manual_closes.json"
    monkeypatch.setattr(view, "MANUAL_CLOSES_FILE", store)
    store.write_text(json.dumps({"closes": [{
        "pair": "ZEC-USDT", "side": "SELL", "amount": 0.07, "breakeven": 1535.42, "pnl": 1.2,
    }]}), encoding="utf-8")
    positions = [
        {"pair": "ZEC-USDT", "side": "SELL", "amount": 0.07, "breakeven": 1535.42, "unrealized": -6.5},
        {"pair": "ETH-USDT", "side": "BUY", "amount": 0.01, "breakeven": 3000, "unrealized": 1.25},
    ]

    visible, rows, note = view.apply_manual_closes(positions, _perf())

    assert [item["pair"] for item in visible] == ["ETH-USDT"]
    assert note == "已手动平仓：ZEC-USDT +1.2000"
    zec = next(row for row in rows if row["controller"] == "okx_pmm_zec")
    assert zec["realized"] == 1.2
    assert zec["unrealized"] == 0
    assert zec["global"] == 1.2
    total = next(row for row in rows if row["controller"] == "GLOBAL TOTAL")
    assert round(total["realized"], 2) == 2.2
    assert round(total["unrealized"], 2) == 1.25
    assert round(total["global"], 2) == 3.45
    eth = next(row for row in rows if row["controller"] == "okx_pmm_eth")
    assert eth["global_pct"] == "0.70%"


def test_zero_pnl_still_removes_open_unrealized(tmp_path, monkeypatch):
    store = tmp_path / "manual_closes.json"
    monkeypatch.setattr(view, "MANUAL_CLOSES_FILE", store)
    view.mark_manual_close("ZEC-USDT", "SELL", 0.07, 1535.42, 0)
    positions = [
        {"pair": "ZEC-USDT", "side": "SELL", "amount": 0.07, "breakeven": 1535.42, "unrealized": -6.5},
    ]
    perf = [_perf()[0], _perf()[2]]

    visible, rows, note = view.apply_manual_closes(positions, perf)

    assert visible == []
    assert note == "已手动平仓：ZEC-USDT +0.0000"
    zec = rows[0]
    assert zec["realized"] == 0
    assert zec["unrealized"] == 0
    assert zec["global"] == 0
