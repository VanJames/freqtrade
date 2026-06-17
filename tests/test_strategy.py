from __future__ import annotations

import pytest
import pandas as pd

import trading_system.backtest as backtest_module
from trading_system.backtest import (
    BacktestConfig,
    cap_stop,
    daily_macd_momentum_exit_price,
    maybe_exit,
    signal_reward,
)
from trading_system.indicators import ohlcv_frame
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
    engine = StrategyEngine(
        min_stop_loss_pct=0.002,
        high_vol_min_stop_loss_pct=0.004,
        max_stop_loss_pct=0.012,
    )
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


def test_daily_macd_momentum_exit_closes_at_current_close(monkeypatch) -> None:
    position = {
        "side": PositionSide.LONG,
        "entry": 100.0,
        "stop": 98.0,
        "take_profit": None,
        "highest": 105.0,
        "lowest": 100.0,
        "atr": 1.0,
        "trailing_gap_pct": 0.0,
        "min_trailing_activate_r": 1.2,
        "breakeven_activate_r": 0.6,
        "breakeven_buffer_pct": 0.0003,
        "delay_trailing_until_momentum_exit": True,
        "delayed_trailing_activated": False,
        "pending_trailing_gap_pct": 0.01,
        "pending_min_trailing_activate_r": 1.2,
        "pending_breakeven_activate_r": 0.6,
    }

    exit_price, reason = maybe_exit(position, pd.Series({"high": 105.0, "low": 99.0}))

    assert exit_price is None
    assert reason == ""
    assert position["stop"] == pytest.approx(98.0)

    monkeypatch.setattr(
        backtest_module, "daily_macd_momentum_exit", lambda *_args: True
    )
    exit_price, reason = daily_macd_momentum_exit_price(
        position,
        pd.DataFrame({"close": [100.0]}),
        pd.Series({"close": 104.2}),
    )

    assert exit_price == pytest.approx(104.2)
    assert reason == "daily_macd_momentum_exit"
    assert position["delayed_trailing_activated"] is False
    assert position["trailing_gap_pct"] == pytest.approx(0.0)


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

    diagnostics = engine.entry_diagnostics(
        "BTC/USDT:USDT", Regime.TREND_LONG, features, candles, []
    )

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
        candles.append(
            [
                i * 300000,
                open_price,
                open_price + 0.03,
                close_price - 0.03,
                close_price,
                1.0,
            ]
        )
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

    diagnostics = engine.entry_diagnostics(
        "BTC/USDT:USDT", Regime.SHOCK_TREND_DOWN, features, candles, []
    )

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
        candles.append(
            [
                index * 300000,
                open_price,
                open_price + 0.03,
                close_price - 0.03,
                close_price,
                1.0,
            ]
        )
        price = close_price
    candles.append([58 * 300000, price, price + 0.8, price - 0.05, price + 0.65, 1.0])
    candles.append(
        [59 * 300000, price + 0.65, price + 0.7, price + 0.1, price + 0.2, 1.0]
    )
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

    diagnostics = engine.entry_diagnostics(
        "BTC/USDT:USDT", Regime.SHOCK_TREND_DOWN, features, candles, []
    )

    assert diagnostics["action"] == "shock_trend_down"
    assert any(
        item["code"] == "late_entry_risk" and item["value"] == "HIGH"
        for item in diagnostics["requirements"]
    )
    assert not any(
        item["code"] == "late_entry_risk" for item in diagnostics["blockers"]
    )


def test_shock_trend_up_diagnostics_reports_late_high_long_risk() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(58):
        open_price = price
        close_price = price + 0.18
        candles.append(
            [
                index * 300000,
                open_price,
                close_price + 0.03,
                open_price - 0.03,
                close_price,
                1.0,
            ]
        )
        price = close_price
    candles.append([58 * 300000, price, price + 0.05, price - 0.65, price - 0.5, 1.0])
    candles.append(
        [59 * 300000, price - 0.5, price + 0.1, price - 0.55, price + 0.05, 1.0]
    )
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

    diagnostics = engine.entry_diagnostics(
        "BTC/USDT:USDT", Regime.SHOCK_TREND_UP, features, candles, []
    )

    assert diagnostics["action"] == "shock_trend_up"
    assert any(
        item["code"] == "late_entry_risk" and item["value"] == "HIGH"
        for item in diagnostics["requirements"]
    )
    assert not any(
        item["code"] == "late_entry_risk" for item in diagnostics["blockers"]
    )


