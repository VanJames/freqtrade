from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from trading_system.models import PositionSide, Regime


CHECK_WEIGHTS = {
    "regime_direction": 18,
    "multi_timeframe": 16,
    "one_hour_trend": 12,
    "four_hour_trend": 10,
    "pullback": 8,
    "confirmation_candle": 8,
    "momentum_cross": 12,
    "momentum_positive": 7,
    "price_location": 8,
    "ema_slope": 6,
    "rsi_quality": 7,
    "not_chasing": 6,
    "range_position": 5,
    "liquidity_sweep": 18,
    "reclaim": 18,
    "volume_spike": 14,
    "pin_bar": 16,
}

PENALTY_WEIGHTS = {
    "directional_conflict": 12,
    "rsi_extreme": 12,
    "overextended": 10,
    "counter_ema": 10,
    "extreme_volatility": 8,
}


@dataclass(frozen=True, slots=True)
class Opportunity:
    score: int
    grade: str
    confidence: float
    risk_multiplier: float
    allow_trade: bool
    reasons: list[str]


def score_opportunity(
    *,
    symbol: str,
    regime: Regime,
    side: PositionSide,
    checks: Mapping[str, bool],
    penalties: Mapping[str, bool] | None = None,
    reward_risk: float = 0.0,
    volatility_tier: str = "NORMAL",
    min_score: int | None = None,
) -> Opportunity:
    raw_score = 0
    reasons: list[str] = []
    for name, passed in checks.items():
        if not passed:
            continue
        weight = CHECK_WEIGHTS.get(name, 0)
        raw_score += weight
        if weight:
            reasons.append(name)

    if reward_risk >= 2.4:
        raw_score += 8
        reasons.append("rr_excellent")
    elif reward_risk >= 1.8:
        raw_score += 5
        reasons.append("rr_good")
    elif reward_risk >= 1.3:
        raw_score += 2
        reasons.append("rr_acceptable")

    for name, active in (penalties or {}).items():
        if not active:
            continue
        raw_score -= PENALTY_WEIGHTS.get(name, 0)
        reasons.append(f"penalty:{name}")

    score = max(0, min(100, int(round(raw_score / 1.3))))
    threshold = min_score if min_score is not None else default_min_score(regime)
    grade = grade_for_score(score)
    return Opportunity(
        score=score,
        grade=grade,
        confidence=score / 100,
        risk_multiplier=risk_multiplier_for(symbol, regime, side, score, volatility_tier),
        allow_trade=score >= threshold,
        reasons=reasons,
    )


def default_min_score(regime: Regime) -> int:
    if regime in {Regime.TREND_LONG, Regime.TREND_SHORT}:
        return 68
    if regime in {Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN}:
        return 70
    if regime == Regime.SHOCK:
        return 64
    return 100


def grade_for_score(score: int) -> str:
    if score >= 88:
        return "A"
    if score >= 78:
        return "B"
    if score >= 68:
        return "C"
    if score >= 58:
        return "D"
    return "F"


def risk_multiplier_for(
    symbol: str,
    regime: Regime,
    side: PositionSide,
    score: int,
    volatility_tier: str,
) -> float:
    if score >= 88:
        level = "A"
    elif score >= 78:
        level = "B"
    elif score >= 68:
        level = "C"
    else:
        level = "D"

    by_regime = {
        Regime.TREND_LONG: {"A": 0.75, "B": 0.58, "C": 0.42, "D": 0.20},
        Regime.TREND_SHORT: {"A": 0.58, "B": 0.44, "C": 0.30, "D": 0.15},
        Regime.SHOCK_TREND_UP: {"A": 0.42, "B": 0.30, "C": 0.18, "D": 0.08},
        Regime.SHOCK_TREND_DOWN: {"A": 0.36, "B": 0.25, "C": 0.16, "D": 0.08},
        Regime.LIQUIDITY_SWEEP_REVERSAL: {"A": 0.72, "B": 0.50, "C": 0.28, "D": 0.10},
        Regime.SHOCK: {"A": 0.58, "B": 0.42, "C": 0.28, "D": 0.14},
    }
    multiplier = by_regime.get(regime, {"D": 0.0}).get(level, 0.0)

    if volatility_tier == "HIGH":
        multiplier *= 0.80
    elif volatility_tier == "EXTREME":
        multiplier *= 0.55

    if symbol.startswith("SOL/"):
        multiplier *= 0.78
    if side == PositionSide.SHORT:
        multiplier *= 0.90
    if symbol.startswith("SOL/") and side == PositionSide.SHORT and regime == Regime.SHOCK_TREND_DOWN:
        multiplier *= 0.65

    return round(max(0.0, min(multiplier, 1.0)), 4)


def opportunity_metadata(opportunity: Opportunity) -> dict[str, object]:
    return {
        "opportunity_score": opportunity.score,
        "opportunity_grade": opportunity.grade,
        "opportunity_confidence": opportunity.confidence,
        "risk_multiplier": opportunity.risk_multiplier,
        "opportunity_reasons": ",".join(opportunity.reasons),
    }


def sol_trade_allowed(
    *,
    symbol: str,
    side: PositionSide,
    regime: Regime,
    score: int,
    close_position_72h: float,
    ret_24h: float,
    ret_72h: float,
    range_72h: float,
) -> bool:
    if not symbol.startswith("SOL/"):
        return True
    if regime in {Regime.TREND_LONG, Regime.TREND_SHORT} and score < 96:
        return False
    if range_72h >= 0.16:
        return False
    if side == PositionSide.LONG:
        if close_position_72h >= 0.88:
            return False
        if close_position_72h >= 0.76 and ret_24h < 0.006:
            return False
        if ret_24h < -0.018 and ret_72h < 0.0:
            return False
    else:
        if regime == Regime.SHOCK_TREND_DOWN and score < 98:
            return False
        if close_position_72h <= 0.18:
            return False
        if close_position_72h <= 0.30 and ret_24h > -0.010:
            return False
        if regime == Regime.SHOCK_TREND_DOWN and ret_24h > -0.003:
            return False
        if ret_24h > 0.018 and ret_72h > 0.0:
            return False
    return True


def sol_structure_stop(
    symbol: str,
    side: PositionSide,
    price: float,
    stop: float,
    max_distance_pct: float = 0.016,
) -> float:
    if not symbol.startswith("SOL/"):
        return stop
    distance = price * max_distance_pct
    if side == PositionSide.LONG:
        return min(stop, price - distance)
    return max(stop, price + distance)
