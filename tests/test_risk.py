from __future__ import annotations

import pytest

from trading_system.config import Settings
from trading_system.models import PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.position_sizing import adjusted_risk_multiplier
from trading_system.risk import RiskManager


def signal(signal_type: SignalType = SignalType.ENTER_TREND) -> TradeSignal:
    return TradeSignal(
        symbol="BTC/USDT:USDT",
        signal_type=signal_type,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.TREND_LONG,
        price=100.0,
        stop_loss=95.0,
    )


def test_position_size_uses_one_percent_risk() -> None:
    risk = RiskManager(Settings(dry_run=True))
    decision = risk.assess(signal(), equity=10_000.0, funding_rate=0.0)

    assert decision.allowed
    assert decision.size == 20.0


def test_directional_risk_limit_blocks_fourth_one_percent_trade() -> None:
    risk = RiskManager(Settings(dry_run=True))
    risk.reserve_risk(PositionSide.LONG)
    risk.reserve_risk(PositionSide.LONG)
    risk.reserve_risk(PositionSide.LONG)

    decision = risk.assess(signal(), equity=10_000.0, funding_rate=0.0)

    assert not decision.allowed
    assert decision.reason == "same_direction_risk_limit"


def test_directional_risk_release_reopens_capacity() -> None:
    risk = RiskManager(Settings(dry_run=True))
    risk.reserve_risk(PositionSide.LONG, 1.5)
    risk.reserve_risk(PositionSide.LONG, 1.5)

    blocked = risk.assess(signal(), equity=10_000.0, funding_rate=0.0)
    risk.release_risk(PositionSide.LONG, 1.5)
    allowed = risk.assess(signal(), equity=10_000.0, funding_rate=0.0)

    assert not blocked.allowed
    assert blocked.reason == "same_direction_risk_limit"
    assert allowed.allowed
    assert risk.direction_risk[PositionSide.LONG] == 0.015


def test_directional_risk_release_does_not_go_negative() -> None:
    risk = RiskManager(Settings(dry_run=True))

    risk.release_risk(PositionSide.SHORT, 3.0)

    assert risk.direction_risk[PositionSide.SHORT] == 0.0


def test_grid_blocked_when_funding_too_expensive() -> None:
    grid_signal = signal(SignalType.ENTER_GRID)
    grid_signal.regime = Regime.SHOCK
    risk = RiskManager(Settings(dry_run=True))

    decision = risk.assess(grid_signal, equity=10_000.0, funding_rate=0.001)

    assert not decision.allowed
    assert decision.reason == "grid_blocked_by_funding_rate"


def test_leverage_cap_limits_position_size() -> None:
    risk = RiskManager(Settings(dry_run=True, trend_symbol_leverage_limit=1.0))
    capped_signal = signal()
    capped_signal.stop_loss = 99.9

    decision = risk.assess(capped_signal, equity=1000.0, funding_rate=0.0)

    assert decision.allowed
    assert decision.size == 10.0


def test_confirmation_sizing_uses_position_side_and_risk_throttle_for_shorts() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    short_signal = TradeSignal(
        symbol="BTC/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.SELL,
        position_side=PositionSide.SHORT,
        regime=Regime.SHOCK_TREND_DOWN,
        price=100.0,
        stop_loss=101.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 0.70,
        },
    )

    assert adjusted_risk_multiplier(short_signal, settings) == pytest.approx(1.5842)


def test_confirmation_sizing_caps_low_trust_sol_xau_shock_trend_down_shorts() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    for symbol in ("SOL/USDT:USDT", "XAU/USDT:USDT"):
        short_signal = TradeSignal(
            symbol=symbol,
            signal_type=SignalType.ENTER_TREND,
            side=Side.SELL,
            position_side=PositionSide.SHORT,
            regime=Regime.SHOCK_TREND_DOWN,
            price=100.0,
            stop_loss=101.0,
            metadata={
                "opportunity_score": 98,
                "opportunity_reasons": (
                    "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                    "momentum_cross,momentum_positive,not_chasing,rr_good"
                ),
                "risk_multiplier": 1.0,
                "risk_throttle": 1.0,
                "close_position_72h": 0.50,
            },
        )

        assert adjusted_risk_multiplier(short_signal, settings) == pytest.approx(0.35)


