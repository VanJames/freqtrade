from __future__ import annotations

from trading_system.hotcoin_sdk import HotcoinWebSession


class FakeResponse:
    def __init__(self, text: str = "{}", payload: dict | None = None) -> None:
        self.text = text
        self.status_code = 200
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_hotcoin_sdk_loads_full_cookie_metadata_and_headers() -> None:
    client = HotcoinWebSession()

    client.load_session_data(
        {
            "token": "tok-1",
            "csrf_token": "csrf-1",
            "device_id": "device-1",
            "session_headers": {"User-Agent": "Agent/1", "Referer": "https://www.hotcoinv5.com/"},
            "full_cookies": [
                {
                    "name": "acw_tc",
                    "value": "waf",
                    "domain": "bi.hotcoins.cn",
                    "path": "/",
                }
            ],
        }
    )

    assert client.device_id == "device-1"
    assert client.csrf_token == "csrf-1"
    assert client.export_session_data()["csrf_token"] == "csrf-1"
    assert client.session.headers["User-Agent"] == "Agent/1"
    cookies = {(cookie.name, cookie.domain) for cookie in client.session.cookies}
    assert ("acw_tc", "bi.hotcoins.cn") in cookies
    assert ("B_TOKEN", "bi.hotcoins.cn") in cookies
    assert ("token", "bi.hotcoins.cn") in cookies


def test_hotcoin_sdk_loads_legacy_cookie_dict() -> None:
    client = HotcoinWebSession()

    client.load_session_data({"token": "tok-2", "cookies": {"acw_tc": "legacy-waf"}})

    cookies = {(cookie.name, cookie.domain) for cookie in client.session.cookies}
    assert ("acw_tc", "bi.hotcoins.cn") in cookies
    assert ("acw_tc", ".hotcoins.cn") in cookies


def test_hotcoin_sdk_waf_retry_preserves_auth_headers_and_sets_cookie() -> None:
    client = HotcoinWebSession()
    client.load_session_data({"token": "tok-3"})
    calls: list[dict] = []

    def fake_request(method, url, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        if len(calls) == 1:
            return FakeResponse("arg1='0123456789abcdef0123456789abcdef01234567'")
        return FakeResponse("{}", {"code": 200})

    client.session.request = fake_request  # type: ignore[method-assign]

    response = client.request("POST", "https://bi.hotcoins.cn/swap/test", json=[{"x": 1}])

    assert response.json()["code"] == 200
    assert len(calls) == 2
    for call in calls:
        headers = call["kwargs"]["headers"]
        assert headers["token"] == "tok-3"
        assert headers["Authorization"] == "Bearer tok-3"
    cookies = {(cookie.name, cookie.domain) for cookie in client.session.cookies}
    assert ("acw_sc__v2", "bi.hotcoins.cn") in cookies


def test_hotcoin_sdk_places_orders_with_v3_encrypted_payload(monkeypatch) -> None:
    client = HotcoinWebSession()
    client.load_session_data({"token": "tok-4", "csrfToken": "csrf-4"})
    calls: list[dict] = []

    monkeypatch.setattr(client, "_get_secret_signing_key", lambda: "signing-secret")
    monkeypatch.setattr(client, "_rsa_oaep_encrypt_base64", lambda value: f"rsa:{value}")
    monkeypatch.setattr(client, "_random_crypto_text", lambda length: "A" * length)

    def fake_request(method, url, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return FakeResponse("{}", {"code": 200, "msg": "ok"})

    client.session.request = fake_request  # type: ignore[method-assign]

    payload = client.place_order(
        symbol="XAU/USDT:USDT",
        side="sell",
        amount=0.0,
        price=1.0,
        order_type="limit",
        reduce_only=True,
    )

    assert payload["code"] == 200
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert "/swap/v3/perpetual/products/XAUUSDT/batch-order" in call["url"]
    assert "token=" not in call["url"]
    headers = call["kwargs"]["headers"]
    assert headers["token"] == "tok-4"
    assert headers["x-csrf-token"] == "csrf-4"
    assert "Authorization" not in headers
    assert "_skip_authorization" not in headers
    assert headers["Origin"] == "https://www.hotcoinv12.com"
    assert headers["Referer"] == "https://www.hotcoinv12.com/"
    body = call["kwargs"]["json"]
    assert set(body) == {"randomKey", "randomIv", "ver", "bizData", "signature", "timestamp"}
    assert body["randomKey"] == "rsa:AAAAAAAAAAAAAAAA"
    assert body["randomIv"] == "rsa:AAAAAAAAAAAAAAAA"
    assert isinstance(body["bizData"], str)
