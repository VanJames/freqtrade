from datetime import datetime, timezone

from trading_notice.config import load_analysis_config
from trading_notice.models import FailureState, KlineSignal, LiquidationSignal, ScrapeValidationResult
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


def failure_liquidation(category="stale_scrape"):
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    failure = FailureState(category, True, 1, "coinglass_web", "failed")
    return LiquidationSignal(
        symbol="ETH/USDT",
        upper_accumulations=[],
        lower_accumulations=[],
        fetched_at=now,
        mapping_version="coinglass-free-heatmap-v1",
        validation=ScrapeValidationResult.failed("coinglass-free-heatmap-v1", "failed"),
        failure=failure,
    )


def valid_liquidation(now=None):
    now = now or datetime(2026, 6, 21, tzinfo=timezone.utc)
    return LiquidationSignal(
        symbol="ETH/USDT",
        upper_accumulations=[],
        lower_accumulations=[],
        fetched_at=now,
        latest_valid_scrape_at=now,
        mapping_version="coinglass-free-heatmap-v1",
        validation=ScrapeValidationResult.ok("coinglass-free-heatmap-v1", 1700.0, now),
        near_tie=True,
    )


def kline_fetcher(_config):
    return [
        KlineSignal(
            symbol="ETH/USDT",
            period="15M",
            last_closed_price=1700.0,
            key_level=1700.0,
            key_level_state="stood_above",
            volume_state="high_breakout",
            evidence="ok",
        )
    ]


def kline_fetcher_with_price(price):
    return lambda _config: [
        KlineSignal(
            symbol="ETH/USDT",
            period="1m",
            last_closed_price=price,
            key_level=price,
            key_level_state="near_level_unconfirmed",
            volume_state="normal",
            evidence=f"price {price}",
        )
    ]


def test_scheduler_skips_overlapping_cycle():
    state = SchedulerState(running=True)

    decision = run_once(config(), state=state, kline_fetcher=kline_fetcher, dry_run_email=True)

    assert decision.status == "no_trade"
    assert "Previous cycle" in decision.no_trade_reason


def test_scheduler_throttles_same_category_failure_notifications():
    sent = []
    state = SchedulerState()

    def send(report, _config):
        sent.append(report)

    for i in range(2):
        run_once(
            config(),
            state=state,
            now=datetime(2026, 6, 21, 10, i, tzinfo=timezone.utc),
            liquidation_fetcher=lambda *_args, **_kwargs: failure_liquidation(),
            kline_fetcher=kline_fetcher,
            email_sender=send,
        )

    assert len(sent) == 1
    assert "stale_scrape" in state.failure_notifications


def test_scheduler_reuses_fresh_liquidation_cache_for_fast_kline_cycles():
    calls = []
    state = SchedulerState()

    def liquidation_fetcher(_config, **kwargs):
        calls.append(kwargs.get("current_market_price"))
        return valid_liquidation(kwargs["now"])

    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 1, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1705.0),
        dry_run_email=True,
    )

    assert calls == [1700.0]


def test_scheduler_refreshes_liquidation_on_large_price_move():
    calls = []
    state = SchedulerState()

    def liquidation_fetcher(_config, **kwargs):
        calls.append(kwargs.get("current_market_price"))
        return valid_liquidation(kwargs["now"])

    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 1, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1720.0),
        dry_run_email=True,
    )

    assert calls == [1700.0, 1720.0]


def test_scheduler_price_move_refresh_respects_cooldown():
    calls = []
    state = SchedulerState()

    def liquidation_fetcher(_config, **kwargs):
        calls.append(kwargs.get("current_market_price"))
        return valid_liquidation(kwargs["now"])

    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 1, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1720.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 2, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1740.0),
        dry_run_email=True,
    )

    assert calls == [1700.0, 1720.0]


def test_scheduler_price_move_refresh_allowed_after_cooldown():
    calls = []
    state = SchedulerState()

    def liquidation_fetcher(_config, **kwargs):
        calls.append(kwargs.get("current_market_price"))
        return valid_liquidation(kwargs["now"])

    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 1, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1720.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 16, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1740.0),
        dry_run_email=True,
    )

    assert calls == [1700.0, 1720.0, 1740.0]


def test_scheduler_refreshes_liquidation_after_regular_interval():
    calls = []
    state = SchedulerState()

    def liquidation_fetcher(_config, **kwargs):
        calls.append(kwargs.get("now"))
        return valid_liquidation(kwargs["now"])

    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )
    run_once(
        config(),
        state=state,
        now=datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc),
        liquidation_fetcher=liquidation_fetcher,
        kline_fetcher=kline_fetcher_with_price(1700.0),
        dry_run_email=True,
    )

    assert len(calls) == 2