def test_late_shock_trend_up_blocks_burst_without_72h_followthrough() -> None:
    assert not StrategyEngine._late_shock_trend_entry_ok(
        PositionSide.LONG,
        close_position_72h=0.7533,
        ret_24h=0.0265,
        ret_72h=0.0080,
        recent_5h_position=0.50,
    )


def test_shock_trend_up_confirmed_requires_late_entry_filter(monkeypatch) -> None:
    engine = StrategyEngine()
    monkeypatch.setattr(engine, "_recent_range_position", lambda *args, **kwargs: 0.65)
    monkeypatch.setattr(engine, "_sol_allowed", lambda *args, **kwargs: True)
    candles = [
        [
            index * 300_000,
            100.0 + index,
            100.2 + index,
            99.8 + index,
            100.1 + index,
            1.0,
        ]
        for index in range(70)
    ]
    frame = ohlcv_frame(candles)
    features = MarketFeatures(
        atr_1h=1.0,
        close_1h=168.0,
        ema20_1h=160.0,
        ema60_1h=150.0,
        current_4h_low=155.0,
        current_4h_high=170.0,
        last_4h_close=168.0,
        prev_4h_close=164.0,
        close_position_72h=0.84,
        ret_24h=0.026,
        ret_72h=0.030,
    )
    current = frame.iloc[-1].copy()
    current.open = 166.0
    current.close = 168.0
    volatility_policy = engine._volatility_policy(168.0, features, frame)

    signal = engine._user_4h_shock_trend_signal(
        "BTC/USDT:USDT",
        Regime.SHOCK_TREND_UP,
        features,
        frame,
        168.0,
        current,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        165.0,
        164.0,
        55.0,
        volatility_policy,
        [],
        {},
    )

    assert signal is None


def test_liquidity_sweep_reversal_detects_downside_reclaim() -> None:
    engine = StrategyEngine(
        enable_liquidity_sweep_reversal=True, max_stop_loss_pct=0.015
    )
    candles = []
    price = 110.0
    for index in range(89):
        open_price = price
        close_price = price - 0.12
        candles.append(
            [
                index * 300000,
                open_price,
                open_price + 0.05,
                close_price - 0.05,
                close_price,
                100.0,
            ]
        )
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


def test_macd_pre_cross_detects_approaching_bullish_cross_before_crossing() -> None:
    engine = StrategyEngine()
    values = []
    price = 100.0
    for index in range(100):
        if index < 55:
            price += 0.20
        elif index < 85:
            price -= 0.01
        else:
            price += 0.02
        values.append(price)
    frame = ohlcv_frame(
        [
            [idx * 300_000, value, value + 0.1, value - 0.1, value, 1.0]
            for idx, value in enumerate(values)
        ]
    )

    assert engine._macd_pre_cross_state(frame, PositionSide.LONG)
    assert not engine._macd_pre_cross_state(frame, PositionSide.SHORT)


