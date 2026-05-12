#!/usr/bin/env python3
"""Hotcoin adapter for account checks and guarded manual order execution.

This is intentionally separate from Freqtrade's ccxt exchange layer. Hotcoin is
not available in ccxt, and wiring website endpoints directly into Freqtrade's
core order loop would bypass several assumptions around orders, wallets and
risk handling.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_API_BASE_URL = "https://api.hotcoin.top"
DEFAULT_WEB_BASE_URL = "https://bi.hotcoins.cn"
DEFAULT_WEB_ORIGIN = "https://www.hotcoinv5.com"
DEFAULT_SESSION_PATH = Path("user_data/hotcoin_session.json")


class HotcoinError(RuntimeError):
    """Raised when Hotcoin returns an unusable response."""


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _json_response(response: requests.Response) -> dict[str, Any] | list[Any]:
    try:
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        body = response.text[:1000] if response is not None else ""
        raise HotcoinError(f"Hotcoin request failed: {exc}; body={body}") from exc


@dataclass
class HotcoinApiClient:
    access_key: str
    secret_key: str
    base_url: str = DEFAULT_API_BASE_URL
    timeout: int = 20
    max_retries: int = 3

    def _sign(self, method: str, host: str, path: str, params: dict[str, Any]) -> str:
        sorted_params = sorted(params.items(), key=lambda item: item[0])
        param_str = "&".join(
            f"{key}={urllib.parse.quote(str(value), safe='')}"
            for key, value in sorted_params
        )
        payload = f"{method.upper()}\n{host}\n{path}\n{param_str}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def request(
        self, method: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        params = params or {}
        parsed = urllib.parse.urlparse(self.base_url)
        host = parsed.hostname or ""
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url.rstrip('/')}{path}"
        auth_params: dict[str, Any] = {
            "AccessKeyId": self.access_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": _utc_timestamp(),
        }

        method_upper = method.upper()
        query_params = auth_params.copy()
        if method_upper == "GET":
            query_params.update(params)
        query_params["Signature"] = self._sign(method_upper, host, path, query_params)

        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if method_upper == "GET":
                    response = requests.get(
                        url, params=query_params, headers=headers, timeout=self.timeout
                    )
                else:
                    response = requests.post(
                        url, params=query_params, json=params, headers=headers, timeout=self.timeout
                    )
                return _json_response(response)
            except (requests.Timeout, requests.ConnectionError, HotcoinError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(attempt + 1)
                    continue
                break
        raise HotcoinError(f"All Hotcoin API retries failed: {last_error}")

    def balance(self) -> dict[str, Any] | list[Any]:
        return self.request("GET", "/api/v1/perpetual/account/assets")

    def positions(self, symbol: str | None = None) -> dict[str, Any] | list[Any]:
        params = {}
        if symbol:
            params["contractCode"] = symbol.replace("/", "-").upper()
        return self.request("GET", "/api/v1/perpetual/position", params)

    def order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "limit",
        price: float | None = None,
        leverage: int = 20,
    ) -> dict[str, Any] | list[Any]:
        contract_code = symbol.replace("/", "-").upper()
        side_map = {
            "buy": "open_long",
            "sell": "close_long",
            "open_long": "open_long",
            "open_short": "open_short",
            "close_long": "close_long",
            "close_short": "close_short",
        }
        payload: dict[str, Any] = {
            "amount": amount,
            "volume": amount,
            "leverRate": leverage,
            "side": side_map.get(side.lower(), side.lower()),
            "type": 11 if order_type.lower() == "market" else 10,
        }
        if price is not None:
            payload["price"] = price
        return self.request(
            "POST", f"/api/v1/perpetual/products/{contract_code}/order", payload
        )


class HotcoinWebClient:
    """Hotcoin web endpoint client using a saved browser token/session."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_WEB_BASE_URL,
        session_path: Path = DEFAULT_SESSION_PATH,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_path = session_path
        self.timeout = timeout
        self.token = token
        self.device_id = os.getenv("HOTCOIN_DEVICE_ID", "P_qWAfKXrQ5xBBUlEAUReGmPALdmrgafYJ")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": os.getenv(
                    "HOTCOIN_USER_AGENT",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{DEFAULT_WEB_ORIGIN}/",
                "Origin": DEFAULT_WEB_ORIGIN,
            }
        )
        self.load_session()

    def load_session(self) -> None:
        if self.session_path.exists():
            data = json.loads(self.session_path.read_text())
            self.token = self.token or data.get("token")
            self.device_id = data.get("device_id", self.device_id)
            for cookie in data.get("full_cookies", []):
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
        if self.token:
            for domain in ["bi.hotcoins.cn", ".hotcoins.cn", ".hotcoinv5.com"]:
                self.session.cookies.set("B_TOKEN", self.token, domain=domain, path="/")
                self.session.cookies.set("token", self.token, domain=domain, path="/")

    def save_session(self) -> None:
        full_cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
            }
            for cookie in self.session.cookies
        ]
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps(
                {
                    "token": self.token,
                    "device_id": self.device_id,
                    "full_cookies": full_cookies,
                },
                indent=2,
            )
        )

    def _auth_url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        params = {
            "lang": "zh_CN",
            "platform": "1",
            "client": "1",
            "deviceId": self.device_id,
            "versionCode": "3.2.0",
            "deviceModel": "Chrome 143.0.0.0 (macOS)",
        }
        if self.token:
            params["token"] = self.token
        return f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> dict[str, Any] | list[Any]:
        if not self.token:
            raise HotcoinError(
                "HOTCOIN_WEB_TOKEN or user_data/hotcoin_session.json is required for web endpoints."
            )
        headers = {"token": self.token, "Authorization": f"Bearer {self.token}"}
        response = self.session.request(
            method.upper(),
            self._auth_url(path),
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
        )
        return _json_response(response)

    def user_info(self) -> dict[str, Any] | list[Any]:
        return self.request("GET", "/hk-web/v2/getUserInfo")

    def balance(self) -> dict[str, Any] | list[Any]:
        endpoints = [
            "/swap/v1/perpetual/account/assets/v1/condition",
            "/swap/v2/perpetual/account/assets",
            "/hk-web/swap/v1/perpetual/account/assets/v1/condition",
        ]
        last: dict[str, Any] | list[Any] | None = None
        for endpoint in endpoints:
            data = self.request("GET", endpoint)
            last = data
            if isinstance(data, dict) and data.get("code") == 200:
                return data
        return last or {"code": 404, "msg": "No working balance endpoint found"}

    def positions(self, symbol: str | None = None) -> dict[str, Any] | list[Any]:
        data = self.request(
            "GET",
            "/swap/v2/perpetual/position/list-all",
            params={"selectedMode": "2", "bizType": "perpetual"},
        )
        if not symbol or not isinstance(data, dict) or data.get("code") != 200:
            return data
        target = symbol.replace("/", "").upper()
        return {
            **data,
            "data": [
                item
                for item in data.get("data", [])
                if target in str(item.get("contractCode", "")).upper()
            ],
        }

    def order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "limit",
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        symbol_fmt = symbol.replace("/", "").upper()
        side_map = {
            "buy": "open_long",
            "sell": "open_short",
            "open_long": "open_long",
            "open_short": "open_short",
            "close_long": "close_long",
            "close_short": "close_short",
        }
        mapped_side = side_map.get(side.lower(), side.lower())
        type_code = 11 if order_type.lower() == "market" else 10
        order: dict[str, Any] = {
            "type": type_code,
            "side": mapped_side,
            "amount": str(amount),
            "contAmount": str(amount),
            "tradeUnit": "usdt",
            "tradeAmountUnit": float(amount),
            "overrideOrderCondition": False,
        }
        if type_code == 10:
            if price is None:
                raise HotcoinError("Limit orders require --price.")
            order["price"] = str(price)
            order["triggerPrice"] = str(price)

        close_side = {"open_long": "close_long", "open_short": "close_short"}.get(mapped_side)
        pre_conditions = []
        if close_side and take_profit is not None:
            order["stopProfit"] = str(take_profit)
            pre_conditions.append(
                {
                    "side": close_side,
                    "type": 12,
                    "algoType": 11,
                    "triggerBy": "last",
                    "triggerPrice": str(take_profit),
                    "stopLimitType": 1,
                    "tradeUnit": "usdt",
                    "tradeAmountUnit": float(amount),
                }
            )
        if close_side and stop_loss is not None:
            order["stopLoss"] = str(stop_loss)
            pre_conditions.append(
                {
                    "side": close_side,
                    "type": 12,
                    "algoType": 11,
                    "triggerBy": "last",
                    "triggerPrice": str(stop_loss),
                    "stopLimitType": 0,
                    "tradeUnit": "usdt",
                    "tradeAmountUnit": float(amount),
                }
            )
        if pre_conditions:
            order["preConditions"] = pre_conditions

        return self.request(
            "POST", f"/swap/v1/perpetual/products/{symbol_fmt}/batch-order", json_body=[order]
        )


