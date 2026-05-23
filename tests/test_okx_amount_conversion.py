from __future__ import annotations

import pytest

from trading_system.exchange import CcxtOkxExchange
from trading_system.models import PositionSide, Side


class FakeOkxApi:
    def __init__(self) -> None:
        self.orders: list[dict[str, object]] = []
        self.fetch_positions_calls = 0

    def market(self, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "contract": True,
            "contractSize": 0.01,
        }

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float,
        params: dict[str, object],
    ) -> dict[str, object]:
        self.orders.append(
            {
                "symbol": symbol,
                "order_type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": params,
            }
        )
        return {
            "id": "order-1",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "filled": amount,
            "remaining": 0,
            "price": price,
            "average": price,
            "status": "closed",
            "fee": {"cost": 0.1},
            "info": {"posSide": params["posSide"], "sz": str(amount), "accFillSz": str(amount)},
        }

    async def fetch_positions(self) -> list[dict[str, object]]:
        self.fetch_positions_calls += 1
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 1.0 + self.fetch_positions_calls,
                "entryPrice": 78000.0,
                "unrealizedPnl": 0.0,
                "info": {"posSide": "long"},
            }
        ]


@pytest.mark.asyncio
async def test_okx_orders_convert_base_amount_to_contracts() -> None:
    exchange = CcxtOkxExchange({})
    api = FakeOkxApi()
    exchange.exchange = api

    order = await exchange.create_order(
        "BTC/USDT:USDT",
        "limit",
        Side.BUY,
        0.064,
        78000.0,
        {"posSide": PositionSide.LONG.value},
    )

    assert api.orders[0]["amount"] == pytest.approx(6.4)
    assert order.amount == pytest.approx(0.064)
    assert order.filled == pytest.approx(0.064)


def test_okx_positions_convert_contracts_to_base_amount() -> None:
    exchange = CcxtOkxExchange({})
    exchange.exchange = FakeOkxApi()

    positions = exchange._parse_positions(
        [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 6.4,
                "entryPrice": 78000.0,
                "unrealizedPnl": 0.0,
                "info": {"posSide": "long"},
            }
        ]
    )

    assert positions[0].contracts == pytest.approx(0.064)


@pytest.mark.asyncio
async def test_okx_positions_cache_can_be_bypassed_for_live_monitoring() -> None:
    exchange = CcxtOkxExchange({}, positions_cache_ttl_seconds=30.0)
    api = FakeOkxApi()
    exchange.exchange = api

    first = await exchange.fetch_positions("BTC/USDT:USDT")
    cached = await exchange.fetch_positions("BTC/USDT:USDT")
    refreshed = await exchange.fetch_positions("BTC/USDT:USDT", refresh=True)

    assert api.fetch_positions_calls == 2
    assert cached[0].contracts == first[0].contracts
    assert refreshed[0].contracts > cached[0].contracts
