from __future__ import annotations

from trading_system.backtest import classify_trade_attribution, trade_attribution_summary
from trading_system.models import PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.backtest import SimTrade
from trading_system.backtest import can_add_to_position


def test_classify_trade_attribution_flags_low_short_chase() -> None:
    attribution, detail = classify_trade_attribution(
        {
            "side": PositionSide.SHORT,
            "regime": Regime.SHOCK_TREND_DOWN,
            "entry": 100.0,
            "stop": 101.0,
            "entry_close_position_72h": 0.05,
            "entry_ret_24h": -0.02,
            "entry_ret_72h": -0.03,
        },
        raw_exit_price=101.0,
        reason="stop_loss",
        pnl=-10.0,
    )

    assert attribution == "chase_low_short"
    assert "72h低位" in detail


def test_classify_trade_attribution_marks_plain_stop_as_reversal_candidate() -> None:
    attribution, detail = classify_trade_attribution(
        {
            "side": PositionSide.LONG,
            "regime": Regime.SHOCK_TREND_UP,
            "entry": 100.0,
            "stop": 98.0,
            "entry_close_position_72h": 0.55,
            "entry_ret_24h": 0.02,
            "entry_ret_72h": 0.03,
        },
        raw_exit_price=98.0,
        reason="stop_loss",
        pnl=-10.0,
    )

    assert attribution == "stop_loss_reversal_candidate"
    assert "stop_loss" in detail


def test_trade_attribution_summary_groups_losses() -> None:
    trades = [
        SimTrade(
            symbol="BTC/USDT:USDT",
            side=PositionSide.SHORT,
            regime=Regime.SHOCK_TREND_DOWN,
            entry_time=0,
            exit_time=0,
            entry_price=100.0,
            exit_price=101.0,
            qty=1.0,
            pnl=-10.0,
            pnl_pct_equity=-0.001,
            reason="stop_loss",
            attribution="chase_low_short",
        ),
        SimTrade(
            symbol="ETH/USDT:USDT",
            side=PositionSide.LONG,
            regime=Regime.SHOCK_TREND_UP,
            entry_time=0,
            exit_time=0,
            entry_price=100.0,
            exit_price=99.0,
            qty=1.0,
            pnl=-5.0,
            pnl_pct_equity=-0.0005,
            reason="stop_loss",
            attribution="chase_low_short",
        ),
    ]

    summary = trade_attribution_summary(trades)

    assert summary["chase_low_short"]["count"] == 2
    assert summary["chase_low_short"]["pnl"] == -15.0


def test_can_add_to_position_blocks_mixed_trend_scout_scale_in() -> None:
    position = {
        "side": PositionSide.LONG,
        "regime": Regime.SHOCK_TREND_UP,
        "entry_stage": "scout",
        "add_count": 0,
        "entry_ret_24h": 0.03,
        "entry_ret_72h": -0.01,
        "entry_close_position_72h": 0.79,
    }
    signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=2300.0,
        stop_loss=2250.0,
        metadata={"entry_stage": "confirmed"},
    )

    assert can_add_to_position(position, signal) is False
