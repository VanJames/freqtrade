"""Explainable rule-state decision logic."""

from __future__ import annotations

from trading_notice.models import AnalysisConfiguration, KlineSignal, LiquidationSignal, StrategyDecision


def decide_strategy(
    config: AnalysisConfiguration,
    liquidation: LiquidationSignal,
    klines: list[KlineSignal],
    cycle_id: str,
) -> StrategyDecision:
    if liquidation.failure is not None:
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="failure",
            basis=liquidation.failure.safe_message,
            failure=liquidation.failure,
        )
    failed_kline = next((item for item in klines if item.failure is not None), None)
    if failed_kline is not None:
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="failure",
            basis=failed_kline.failure.safe_message,
            failure=failed_kline.failure,
        )
    if liquidation.near_tie:
        return _no_trade(cycle_id, config.symbol, "Upper/lower liquidation candidates are near-tied")

    kline = _preferred_kline(klines)
    if kline is None or kline.volume_state == "insufficient":
        return _no_trade(cycle_id, config.symbol, "K-line confirmation is insufficient")

    upper = liquidation.strongest_upper_candidate
    lower = liquidation.strongest_lower_candidate
    threshold = config.thresholds.liquidation_sufficiency
    short_blocker = _higher_timeframe_blocker(klines, "short")
    long_blocker = _higher_timeframe_blocker(klines, "long")

    if lower and lower.strength >= threshold and kline.key_level_state == "broke_below":
        if short_blocker is not None:
            return _no_trade(
                cycle_id,
                config.symbol,
                f"Short blocked by higher timeframe: {short_blocker.evidence}",
            )
        target = lower.price_low
        entry = _entry_price(kline)
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="short",
            matched_scenario="A_breakdown_short",
            trigger_condition=f"Price broke below {kline.key_level:.2f}",
            suggested_entry_price=entry,
            stop_loss=_short_stop_loss(config, kline, upper, entry),
            target_take_profit=target,
            predicted_liquidation_side="longs",
            basis=_basis("Scenario A", liquidation, kline),
        )
    if (
        upper
        and upper.strength >= threshold
        and kline.key_level_state == "stalled_at_resistance"
        and kline.volume_state == "low_rebound"
    ):
        if short_blocker is not None:
            return _no_trade(
                cycle_id,
                config.symbol,
                f"Short blocked by higher timeframe: {short_blocker.evidence}",
        )
        target = lower.price_low if lower else kline.key_level or upper.price_low
        entry = _entry_price(kline)
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="short",
            matched_scenario="B_rebound_trap_short",
            trigger_condition=f"Price stalled at resistance {kline.key_level:.2f} on low volume",
            suggested_entry_price=entry,
            stop_loss=_short_stop_loss(config, kline, upper, entry),
            target_take_profit=target,
            predicted_liquidation_side="longs",
            basis=_basis("Scenario B", liquidation, kline),
        )
    if (
        upper
        and upper.strength >= threshold
        and kline.key_level_state == "stood_above"
        and kline.volume_state == "high_breakout"
    ):
        if long_blocker is not None:
            return _no_trade(
                cycle_id,
                config.symbol,
                f"Long blocked by higher timeframe: {long_blocker.evidence}",
            )
        entry = _entry_price(kline)
        return StrategyDecision(
            cycle_id=cycle_id,
            symbol=config.symbol,
            status="long",
            matched_scenario="C_confirmed_long",
            trigger_condition=f"Price stood above {kline.key_level:.2f} with volume confirmation",
            suggested_entry_price=entry,
            stop_loss=_long_stop_loss(config, kline, lower, entry),
            target_take_profit=upper.price_high,
            predicted_liquidation_side="shorts",
            basis=_basis("Scenario C", liquidation, kline),
        )
    return _no_trade(cycle_id, config.symbol, "Liquidation and K-line evidence did not confirm a scenario")


def _preferred_kline(klines: list[KlineSignal]) -> KlineSignal | None:
    for period in ("15M", "15m", "1H", "1h"):
        for item in klines:
            if item.period == period:
                return item
    return klines[0] if klines else None


def _higher_timeframe_blocker(klines: list[KlineSignal], direction: str) -> KlineSignal | None:
    higher = [item for item in klines if item.period.lower() in {"4h", "1d"}]
    for item in higher:
        period = item.period.lower()
        if direction == "short" and _is_strong_higher_timeframe_long(item, period):
            return item
        if direction == "long" and _is_strong_higher_timeframe_short(item, period):
            return item
    return None


def _is_strong_higher_timeframe_long(kline: KlineSignal, period: str) -> bool:
    stood_above = kline.key_level_state == "stood_above"
    high_breakout = kline.volume_state == "high_breakout"
    above_ma25 = "above_ma25" in kline.ma_position
    if period == "1d" and stood_above:
        return True
    return stood_above and (high_breakout or above_ma25)


def _is_strong_higher_timeframe_short(kline: KlineSignal, period: str) -> bool:
    broke_below = kline.key_level_state == "broke_below"
    high_breakdown = kline.volume_state == "high_breakdown"
    below_ma25 = "below_ma25" in kline.ma_position
    if period == "1d" and broke_below:
        return True
    return broke_below and (high_breakdown or below_ma25)


def _basis(label: str, liquidation: LiquidationSignal, kline: KlineSignal) -> str:
    upper = liquidation.strongest_upper_candidate
    lower = liquidation.strongest_lower_candidate
    parts = [label, kline.evidence]
    if upper:
        parts.append(
            f"upper_short {upper.price_low:.2f}-{upper.price_high:.2f} strength {upper.strength:.2f}"
        )
    if lower:
        parts.append(
            f"lower_long {lower.price_low:.2f}-{lower.price_high:.2f} strength {lower.strength:.2f}"
        )
    parts.append(f"mapping {liquidation.mapping_version}")
    return "; ".join(parts)


def _entry_price(kline: KlineSignal) -> float:
    return float(kline.last_closed_price or kline.key_level or 0.0)


def _short_stop_loss(
    config: AnalysisConfiguration,
    kline: KlineSignal,
    upper,
    entry: float,
) -> float:
    buffer = config.thresholds.broke_below_confirmation
    candidates = []
    if kline.key_level is not None:
        candidates.append(kline.key_level * (1 + buffer))
    if upper is not None and upper.price_high > entry:
        candidates.append(upper.price_high)
    candidates.append(entry * (1 + max(buffer, 0.001)))
    return max(item for item in candidates if item > entry)


def _long_stop_loss(
    config: AnalysisConfiguration,
    kline: KlineSignal,
    lower,
    entry: float,
) -> float:
    buffer = config.thresholds.stood_above_confirmation
    candidates = []
    if kline.key_level is not None:
        candidates.append(kline.key_level * (1 - buffer))
    if lower is not None and lower.price_low < entry:
        candidates.append(lower.price_low)
    candidates.append(entry * (1 - max(buffer, 0.001)))
    return min(item for item in candidates if 0 < item < entry)


def _no_trade(cycle_id: str, symbol: str, reason: str) -> StrategyDecision:
    return StrategyDecision(
        cycle_id=cycle_id,
        symbol=symbol,
        status="no_trade",
        matched_scenario="none",
        predicted_liquidation_side="none",
        no_trade_reason=reason,
        basis=reason,
    )
