from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import getLogger
from typing import Any

import asyncpg
import requests

from trading_system.config import Settings
from trading_system.exchange import CcxtOkxExchange, ExchangeClient
from trading_system.models import OrderResult, Position, PositionSide, Side

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


async def save_hotcoin_session(dsn: str, account_key: str, session_data: dict[str, Any]) -> None:
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


async def load_hotcoin_session(dsn: str, account_key: str = HOTCOIN_SESSION_KEY) -> dict[str, Any] | None:
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
    """Small Hotcoin web client copied from the observed browser API shape.

    The public strategy-facing interface stays in HotcoinExchange. This class only handles
    QR login, stored cookies/token, and Hotcoin web account/order endpoints.
    """

    def __init__(
        self,
        *,
        base_url: str = HOTCOIN_WEB_BASE_URL,
        device_id: str = DEFAULT_DEVICE_ID,
        timeout: float = 30.0,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.timeout = timeout
        self.retries = retries
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
            self.session.headers.update({str(k): str(v) for k, v in headers.items()})
        for item in data.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None or value is None:
                continue
            self.session.cookies.set(
                str(name),
                str(value),
                domain=str(item.get("domain") or ".hotcoins.cn"),
                path=str(item.get("path") or "/"),
            )
        self._apply_token()

    def export_session_data(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "device_id": self.device_id,
            "session_headers": dict(self.session.headers),
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                }
                for cookie in self.session.cookies
            ],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    def start_qr_login(self) -> HotcoinQrLogin:
        response = self.request("GET", f"{HOTCOIN_QR_BASE_URL}/hk-web/scanLogin/generateQRCodeToken")
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"Hotcoin QR token request failed: {payload.get('msg') or payload.get('code')}")
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
                "deviceModel": "Chrome 143.0.0.0 (macOS)",
                "lang": "zh_CN",
            }
        )
        url = f"{HOTCOIN_LOGIN_BASE_URL}/hk-web/scanLogin/login?{query}"
        headers = dict(self.session.headers)
        headers.pop("Content-Type", None)
        response = self.session.post(url, files={"qrCodeToken": (None, qr_token)}, headers=headers, timeout=self.timeout)
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
        response = self.request("GET", self._add_auth_params(f"{self.base_url}/hk-web/v2/getUserInfo"))
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
        for endpoint in endpoints:
            response = self.request("GET", self._add_auth_params(f"{self.base_url}{endpoint}"))
            payload = response.json()
            if payload.get("code") == 200:
                return payload
            last = payload
        return last or {"code": 500, "msg": "no hotcoin balance endpoint succeeded"}

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        response = self.request(
            "GET",
            self._add_auth_params(f"{self.base_url}/swap/v2/perpetual/position/list-all"),
            params={"selectedMode": "2", "bizType": "perpetual"},
        )
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"Hotcoin positions request failed: {payload.get('msg') or payload.get('code')}")
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            return []
        if symbol:
            target = hotcoin_symbol(symbol)
            return [row for row in rows if target in str(row.get("contractCode") or "").upper()]
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
        endpoint = self._add_auth_params(f"{self.base_url}/swap/v1/perpetual/products/{hotcoin_symbol(symbol)}/batch-order")
        response = self.request("POST", endpoint, json=[item])
        return response.json()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._apply_token(url)
        headers = kwargs.pop("headers", {})
        merged_headers = dict(self.session.headers)
        merged_headers.update(headers)
        if self.token:
            merged_headers["token"] = self.token
            merged_headers["Authorization"] = f"Bearer {self.token}"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self.session.request(method, url, headers=merged_headers, timeout=self.timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise
        raise last_error or RuntimeError(f"Hotcoin request failed: {method} {url}")

    def _add_auth_params(self, url: str) -> str:
        separator = "&" if "?" in url else "?"
        query = (
            f"lang=zh_CN&platform=1&client=1&deviceId={urllib.parse.quote(self.device_id)}"
            "&versionCode=3.2.0&deviceModel=Chrome+143.0.0.0+(macOS)"
        )
        if self.token:
            query += f"&token={urllib.parse.quote(self.token)}"
        return f"{url}{separator}{query}"

    def _apply_token(self, request_url: str | None = None) -> None:
        if not self.token:
            return
        domains = [".hotcoins.cn", "hotcoins.cn", ".hotcoinv5.com"]
        if request_url:
            parsed = urllib.parse.urlparse(request_url)
            if parsed.netloc:
                domains.append(parsed.netloc)
        for domain in domains:
            self.session.cookies.set("B_TOKEN", self.token, domain=domain, path="/")
            self.session.cookies.set("token", self.token, domain=domain, path="/")

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
        if reduce_only:
            return "close_long" if side == "sell" else "close_short"
        return "open_long" if side == "buy" else "open_short"


class HotcoinExchange(ExchangeClient):
    """ExchangeClient adapter for Hotcoin trading with OKX public market data."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.public_data = CcxtOkxExchange({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        self.client = HotcoinWebSession(
            base_url=settings.hotcoin_base_url,
            device_id=settings.hotcoin_device_id,
            timeout=settings.exchange_request_timeout_seconds,
        )
        self._orders: dict[str, OrderResult] = {}

    async def initialize(self) -> None:
        import ccxt.pro as ccxtpro

        self.public_data.exchange = ccxtpro.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        await self.public_data.api.load_markets()
        session_data = await load_hotcoin_session(self.settings.postgres_dsn)
        self.client.load_session_data(session_data)
        if not self.client.token:
            raise RuntimeError("Hotcoin is selected but QR login session is missing. Log in from dashboard first.")
        user = await asyncio.to_thread(self.client.get_user_info)
        if not user:
            raise RuntimeError("Hotcoin QR login session is expired. Re-scan from dashboard.")
        logger.info("hotcoin exchange initialized with OKX public market data")

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return await self.public_data.fetch_ohlcv(symbol, timeframe, limit)

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        return await self.public_data.watch_ohlcv(symbol, timeframe)

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        return await self.public_data.fetch_order_book(symbol)

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, Any],
    ) -> OrderResult:
        hotcoin_amount = max(amount * 100.0, 0.0)
        reduce_only = str(params.get("reduceOnly", "")).lower() == "true" or bool(params.get("reduceOnly"))
        payload = await asyncio.to_thread(
            self.client.place_order,
            symbol=symbol,
            side=side.value,
            amount=hotcoin_amount,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
        )
        order_id = self._extract_order_id(payload)
        ord_type = str(params.get("ordType") or "").lower()
        status = "open" if ord_type == "post_only" and not reduce_only else "closed"
        order = OrderResult(
            id=order_id,
            symbol=symbol,
            side=side,
            position_side=PositionSide(params.get("posSide", "long")),
            amount=amount,
            price=price,
            status=status,
            filled=amount if status == "closed" else 0.0,
            remaining=0.0 if status == "closed" else amount,
            average=price,
            raw=payload,
        )
        self._orders[order.id] = order
        return order

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        order = self._orders.get(order_id)
        if order:
            return order
        raise KeyError(f"hotcoin order not found in local tracker: {order_id}")

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        order = self._orders.get(order_id)
        if order:
            order.status = "canceled"
        logger.warning("hotcoin cancel_order endpoint is not mapped; local order marked canceled order_id=%s symbol=%s", order_id, symbol)

    async def close_position(self, position: Position) -> OrderResult | None:
        if position.contracts <= 0:
            return None
        side = Side.SELL if position.side == PositionSide.LONG else Side.BUY
        book = await self.fetch_order_book(position.symbol)
        price = book["bids"][0][0] if side == Side.SELL else book["asks"][0][0]
        return await self.create_order(
            position.symbol,
            "limit",
            side,
            position.contracts,
            price,
            {"ordType": "ioc", "posSide": position.side.value, "reduceOnly": True},
        )

    async def close_all_positions(self) -> list[OrderResult]:
        results: list[OrderResult] = []
        for position in await self.fetch_positions():
            order = await self.close_position(position)
            if order:
                results.append(order)
        return results

    async def fetch_balance_equity(self) -> float:
        payload = await asyncio.to_thread(self.client.get_balance)
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return 0.0
        return self._float_value(data.get("totalAccountRights"), data.get("totalMarginBalance"), data.get("equity"), 0.0)

    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        rows = await asyncio.to_thread(self.client.get_positions, symbol)
        return [self._parse_position(row) for row in rows if isinstance(row, dict) and self._position_amount(row) > 0]

    async def fetch_funding_rate(self, symbol: str) -> float:
        return await self.public_data.fetch_funding_rate(symbol)

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        logger.info("hotcoin leverage setup skipped symbol=%s leverage=%s; leverage is managed on Hotcoin account", symbol, leverage)

    async def close(self) -> None:
        await self.public_data.close()

    def _parse_position(self, row: dict[str, Any]) -> Position:
        raw_side = str(row.get("side") or row.get("positionSide") or row.get("direction") or "").lower()
        side = PositionSide.SHORT if "short" in raw_side or raw_side in {"sell", "open_short"} else PositionSide.LONG
        return Position(
            symbol=ccxt_symbol(str(row.get("contractCode") or row.get("symbol") or "")),
            side=side,
            contracts=self._position_amount(row) / 100.0,
            entry_price=self._float_value(row.get("entryPrice"), row.get("avgPrice"), row.get("openPrice"), 0.0),
            unrealized_pnl=self._float_value(row.get("unrealizedPnl"), row.get("unRealizedSurplus"), row.get("profit"), 0.0),
            metadata=row,
        )

    @staticmethod
    def _position_amount(row: dict[str, Any]) -> float:
        return HotcoinExchange._float_value(
            row.get("availablePosition"),
            row.get("amount"),
            row.get("volume"),
            row.get("position"),
            row.get("contAmount"),
            0.0,
        )

    @staticmethod
    def _extract_order_id(payload: dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            return str(first.get("orderId") or first.get("id") or first.get("ordId") or int(time.time() * 1000))
        if isinstance(data, dict):
            return str(data.get("orderId") or data.get("id") or data.get("ordId") or int(time.time() * 1000))
        return str(int(time.time() * 1000))

    @staticmethod
    def _float_value(*values: Any) -> float:
        for value in values:
            try:
                if value is not None and value != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0


def hotcoin_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "").upper()


def ccxt_symbol(contract_code: str) -> str:
    raw = contract_code.replace("-", "").replace("_", "").upper()
    if raw.endswith("USDT") and "/" not in raw:
        base = raw[: -len("USDT")]
        return f"{base}/USDT:USDT"
    return contract_code
