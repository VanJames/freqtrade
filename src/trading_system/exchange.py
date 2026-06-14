from __future__ import annotations

import asyncio
import itertools
import time
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any

from trading_system.models import OrderResult, Position, PositionSide, Side

logger = getLogger(__name__)


class ExchangeClient(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        raise NotImplementedError

    @abstractmethod
    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        raise NotImplementedError

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, Any],
    ) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close_position(self, position: Position) -> OrderResult | None:
        raise NotImplementedError

    @abstractmethod
    async def close_all_positions(self) -> list[OrderResult]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_balance_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: float) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class DryRunExchange(ExchangeClient):
    def __init__(self, symbols: list[str], initial_equity: float = 10_000.0) -> None:
        self.symbols = symbols
        self.equity = initial_equity
        self.orders: dict[str, OrderResult] = {}
        self.positions: list[Position] = []
        self._ids = itertools.count(1)
        self._last_price: dict[str, float] = {symbol: 100.0 + idx * 20 for idx, symbol in enumerate(symbols)}

    async def initialize(self) -> None:
        logger.info("dry-run exchange initialized")

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        base = self._last_price.get(symbol, 100.0)
        step_ms = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}.get(timeframe, 60_000)
        now = 1_700_000_000_000
        rows: list[list[float]] = []
        for idx in range(limit):
            drift = (idx - limit / 2) * 0.03
            close = base + drift
            high = close * 1.002
            low = close * 0.998
            rows.append([now - (limit - idx) * step_ms, close * 0.999, high, low, close, 1000 + idx])
        return rows

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        await asyncio.sleep(0.05)
        last = self._last_price.get(symbol, 100.0) * 1.0002
        self._last_price[symbol] = last
        return [[1_700_000_000_000, last * 0.999, last * 1.002, last * 0.998, last, 1000.0]]

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        price = self._last_price.get(symbol, 100.0)
        return {"bids": [[price * 0.9999, 1000.0]], "asks": [[price * 1.0001, 1000.0]]}

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, Any],
    ) -> OrderResult:
        order_id = f"dry-{next(self._ids)}"
        position_side = PositionSide(params.get("posSide", "long"))
        filled = 0.0 if params.get("ordType") == "post_only" else amount
        status = "open" if filled == 0 else "closed"
        result = OrderResult(
            order_id,
            symbol,
            side,
            position_side,
            amount,
            price,
            status,
            filled,
            amount - filled,
            average=price,
        )
        self.orders[order_id] = result
        logger.info("dry-run order %s", result)
        return result

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        return self.orders[order_id]

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        order = self.orders[order_id]
        order.status = "canceled"
        logger.info("dry-run cancel order_id=%s symbol=%s", order_id, symbol)

    async def close_position(self, position: Position) -> OrderResult | None:
        if position.contracts <= 0:
            return None
        side = Side.SELL if position.side == PositionSide.LONG else Side.BUY
        book = await self.fetch_order_book(position.symbol)
        price = book["bids"][0][0] if side == Side.SELL else book["asks"][0][0]
        order = await self.create_order(
            position.symbol,
            "limit",
            side,
            position.contracts,
            price,
            {"ordType": "ioc", "posSide": position.side.value, "reduceOnly": True},
        )
        self.positions = [pos for pos in self.positions if pos is not position]
        return order

    async def close_all_positions(self) -> list[OrderResult]:
        results: list[OrderResult] = []
        for position in list(self.positions):
            order = await self.close_position(position)
            if order:
                results.append(order)
        return results

    async def fetch_balance_equity(self) -> float:
        return self.equity

    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
        if symbol is None:
            return list(self.positions)
        return [pos for pos in self.positions if pos.symbol == symbol]

    async def fetch_funding_rate(self, symbol: str) -> float:
        return 0.0

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        logger.info("dry-run set leverage symbol=%s leverage=%s", symbol, leverage)

    async def close(self) -> None:
        return None


