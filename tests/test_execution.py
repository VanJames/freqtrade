from __future__ import annotations

import asyncio

import pytest

from trading_system.execution import ExecutionEngine
from trading_system.exchange import ExchangeClient
from trading_system.models import OrderResult, Position, PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.risk import RiskDecision


class RecordingExchange(ExchangeClient):
    def __init__(self) -> None:
        self.params: list[dict[str, object]] = []

    async def initialize(self) -> None:
        return None

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return []

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        return []

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        return {"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, object],
    ) -> OrderResult:
        self.params.append(params)
        return OrderResult("order-1", symbol, side, PositionSide(params["posSide"]), amount, price, "open", remaining=amount)

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        return OrderResult(order_id, symbol, Side.BUY, PositionSide.LONG, 1.0, 100.0, "closed", filled=1.0)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        return None

    async def close_position(self, position: Position) -> OrderResult | None:
        return None

    async def close_all_positions(self) -> list[OrderResult]:
        return []

    async def fetch_balance_equity(self) -> float:
        return 1000.0

    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
        return []

    async def fetch_funding_rate(self, symbol: str) -> float:
        return 0.0

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        return None

    async def close(self) -> None:
        return None


def signal(signal_type: SignalType = SignalType.ENTER_TREND) -> TradeSignal:
    return TradeSignal(
        symbol="SOL/USDT:USDT",
        signal_type=signal_type,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        reason="unit_test_signal",
    )


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def notify_order_signal(self, trade_signal: TradeSignal, *, order_price: float, amount: float) -> None:
        self.calls.append({"signal": trade_signal, "order_price": order_price, "amount": amount})


@pytest.mark.asyncio
async def test_entry_uses_okx_post_only_ord_type() -> None:
    exchange = RecordingExchange()
    engine = ExecutionEngine(exchange)
    order = await engine.execute(signal(), RiskDecision(True, size=1.0))

    assert order.status == "open"
    assert exchange.params[0]["ordType"] == "post_only"
    for task in engine.background_tasks:
        task.cancel()
    await asyncio.gather(*engine.background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_entry_sends_email_notification_before_order() -> None:
    exchange = RecordingExchange()
    notifier = RecordingNotifier()
    engine = ExecutionEngine(exchange, email_notifier=notifier)  # type: ignore[arg-type]
    order = await engine.execute(signal(), RiskDecision(True, size=1.25))

    assert order.status == "open"
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["order_price"] == pytest.approx(99.0)
    assert notifier.calls[0]["amount"] == pytest.approx(1.25)
    for task in engine.background_tasks:
        task.cancel()
    await asyncio.gather(*engine.background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_exit_uses_ioc_ord_type() -> None:
    exchange = RecordingExchange()
    notifier = RecordingNotifier()
    engine = ExecutionEngine(exchange, email_notifier=notifier)  # type: ignore[arg-type]
    exit_signal = signal(SignalType.EXIT)
    exit_signal.metadata = {"contracts": 1.0, "reduce_only": True}
    order = await engine.execute(exit_signal, RiskDecision(True, size=1.0))

    assert order.status == "open"
    assert exchange.params[0]["ordType"] == "ioc"
    assert exchange.params[0]["reduceOnly"] is True
    assert notifier.calls == []
