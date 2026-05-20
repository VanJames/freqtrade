from __future__ import annotations

import pytest

from trading_system.backtest import BacktestConfig, cap_stop, signal_reward
from trading_system.models import MarketFeatures, PositionSide, Regime
from trading_system.strategy import StrategyEngine
from trading_system.volatility import build_volatility_policy


def test_short_stop_expands_to_minimum_distance() -> None:
    engine = StrategyEngine(min_stop_loss_pct=0.002, max_stop_loss_pct=0.012)

    stop = engine._cap_stop(100.0, 100.01, PositionSide.SHORT)

    assert stop == pytest.approx(100.2)


def test_long_stop_expands_to_minimum_distance() -> None:
    engine = StrategyEngine(min_stop_loss_pct=0.002, max_stop_loss_pct=0.012)

    stop = engine._cap_stop(100.0, 99.99, PositionSide.LONG)

    assert stop == pytest.approx(99.8)


def test_high_vol_stop_uses_wider_minimum_distance() -> None:
    engine = StrategyEngine(min_stop_loss_pct=0.002, high_vol_min_stop_loss_pct=0.004, max_stop_loss_pct=0.012)
    policy = build_volatility_policy(
        price=100.0,
        atr_1h=0.5,
        base_min_stop_loss_pct=0.002,
        high_vol_min_stop_loss_pct=0.004,
        extreme_vol_min_stop_loss_pct=0.005,
    )

    stop = engine._cap_stop(100.0, 99.99, PositionSide.LONG, policy)

    assert stop == pytest.approx(99.6)


def test_backtest_reward_never_below_min_take_profit_pct() -> None:
    config = BacktestConfig(symbols=["ETH/USDT:USDT"], min_take_profit_pct=0.004)
    stop = cap_stop(2300.0, 2300.1, PositionSide.SHORT, config)

    reward = signal_reward(2300.0, stop, 1.8, config)

    assert stop == pytest.approx(2304.6)
    assert reward == pytest.approx(9.2)


def test_entry_diagnostics_reports_missing_conditions_for_trend_long() -> None:
    engine = StrategyEngine()
    candles = [[i * 300000, 100.0, 100.2, 99.8, 100.0, 1.0] for i in range(60)]
    features = MarketFeatures(
        atr_1h=1.0,
        close_1h=100.0,
        ema20_1h=101.0,
        ema60_1h=100.0,
        range_high_4h=105.0,
        range_low_4h=95.0,
        previous_1h_low=99.0,
        previous_1h_high=101.0,
        current_4h_low=98.0,
        current_4h_high=104.0,
        last_4h_close=104.0,
        prev_4h_close=103.0,
    )

    diagnostics = engine.entry_diagnostics("BTC/USDT:USDT", Regime.TREND_LONG, features, candles, [])

    assert diagnostics["action"] == "trend_long"
    assert diagnostics["summary"] == "waiting_for_conditions"
    assert any(item["code"] == "macd_cross_up" for item in diagnostics["blockers"])
