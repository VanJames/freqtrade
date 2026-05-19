from __future__ import annotations

import asyncio
from logging import getLogger
from collections.abc import Awaitable, Callable

from trading_system.exchange import ExchangeClient
from trading_system.models import OrderResult, Side, SignalType, TradeSignal
from trading_system.risk import RiskDecision

logger = getLogger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        exchange: ExchangeClient,
        twap_timeout: int = 15,
        twap_callback: Callable[[str, float, float], Awaitable[None]] | None = None,
    ) -> None:
        self.exchange = exchange
        self.twap_timeout = twap_timeout
        self.twap_callback = twap_callback
        self.background_tasks: set[asyncio.Task[None]] = set()

    async def execute(self, signal: TradeSignal, decision: RiskDecision) -> OrderResult:
        book = await self.exchange.fetch_order_book(signal.symbol)
        reduce_only = bool(signal.metadata.get("reduce_only")) or signal.signal_type in {
            SignalType.EXIT,
            SignalType.RELEASE_HEDGE,
        }
        if signal.side == Side.BUY:
            price = float(book["asks"][0][0] if reduce_only else book["bids"][0][0])
        else:
            price = float(book["bids"][0][0] if reduce_only else book["asks"][0][0])

        order = await self.exchange.create_order(
            signal.symbol,
            "limit",
            signal.side,
            decision.size,
            price,
            {
                "ordType": "ioc" if reduce_only else "post_only",
                "posSide": signal.position_side.value,
                "reduceOnly": reduce_only,
            },
        )
        if reduce_only:
            return order
        task = asyncio.create_task(self.track_and_twap(order, signal))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return order

    async def execute_limit(
        self,
        signal: TradeSignal,
        amount: float,
        price: float,
        post_only: bool = True,
    ) -> OrderResult:
        order = await self.exchange.create_order(
            signal.symbol,
            "limit",
            signal.side,
            amount,
            price,
            {"ordType": "post_only" if post_only else "ioc", "posSide": signal.position_side.value},
        )
        if post_only:
            task = asyncio.create_task(self.track_and_twap(order, signal))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        return order

    async def track_and_twap(self, order: OrderResult, signal: TradeSignal) -> None:
        await asyncio.sleep(self.twap_timeout)
        current = await self.exchange.fetch_order(order.id, order.symbol)
        if current.status == "closed" or current.remaining <= 0:
            return

        await self.exchange.cancel_order(order.id, order.symbol)
        remaining = current.remaining
        logger.info("twap started order_id=%s remaining=%s", order.id, remaining)
        for _ in range(5):
            book = await self.exchange.fetch_order_book(order.symbol)
            if signal.side == Side.BUY:
                price = float(book["asks"][0][0])
            else:
                price = float(book["bids"][0][0])
            fill = await self.exchange.create_order(
                order.symbol,
                "limit",
                signal.side,
                remaining / 5,
                price,
                {"ordType": "ioc", "posSide": signal.position_side.value},
            )
            if self.twap_callback:
                await self.twap_callback(order.id, fill.filled, fill.fee)
            await asyncio.sleep(3)

    async def drain(self) -> None:
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