def _client(kind: str) -> HotcoinApiClient | HotcoinWebClient:
    _load_dotenv()
    if kind == "api":
        key = os.getenv("HOTCOIN_ACCESS_KEY", "")
        secret = os.getenv("HOTCOIN_SECRET_KEY", "")
        if not key or not secret:
            raise HotcoinError("HOTCOIN_ACCESS_KEY and HOTCOIN_SECRET_KEY are required.")
        return HotcoinApiClient(
            access_key=key,
            secret_key=secret,
            base_url=os.getenv("HOTCOIN_API_BASE_URL", DEFAULT_API_BASE_URL),
            timeout=int(os.getenv("HOTCOIN_TIMEOUT", "20")),
        )
    return HotcoinWebClient(
        token=os.getenv("HOTCOIN_WEB_TOKEN"),
        base_url=os.getenv("HOTCOIN_WEB_BASE_URL", DEFAULT_WEB_BASE_URL),
        session_path=Path(os.getenv("HOTCOIN_SESSION_PATH", str(DEFAULT_SESSION_PATH))),
        timeout=int(os.getenv("HOTCOIN_TIMEOUT", "20")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Hotcoin account/order adapter")
    parser.add_argument("action", choices=["user-info", "balance", "positions", "order"])
    parser.add_argument("--mode", choices=["api", "web"], default=os.getenv("HOTCOIN_MODE", "web"))
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--side", default="open_long")
    parser.add_argument("--amount", type=float, default=0.0)
    parser.add_argument("--type", dest="order_type", choices=["limit", "market"], default="limit")
    parser.add_argument("--price", type=float)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--stop-loss", type=float)
    parser.add_argument("--take-profit", type=float)
    parser.add_argument("--yes", action="store_true", help="Actually place order.")
    args = parser.parse_args()

    client = _client(args.mode)
    if args.action == "user-info":
        if not isinstance(client, HotcoinWebClient):
            raise HotcoinError("user-info is only available in --mode web.")
        result = client.user_info()
    elif args.action == "balance":
        result = client.balance()
    elif args.action == "positions":
        result = client.positions(args.symbol)
    else:
        if not args.yes:
            result = {
                "dry_run": True,
                "message": "Order was not sent. Add --yes to submit to Hotcoin.",
                "order": {
                    "mode": args.mode,
                    "symbol": args.symbol,
                    "side": args.side,
                    "amount": args.amount,
                    "type": args.order_type,
                    "price": args.price,
                    "leverage": args.leverage,
                    "stop_loss": args.stop_loss,
                    "take_profit": args.take_profit,
                },
            }
        elif isinstance(client, HotcoinApiClient):
            result = client.order(
                args.symbol,
                args.side,
                args.amount,
                args.order_type,
                args.price,
                args.leverage,
            )
        else:
            result = client.order(
                args.symbol,
                args.side,
                args.amount,
                args.order_type,
                args.price,
                args.stop_loss,
                args.take_profit,
            )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
