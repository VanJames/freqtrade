from __future__ import annotations

from trading_system.config import Settings
from trading_system.models import PositionSide, Regime, Side, SignalType, TradeSignal
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
