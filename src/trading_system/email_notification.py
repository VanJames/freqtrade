from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from logging import getLogger
from pathlib import Path
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
        status: str = "ready",
        note: str = "",
    ) -> None:
        if not self.config.ready():
            return
        if signal.signal_type not in {SignalType.ENTER_TREND, SignalType.ENTER_GRID, SignalType.HEDGE_TRANSITION}:
            return
        try:
            await asyncio.to_thread(self._send_order_signal, signal, order_price, amount, status, note)
        except Exception:
            logger.exception(
                "order email notification failed symbol=%s side=%s reason=%s",
                signal.symbol,
                signal.position_side.value,
                signal.reason,
            )

    def _send_order_signal(
        self,
        signal: TradeSignal,
        order_price: float,
        amount: float,
        status: str,
        note: str,
    ) -> None:
        message = build_order_signal_email(
            self.config,
            signal,
            order_price=order_price,
            amount=amount,
            status=status,
            note=note,
        )
        send_email_message(self.config, message)


class ReportEmailNotifier:
    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    def notify_runtime_tuning_report(self, report_path: Path, summary: str = "") -> bool:
        if not self.config.ready():
            return False
        try:
            message = build_runtime_tuning_report_email(self.config, report_path, summary=summary)
            send_email_message(self.config, message)
            return True
        except Exception:
            logger.exception("runtime tuning report email notification failed report=%s", report_path)
            return False


def send_email_message(config: SmtpConfig, message: EmailMessage) -> None:
    if config.use_ssl:
        with smtplib.SMTP_SSL(config.host, config.port, timeout=20) as smtp:
            smtp.login(config.user, config.password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config.host, config.port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(config.user, config.password)
            smtp.send_message(message)


def build_order_signal_email(
    config: SmtpConfig,
    signal: TradeSignal,
    *,
    order_price: float,
    amount: float,
    status: str = "ready",
    note: str = "",
) -> EmailMessage:
    status_label = "未下单" if status != "ready" else "下单信号"
    subject = f"[Quant] {status_label} {signal.symbol} {signal.position_side.value.upper()}"
    message = EmailMessage()
    message["From"] = config.user
    message["To"] = config.to
    message["Subject"] = subject
    message.set_content(format_order_signal(signal, order_price=order_price, amount=amount, status=status, note=note))
    return message


def build_runtime_tuning_report_email(config: SmtpConfig, report_path: Path, summary: str = "") -> EmailMessage:
    report_text = report_path.read_text(encoding="utf-8")
    subject = f"[Quant] 调参报告 {report_path.name}"
    body = "\n".join(
        item
        for item in [
            "自动调参报告已生成。",
            "",
            summary.strip(),
            "",
            f"报告文件: {report_path}",
            "",
            report_text,
        ]
        if item
    )
    message = EmailMessage()
    message["From"] = config.user
    message["To"] = config.to
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        report_text.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=report_path.name,
    )
    return message


def format_order_signal(
    signal: TradeSignal,
    *,
    order_price: float,
    amount: float,
    status: str = "ready",
    note: str = "",
) -> str:
    metadata = signal.metadata or {}
    take_profit = "-" if signal.take_profit is None else f"{signal.take_profit:.8f}"
    lines = [
        "量化交易下单信号",
        "",
        f"时间: {datetime.now(timezone.utc).isoformat()}",
        f"状态: {'准备下单' if status == 'ready' else status}",
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
    if note:
        lines.append(f"备注: {note}")
    strategy_route = metadata.get("strategy_route")
    if strategy_route:
        lines.append(f"策略路径: {strategy_route}")
    return "\n".join(lines)
