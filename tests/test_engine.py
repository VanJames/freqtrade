from __future__ import annotations

import pytest

from trading_system.config import Settings
from trading_system.engine import OKXQuantEngine
from trading_system.exchange import ExchangeClient, okx_setting_blocked
from trading_system.models import OrderResult, Position, Side


@pytest.mark.asyncio
async def test_engine_dry_run_once_initializes_and_ticks() -> None:
    settings = Settings(dry_run=True, symbols=["BTC/USDT:USDT"])
    engine = OKXQuantEngine(settings)
    engine.execution.twap_timeout = 0

    await engine.initialize(init_store=False)
    await engine.run_once()
    await engine.shutdown()

    assert len(engine.klines["BTC/USDT:USDT"]["5m"]) > 0
    assert len(engine.klines["BTC/USDT:USDT"]["1h"]) > 0


def test_okx_setting_blocked_detects_error_59000() -> None:
    exc = Exception('okx {"code":"59000","msg":"Setting failed. Cancel any open orders, close positions, and stop trading bots first."}')

    assert okx_setting_blocked(exc)


class FailingInitializeExchange(ExchangeClient):
    def __init__(self) -> None:
        self.closed = False

    async def initialize(self) -> None:
        raise RuntimeError("boom")

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return []

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        return []

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        return {"bids": [], "asks": []}

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, object],
    ) -> OrderResult:
        raise RuntimeError("not used")

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        raise RuntimeError("not used")

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        return None

    async def close_position(self, position: Position) -> OrderResult | None:
        return None

    async def close_all_positions(self) -> list[OrderResult]:
        return []

    async def fetch_balance_equity(self) -> float:
        return 0.0

    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        return []

    async def fetch_funding_rate(self, symbol: str) -> float:
        return 0.0

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_engine_closes_exchange_after_initialize_failure() -> None:
    exchange = FailingInitializeExchange()
    engine = OKXQuantEngine(Settings(dry_run=True, symbols=["BTC/USDT:USDT"]), exchange=exchange)

    with pytest.raises(RuntimeError, match="boom"):
        await engine.initialize(init_store=False)

    assert exchange.closed is True
