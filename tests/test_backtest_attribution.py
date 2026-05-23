from __future__ import annotations

from trading_system.backtest import classify_trade_attribution, trade_attribution_summary
from trading_system.models import PositionSide, Regime
from trading_system.backtest import SimTrade


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