def test_confirmation_sizing_caps_low_range_shock_trend_down_shorts() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    short_signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.SELL,
        position_side=PositionSide.SHORT,
        regime=Regime.SHOCK_TREND_DOWN,
        price=100.0,
        stop_loss=101.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "close_position_72h": 0.31,
            "ret_24h": -0.004,
            "ret_72h": -0.025,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(short_signal, settings) == pytest.approx(0.85)


def test_confirmation_sizing_caps_local_top_shock_trend_up_longs() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    long_signal = TradeSignal(
        symbol="XAU/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=4700.0,
        stop_loss=4650.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "recent_5h_position": 0.95,
            "close_position_72h": 0.75,
            "ret_24h": 0.004,
            "ret_72h": 0.02,
            "range_72h": 0.04,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(long_signal, settings) == pytest.approx(0.9)


def test_confirmation_sizing_caps_weak_drift_shock_trend_up_longs() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    long_signal = TradeSignal(
        symbol="XAU/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=4700.0,
        stop_loss=4650.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "recent_5h_position": 0.62,
            "close_position_72h": 0.62,
            "ret_24h": 0.003,
            "ret_72h": -0.006,
            "range_72h": 0.04,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(long_signal, settings) == pytest.approx(0.75)


def test_confirmation_sizing_does_not_cap_wide_range_shock_trend_up_longs() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=1.5,
        confirmation_max_risk_multiplier=3.0,
    )
    long_signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=2300.0,
        stop_loss=2250.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "recent_5h_position": 0.95,
            "close_position_72h": 0.75,
            "ret_24h": 0.004,
            "ret_72h": 0.006,
            "range_72h": 0.12,
            "volatility_tier": "HIGH",
        },
    )

    assert adjusted_risk_multiplier(long_signal, settings) == pytest.approx(1.5)


def test_confirmation_sizing_caps_mixed_shock_trend_up_longs_near_72h_high() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    long_signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=2300.0,
        stop_loss=2250.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "recent_5h_position": 0.80,
            "close_position_72h": 0.79,
            "ret_24h": 0.047,
            "ret_72h": -0.002,
            "range_72h": 0.12,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(long_signal, settings) == pytest.approx(1.0)


def test_confirmation_sizing_caps_late_burst_without_72h_followthrough() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    long_signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=2300.0,
        stop_loss=2250.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "recent_5h_position": 0.70,
            "close_position_72h": 0.80,
            "ret_24h": 0.045,
            "ret_72h": 0.004,
            "range_72h": 0.12,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(long_signal, settings) == pytest.approx(1.0)


def test_confirmation_sizing_caps_mixed_shock_trend_down_shorts() -> None:
    settings = Settings(
        dry_run=True,
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=3.0,
        confirmation_max_risk_multiplier=3.0,
    )
    short_signal = TradeSignal(
        symbol="BTC/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.SELL,
        position_side=PositionSide.SHORT,
        regime=Regime.SHOCK_TREND_DOWN,
        price=100.0,
        stop_loss=101.0,
        metadata={
            "opportunity_score": 98,
            "opportunity_reasons": (
                "multi_timeframe,one_hour_trend,pullback,confirmation_candle,"
                "momentum_cross,momentum_positive,not_chasing,rr_good"
            ),
            "risk_multiplier": 1.0,
            "risk_throttle": 1.0,
            "close_position_72h": 0.50,
            "ret_24h": -0.006,
            "ret_72h": 0.012,
            "volatility_tier": "NORMAL",
        },
    )

    assert adjusted_risk_multiplier(short_signal, settings) == pytest.approx(0.75)
