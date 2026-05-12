import json

from fastapi.testclient import TestClient

from scripts import trade_settings_server as server


def _setup_paths(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SETTINGS_USERNAME=admin\nSETTINGS_PASSWORD=secret\n")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "ENV_PATH", env_path)
    monkeypatch.setattr(server, "STATE_PATH", tmp_path / "user_data" / "trade_execution.json")
    monkeypatch.setattr(server, "SESSION_PATH", tmp_path / "user_data" / "hotcoin_session.json")
    return env_path


def test_settings_page_requires_basic_auth(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.get("/")

    assert response.status_code == 401


def test_save_settings_writes_state_without_restart(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    client = TestClient(server.app)

    response = client.post(
        "/settings",
        data={
            "exchange": "hotcoin",
            "hotcoin_amount": "3",
            "hotcoin_order_type": "market",
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    state = json.loads(server.STATE_PATH.read_text())
    assert state["hotcoin_signal_bridge_enabled"] is True
    assert state["hotcoin_signal_execute"] is False
    assert state["hotcoin_order_amount"] == 3.0
    assert state["hotcoin_session_path"] == "/freqtrade/user_data/hotcoin_session.json"


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
        },
        auth=("admin", "secret"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    state = json.loads(server.STATE_PATH.read_text())
    assert state["hotcoin_signal_bridge_enabled"] is False
    assert state["hotcoin_signal_execute"] is True
    assert state["hotcoin_order_type"] == "limit"


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
