from datetime import datetime, timezone

from tests.unit.test_decision import kline, liquidation
from trading_notice.config import load_analysis_config
from trading_notice.models import FailureState
from trading_notice.scheduler import SchedulerState, run_once


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


def test_dry_run_strategy_email_cycle_scenario_c():
    decision = run_once(
        config(),
        state=SchedulerState(),
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        dry_run_email=True,
        liquidation_fetcher=lambda *_args, **_kwargs: liquidation(),
        kline_fetcher=lambda _config: kline("stood_above", "high_breakout"),
    )

    assert decision.status == "long"
    assert decision.matched_scenario == "C_confirmed_long"


def test_no_trade_cycle_sends_no_strategy_email():
    decision = run_once(
        config(),
        state=SchedulerState(),
        dry_run_email=True,
        liquidation_fetcher=lambda *_args, **_kwargs: liquidation(near_tie=True),
        kline_fetcher=lambda _config: kline("stood_above", "high_breakout"),
    )

    assert decision.status == "no_trade"


def test_dependency_failure_cycle_builds_failure_path():
    failure = FailureState("stale_scrape", True, 0, "coinglass_web", "stale")
    decision = run_once(
        config(),
        state=SchedulerState(),
        dry_run_email=True,
        liquidation_fetcher=lambda *_args, **_kwargs: liquidation(failure=failure),
        kline_fetcher=lambda _config: kline("stood_above", "high_breakout"),
    )

    assert decision.status == "failure"
    assert decision.failure.category == "stale_scrape"
