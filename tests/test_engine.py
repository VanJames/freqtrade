from __future__ import annotations

import logging

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
