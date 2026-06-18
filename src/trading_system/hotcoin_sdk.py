from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import getLogger
from typing import Any

import asyncpg
import requests

logger = getLogger(__name__)

HOTCOIN_QR_BASE_URL = "https://binn.adffhttct.com"
HOTCOIN_LOGIN_BASE_URL = "https://binn.hotcoins.cn"
HOTCOIN_WEB_BASE_URL = "https://bi.hotcoins.cn"
DEFAULT_DEVICE_ID = "P_qWAfKXrQ5xBBUlEAUReGmPALdmrgafYJ"
HOTCOIN_SESSION_KEY = "default"


@dataclass(slots=True)
class HotcoinQrLogin:
    token: str
    image_data: str
    expires_at: str


def dashboard_dsn(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def save_hotcoin_session(
    dsn: str, account_key: str, session_data: dict[str, Any]
) -> None:
    conn = await asyncpg.connect(dashboard_dsn(dsn))
    try:
        await ensure_hotcoin_session_schema(conn)
        await conn.execute(
            """
            insert into exchange_sessions(exchange_id, account_key, session_data, updated_at)
            values('hotcoin', $1, $2, now())
            on conflict(exchange_id, account_key)
            do update set session_data = excluded.session_data, updated_at = excluded.updated_at
            """,
            account_key,
            json.dumps(session_data, ensure_ascii=False),
        )
    finally:
        await conn.close()


async def load_hotcoin_session(
    dsn: str, account_key: str = HOTCOIN_SESSION_KEY
) -> dict[str, Any] | None:
    conn = await asyncpg.connect(dashboard_dsn(dsn))
    try:
        await ensure_hotcoin_session_schema(conn)
        raw = await conn.fetchval(
            """
            select session_data
            from exchange_sessions
            where exchange_id = 'hotcoin' and account_key = $1
            """,
            account_key,
        )
    finally:
        await conn.close()
    if not raw:
        return None
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def ensure_hotcoin_session_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        create table if not exists exchange_sessions (
            exchange_id varchar(32) not null,
            account_key varchar(64) not null,
            session_data text not null,
            updated_at timestamp with time zone not null,
            primary key(exchange_id, account_key)
        )
        """
    )


class HotcoinWebSession:
    """Hotcoin browser API client with ccxt-like account/trading methods.

    This intentionally preserves the browser session shape used by Hotcoin's web
    app: saved headers, full cookie metadata, WAF challenge cookie, and token in
    both headers and cookies.
    """

    def __init__(
        self,
        *,
        base_url: str = HOTCOIN_WEB_BASE_URL,
        device_id: str = DEFAULT_DEVICE_ID,
        timeout: float = 30.0,
        retries: int = 3,
        session_loader: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.timeout = timeout
        self.retries = retries
        self.session_loader = session_loader
        self.token = ""
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://www.hotcoinv5.com/",
                "Origin": "https://www.hotcoinv5.com",
                "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "cross-site",
                "priority": "u=1, i",
            }
        )

    def load_session_data(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        self.token = str(data.get("token") or "")
        self.device_id = str(data.get("device_id") or self.device_id)
        headers = data.get("session_headers")
        if isinstance(headers, dict):
            self.session.headers.clear()
            self.session.headers.update({str(k): str(v) for k, v in headers.items()})

        loaded_cookie = False
        full_cookies = data.get("full_cookies")
        if isinstance(full_cookies, list):
            for item in full_cookies:
                loaded_cookie = self._set_cookie_from_item(item) or loaded_cookie

        cookies = data.get("cookies")
        if isinstance(cookies, list):
            for item in cookies:
                loaded_cookie = self._set_cookie_from_item(item) or loaded_cookie
        elif isinstance(cookies, dict):
            for domain in self._token_cookie_domains():
                for name, value in cookies.items():
                    if value is None:
                        continue
                    self.session.cookies.set(str(name), str(value), domain=domain, path="/")
                    loaded_cookie = True

        if not loaded_cookie and self.token:
            logger.warning("hotcoin session loaded without saved cookies; applying token cookies only")
        self._apply_token()

    def export_session_data(self) -> dict[str, Any]:
        full_cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
            }
            for cookie in self.session.cookies
        ]
        return {
            "token": self.token,
            "device_id": self.device_id,
            "session_headers": dict(self.session.headers),
            "full_cookies": full_cookies,
            "cookies": full_cookies,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    def start_qr_login(self) -> HotcoinQrLogin:
        response = self.request(
            "GET", f"{HOTCOIN_QR_BASE_URL}/hk-web/scanLogin/generateQRCodeToken"
        )
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(
                f"Hotcoin QR token request failed: {payload.get('msg') or payload.get('code')}"
            )
        token = str((payload.get("data") or {}).get("qrCodeToken") or "")
        if not token:
            raise RuntimeError("Hotcoin QR token response missing qrCodeToken")
        return HotcoinQrLogin(
            token=token,
            image_data=self._qr_image_data(token),
            expires_at=datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat(),
        )

    def poll_qr_login(self, qr_token: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "platform": "1",
                "client": "1",
                "deviceId": self.device_id,
                "versionCode": "3.2.0",
                "deviceModel": self._device_model(),
                "lang": "zh_CN",
            }
        )
        url = f"{HOTCOIN_LOGIN_BASE_URL}/hk-web/scanLogin/login?{query}"
        headers = dict(self.session.headers)
        headers.pop("Content-Type", None)
        response = self.session.post(
            url, files={"qrCodeToken": (None, qr_token)}, headers=headers, timeout=self.timeout
        )
        payload = response.json()
        code = payload.get("code")
        if code == 200:
            token = str((payload.get("data") or {}).get("token") or "")
            if not token:
                return {"status": "error", "message": "Hotcoin login succeeded without token"}
            self.token = token
            self._apply_token()
            user = self.get_user_info()
            return {"status": "connected", "session": self.export_session_data(), "user": user}
        if code in {201, 10030}:
            return {"status": "pending", "message": payload.get("msg") or "waiting for scan"}
        return {"status": "error", "message": payload.get("msg") or str(payload)}

    def get_user_info(self) -> dict[str, Any] | None:
        response = self.request(
            "GET", self._add_auth_params(f"{self.base_url}/hk-web/v2/getUserInfo")
        )
        payload = response.json()
        if payload.get("code") == 200 and isinstance(payload.get("data"), dict):
            return payload["data"]
        return None

    def get_balance(self) -> dict[str, Any]:
        endpoints = [
            "/swap/v1/perpetual/account/assets/v1/condition",
            "/swap/v2/perpetual/account/assets",
            "/hk-web/swap/v1/perpetual/account/assets/v1/condition",
        ]
        last: dict[str, Any] = {}
        all_401 = True
        for endpoint in endpoints:
            payload = self._json_request("GET", self._add_auth_params(f"{self.base_url}{endpoint}"))
            if payload.get("code") == 200:
                return payload
            if payload.get("code") != 401:
                all_401 = False
            last = payload
        if all_401 and self._reload_session():
            for endpoint in endpoints:
                payload = self._json_request(
                    "GET", self._add_auth_params(f"{self.base_url}{endpoint}")
                )
                if payload.get("code") == 200:
                    return payload
                last = payload
        return last or {"code": 500, "msg": "no hotcoin balance endpoint succeeded"}

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        payload = self._json_request(
            "GET",
            self._add_auth_params(f"{self.base_url}/swap/v2/perpetual/position/list-all"),
            params={"selectedMode": "2", "bizType": "perpetual"},
        )
        if payload.get("code") == 401 and self._reload_session():
            payload = self._json_request(
                "GET",
                self._add_auth_params(f"{self.base_url}/swap/v2/perpetual/position/list-all"),
                params={"selectedMode": "2", "bizType": "perpetual"},
            )
        if payload.get("code") != 200:
            raise RuntimeError(
                f"Hotcoin positions request failed: {payload.get('msg') or payload.get('code')}"
            )
        rows = _extract_position_rows(payload)
        if symbol:
            target = hotcoin_symbol(symbol)
            return [
                row
                for row in rows
                if target
                in str(
                    _first_present(
                        row,
                        "contractCode",
                        "contract_code",
                        "symbol",
                        "contractName",
                        "contract",
                        "productCode",
                        "instrumentId",
                    )
                    or ""
                ).upper()
            ]
        return rows

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        amount: float,
        price: float | None,
        order_type: str,
        reduce_only: bool,
    ) -> dict[str, Any]:
        mapped_side = self._map_side(side, reduce_only)
        type_code = 11 if order_type.lower() == "market" else 10
        item: dict[str, Any] = {
            "type": type_code,
            "side": mapped_side,
            "amount": str(amount),
            "contAmount": str(amount),
            "tradeUnit": "usdt",
            "tradeAmountUnit": float(amount),
            "overrideOrderCondition": False,
        }
        if type_code == 10 and price:
            item["price"] = str(price)
            item["triggerPrice"] = str(price)
        endpoint = self._add_auth_params(
            f"{self.base_url}/swap/v1/perpetual/products/{hotcoin_symbol(symbol)}/batch-order"
        )
        payload = self._json_request("POST", endpoint, json=[item])
        if payload.get("code") == 401 and self._reload_session():
            endpoint = self._add_auth_params(
                f"{self.base_url}/swap/v1/perpetual/products/{hotcoin_symbol(symbol)}/batch-order"
            )
            payload = self._json_request("POST", endpoint, json=[item])
        return payload

    def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        reduce_only = bool(params.get("reduceOnly"))
        return self.place_order(
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
        )

    def fetch_balance(self) -> dict[str, Any]:
        return self.get_balance()

    def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.get_positions(symbol)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._prepare_auth(url, kwargs)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                waf_arg = self._waf_arg(response.text)
                if waf_arg:
                    self._set_waf_cookie(waf_arg, url)
                    self._prepare_auth(url, kwargs)
                    response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                return response
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise
        raise last_error or RuntimeError(f"Hotcoin request failed: {method} {url}")

    def _json_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, url, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            return {"code": response.status_code, "msg": response.text[:200]}
        return payload if isinstance(payload, dict) else {"code": 500, "data": payload}

    def _reload_session(self) -> bool:
        if not self.session_loader:
            return False
        data = self.session_loader()
        if not data:
            return False
        self.load_session_data(data)
        return bool(self.token)

    def _prepare_auth(self, url: str, kwargs: dict[str, Any]) -> None:
        self._apply_token(url)
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers.setdefault("token", self.token)
            headers.setdefault("Authorization", f"Bearer {self.token}")
        merged_headers = dict(self.session.headers)
        merged_headers.update(headers)
        kwargs["headers"] = merged_headers

    def _add_auth_params(self, url: str) -> str:
        separator = "&" if "?" in url else "?"
        query = (
            f"lang=zh_CN&platform=1&client=1&deviceId={urllib.parse.quote(self.device_id)}"
            f"&versionCode=3.2.0&deviceModel={urllib.parse.quote_plus(self._device_model())}"
        )
        if self.token:
            query += f"&token={urllib.parse.quote(self.token)}"
        return f"{url}{separator}{query}"

    def _apply_token(self, request_url: str | None = None) -> None:
        if not self.token:
            return
        for domain in self._token_cookie_domains(request_url):
            self.session.cookies.set("B_TOKEN", self.token, domain=domain, path="/")
            self.session.cookies.set("token", self.token, domain=domain, path="/")

    def _token_cookie_domains(self, request_url: str | None = None) -> list[str]:
        domains = [self._host(self.base_url), ".hotcoins.cn", "hotcoins.cn", ".hotcoinv5.com"]
        if request_url:
            host = self._host(request_url)
            if host:
                domains.append(host)
        return list(dict.fromkeys(domain for domain in domains if domain))

    def _device_model(self) -> str:
        user_agent = self.session.headers.get("User-Agent", "Chrome 143.0.0.0")
        chrome_match = re.search(r"Chrome/([\d.]+)", user_agent)
        os_match = re.search(r"\(([^)]+)\)", user_agent)
        chrome_ver = chrome_match.group(1) if chrome_match else "143.0.0.0"
        os_name = os_match.group(1) if os_match else "macOS"
        return f"Chrome {chrome_ver} ({os_name})"

    @staticmethod
    def _host(url: str) -> str:
        parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
        return parsed.netloc or parsed.path

    def _set_cookie_from_item(self, item: object) -> bool:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        value = item.get("value")
        if name is None or value is None:
            return False
        domain = str(item.get("domain") or ".hotcoins.cn")
        path = str(item.get("path") or "/")
        self.session.cookies.set(str(name), str(value), domain=domain, path=path)
        return True

    def _set_waf_cookie(self, arg1: str, request_url: str) -> None:
        value = self._get_acw_sc_v2(arg1)
        for domain in [self._host(self.base_url), "hotcoins.cn", ".hotcoins.cn", self._host(request_url)]:
            if domain:
                self.session.cookies.set("acw_sc__v2", value, domain=domain, path="/")

    @staticmethod
    def _waf_arg(text: str) -> str | None:
        marker = "arg1='"
        if marker not in text:
            return None
        start = text.find(marker) + len(marker)
        end = text.find("'", start)
        return text[start:end] if end > start else None

    @staticmethod
    def _get_acw_sc_v2(arg1: str) -> str:
        key = "3000176000856006061501533003690027800375"
        mapping = [
            0xF,
            0x23,
            0x1D,
            0x18,
            0x21,
            0x10,
            0x1,
            0x26,
            0xA,
            0x9,
            0x13,
            0x1F,
            0x28,
            0x1B,
            0x16,
            0x17,
            0x19,
            0xD,
            0x6,
            0xB,
            0x27,
            0x12,
            0x14,
            0x8,
            0xE,
            0x15,
            0x20,
            0x1A,
            0x2,
            0x1E,
            0x7,
            0x4,
            0x11,
            0x5,
            0x3,
            0x1C,
            0x22,
            0x25,
            0xC,
            0x24,
        ]
        unboxed = [""] * len(arg1)
        for i, char in enumerate(arg1):
            for j, map_val in enumerate(mapping):
                if map_val == i + 1 and j < len(unboxed):
                    unboxed[j] = char
        unboxed_str = "".join(unboxed)
        result = ""
        for i in range(0, min(len(unboxed_str), len(key)), 2):
            val1 = int(unboxed_str[i : i + 2], 16)
            val2 = int(key[i : i + 2], 16)
            result += f"{val1 ^ val2:02x}"
        return result

    @staticmethod
    def _qr_image_data(token: str) -> str:
        import qrcode

        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(token)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")

    @staticmethod
    def _map_side(side: str, reduce_only: bool) -> str:
        side = side.lower()
        if side in {"open_long", "open_short", "close_long", "close_short"}:
            return side
        if reduce_only:
            return "close_long" if side == "sell" else "close_short"
        return "open_long" if side == "buy" else "open_short"


def hotcoin_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "").upper()


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _extract_position_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "data", "positions", "positionList"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _extract_position_rows({"data": value})
                if nested:
                    return nested
    return [data] if isinstance(data, dict) else []
