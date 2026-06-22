"""Email composition and SMTP delivery."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from trading_notice.models import (
    AnalysisConfiguration,
    FailureNotification,
    StrategyDecision,
    StrategyEmailReport,
)


def build_strategy_email(
    decision: StrategyDecision, config: AnalysisConfiguration
) -> StrategyEmailReport:
    if decision.status not in {"long", "short"}:
        raise ValueError("Strategy email can only be built for long/short decisions")
    if (
        decision.target_take_profit is None
        or decision.trigger_condition is None
        or decision.suggested_entry_price is None
        or decision.stop_loss is None
    ):
        raise ValueError("Trading decision is missing entry, stop, target, or trigger condition")
    direction = decision.status
    subject = (
        f"{direction.upper()} entry {decision.suggested_entry_price:.2f} "
        f"SL {decision.stop_loss:.2f} target {decision.target_take_profit:.2f}"
    )
    body = (
        "<html><body>"
        f"<h1>{direction.upper()} {decision.matched_scenario}</h1>"
        f"<p><strong>Direction:</strong> {direction}</p>"
        f"<p><strong>Trigger:</strong> {decision.trigger_condition}</p>"
        f"<p><strong>Suggested Entry:</strong> {decision.suggested_entry_price:.2f}</p>"
        f"<p><strong>Stop Loss:</strong> {decision.stop_loss:.2f}</p>"
        f"<p><strong>Target Take Profit:</strong> {decision.target_take_profit:.2f}</p>"
        f"<p><strong>Predicted Liquidation Side:</strong> {decision.predicted_liquidation_side}</p>"
        f"<p><strong>Technical Basis:</strong> {decision.basis}</p>"
        "</body></html>"
    )
    return StrategyEmailReport(
        cycle_id=decision.cycle_id,
        subject=subject,
        recipients=config.recipients,
        direction=direction,  # type: ignore[arg-type]
        trigger_condition=decision.trigger_condition,
        suggested_entry_price=decision.suggested_entry_price,
        stop_loss=decision.stop_loss,
        target_take_profit=decision.target_take_profit,
        predicted_liquidation_side=decision.predicted_liquidation_side,  # type: ignore[arg-type]
        technical_basis=decision.basis,
        body_html=body,
    )


def build_failure_notification(
    decision: StrategyDecision, config: AnalysisConfiguration
) -> FailureNotification:
    if decision.failure is None:
        raise ValueError("Failure notification requires a failure decision")
    return FailureNotification(
        cycle_id=decision.cycle_id,
        failure_category=decision.failure.category,
        message=_redact(decision.failure.safe_message),
        recipients=config.recipients,
    )


def send_email(
    report: StrategyEmailReport | FailureNotification,
    config: AnalysisConfiguration,
    *,
    smtp_factory: Any = smtplib.SMTP,
    smtp_ssl_factory: Any = smtplib.SMTP_SSL,
) -> StrategyEmailReport | FailureNotification:
    host_ref = config.smtp_settings_ref.get("SMTP_HOST", "SMTP_HOST")
    port_ref = config.smtp_settings_ref.get("SMTP_PORT", "SMTP_PORT")
    from_ref = config.smtp_settings_ref.get("SMTP_FROM", "SMTP_FROM")
    import os

    host = os.environ.get(host_ref, host_ref)
    port = int(os.environ.get(port_ref, "25") if port_ref == "SMTP_PORT" else port_ref)
    sender = os.environ.get(from_ref, from_ref)
    message = MIMEMultipart("alternative")
    message["Subject"] = report.subject if isinstance(report, StrategyEmailReport) else "Trading notice failure"
    message["From"] = sender
    message["To"] = ", ".join(report.recipients)
    body = report.body_html if isinstance(report, StrategyEmailReport) else _redact(report.message)
    message.attach(MIMEText(body, "html"))
    use_ssl = _env_bool("SMTP_USE_SSL", default=port == 465)
    use_tls = _env_bool("SMTP_USE_TLS", default=(port == 587 and not use_ssl))
    factory = smtp_ssl_factory if use_ssl else smtp_factory
    with factory(host, port) as smtp:
        if use_tls and not use_ssl:
            smtp.starttls()
        username_ref = config.smtp_settings_ref.get("SMTP_USERNAME")
        password_ref = config.smtp_settings_ref.get("SMTP_PASSWORD")
        if username_ref and password_ref:
            smtp.login(os.environ.get(username_ref, username_ref), os.environ.get(password_ref, ""))
        smtp.sendmail(sender, list(report.recipients), message.as_string())
    return _replace_status(report, "sent")


def _replace_status(
    report: StrategyEmailReport | FailureNotification, status: str
) -> StrategyEmailReport | FailureNotification:
    from dataclasses import replace

    return replace(report, send_status=status)


def _redact(message: str) -> str:
    lowered = message.lower()
    if any(word in lowered for word in ("password", "secret", "token", "api key", "apikey")):
        return "Failure occurred; sensitive details redacted"
    return message


def _env_bool(name: str, *, default: bool = False) -> bool:
    import os

    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
