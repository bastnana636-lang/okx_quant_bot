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
    assert round(zec["global"], 10) == 1.2
    total = next(row for row in rows if row["controller"] == "GLOBAL TOTAL")
    assert round(total["realized"], 2) == 2.2
    assert round(total["unrealized"], 2) == 1.25
    assert round(total["global"], 2) == 3.45
    eth = next(row for row in rows if row["controller"] == "okx_pmm_eth")
    assert eth["global_pct"] == "0.70%"


def test_close_request_is_written_for_a_known_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(view, "BASE_DIR", tmp_path)
    monkeypatch.setattr(view, "STATUS_FILE", tmp_path / "missing.json")
    conf = tmp_path / "conf" / "controllers"
    conf.mkdir(parents=True)
    (conf / "conf_okx_pmm_btc.yml").write_text("id: okx_pmm_btc\n", encoding="utf-8")

    message = view.request_position_close("okx_pmm_btc")

    assert (tmp_path / "data" / "dashboard" / "close_requests" / "okx_pmm_btc").is_file()
    assert "okx_pmm_btc" in message


def test_close_request_rejects_unknown_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(view, "BASE_DIR", tmp_path)
    monkeypatch.setattr(view, "STATUS_FILE", tmp_path / "missing.json")
    (tmp_path / "conf" / "controllers").mkdir(parents=True)

    for controller in ("../btc", "GLOBAL TOTAL", "okx_pmm_missing"):
        try:
            view.request_position_close(controller)
        except ValueError:
            continue
        raise AssertionError(controller)


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


def test_session_metrics_resets_realized_and_sums_global_pnl():
    view.reset_session_baseline()
    perf = [
        {"controller": "okx_pmm_btc", "realized": 10.0, "unrealized": 0.0, "global": 10.0, "global_pct": "0.1%", "volume": 1000},
        {"controller": "okx_pmm_eth", "realized": 5.0, "unrealized": -2.0, "global": 3.0, "global_pct": "0.1%", "volume": 500},
        {"controller": "GLOBAL TOTAL", "realized": 15.0, "unrealized": -2.0, "global": 13.0, "global_pct": "0.1%", "volume": 1500},
    ]
    engine = {"start_time": 100000, "uptime": 10}
    r, u, g, pct, hist = view._sync_session_metrics(engine, 1000, perf, 500.0)

    # Initial start: baseline captures 10.0 and 5.0, so realized resets to 0.0!
    assert r == 0.0
    assert u == -2.0
    assert g == -2.0  # global = realized (0) + unrealized (-2)
    assert perf[0]["realized"] == 0.0
    assert perf[1]["realized"] == 0.0

    # Trade occurs: btc gains +1.5 realized
    perf2 = [
        {"controller": "okx_pmm_btc", "realized": 11.5, "unrealized": 0.0, "global": 11.5, "global_pct": "0.1%", "volume": 1100},
        {"controller": "okx_pmm_eth", "realized": 5.0, "unrealized": 0.5, "global": 5.5, "global_pct": "0.1%", "volume": 500},
        {"controller": "GLOBAL TOTAL", "realized": 16.5, "unrealized": 0.5, "global": 17.0, "global_pct": "0.1%", "volume": 1600},
    ]
    r2, u2, g2, pct2, hist2 = view._sync_session_metrics(engine, 1005, perf2, 502.0)
    assert r2 == 1.5
    assert u2 == 0.5
    assert g2 == 2.0  # strictly r2 + u2 = 1.5 + 0.5 = 2.0


def test_handler_post_restart_and_stop_validation(monkeypatch):
    import io

    class DummyHandler(view.Handler):
        def __init__(self, path, headers=None, client_address=("127.0.0.1", 12345), body=b""):
            self.path = path
            self.client_address = client_address
            self.headers = headers or {}
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.responses = []

        def send_response(self, code, message=None):
            self.responses.append(code)

        def send_header(self, keyword, value):
            pass

        def end_headers(self):
            pass

    # 1. Missing X-Requested-With header -> 403
    h_no_header = DummyHandler("/api/restart")
    h_no_header.do_POST()
    data = json.loads(h_no_header.wfile.getvalue().decode("utf-8"))
    assert h_no_header.responses[0] == 403
    assert data["message"] == "请求校验失败。"

    # 2. Non-local IP address -> 403
    h_remote = DummyHandler("/api/restart", headers={"X-Requested-With": "OKX-Dashboard"}, client_address=("192.168.1.100", 12345))
    h_remote.do_POST()
    data = json.loads(h_remote.wfile.getvalue().decode("utf-8"))
    assert h_remote.responses[0] == 403
    assert data["message"] == "只允许从本机控制策略。"

    # 3. Valid local request with header -> successful dispatch
    monkeypatch.setattr(view, "restart_strategy", lambda: (True, "策略已启动"))
    h_valid = DummyHandler("/api/restart", headers={"X-Requested-With": "OKX-Dashboard"})
    h_valid.do_POST()
    data = json.loads(h_valid.wfile.getvalue().decode("utf-8"))
    assert h_valid.responses[0] == 202
    assert data["ok"] is True
    assert data["message"] == "策略已启动"

    # 4. Stop endpoint with case-insensitive header
    monkeypatch.setattr(view, "stop_strategy", lambda: (True, "策略已停止"))
    h_stop = DummyHandler("/api/stop", headers={"X-Requested-With": "okx-dashboard"})
    h_stop.do_POST()
    data = json.loads(h_stop.wfile.getvalue().decode("utf-8"))
    assert h_stop.responses[0] == 202
    assert data["ok"] is True
    assert data["message"] == "策略已停止"

