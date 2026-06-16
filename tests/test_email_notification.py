from __future__ import annotations

from trading_system.email_notification import SmtpConfig, build_order_signal_email, format_order_signal
from trading_system.models import PositionSide, Regime, Side, SignalType, TradeSignal


def test_order_signal_email_contains_order_fields() -> None:
    signal = TradeSignal(
        symbol="BTC/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        reason="pullback_confirmed",
        metadata={"opportunity_grade": "A", "opportunity_score": 98, "risk_multiplier": 3.0},
    )
    text = format_order_signal(signal, order_price=99.5, amount=0.25)

    assert "品种: BTC/USDT:USDT" in text
    assert "方向: long" in text
    assert "下单价: 99.50000000" in text
    assert "数量: 0.25000000" in text
    assert "止损: 98.00000000" in text
    assert "止盈: 104.00000000" in text
    assert "理由: pullback_confirmed" in text


def test_build_order_signal_email_sets_headers() -> None:
    config = SmtpConfig(
        enabled=True,
        user="sender@example.com",
        password="secret",
        to="receiver@example.com",
        host="smtp.example.com",
    )
    signal = TradeSignal(
        symbol="ETH/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.SELL,
        position_side=PositionSide.SHORT,
        regime=Regime.TREND_SHORT,
        price=2000.0,
        stop_loss=2020.0,
        take_profit=1960.0,
    )

    message = build_order_signal_email(config, signal, order_price=2001.0, amount=1.0)

    assert message["From"] == "sender@example.com"
    assert message["To"] == "receiver@example.com"
    assert "ETH/USDT:USDT" in str(message["Subject"])


def test_blocked_order_signal_email_marks_not_submitted() -> None:
    signal = TradeSignal(
        symbol="XAU/USDT:USDT",
        signal_type=SignalType.ENTER_TREND,
        side=Side.BUY,
        position_side=PositionSide.LONG,
        regime=Regime.SHOCK_TREND_UP,
        price=4300.0,
        stop_loss=4250.0,
        take_profit=4400.0,
        reason="unit_test_blocked",
    )
    text = format_order_signal(
        signal,
        order_price=4300.0,
        amount=0.0,
        status="风控拒单",
        note="未真实下单: low_equity; account_equity=1.0000",
    )

    assert "状态: 风控拒单" in text
    assert "备注: 未真实下单: low_equity" in text
