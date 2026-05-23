from __future__ import annotations

import logging

import pytest

from trading_system.config import Settings
from trading_system.engine import OKXQuantEngine
from trading_system.exchange import ExchangeClient, okx_setting_blocked
from trading_system.models import OrderResult, Position, PositionSide, Regime, Side, TrailingState
from trading_system.position_manager import PositionManager


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


@pytest.mark.asyncio
async def test_engine_run_once_records_entry_diagnostics_for_all_symbols() -> None:
    settings = Settings(dry_run=True, symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"])
    engine = OKXQuantEngine(settings)
    engine.execution.twap_timeout = 0

    await engine.initialize(init_store=False)
    await engine.run_once()
    await engine.shutdown()

    assert "entry_diagnostics" in engine.symbol_status["BTC/USDT:USDT"]
    assert "entry_diagnostics" in engine.symbol_status["ETH/USDT:USDT"]


@pytest.mark.asyncio
async def test_engine_records_timeout_when_live_ohlcv_hangs() -> None:
    settings = Settings(
        dry_run=True,
        symbols=["ETH/USDT:USDT"],
        live_ohlcv_timeout_seconds=0.01,
    )
    engine = OKXQuantEngine(settings)

    await engine.initialize(init_store=False)
    with pytest.raises(TimeoutError):
        await engine._build_symbol_signals("ETH/USDT:USDT")
    await engine.shutdown()

    diagnostics = engine.symbol_status["ETH/USDT:USDT"]["entry_diagnostics"]
    assert diagnostics["summary"] == "symbol_loop_error"
    assert diagnostics["blockers"][0]["code"] == "live_5m_ohlcv_timeout"


def test_okx_setting_blocked_detects_error_59000() -> None:
    exc = Exception('okx {"code":"59000","msg":"Setting failed. Cancel any open orders, close positions, and stop trading bots first."}')

    assert okx_setting_blocked(exc)


def test_entry_diagnostic_log_throttles_dynamic_metric_changes(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        dry_run=True,
        symbols=["BTC/USDT:USDT"],
        diagnostics_log_interval_seconds=60,
    )
    engine = OKXQuantEngine(settings)

    with caplog.at_level(logging.INFO, logger="trading_system.engine"):
        engine._log_diagnostic_status(
            "BTC/USDT:USDT",
            "entry:shock_trend_up",
            "waiting_for_conditions",
            "SHOCK_TREND_UP",
            100.0,
            [{"code": "opportunity_score", "passed": False, "value": 61}],
            {"score": 61},
        )
        engine._log_diagnostic_status(
            "BTC/USDT:USDT",
            "entry:shock_trend_up",
            "waiting_for_conditions",
            "SHOCK_TREND_UP",
            100.1,
            [{"code": "opportunity_score", "passed": False, "value": 62}],
            {"score": 62},
        )

    messages = [record.message for record in caplog.records if record.message.startswith("entry diagnostic")]
    assert len(messages) == 1


def test_display_entry_diagnostics_prefers_completed_result_over_transient_status() -> None:
    engine = OKXQuantEngine(Settings(dry_run=True, symbols=["BTC/USDT:USDT"]))
    completed = {"summary": "waiting_for_conditions", "action": "shock_trend_up"}
    transient = {"summary": "waiting_market_data", "action": "refresh_higher_timeframes"}

    assert engine._display_entry_diagnostics(
        {
            "entry_diagnostics": transient,
            "last_completed_entry_diagnostics": completed,
        }
    ) is completed


def test_position_manager_recovers_existing_long_position_with_protective_stop() -> None:
    manager = PositionManager(
        min_stop_loss_pct=0.002,
        max_stop_loss_pct=0.012,
        trailing_gap_pct=0.0025,
        min_trailing_activate_r=1.0,
        recovered_risk_multiplier=0.7,
    )
    position = Position(
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        contracts=0.01,
        entry_price=100.0,
    )

    recovered = manager.recover_missing_states(
        "BTC/USDT:USDT",
        [position],
        price=101.0,
        atr_value=1.0,
        regime=Regime.SHOCK_TREND_UP,
    )

    state = manager.trailing[("BTC/USDT:USDT", PositionSide.LONG)]
    assert len(recovered) == 1
    assert state.stop_loss == 98.8
    assert state.trailing_gap_pct == 0.0025
    assert state.risk_multiplier == 0.7


def test_position_manager_recovers_existing_position_with_signal_hint() -> None:
    manager = PositionManager()
    position = Position(
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        contracts=0.1,
        entry_price=77704.47,
    )

    manager.recover_missing_states(
        "BTC/USDT:USDT",
        [position],
        price=77800.0,
        atr_value=385.0,
        regime=Regime.SHOCK_TREND_UP,
        signal_hints={
            ("BTC/USDT:USDT", PositionSide.LONG): {
                "stop_loss": 77531.0272,
                "take_profit": 78028.22016,
                "trailing_gap_pct": 0.0025,
                "min_trailing_activate_r": 1.0,
                "risk_multiplier": 1.5,
            }
        },
    )

    state = manager.trailing[("BTC/USDT:USDT", PositionSide.LONG)]
    assert state.stop_loss == pytest.approx(77531.0272)
    assert state.take_profit == pytest.approx(78028.22016)
    assert state.risk_multiplier == pytest.approx(1.5)


def test_position_manager_removes_recovered_state_after_position_disappears() -> None:
    manager = PositionManager()
    position = Position(
        symbol="ETH/USDT:USDT",
        side=PositionSide.SHORT,
        contracts=1.0,
        entry_price=2000.0,
    )
    manager.recover_missing_states(
        "ETH/USDT:USDT",
        [position],
        price=1990.0,
        atr_value=20.0,
        regime=Regime.SHOCK_TREND_DOWN,
    )

    manager.recover_missing_states(
        "ETH/USDT:USDT",
        [],
        price=1990.0,
        atr_value=20.0,
        regime=Regime.UNKNOWN,
    )

    assert ("ETH/USDT:USDT", PositionSide.SHORT) not in manager.trailing
    assert not manager.recovered_positions


class PositionMonitorExchange(ExchangeClient):
    def __init__(self) -> None:
        self.orders: list[OrderResult] = []
        self.position = Position(
            symbol="BTC/USDT:USDT",
            side=PositionSide.LONG,
            contracts=0.01,
            entry_price=100.0,
        )

    async def initialize(self) -> None:
        return None

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return []

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        return []

    async def fetch_order_book(self, symbol: str) -> dict[str, list[list[float]]]:
        return {"bids": [[103.0, 1.0]], "asks": [[103.2, 1.0]]}

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: Side,
        amount: float,
        price: float,
        params: dict[str, object],
    ) -> OrderResult:
        order = OrderResult(
            "exit-1",
            symbol,
            side,
            PositionSide(str(params["posSide"])),
            amount,
            price,
            "closed",
            filled=amount,
            average=price,
        )
        self.orders.append(order)
        return order

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        return self.orders[-1]

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        return None

    async def close_position(self, position: Position) -> OrderResult | None:
        return None

    async def close_all_positions(self) -> list[OrderResult]:
        return []

    async def fetch_balance_equity(self) -> float:
        return 1000.0

    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
        return [self.position] if symbol in {None, "BTC/USDT:USDT"} else []

    async def fetch_funding_rate(self, symbol: str) -> float:
        return 0.0

    async def set_leverage(self, symbol: str, leverage: float) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_position_monitor_uses_order_book_price_for_trailing_exit() -> None:
    exchange = PositionMonitorExchange()
    settings = Settings(dry_run=True, symbols=["BTC/USDT:USDT"])
    engine = OKXQuantEngine(settings, exchange=exchange)
    engine.klines["BTC/USDT:USDT"]["1h"] = [
        [1, 100.0, 100.5, 99.5, 100.0, 1.0],
        [2, 100.0, 100.5, 99.5, 100.0, 1.0],
    ]
    engine.position_manager.trailing[("BTC/USDT:USDT", PositionSide.LONG)] = TrailingState(
        symbol="BTC/USDT:USDT",
        position_side=PositionSide.LONG,
        entry_price=100.0,
        atr=1.0,
        stop_loss=104.0,
        highest_price=105.0,
        lowest_price=100.0,
        active=True,
    )

    await engine._monitor_positions_once()

    assert len(exchange.orders) == 1
    assert exchange.orders[0].side == Side.SELL
    assert exchange.orders[0].position_side == PositionSide.LONG


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

    async def fetch_positions(self, symbol: str | None = None, *, refresh: bool = False) -> list[Position]:
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
