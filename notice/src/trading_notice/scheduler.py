"""Cycle orchestration and recurrence helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from trading_notice.decision import decide_strategy
from trading_notice.emailer import (
    build_failure_notification,
    build_strategy_email,
    send_email,
)
from trading_notice.kline import analyze_kline_signals
from trading_notice.liquidation_scraper import ScrapeRuntimeState, scrape_liquidation_signal
from trading_notice.models import (
    AnalysisConfiguration,
    FailureNotification,
    KlineSignal,
    LiquidationSignal,
    StrategyDecision,
)


@dataclass
class SchedulerState:
    running: bool = False
    last_decision_key: tuple[str, str, float | None] | None = None
    failure_notifications: dict[str, FailureNotification] = field(default_factory=dict)
    scrape_state: ScrapeRuntimeState = field(default_factory=ScrapeRuntimeState)
    last_liquidation_reference_price: float | None = None
    last_price_move_refresh_at: datetime | None = None


def run_once(
    config: AnalysisConfiguration,
    *,
    state: SchedulerState | None = None,
    cycle_id: str | None = None,
    now: datetime | None = None,
    dry_run_email: bool = False,
    liquidation_fetcher: Callable[..., object] = scrape_liquidation_signal,
    kline_fetcher: Callable[..., object] = analyze_kline_signals,
    email_sender: Callable[..., object] = send_email,
) -> StrategyDecision:
    state = state or SchedulerState()
    now = now or datetime.now(timezone.utc)
    cycle_id = cycle_id or now.strftime("%Y%m%dT%H%M%SZ")
    if state.running:
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="no_trade",
            no_trade_reason="Previous cycle still running",
            basis="Skipped overlapping cycle",
        )
    state.running = True
    try:
        klines = kline_fetcher(config)
        market_price = _latest_kline_price(klines)
        liquidation = _liquidation_for_cycle(
            config,
            state,
            now,
            market_price,
            liquidation_fetcher,
        )
        decision = decide_strategy(config, liquidation, klines, cycle_id)
        if decision.status in {"long", "short"}:
            key = (decision.status, decision.matched_scenario, decision.target_take_profit)
            if key != state.last_decision_key:
                report = build_strategy_email(decision, config)
                if not dry_run_email:
                    email_sender(report, config)
                state.last_decision_key = key
        elif decision.status == "failure":
            notification = build_failure_notification(decision, config)
            prior = state.failure_notifications.get(notification.failure_category)
            should_send = prior is None or (
                prior.cooldown_expires_at is not None and prior.cooldown_expires_at <= now
            )
            if should_send:
                if not dry_run_email:
                    email_sender(notification, config)
                from dataclasses import replace

                state.failure_notifications[notification.failure_category] = replace(
                    notification,
                    cooldown_expires_at=now + timedelta(seconds=config.failure_cooldown_seconds),
                )
        else:
            state.failure_notifications.clear()
        return decision
    finally:
        state.running = False


def run_forever(
    config: AnalysisConfiguration, *, dry_run_email: bool = False
) -> None:  # pragma: no cover - operational loop
    import time

    state = SchedulerState()
    while True:
        now = datetime.now(timezone.utc)
        print(f"notice_cycle_start cycle_id={now.strftime('%Y%m%dT%H%M%SZ')}", flush=True)
        try:
            decision = run_once(config, state=state, now=now, dry_run_email=dry_run_email)
        except Exception as exc:
            print(
                f"notice_cycle_error error_type={type(exc).__name__} message={str(exc)[:240]}",
                flush=True,
            )
        else:
            parts = [
                f"cycle_status={decision.status}",
                f"symbol={decision.symbol}",
                f"scenario={decision.matched_scenario}",
            ]
            if decision.target_take_profit is not None:
                parts.append(f"target_take_profit={decision.target_take_profit:.8f}")
            if decision.suggested_entry_price is not None:
                parts.append(f"suggested_entry_price={decision.suggested_entry_price:.8f}")
            if decision.stop_loss is not None:
                parts.append(f"stop_loss={decision.stop_loss:.8f}")
            if decision.no_trade_reason:
                parts.append(f"no_trade_reason={decision.no_trade_reason}")
            if decision.failure:
                parts.append(f"failure_category={decision.failure.category}")
                parts.append(f"failure_message={decision.failure.safe_message}")
            print(" ".join(parts), flush=True)
        print(
            f"notice_sleep seconds={config.kline_check_interval_seconds}",
            flush=True,
        )
        time.sleep(config.kline_check_interval_seconds)


def _liquidation_for_cycle(
    config: AnalysisConfiguration,
    state: SchedulerState,
    now: datetime,
    market_price: float | None,
    liquidation_fetcher: Callable[..., object],
) -> LiquidationSignal:
    refresh_reason = _liquidation_refresh_reason(config, state, now, market_price)
    if refresh_reason is None:
        cached = state.scrape_state.latest_valid_signal
        if cached is not None:
            return cached

    liquidation = liquidation_fetcher(
        config,
        now=now,
        state=state.scrape_state,
        current_market_price=market_price,
    )
    if not isinstance(liquidation, LiquidationSignal):
        raise TypeError("liquidation_fetcher must return LiquidationSignal")
    if liquidation.failure is None:
        state.scrape_state.latest_valid_signal = liquidation
        state.scrape_state.latest_valid_scrape_at = liquidation.latest_valid_scrape_at
        state.scrape_state.last_scrape_attempt_at = now
        if market_price is not None:
            state.last_liquidation_reference_price = market_price
        if refresh_reason == "price_move":
            state.last_price_move_refresh_at = now
    return liquidation


def _liquidation_refresh_reason(
    config: AnalysisConfiguration,
    state: SchedulerState,
    now: datetime,
    market_price: float | None,
) -> str | None:
    latest_scrape_at = state.scrape_state.latest_valid_scrape_at
    if state.scrape_state.latest_valid_signal is None or latest_scrape_at is None:
        return "initial"
    if (now - latest_scrape_at).total_seconds() >= config.scrape_min_interval_seconds:
        return "interval"
    reference_price = state.last_liquidation_reference_price
    if market_price is None or reference_price is None or reference_price <= 0:
        return None
    relative_move = abs(market_price - reference_price) / reference_price
    if relative_move < config.liquidation_price_move_trigger:
        return None
    last_price_move_at = state.last_price_move_refresh_at
    if (
        last_price_move_at is not None
        and (now - last_price_move_at).total_seconds()
        < config.liquidation_price_move_cooldown_seconds
    ):
        return None
    return "price_move"


def _latest_kline_price(klines: object) -> float | None:
    if not isinstance(klines, list):
        return None
    for preferred in ("1m", "1M", "15m", "15M", "1h", "1H"):
        for item in klines:
            if (
                isinstance(item, KlineSignal)
                and item.period == preferred
                and item.last_closed_price is not None
            ):
                return item.last_closed_price
    for item in klines:
        if isinstance(item, KlineSignal) and item.last_closed_price is not None:
            return item.last_closed_price
    return None
