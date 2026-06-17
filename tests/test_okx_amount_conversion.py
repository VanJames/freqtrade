from __future__ import annotations

import pytest

from trading_system.config import Settings
from trading_system.exchange import CcxtOkxExchange
from trading_system.hotcoin import (
    HotcoinExchange,
    ensure_hotcoin_order_success,
    extract_hotcoin_equity,
    extract_hotcoin_order_metrics,
    extract_hotcoin_position_rows,
)
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


class FakeHotcoinClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def place_order(self, **kwargs) -> dict[str, object]:
        return self.payload


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


def test_hotcoin_units_convert_through_contract_size() -> None:
    exchange = HotcoinExchange(Settings(dry_run=True))
    exchange.public_data.exchange = FakeOkxApi()

    assert exchange._base_to_hotcoin_units("ETH/USDT:USDT", 0.04) == pytest.approx(400.0)
    assert exchange._hotcoin_units_to_base("ETH/USDT:USDT", 400.0) == pytest.approx(0.04)


def test_hotcoin_positions_convert_exchange_units_to_base_amount() -> None:
    exchange = HotcoinExchange(Settings(dry_run=True))
    exchange.public_data.exchange = FakeOkxApi()

    position = exchange._parse_position(
        {
            "contractCode": "ETHUSDT",
            "side": "long",
            "availablePosition": "400",
            "entryPrice": "2130",
        }
    )

    assert position.symbol == "ETH/USDT:USDT"
    assert position.contracts == pytest.approx(0.04)


def test_hotcoin_equity_reads_nested_usdt_asset_shape() -> None:
    payload = {
        "code": 200,
        "data": {
            "assets": [
                {"coin": "BTC", "equity": "0.01"},
                {"coin": "USDT", "accountRights": "123.45", "availableBalance": "100.00"},
            ]
        },
    }

    assert extract_hotcoin_equity(payload) == pytest.approx(123.45)


def test_hotcoin_equity_reads_direct_account_shape() -> None:
    payload = {"code": 200, "data": {"totalMarginBalance": "88.8"}}

    assert extract_hotcoin_equity(payload) == pytest.approx(88.8)


def test_hotcoin_position_rows_read_nested_list_shape() -> None:
    payload = {
        "code": 200,
        "data": {
            "records": [
                {
                    "symbol": "ETHUSDT",
                    "positionSide": "2",
                    "holdAmount": "400",
                    "avgOpenPrice": "2130",
                    "floatingProfit": "-12.34",
                }
            ]
        },
    }

    rows = extract_hotcoin_position_rows(payload)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETHUSDT"


def test_hotcoin_position_parser_reads_side_entry_and_unrealized_pnl_aliases() -> None:
    exchange = HotcoinExchange(Settings(dry_run=True))
    exchange.public_data.exchange = FakeOkxApi()

    position = exchange._parse_position(
        {
            "symbol": "ETHUSDT",
            "positionSide": "做空",
            "holdAmount": "400",
            "avgOpenPrice": "2130",
            "floatingProfit": "-12.34",
        }
    )

    assert position.symbol == "ETH/USDT:USDT"
    assert position.side == PositionSide.SHORT
    assert position.contracts == pytest.approx(0.04)
    assert position.entry_price == pytest.approx(2130.0)
    assert position.unrealized_pnl == pytest.approx(-12.34)


def test_hotcoin_order_metrics_read_realized_pnl_aliases() -> None:
    payload = {
        "code": 200,
        "data": [
            {
                "orderId": "abc",
                "dealAmount": "400",
                "dealAvgPrice": "2140",
                "tradeFee": "0.12",
                "closeProfit": "9.88",
            }
        ],
    }

    metrics = extract_hotcoin_order_metrics(payload)

    assert metrics["filled"] == pytest.approx(400.0)
    assert metrics["average"] == pytest.approx(2140.0)
    assert metrics["fee"] == pytest.approx(0.12)
    assert metrics["realized_pnl"] == pytest.approx(9.88)


@pytest.mark.asyncio
async def test_hotcoin_ioc_without_fill_metrics_does_not_assume_filled() -> None:
    exchange = HotcoinExchange(Settings(dry_run=True))
    exchange.public_data.exchange = FakeOkxApi()
    exchange.client = FakeHotcoinClient({"code": 200, "data": [{"orderId": "abc"}]})  # type: ignore[assignment]

    order = await exchange.create_order(
        "ETH/USDT:USDT",
        "limit",
        Side.BUY,
        0.04,
        2130.0,
        {"ordType": "ioc", "posSide": "long"},
    )

    assert order.id == "abc"
    assert order.status == "canceled"
    assert order.filled == 0.0
    assert order.remaining == pytest.approx(0.04)


def test_hotcoin_order_rejected_response_raises() -> None:
    with pytest.raises(RuntimeError, match="Hotcoin order rejected"):
        ensure_hotcoin_order_success({"code": 500, "msg": "余额不足"})


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
