from trading_notice.config import load_analysis_config
from trading_notice.emailer import build_failure_notification, build_strategy_email, send_email
from trading_notice.models import FailureState, StrategyDecision


def config():
    return load_analysis_config(
        {
            "TRADING_SYMBOL": "ETH/USDT",
            "ANALYSIS_INTERVAL": "15m",
            "RECIPIENTS": "trader@example.test",
            "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_FROM": "alerts@example.test",
        }
    )


def long_decision():
    return StrategyDecision(
        cycle_id="c1",
        symbol="ETH/USDT",
        status="long",
        matched_scenario="C_confirmed_long",
        trigger_condition="Price stood above 1700.00 with volume confirmation",
        suggested_entry_price=1702.0,
        stop_loss=1688.0,
        target_take_profit=1780.0,
        predicted_liquidation_side="shorts",
        basis="Scenario C; upper_short 1740-1780; mapping coinglass-free-heatmap-v1",
    )


def test_strategy_email_contains_required_subject_and_body_fields():
    report = build_strategy_email(long_decision(), config())

    assert "LONG" in report.subject
    assert "1702.00" in report.subject
    assert "1688.00" in report.subject
    assert "1780.00" in report.subject
    assert "Direction" in report.body_html
    assert "Trigger" in report.body_html
    assert "Suggested Entry" in report.body_html
    assert "Stop Loss" in report.body_html
    assert "Target Take Profit" in report.body_html
    assert "Predicted Liquidation Side" in report.body_html
    assert "Technical Basis" in report.body_html
    assert "C_confirmed_long" in report.body_html


def test_failure_notification_redacts_sensitive_messages():
    decision = StrategyDecision(
        cycle_id="c1",
        symbol="ETH/USDT",
        status="failure",
        failure=FailureState("smtp", True, 1, "smtp", "password=secret"),
    )

    notification = build_failure_notification(decision, config())

    assert "password" not in notification.message.lower()
    assert notification.failure_category == "smtp"


def test_send_email_uses_starttls_for_587(monkeypatch):
    cfg = config()
    report = build_strategy_email(long_decision(), cfg)
    events = []

    class FakeSMTP:
        def __init__(self, host, port):
            events.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self):
            events.append(("starttls",))

        def sendmail(self, sender, recipients, message):
            events.append(("sendmail", sender, tuple(recipients), "LONG" in message))

    monkeypatch.delenv("SMTP_USE_SSL", raising=False)
    monkeypatch.delenv("SMTP_USE_TLS", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.test")

    sent = send_email(report, cfg, smtp_factory=FakeSMTP)

    assert sent.send_status == "sent"
    assert ("starttls",) in events
    assert events[-1][0] == "sendmail"


def test_send_email_uses_ssl_for_465(monkeypatch):
    cfg = config()
    report = build_strategy_email(long_decision(), cfg)
    events = []

    class PlainSMTP:
        def __init__(self, host, port):
            events.append(("plain", host, port))

    class SslSMTP:
        def __init__(self, host, port):
            events.append(("ssl", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def sendmail(self, sender, recipients, message):
            events.append(("sendmail", sender, tuple(recipients)))

    monkeypatch.delenv("SMTP_USE_SSL", raising=False)
    monkeypatch.delenv("SMTP_USE_TLS", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.test")

    sent = send_email(
        report,
        cfg,
        smtp_factory=PlainSMTP,
        smtp_ssl_factory=SslSMTP,
    )

    assert sent.send_status == "sent"
    assert events[0] == ("ssl", "smtp.example.test", 465)
