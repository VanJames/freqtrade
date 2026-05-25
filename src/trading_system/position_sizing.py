from __future__ import annotations

from typing import Any, Protocol

from trading_system.models import PositionSide, Regime, TradeSignal


class SizingSettings(Protocol):
    max_signal_risk_multiplier: float
    confirmation_position_sizing: bool
    confirmation_max_risk_multiplier: float


def adjusted_risk_multiplier(signal: TradeSignal | dict[str, Any], settings: SizingSettings) -> float:
    base = max(0.0, min(raw_risk_multiplier(signal), settings.max_signal_risk_multiplier))
    throttle = risk_throttle(signal)
    if not settings.confirmation_position_sizing:
        return round(base * throttle, 4)

    score = opportunity_score(signal)
    regime = signal_value(signal, "regime")
    side = signal_value(signal, "position_side") or signal_value(signal, "side")
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
    if regime not in {
        Regime.TREND_LONG,
        Regime.TREND_SHORT,
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_DOWN,
        Regime.LIQUIDITY_SWEEP_REVERSAL,
    }:
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
    if regime == Regime.LIQUIDITY_SWEEP_REVERSAL:
        boosted *= 0.75
    if side == PositionSide.SHORT:
        boosted *= 0.92
    if symbol.startswith("SOL/"):
        boosted *= 0.85
    if symbol.startswith("SOL/") and side == PositionSide.SHORT and regime == Regime.SHOCK_TREND_DOWN:
        boosted *= 0.65
    boosted *= throttle

    cap = quality_risk_cap(signal, settings)
    floor = min(base * throttle, cap)
    return round(
        max(
            floor,
            min(boosted, settings.confirmation_max_risk_multiplier, settings.max_signal_risk_multiplier, cap),
        ),
        4,
    )


def raw_risk_multiplier(signal: TradeSignal | dict[str, Any]) -> float:
    try:
        return float(signal_metadata(signal).get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0


def risk_throttle(signal: TradeSignal | dict[str, Any]) -> float:
    try:
        return max(0.0, min(float(signal_metadata(signal).get("risk_throttle", 1.0)), 1.0))
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


def quality_risk_cap(signal: TradeSignal | dict[str, Any], settings: SizingSettings) -> float:
    cap = min(settings.confirmation_max_risk_multiplier, settings.max_signal_risk_multiplier)
    regime = signal_value(signal, "regime")
    side = signal_value(signal, "position_side") or signal_value(signal, "side")
    if regime == Regime.LIQUIDITY_SWEEP_REVERSAL:
        return max(0.1, min(cap, 1.2))
    if regime != Regime.SHOCK_TREND_DOWN or side != PositionSide.SHORT:
        if regime == Regime.SHOCK_TREND_UP and side == PositionSide.LONG:
            recent_position = metadata_float(signal, "recent_5h_position", 0.5)
            close_position = metadata_float(signal, "close_position_72h", 0.5)
            ret_24h = metadata_float(signal, "ret_24h", 0.0)
            ret_72h = metadata_float(signal, "ret_72h", 0.0)
            range_72h = metadata_float(signal, "range_72h", 1.0)
            mixed_trend = ret_24h * ret_72h < 0
            if mixed_trend:
                cap = min(cap, 1.5)
            if mixed_trend and close_position >= 0.75:
                cap = min(cap, 1.0)
            if mixed_trend and recent_position >= 0.85:
                cap = min(cap, 0.9)
            quiet_weak_up_drift = range_72h < 0.06 and ret_24h < 0.008
            if quiet_weak_up_drift and recent_position >= 0.90:
                cap = min(cap, 0.90)
            if quiet_weak_up_drift and ret_72h <= 0 and ret_24h < 0.006:
                cap = min(cap, 0.75)
            if quiet_weak_up_drift and close_position >= 0.80 and ret_24h < 0.006:
                cap = min(cap, 1.10)
            return max(0.1, cap)
        return cap

    symbol = str(signal_value(signal, "symbol") or "")
    if symbol.startswith(("SOL/", "XAU/")):
        cap = min(cap, 0.35)

    close_position = metadata_float(signal, "close_position_72h", 0.5)
    ret_72h = metadata_float(signal, "ret_72h", 0.0)
    ret_24h = metadata_float(signal, "ret_24h", 0.0)
    volatility_tier = str(signal_metadata(signal).get("volatility_tier", "NORMAL")).upper()
    mixed_trend = ret_24h * ret_72h < 0

    if mixed_trend:
        cap = min(cap, 0.75)

    if close_position < 0.20:
        cap = min(cap, 0.35)
    elif close_position < 0.30:
        cap = min(cap, 0.65)
    elif close_position < 0.35:
        cap = min(cap, 0.85)
    elif close_position < 0.45:
        cap = min(cap, 1.10)

    if close_position < 0.35 and ret_72h <= -0.05:
        cap *= 0.8
    if close_position < 0.35 and ret_24h <= -0.018:
        cap *= 0.85
    if volatility_tier == "HIGH":
        cap *= 0.9
    elif volatility_tier == "EXTREME":
        cap *= 0.75
    return max(0.1, cap)


def metadata_float(signal: TradeSignal | dict[str, Any], key: str, default: float) -> float:
    try:
        return float(signal_metadata(signal).get(key, default))
    except (TypeError, ValueError):
        return default


def signal_metadata(signal: TradeSignal | dict[str, Any]) -> dict[str, Any]:
    if isinstance(signal, dict):
        return signal
    return signal.metadata


def signal_value(signal: TradeSignal | dict[str, Any], key: str) -> Any:
    if isinstance(signal, dict):
        return signal.get(key)
    return getattr(signal, key)
