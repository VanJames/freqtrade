import json

from fastapi.testclient import TestClient

from scripts import trade_settings_server as server


def _setup_paths(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SETTINGS_USERNAME=admin\nSETTINGS_PASSWORD=secret\n")
    config_path = tmp_path / "user_data" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"dry_run": True, "exchange": {"name": "okx"}}))
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "ENV_PATH", env_path)
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "STATE_PATH", tmp_path / "user_data" / "trade_execution.json")
    monkeypatch.setattr(server, "SESSION_PATH", tmp_path / "user_data" / "hotcoin_session.json")
    monkeypatch.setattr(
        server,
        "SIGNAL_DIAGNOSTICS_PATH",
        tmp_path / "user_data" / "signals" / "signal_diagnostics.jsonl",
    )
    monkeypatch.setattr(
        server, "RESEARCH_LEDGER_PATH", tmp_path / "user_data" / "autoopt" / "research_ledger.jsonl"
    )
    monkeypatch.setattr(server, "PUBLIC_BASE_PATH", "")
    return env_path


def test_settings_page_requires_basic_auth(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.get("/")

    assert response.status_code == 401


def test_landing_page_has_ai_trading_menu(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.get("/landing")

    assert response.status_code == 200
    assert "<title>AI Trading</title>" in response.text
    assert 'href="/trade"' in response.text
    assert 'href="/trade-settings/"' in response.text
    assert 'href="/trade-settings/signals"' in response.text


def test_save_settings_writes_state_without_restart(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "hotcoin",
            "hotcoin_amount": "3",
            "hotcoin_order_type": "market",
            "freqtrade_mode": "dry_run",
            "live_trading_disabled": "true",
            "direction_advisor_gate_enabled": "true",
            "direction_advisor_gate_max_age_minutes": "75",
            "direction_advisor_gate_stale_policy": "hold",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    state = json.loads(server.STATE_PATH.read_text())
    assert state["hotcoin_signal_bridge_enabled"] is True
    assert state["hotcoin_signal_execute"] is False
    assert state["live_trading_disabled"] is True
    assert state["direction_advisor_gate_enabled"] is True
    assert state["direction_advisor_gate_max_age_minutes"] == 75.0
    assert state["direction_advisor_gate_stale_policy"] == "hold"
    assert state["direction_advisor_path"] == "/freqtrade/user_data/autoopt/direction_advisor.json"
    assert state["hotcoin_order_amount"] == 3.0
    assert state["hotcoin_session_path"] == "/freqtrade/user_data/hotcoin_session.json"
    assert json.loads(server.CONFIG_PATH.read_text())["dry_run"] is True


def test_audit_pages_render_jsonl_tail(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    server.SIGNAL_DIAGNOSTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.SIGNAL_DIAGNOSTICS_PATH.write_text(
        json.dumps({"pair": "BTC/USDT:USDT", "long_blockers": ["adx<=23"]}) + "\n"
    )
    server.RESEARCH_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.RESEARCH_LEDGER_PATH.write_text(
        json.dumps({"run_id": "20260513-010000", "promoted": False}) + "\n"
    )
    client = TestClient(server.app)

    signals = client.get("/signals", auth=("admin", "secret"))
    research = client.get("/research", auth=("admin", "secret"))

    assert signals.status_code == 200
    assert "信号诊断" in signals.text
    assert "BTC/USDT:USDT" in signals.text
    assert research.status_code == 200
    assert "优化审计" in research.text
    assert "20260513-010000" in research.text


def test_save_settings_redirects_with_public_base_path(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "PUBLIC_BASE_PATH", "/trade-settings")
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "hotcoin",
            "hotcoin_amount": "3",
            "hotcoin_order_type": "market",
            "freqtrade_mode": "dry_run",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/trade-settings/?message=")


def test_save_settings_can_disable_hotcoin(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "freqtrade",
            "hotcoin_amount": "0",
            "hotcoin_order_type": "limit",
            "hotcoin_execute": "true",
            "freqtrade_mode": "dry_run",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    state = json.loads(server.STATE_PATH.read_text())
    assert state["hotcoin_signal_bridge_enabled"] is False
    assert state["hotcoin_signal_execute"] is True
    assert state["hotcoin_order_type"] == "limit"


def test_save_settings_switches_freqtrade_to_live_with_confirmation(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "freqtrade",
            "hotcoin_amount": "0",
            "hotcoin_order_type": "market",
            "freqtrade_mode": "live",
            "live_confirm": "true",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert json.loads(server.CONFIG_PATH.read_text())["dry_run"] is False


def test_save_settings_rejects_live_without_confirmation(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "freqtrade",
            "hotcoin_amount": "0",
            "hotcoin_order_type": "market",
            "freqtrade_mode": "live",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert json.loads(server.CONFIG_PATH.read_text())["dry_run"] is True


def test_hotcoin_qr_start_renders_qr(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)

    class FakeHotcoinWebClient:
        def __init__(self, session_path):
            self.session_path = session_path

        def generate_qr_token(self):
            return "qr-token"

        def check_qr_login(self, token):
            return {"code": 200, "data": {"token": "web-token"}}

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server, "HotcoinWebClient", FakeHotcoinWebClient)
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    client = TestClient(server.app)

    response = client.post(
        "/hotcoin/qr/start",
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert server._qr_state["status"] == "logged_in"
    assert "扫码登录成功" in server._qr_state["message"]


def test_hotcoin_qr_poll_keeps_waiting_on_timeout(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    attempts = {"count": 0}

    class FakeHotcoinWebClient:
        def __init__(self, session_path):
            self.session_path = session_path

        def check_qr_login(self, token):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise server.requests.Timeout("read timed out")
            return {"code": 200, "data": {"token": "web-token"}}

    monkeypatch.setattr(server, "HotcoinWebClient", FakeHotcoinWebClient)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    server._poll_hotcoin_qr("qr-token")

    assert attempts["count"] == 2
    assert server._qr_state["status"] == "logged_in"


def test_hotcoin_qr_status_endpoint(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    server._qr_state.update(
        {
            "status": "logged_in",
            "message": "扫码登录成功，session 已保存。",
            "started_at": 123,
        }
    )
    client = TestClient(server.app)

    response = client.get("/hotcoin/qr/status", auth=("admin", "secret"))

    assert response.status_code == 200
    assert response.json() == {
        "status": "logged_in",
        "message": "扫码登录成功，session 已保存。",
        "started_at": 123,
    }


def test_hotcoin_status_prefers_session_token(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    server.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.SESSION_PATH.write_text(json.dumps({"token": "abcdef1234567890"}))
    client = TestClient(server.app)

    response = client.get("/hotcoin/status", auth=("admin", "secret"))

    assert response.status_code == 200
    assert response.json() == {
        "token_mask": "abcd...7890",
        "has_token": True,
        "session_exists": True,
        "verified": None,
        "message": "已保存登录态，建议点击验证确认是否仍有效。",
    }


def test_hotcoin_verify_login_success(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    server.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.SESSION_PATH.write_text(json.dumps({"token": "abcdef1234567890"}))

    class FakeHotcoinWebClient:
        def __init__(self, session_path):
            self.session_path = session_path

        def user_info(self):
            return {"code": 200, "data": {"fid": 123}}

    monkeypatch.setattr(server, "HotcoinWebClient", FakeHotcoinWebClient)
    client = TestClient(server.app)

    response = client.get("/hotcoin/verify", auth=("admin", "secret"))

    assert response.status_code == 200
    assert response.json()["verified"] is True
    assert response.json()["message"] == "Hotcoin 登录态有效。"


def test_hotcoin_verify_login_failure(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    server.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.SESSION_PATH.write_text(json.dumps({"token": "abcdef1234567890"}))

    class FakeHotcoinWebClient:
        def __init__(self, session_path):
            self.session_path = session_path

        def user_info(self):
            return {"code": 401, "msg": "unauthorized"}

    monkeypatch.setattr(server, "HotcoinWebClient", FakeHotcoinWebClient)
    client = TestClient(server.app)

    response = client.get("/hotcoin/verify", auth=("admin", "secret"))

    assert response.status_code == 200
    assert response.json()["verified"] is False
    assert "可能已失效" in response.json()["message"]
