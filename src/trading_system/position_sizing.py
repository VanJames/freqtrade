from __future__ import annotations

from typing import Any, Protocol

from trading_system.models import PositionSide, Regime, TradeSignal


class SizingSettings(Protocol):
    max_signal_risk_multiplier: float
    confirmation_position_sizing: bool
    confirmation_max_risk_multiplier: float


def adjusted_risk_multiplier(signal: TradeSignal | dict[str, Any], settings: SizingSettings) -> float:
    base = max(0.0, min(raw_risk_multiplier(signal), settings.max_signal_risk_multiplier))
    if not settings.confirmation_position_sizing:
        return base

    score = opportunity_score(signal)
    regime = signal_value(signal, "regime")
    side = signal_value(signal, "side")
    symbol = str(signal_value(signal, "symbol") or "")
    reasons = opportunity_reasons(signal)
    required = {
        "multi_timeframe",
        "one_hour_trend",
        "pullback",
        "confirmation_candle",
        "momentum_cross",
        "momentum_positive",
        "not_chasing",
    }
    required_hits = len(required & reasons)
    if regime not in {Regime.TREND_LONG, Regime.TREND_SHORT, Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN}:
        return base

    boosted = base
    if score >= 98 and required_hits >= 7 and "rr_good" in reasons:
        boosted = 3.0
    elif score >= 95 and required_hits >= 6:
        boosted = 1.8
    elif score >= 88 and required_hits >= 5:
        boosted = max(base, 1.0)

    if regime in {Regime.TREND_SHORT, Regime.SHOCK_TREND_DOWN}:
        boosted *= 0.82
    if side == PositionSide.SHORT:
        boosted *= 0.92
    if symbol.startswith("SOL/"):
        boosted *= 0.85

    return round(max(base, min(boosted, settings.confirmation_max_risk_multiplier, settings.max_signal_risk_multiplier)), 4)


def raw_risk_multiplier(signal: TradeSignal | dict[str, Any]) -> float:
    try:
        return float(signal_metadata(signal).get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0


def opportunity_score(signal: TradeSignal | dict[str, Any]) -> int:
    try:
        return int(signal_metadata(signal).get("opportunity_score", 0) or 0)
    except (TypeError, ValueError):
        return 0


def opportunity_reasons(signal: TradeSignal | dict[str, Any]) -> set[str]:
    return {
        item.strip()
        for item in str(signal_metadata(signal).get("opportunity_reasons", "")).split(",")
        if item.strip()
    }


def signal_metadata(signal: TradeSignal | dict[str, Any]) -> dict[str, Any]:
    if isinstance(signal, dict):
        return signal
    return signal.metadata


def signal_value(signal: TradeSignal | dict[str, Any], key: str) -> Any:
    if isinstance(signal, dict):
        return signal.get(key)
    return getattr(signal, key)