class CcxtOkxExchange(ExchangeClient):
    def __init__(self, config: dict[str, Any], *, positions_cache_ttl_seconds: float = 2.0) -> None:
        self.config = config
        self.exchange: Any | None = None
        self._positions_cache: tuple[float, list[Position]] | None = None
        self._positions_lock = asyncio.Lock()
        self._positions_cache_ttl_seconds = max(0.0, float(positions_cache_ttl_seconds))

    async def initialize(self) -> None:
        import ccxt.pro as ccxtpro

        self.exchange = ccxtpro.okx(self.config)
        self.exchange.has["fetchCurrencies"] = False
        await self.api.load_markets()
        try:
            await self.exchange.set_position_mode(True)
        except Exception as exc:
            if okx_setting_blocked(exc):
                logger.warning(
                    "okx position mode setup skipped: cancel open orders, close positions, "
                    "and stop OKX trading bots before changing position mode"
                )
                return
            await self.close()
            raise

    @property
    def api(self) -> Any:
        if self.exchange is None:
            raise RuntimeError("exchange not initialized")
        return self.exchange

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return await self.api.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        return await self.api.watch_ohlcv(symbol, timeframe)

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        return await self.api.fetch_order_book(symbol)

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, Any],
    ) -> OrderResult:
        exchange_amount = self._to_exchange_amount(symbol, amount)
        raw = await self.api.create_order(symbol, order_type, side.value, exchange_amount, price, params)
        self._invalidate_positions_cache()
        return self._parse_order_result(
            raw,
            fallback_symbol=symbol,
            fallback_side=side,
            fallback_position_side=PositionSide(params.get("posSide", "long")),
            fallback_amount=amount,
            fallback_price=price,
        )

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        raw = await self.api.fetch_order(order_id, symbol)
        side = Side(raw["side"])
        params = raw.get("info", {})
        return self._parse_order_result(
            raw,
            fallback_symbol=symbol,
            fallback_side=side,
            fallback_position_side=PositionSide(params.get("posSide", "long")),
            fallback_amount=0.0,
            fallback_price=0.0,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        await self.api.cancel_order(order_id, symbol)

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
        for position in await self.fetch_positions(refresh=True):
            order = await self.close_position(position)
            if order:
                results.append(order)
        return results

    async def fetch_balance_equity(self) -> float:
        balance = await self.api.fetch_balance({"type": "swap"})
        info = balance.get("info", {})
        details = (info.get("data") or [{}])[0].get("details") or []
        if details:
            return float(details[0].get("eq") or details[0].get("cashBal") or 0.0)
        return float(balance.get("USDT", {}).get("total") or 0.0)

    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
        now = time.monotonic()
        async with self._positions_lock:
            if (
                not refresh
                and self._positions_cache
                and now - self._positions_cache[0] <= self._positions_cache_ttl_seconds
            ):
                return self._filter_positions(self._positions_cache[1], symbol)
            raw_positions = await self.api.fetch_positions()
            positions = self._parse_positions(raw_positions)
            self._positions_cache = (now, positions)
            return self._filter_positions(positions, symbol)

    def _invalidate_positions_cache(self) -> None:
        self._positions_cache = None

    def _parse_order_result(
        self,
        raw: dict[str, Any],
        *,
        fallback_symbol: str,
        fallback_side: Side,
        fallback_position_side: PositionSide,
        fallback_amount: float,
        fallback_price: float,
    ) -> OrderResult:
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else {}
        average = self._float_value(
            raw.get("average"),
            raw.get("avgPrice"),
            info.get("avgPx"),
            raw.get("price"),
            fallback_price,
        )
        symbol = str(raw.get("symbol") or fallback_symbol)
        raw_amount = self._float_value(raw.get("amount"), info.get("sz"), 0.0)
        raw_filled = self._float_value(raw.get("filled"), info.get("accFillSz"), 0.0)
        raw_remaining = self._float_value(raw.get("remaining"), 0.0)
        amount = self._from_exchange_amount(symbol, raw_amount) if raw_amount else fallback_amount
        filled = self._from_exchange_amount(symbol, raw_filled)
        remaining = self._from_exchange_amount(symbol, raw_remaining)
        return OrderResult(
            id=str(raw["id"]),
            symbol=symbol,
            side=Side(raw.get("side") or fallback_side.value),
            position_side=PositionSide(info.get("posSide") or fallback_position_side.value),
            amount=amount,
            price=self._float_value(raw.get("price"), info.get("px"), fallback_price),
            status=str(raw.get("status") or "open"),
            filled=filled,
            remaining=remaining,
            fee=self._float_value(fee.get("cost"), info.get("fee"), 0.0),
            average=average,
            realized_pnl=self._optional_float(raw.get("pnl"), info.get("pnl"), info.get("realizedPnl")),
            raw=raw,
        )

    def _contract_size(self, symbol: str) -> float:
        market = self.api.market(symbol)
        if not market.get("contract"):
            return 1.0
        return float(market.get("contractSize") or 1.0)

    def _to_exchange_amount(self, symbol: str, base_amount: float) -> float:
        contract_size = self._contract_size(symbol)
        return base_amount / contract_size if contract_size > 0 else base_amount

    def _from_exchange_amount(self, symbol: str, exchange_amount: float) -> float:
        contract_size = self._contract_size(symbol)
        return exchange_amount * contract_size if contract_size > 0 else exchange_amount

    @staticmethod
    def _float_value(*values: Any) -> float:
        for value in values:
            try:
                if value is not None and value != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @classmethod
    def _optional_float(cls, *values: Any) -> float | None:
        for value in values:
            try:
                if value is not None and value != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _parse_positions(self, raw_positions: list[dict[str, Any]]) -> list[Position]:
        positions: list[Position] = []
        for raw in raw_positions:
            raw_contracts = float(raw.get("contracts") or 0.0)
            if raw_contracts <= 0:
                continue
            symbol = raw["symbol"]
            base_contracts = self._from_exchange_amount(symbol, raw_contracts)
            side = PositionSide(raw.get("side") or raw.get("info", {}).get("posSide"))
            positions.append(
                Position(
                    symbol=symbol,
                    side=side,
                    contracts=base_contracts,
                    entry_price=float(raw.get("entryPrice") or 0.0),
                    unrealized_pnl=float(raw.get("unrealizedPnl") or 0.0),
                    metadata=raw,
                )
            )
        return positions

    @staticmethod
    def _filter_positions(positions: list[Position], symbol: str | None = None) -> list[Position]:
        if symbol is None:
            return list(positions)
        return [position for position in positions if position.symbol == symbol]

    async def fetch_funding_rate(self, symbol: str) -> float:
        funding = await self.api.fetch_funding_rate(symbol)
        return float(funding.get("fundingRate") or funding.get("info", {}).get("fundingRate") or 0.0)

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        try:
            await self.api.set_leverage(int(leverage), symbol, {"mgnMode": "cross"})
        except Exception as exc:
            if okx_setting_blocked(exc):
                logger.warning(
                    "okx leverage setup skipped symbol=%s leverage=%s: cancel open orders, "
                    "close positions, and stop OKX trading bots before changing leverage",
                    symbol,
                    leverage,
                )
                return
            raise

    async def close(self) -> None:
        if self.exchange is not None:
            await self.exchange.close()
            self.exchange = None


def okx_setting_blocked(exc: Exception) -> bool:
    text = str(exc)
    return "59000" in text or "Cancel any open orders, close positions, and stop trading bots first" in text
