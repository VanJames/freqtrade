from __future__ import annotations

from trading_system.llm_regime import LLMRegimeReviewer, RegimeReview
from trading_system.models import Regime


def test_low_confidence_override_is_rejected() -> None:
    reviewer = LLMRegimeReviewer(enabled=False)
    review = RegimeReview(
        action="OVERRIDE",
        proposed_regime=Regime.SHOCK_TREND_UP,
        confidence=0.5,
        allow_trade=True,
        risk_multiplier=1.0,
        reasons=[],
        missing_evidence=[],
    )

    guarded = reviewer.guardrail(Regime.SHOCK, review)

    assert guarded.action == "KEEP"
    assert guarded.proposed_regime == Regime.SHOCK
    assert guarded.allow_trade


def test_trend_short_requires_high_confidence() -> None:
    reviewer = LLMRegimeReviewer(enabled=False)
    review = RegimeReview(
        action="OVERRIDE",
        proposed_regime=Regime.TREND_SHORT,
        confidence=0.75,
        allow_trade=True,
        risk_multiplier=1.0,
        reasons=[],
        missing_evidence=[],
    )

    guarded = reviewer.guardrail(Regime.SHOCK_TREND_DOWN, review)

    assert guarded.proposed_regime == Regime.SHOCK_TREND_DOWN
    assert not guarded.allow_trade
    assert guarded.risk_multiplier == 0.5


def test_deepseek_provider_defaults() -> None:
    reviewer = LLMRegimeReviewer(provider="deepseek", model="deepseek-chat")

    assert reviewer.provider == "deepseek"
    assert reviewer.api_key_env == "DEEPSEEK_API_KEY"
    assert reviewer.base_url == "https://api.deepseek.com"


def test_literal_api_key_is_supported_without_env_lookup() -> None:
    reviewer = LLMRegimeReviewer(provider="deepseek", api_key_env="sk-test")

    assert reviewer.api_key() == "sk-test"