def test_two_candle_momentum_requires_5m_and_15m_expanding_candles() -> None:
    engine = StrategyEngine(enable_two_candle_momentum=True)
    candles = []
    price = 100.0
    for index in range(30):
        close = price + 0.01
        candles.append([index * 300_000, price, close + 0.03, price - 0.03, close, 1.0])
        price = close
    for index, (open_price, close_price, high, low) in enumerate(
        [
            (100.30, 100.36, 100.38, 100.28),
            (100.36, 100.42, 100.44, 100.35),
            (100.42, 100.48, 100.50, 100.40),
            (100.48, 100.56, 100.58, 100.47),
            (100.56, 100.66, 100.69, 100.55),
            (100.66, 100.78, 100.82, 100.65),
        ],
        start=30,
    ):
        candles.append([index * 300_000, open_price, high, low, close_price, 1.0])
    features = MarketFeatures(
        atr_1h=0.5,
        close_1h=100.78,
        ema20_1h=101.0,
        ema60_1h=100.0,
        close_position_72h=0.5,
        ret_24h=0.01,
    )

    signals = engine.build_signals(
        "BTC/USDT:USDT",
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_UP,
        features,
        candles,
        [],
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.position_side == PositionSide.LONG
    assert signal.reason == "two_candle_5m_15m_momentum_long"
    assert signal.metadata["risk_multiplier"] == 0.35
    assert signal.metadata["strategy_route"] == "two_candle_momentum_experimental"


def test_two_candle_momentum_is_disabled_by_default() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(30):
        close = price + 0.01
        candles.append([index * 300_000, price, close + 0.03, price - 0.03, close, 1.0])
        price = close
    for index, (open_price, close_price, high, low) in enumerate(
        [
            (100.30, 100.36, 100.38, 100.28),
            (100.36, 100.42, 100.44, 100.35),
            (100.42, 100.48, 100.50, 100.40),
            (100.48, 100.56, 100.58, 100.47),
            (100.56, 100.66, 100.69, 100.55),
            (100.66, 100.78, 100.82, 100.65),
        ],
        start=30,
    ):
        candles.append([index * 300_000, open_price, high, low, close_price, 1.0])
    features = MarketFeatures(
        atr_1h=0.5,
        close_1h=100.78,
        ema20_1h=101.0,
        ema60_1h=100.0,
        close_position_72h=0.5,
        ret_24h=0.01,
    )

    signals = engine.build_signals(
        "BTC/USDT:USDT",
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_UP,
        features,
        candles,
        [],
    )

    assert signals == []


def test_down_continuation_short_catches_weak_rebound_failure() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(75):
        close = price - 0.015
        candles.append([index * 300_000, price, price + 0.02, close - 0.02, close, 1.0])
        price = close
    for index, delta in enumerate(
        [0.15, 0.12, 0.10, 0.08, -0.06, -0.07, -0.06, -0.07, -0.06, -0.07],
        start=75,
    ):
        open_price = price
        close = price + delta
        candles.append(
            [
                index * 300_000,
                open_price,
                max(open_price, close) + 0.03,
                min(open_price, close) - 0.03,
                close,
                1.0,
            ]
        )
        price = close
    features = MarketFeatures(
        atr_1h=0.5,
        close_1h=price,
        ema20_1h=99.0,
        ema60_1h=100.0,
        current_4h_high=101.0,
        current_4h_low=95.0,
        last_4h_close=98.0,
        prev_4h_close=99.0,
        close_position_72h=0.45,
        ret_24h=-0.012,
        ret_72h=-0.02,
        range_72h=0.08,
    )

    signals = engine.build_signals(
        "BTC/USDT:USDT",
        Regime.SHOCK_TREND_DOWN,
        Regime.SHOCK_TREND_DOWN,
        features,
        candles,
        [],
        context={"adaptive_continuation_short": True},
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.position_side == PositionSide.SHORT
    assert signal.reason == "user_4h_down_continuation_short"
    assert signal.metadata["entry_stage"] == "continuation"
    assert signal.metadata["risk_multiplier"] == 0.62


def test_down_continuation_short_skips_xau_noise() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(75):
        close = price - 0.015
        candles.append([index * 300_000, price, price + 0.02, close - 0.02, close, 1.0])
        price = close
    for index, delta in enumerate(
        [0.15, 0.12, 0.10, 0.08, -0.06, -0.07, -0.06, -0.07, -0.06, -0.07],
        start=75,
    ):
        open_price = price
        close = price + delta
        candles.append(
            [
                index * 300_000,
                open_price,
                max(open_price, close) + 0.03,
                min(open_price, close) - 0.03,
                close,
                1.0,
            ]
        )
        price = close
    features = MarketFeatures(
        atr_1h=0.5,
        close_1h=price,
        ema20_1h=99.0,
        ema60_1h=100.0,
        current_4h_high=101.0,
        current_4h_low=95.0,
        last_4h_close=98.0,
        prev_4h_close=99.0,
        close_position_72h=0.45,
        ret_24h=-0.012,
        ret_72h=-0.02,
        range_72h=0.08,
    )

    signals = engine.build_signals(
        "XAU/USDT:USDT",
        Regime.SHOCK_TREND_DOWN,
        Regime.SHOCK_TREND_DOWN,
        features,
        candles,
        [],
    )

    assert signals == []


def test_down_continuation_short_skips_eth_low_edge() -> None:
    engine = StrategyEngine()
    candles = []
    price = 100.0
    for index in range(75):
        close = price - 0.015
        candles.append([index * 300_000, price, price + 0.02, close - 0.02, close, 1.0])
        price = close
    for index, delta in enumerate(
        [0.15, 0.12, 0.10, 0.08, -0.06, -0.07, -0.06, -0.07, -0.06, -0.07],
        start=75,
    ):
        open_price = price
        close = price + delta
        candles.append(
            [
                index * 300_000,
                open_price,
                max(open_price, close) + 0.03,
                min(open_price, close) - 0.03,
                close,
                1.0,
            ]
        )
        price = close
    features = MarketFeatures(
        atr_1h=0.5,
        close_1h=price,
        ema20_1h=99.0,
        ema60_1h=100.0,
        current_4h_high=101.0,
        current_4h_low=95.0,
        last_4h_close=98.0,
        prev_4h_close=99.0,
        close_position_72h=0.45,
        ret_24h=-0.012,
        ret_72h=-0.02,
        range_72h=0.08,
    )

    signals = engine.build_signals(
        "ETH/USDT:USDT",
        Regime.SHOCK_TREND_DOWN,
        Regime.SHOCK_TREND_DOWN,
        features,
        candles,
        [],
    )

    assert signals == []


def _daily_macd_test_rows(
    count: int,
    start: float,
    step: float,
    down_factor: float,
    timeframe_ms: int,
    start_ms: int = 0,
) -> list[list[float]]:
    rows = []
    price = start
    for index in range(count):
        delta = step if index % 3 != 1 else -step * down_factor
        open_price = price
        close = price + delta
        rows.append(
            [
                start_ms + index * timeframe_ms,
                open_price,
                max(open_price, close) + 0.04,
                min(open_price, close) - 0.04,
                close,
                1.0,
            ]
        )
        price = close
    return rows


def _daily_macd_one_hour_red_rows() -> list[list[float]]:
    price = 120.0
    rows = []
    for index in range(45):
        open_price = price
        close = price - 0.2
        rows.append(
            [
                index * 3_600_000,
                open_price,
                max(open_price, close) + 0.05,
                min(open_price, close) - 0.05,
                close,
                10.0,
            ]
        )
        price = close
    return rows


def _daily_macd_live_5m_green_rows() -> list[list[float]]:
    current_hour_start = 45 * 3_600_000
    start_ms = current_hour_start - 68 * 300_000
    rows = []
    price = 111.0
    for index in range(68):
        open_price = price
        close = price + 0.005
        rows.append(
            [
                start_ms + index * 300_000,
                open_price,
                close + 0.03,
                open_price - 0.03,
                close,
                1.0,
            ]
        )
        price = close
    for index in range(12):
        progress = (index + 1) / 12
        open_price = 111.0 + 2.0 * index / 12
        close = 111.0 + 2.0 * progress
        rows.append(
            [
                current_hour_start + index * 300_000,
                open_price,
                close + 0.05,
                open_price - 0.05,
                close,
                2.0,
            ]
        )
    return rows


def _daily_macd_golden_cross_rows() -> list[list[float]]:
    prices = []
    price = 120.0
    for _ in range(5):
        prices.append(price)
    for _ in range(28):
        price -= 0.7
        prices.append(price)
    for _ in range(2):
        price += 2.0
        prices.append(price)
    return [
        [index * 86_400_000, close - 0.2, close + 0.4, close - 0.4, close, 10.0]
        for index, close in enumerate(prices)
    ]


def _daily_macd_weakening_golden_rows() -> list[list[float]]:
    rows = _daily_macd_golden_cross_rows()
    price = rows[-1][4]
    for delta in [-3.0, -2.0, 0.0]:
        index = len(rows)
        price += delta
        rows.append(
            [
                index * 86_400_000,
                price - 0.2,
                price + 0.3,
                price - 0.3,
                price,
                10.0,
            ]
        )
    return rows


def test_daily_macd_breakout_long_requires_explicit_enable() -> None:
    candles_5m = _daily_macd_live_5m_green_rows()
    candles_1h = _daily_macd_one_hour_red_rows()
    candles_4h = _daily_macd_test_rows(35, 100.0, 0.8, 0.35, 14_400_000)
    candles_1d = _daily_macd_golden_cross_rows()
    features = MarketFeatures(
        atr_1h=0.8,
        close_1h=candles_5m[-1][4],
        ema20_1h=105.0,
        ema60_1h=101.0,
        previous_1h_low=101.0,
        previous_1h_high=108.0,
        current_4h_low=100.0,
        current_4h_high=110.0,
        last_4h_close=108.0,
        prev_4h_close=106.0,
        close_position_72h=0.9,
        ret_24h=0.01,
        ret_72h=0.06,
        range_24h=0.05,
        range_72h=0.08,
    )

    disabled = StrategyEngine()
    enabled = StrategyEngine(enable_daily_macd_breakout=True)

    assert (
        disabled.build_signals(
            "XAU/USDT:USDT",
            Regime.SHOCK_TREND_UP,
            Regime.SHOCK_TREND_UP,
            features,
            candles_5m,
            [],
            candles_1h,
            candles_4h,
            candles_1d,
        )
        == []
    )
    signals = enabled.build_signals(
        "XAU/USDT:USDT",
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_UP,
        features,
        candles_5m,
        [],
        candles_1h,
        candles_4h,
        candles_1d,
    )

    assert len(signals) == 1
    assert signals[0].position_side == PositionSide.LONG
    assert signals[0].reason == "daily_macd_golden_cross_breakout_long"
    assert signals[0].metadata["strategy_route"] == "daily_macd_breakout"
    assert signals[0].metadata["daily_macd_mode"] == "golden_cross_live"
    assert (
        signals[0].metadata["daily_macd_mode_ts"]
        == int(candles_1d[-1][0]) // 86_400_000 * 86_400_000
    )
    assert signals[0].take_profit is None
    assert signals[0].metadata["delay_trailing_until_momentum_exit"] is True
    assert signals[0].metadata["trailing_gap_pct"] == 0.0
    assert 0.0045 <= signals[0].metadata["pending_trailing_gap_pct"] <= 0.018
    assert 1.4 <= signals[0].metadata["min_trailing_activate_r"] <= 3.0
    assert 0.8 <= signals[0].metadata["breakeven_activate_r"] <= 1.2
    assert signals[0].metadata["daily_macd_reward_risk"] >= 2.4
    assert signals[0].metadata["daily_atr_pct"] > 0
    assert signals[0].metadata["daily_macd_hist_strength"] > 0
    assert signals[0].metadata["daily_structure_stop_basis"] == "daily_support"
    assert signals[0].stop_loss < signals[0].price * (1 - enabled.max_stop_loss_pct)


def test_daily_macd_breakout_stops_when_daily_histogram_weakens() -> None:
    engine = StrategyEngine(enable_daily_macd_breakout=True)
    candles_5m = _daily_macd_live_5m_green_rows()
    candles_1h = _daily_macd_one_hour_red_rows()
    candles_4h = _daily_macd_test_rows(35, 100.0, 0.8, 0.35, 14_400_000)
    features = MarketFeatures(
        atr_1h=0.8,
        close_1h=candles_5m[-1][4],
        ema20_1h=105.0,
        ema60_1h=101.0,
        previous_1h_low=101.0,
        previous_1h_high=108.0,
        current_4h_low=100.0,
        current_4h_high=110.0,
        last_4h_close=108.0,
        prev_4h_close=106.0,
        close_position_72h=0.9,
        ret_24h=0.01,
        ret_72h=0.06,
    )

    signals = engine.build_signals(
        "XAU/USDT:USDT",
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_UP,
        features,
        candles_5m,
        [],
        candles_1h,
        candles_4h,
        _daily_macd_weakening_golden_rows(),
    )

    assert signals == []
