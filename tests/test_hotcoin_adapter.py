import json
from pathlib import Path

import pytest

from scripts.hotcoin_adapter import HotcoinApiClient, HotcoinError, HotcoinWebClient


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def test_api_order_uses_hotcoin_contract_and_payload(monkeypatch):
    captured = {}

    def fake_post(url, params, json, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return DummyResponse({"code": 200, "data": {"orderId": "abc"}})

    monkeypatch.setattr("scripts.hotcoin_adapter.requests.post", fake_post)

    client = HotcoinApiClient("access", "secret", base_url="https://api.example.com")
    result = client.order("BTC/USDT", "open_short", 2, "limit", 81000, 10)

    assert result["code"] == 200
    assert captured["url"] == "https://api.example.com/api/v1/perpetual/products/BTC-USDT/order"
    assert captured["json"]["side"] == "open_short"
    assert captured["json"]["amount"] == 2
    assert captured["json"]["type"] == 10
    assert captured["json"]["price"] == 81000
    assert "Signature" in captured["params"]


def test_web_client_generates_and_saves_qr_session(monkeypatch, tmp_path):
    session_path = tmp_path / "hotcoin_session.json"

    class FakeCookies:
        def set(self, *args, **kwargs):
            return None

        def __iter__(self):
            return iter(())

    class FakeSession:
        headers = {}
        cookies = FakeCookies()

        def get(self, url, timeout):
            return DummyResponse({"code": 200, "data": {"qrCodeToken": "qr-token"}})

        def post(self, url, params, files, headers, timeout):
            return DummyResponse({"code": 200, "data": {"token": "web-token"}})

    client = HotcoinWebClient(session_path=session_path)
    client.session = FakeSession()

    assert client.generate_qr_token() == "qr-token"
    result = client.check_qr_login("qr-token")

    assert result["code"] == 200
    assert json.loads(session_path.read_text())["token"] == "web-token"


def test_web_order_limit_requires_price(tmp_path):
    client = HotcoinWebClient(token="token", session_path=tmp_path / "session.json")

    with pytest.raises(HotcoinError, match="Limit orders require"):
        client.order("BTC/USDT", "open_long", 1, order_type="limit")


def test_web_order_does_not_send_without_yes_via_main(capsys, monkeypatch):
    from scripts import hotcoin_adapter

    monkeypatch.setattr(
        "sys.argv",
        [
            "hotcoin_adapter.py",
            "order",
            "--mode",
            "web",
            "--symbol",
            "BTC/USDT",
            "--side",
            "open_long",
            "--amount",
            "1",
            "--type",
            "market",
        ],
    )

    assert hotcoin_adapter.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert "Add --yes" in output["message"]
