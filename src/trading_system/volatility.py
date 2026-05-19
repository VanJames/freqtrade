from __future__ import annotations

from dataclasses import dataclass

from trading_system.models import Regime


@dataclass(slots=True)
class VolatilityPolicy:
    tier: str
    atr_pct: float
    ret_24h: float
    range_24h: float
    range_72h: float
    min_stop_loss_pct: float
    stop_buffer_atr: float
    breakout_retrace_atr: float
    require_multi_timeframe: bool
    require_near_breakout: bool
    risk_multiplier: float
    llm_review_required: bool


def build_volatility_policy(
    price: float,
    atr_1h: float,
    base_min_stop_loss_pct: float,
    high_vol_min_stop_loss_pct: float,
    extreme_vol_min_stop_loss_pct: float,
    ret_24h: float = 0.0,
    range_24h: float = 0.0,
    range_72h: float = 0.0,
) -> VolatilityPolicy:
    atr_pct = atr_1h / price if price else 0.0
    high_vol = atr_pct >= 0.0045 or abs(ret_24h) >= 0.025 or range_24h >= 0.035 or range_72h >= 0.070
    extreme_vol = atr_pct >= 0.0075 or abs(ret_24h) >= 0.045 or range_24h >= 0.055 or range_72h >= 0.100

    if extreme_vol:
        return VolatilityPolicy(
            tier="EXTREME",
            atr_pct=atr_pct,
            ret_24h=ret_24h,
            range_24h=range_24h,
            range_72h=range_72h,
            min_stop_loss_pct=max(base_min_stop_loss_pct, extreme_vol_min_stop_loss_pct),
            stop_buffer_atr=0.35,
            breakout_retrace_atr=0.50,
            require_multi_timeframe=True,
            require_near_breakout=True,
            risk_multiplier=0.50,
            llm_review_required=True,
        )
    if high_vol:
        return VolatilityPolicy(
            tier="HIGH",
            atr_pct=atr_pct,
            ret_24h=ret_24h,
            range_24h=range_24h,
            range_72h=range_72h,
            min_stop_loss_pct=max(base_min_stop_loss_pct, high_vol_min_stop_loss_pct),
            stop_buffer_atr=0.20,
            breakout_retrace_atr=0.80,
            require_multi_timeframe=False,
            require_near_breakout=True,
            risk_multiplier=0.70,
            llm_review_required=True,
        )
    return VolatilityPolicy(
        tier="NORMAL",
        atr_pct=atr_pct,
        ret_24h=ret_24h,
        range_24h=range_24h,
        range_72h=range_72h,
        min_stop_loss_pct=base_min_stop_loss_pct,
        stop_buffer_atr=0.0,
        breakout_retrace_atr=0.0,
        require_multi_timeframe=False,
        require_near_breakout=False,
        risk_multiplier=1.0,
        llm_review_required=False,
    )


def needs_llm_review(policy: VolatilityPolicy, regime: Regime) -> bool:
    return policy.llm_review_required and regime in {
        Regime.TREND_LONG,
        Regime.TREND_SHORT,
        Regime.SHOCK_TREND_UP,
        Regime.SHOCK_TREND_DOWN,
    }
