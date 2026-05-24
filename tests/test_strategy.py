from __future__ import annotations

import pytest
import pandas as pd

from trading_system.backtest import BacktestConfig, cap_stop, maybe_exit, signal_reward
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


def test_short_breakeven_protects_profit_before_full_trailing_activation() -> None:
    position = {
        "side": PositionSide.SHORT,
        "entry": 100.0,
        "stop": 101.0,
        "take_profit": 96.0,
        "lowest": 99.2,
        "highest": 100.0,
        "atr": 2.0,
        "trailing_gap_pct": 0.0025,
        "min_trailing_activate_r": 1.2,
        "breakeven_activate_r": 0.65,
        "breakeven_buffer_pct": 0.0003,
    }

    exit_price, reason = maybe_exit(position, pd.Series({"high": 100.0, "low": 99.2}))

    assert exit_price == pytest.approx(99.97)
    assert reason == "trailing_stop"


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


def test_shock_trend_down_diagnostics_reports_low_range_rebound_gate() -> None:
    engine = StrategyEngine()
    candles = []
    price = 104.0
    for i in range(60):
        open_price = price
        close_price = price - 0.12
        candles.append([i * 300000, open_price, open_price + 0.03, close_price - 0.03, close_price, 1.0])
        price = close_price
    features = MarketFeatures(
        atr_1h=1.0,
        close_1h=100.0,
        ema20_1h=101.0,
        ema60_1h=102.0,
        range_high_4h=110.0,
        range_low_4h=95.0,
        previous_1h_low=98.0,
        previous_1h_high=102.0,
        current_4h_low=96.0,
        current_4h_high=104.0,
        last_4h_close=99.0,
        prev_4h_close=100.0,
        close_position_72h=0.09,
        ret_24h=-0.02,
        ret_72h=-0.01,
    )

    diagnostics = engine.entry_diagnostics("BTC/USDT:USDT", Regime.SHOCK_TREND_DOWN, features, candles, [])

    assert diagnostics["action"] == "shock_trend_down"
    assert diagnostics["summary"] == "waiting_for_conditions"
    assert any(item["code"] == "low_range_rebound" for item in diagnostics["blockers"])


def test_shock_trend_down_diagnostics_reports_late_low_short_risk() -> None:
    engine = StrategyEngine()
    candles = []
    price = 110.0
    for index in range(58):
        open_price = price
        close_price = price - 0.18
        candles.append([index * 300000, open_price, open_price + 0.03, close_price - 0.03, close_price, 1.0])
        price = close_price
    candles.append([58 * 300000, price, price + 0.8, price - 0.05, price + 0.65, 1.0])
    candles.append([59 * 300000, price + 0.65, price + 0.7, price + 0.1, price + 0.2, 1.0])
    features = MarketFeatures(
        atr_1h=1.0,
        close_1h=100.0,
        ema20_1h=103.0,
        ema60_1h=104.0,
        range_high_4h=112.0,
        range_low_4h=95.0,
        previous_1h_low=98.0,
        previous_1h_high=102.0,
        current_4h_low=96.0,
        current_4h_high=104.0,
        last_4h_close=99.0,
        prev_4h_close=100.0,
        close_position_72h=0.17,
        ret_24h=-0.035,
        ret_72h=-0.02,
    )

    diagnostics = engine.entry_diagnostics("BTC/USDT:USDT", Regime.SHOCK_TREND_DOWN, features, candles, [])

    assert diagnostics["action"] == "shock_trend_down"
    assert any(
        item["code"] == "late_entry_risk" and item["value"] == "HIGH"
        for item in diagnostics["requirements"]
    )
    assert not any(item["code"] == "late_entry_risk" for item in diagnostics["blockers"])


def test_shock_trend_up_diagnostics_reports_late_high_long_risk() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(58):
        open_price = price
        close_price = price + 0.18
        candles.append([index * 300000, open_price, close_price + 0.03, open_price - 0.03, close_price, 1.0])
        price = close_price
    candles.append([58 * 300000, price, price + 0.05, price - 0.65, price - 0.5, 1.0])
    candles.append([59 * 300000, price - 0.5, price + 0.1, price - 0.55, price + 0.05, 1.0])
    features = MarketFeatures(
        atr_1h=1.0,
        close_1h=110.0,
        ema20_1h=103.0,
        ema60_1h=102.0,
        range_high_4h=112.0,
        range_low_4h=95.0,
        previous_1h_low=100.0,
        previous_1h_high=108.0,
        current_4h_low=104.0,
        current_4h_high=112.0,
        last_4h_close=110.0,
        prev_4h_close=108.0,
        close_position_72h=0.84,
        ret_24h=0.026,
        ret_72h=0.03,
    )

    diagnostics = engine.entry_diagnostics("BTC/USDT:USDT", Regime.SHOCK_TREND_UP, features, candles, [])

    assert diagnostics["action"] == "shock_trend_up"
    assert any(
        item["code"] == "late_entry_risk" and item["value"] == "HIGH"
        for item in diagnostics["requirements"]
    )
    assert not any(item["code"] == "late_entry_risk" for item in diagnostics["blockers"])


def test_liquidity_sweep_reversal_detects_downside_reclaim() -> None:
    engine = StrategyEngine(enable_liquidity_sweep_reversal=True, max_stop_loss_pct=0.015)
    candles = []
    price = 110.0
    for index in range(89):
        open_price = price
        close_price = price - 0.12
        candles.append([index * 300000, open_price, open_price + 0.05, close_price - 0.05, close_price, 100.0])
        price = close_price
    candles.append([89 * 300000, 100.0, 100.35, 99.1, 100.2, 260.0])
    candles.append([90 * 300000, 100.18, 100.45, 99.85, 100.36, 120.0])
    features = MarketFeatures(
        atr_1h=0.4,
        close_1h=100.2,
        ema20_1h=99.0,
        ema60_1h=101.0,
        range_high_4h=112.0,
        range_low_4h=97.0,
        previous_1h_low=98.0,
        previous_1h_high=102.0,
        current_4h_low=99.1,
        current_4h_high=101.0,
        last_4h_close=99.0,
        prev_4h_close=101.0,
        ret_24h=-0.02,
        range_72h=0.05,
        range_amplitude_4h=0.05,
    )

    signals = engine.build_signals(
        "BTC/USDT:USDT",
        Regime.SHOCK_TREND_DOWN,
        Regime.SHOCK_TREND_DOWN,
        features,
        candles,
        [],
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.regime == Regime.LIQUIDITY_SWEEP_REVERSAL
    assert signal.position_side == PositionSide.LONG
    assert signal.reason == "liquidity_sweep_downside_confirmed_long"
    assert signal.metadata["opportunity_score"] >= 92
    assert signal.metadata["confirmation_mode"] == "next_5m"
