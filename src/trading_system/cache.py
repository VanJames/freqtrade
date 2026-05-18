from __future__ import annotations

from datetime import datetime
from logging import getLogger
from typing import Any

import orjson
from redis.asyncio import Redis

from trading_system.models import HedgeLock, OrderResult, Position

logger = getLogger(__name__)


class StateCache:
    def __init__(self, redis_url: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.redis: Redis | None = Redis.from_url(redis_url, decode_responses=True) if enabled else None

    async def ping(self) -> bool:
        if not self.redis:
            return False
        try:
            await self.redis.ping()
            return True
        except Exception:
            logger.warning("redis unavailable; continuing without high-frequency cache")
            self.redis = None
            return False

    async def set_positions(self, positions: list[Position]) -> None:
        await self._set(
            "positions",
            [
                {
                    "symbol": pos.symbol,
                    "side": pos.side.value,
                    "contracts": pos.contracts,
                    "entry_price": pos.entry_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                }
                for pos in positions
            ],
        )

    async def set_open_orders(self, orders: list[OrderResult]) -> None:
        await self._set(
            "open_orders",
            [
                {
                    "id": order.id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "amount": order.amount,
                    "price": order.price,
                    "status": order.status,
                    "remaining": order.remaining,
                }
                for order in orders
                if order.status == "open"
            ],
        )

    async def set_hedge_locks(self, locks: dict[str, HedgeLock]) -> None:
        await self._set(
            "hedge_locks",
            {
                symbol: {
                    "symbol": lock.symbol,
                    "grid_side": lock.grid_side.value,
                    "hedge_side": lock.hedge_side.value,
                    "contracts": lock.contracts,
                    "created_at": lock.created_at.isoformat(),
                    "active": lock.active,
                }
                for symbol, lock in locks.items()
            },
        )

    async def _set(self, key: str, value: Any) -> None:
        if not self.redis:
            return
        await self.redis.set(f"okx_quant:{key}", orjson.dumps(value).decode("utf-8"))

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)

