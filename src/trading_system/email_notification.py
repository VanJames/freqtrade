from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from logging import getLogger
import smtplib
from typing import Protocol

from trading_system.models import SignalType, TradeSignal

logger = getLogger(__name__)


class EmailSettings(Protocol):
    email_enabled: bool
    email_user: str
    email_pass: str
    email_to: str
    smtp_host: str
    smtp_port: int
    smtp_use_ssl: bool


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    enabled: bool
    user: str
    password: str
    to: str
    host: str
    port: int = 465
    use_ssl: bool = True

    @classmethod
    def from_settings(cls, settings: EmailSettings) -> SmtpConfig:
        return cls(
            enabled=bool(settings.email_enabled),
            user=settings.email_user.strip(),
            password=settings.email_pass,
            to=settings.email_to.strip(),
            host=settings.smtp_host.strip(),
            port=int(settings.smtp_port or 465),
            use_ssl=bool(settings.smtp_use_ssl),
        )

    def ready(self) -> bool:
        return bool(self.enabled and self.user and self.password and self.to and self.host)


class OrderEmailNotifier:
    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    async def notify_order_signal(
        self,
        signal: TradeSignal,
        *,
        order_price: float,
        amount: float,
    ) -> None:
        if not self.config.ready():
            return
        if signal.signal_type not in {SignalType.ENTER_TREND, SignalType.ENTER_GRID, SignalType.HEDGE_TRANSITION}:
            return
        try:
            await asyncio.to_thread(self._send_order_signal, signal, order_price, amount)
        except Exception:
            logger.exception(
                "order email notification failed symbol=%s side=%s reason=%s",
                signal.symbol,
                signal.position_side.value,
                signal.reason,
            )

    def _send_order_signal(self, signal: TradeSignal, order_price: float, amount: float) -> None:
        message = build_order_signal_email(self.config, signal, order_price=order_price, amount=amount)
        if self.config.use_ssl:
            with smtplib.SMTP_SSL(self.config.host, self.config.port, timeout=20) as smtp:
                smtp.login(self.config.user, self.config.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.config.host, self.config.port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.config.user, self.config.password)
                smtp.send_message(message)


def build_order_signal_email(
    config: SmtpConfig,
    signal: TradeSignal,
    *,
    order_price: float,
    amount: float,
) -> EmailMessage:
    subject = f"[Quant] 下单信号 {signal.symbol} {signal.position_side.value.upper()}"
    message = EmailMessage()
    message["From"] = config.user
    message["To"] = config.to
    message["Subject"] = subject
    message.set_content(format_order_signal(signal, order_price=order_price, amount=amount))
    return message


def format_order_signal(signal: TradeSignal, *, order_price: float, amount: float) -> str:
    metadata = signal.metadata or {}
    take_profit = "-" if signal.take_profit is None else f"{signal.take_profit:.8f}"
    lines = [
        "量化交易下单信号",
        "",
        f"时间: {datetime.now(timezone.utc).isoformat()}",
        f"品种: {signal.symbol}",
        f"方向: {signal.position_side.value}",
        f"交易动作: {signal.side.value}",
        f"订单类型: {signal.signal_type.value}",
        f"下单价: {order_price:.8f}",
        f"信号价: {signal.price:.8f}",
        f"数量: {amount:.8f}",
        f"止损: {signal.stop_loss:.8f}",
        f"止盈: {take_profit}",
        f"行情: {signal.regime.value}",
        f"理由: {signal.reason}",
        f"机会评分: {metadata.get('opportunity_grade', '-')}{metadata.get('opportunity_score', '-')}",
        f"风险倍数: {metadata.get('risk_multiplier', '-')}",
    ]
    strategy_route = metadata.get("strategy_route")
    if strategy_route:
        lines.append(f"策略路径: {strategy_route}")
    return "\n".join(lines)
