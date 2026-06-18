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
