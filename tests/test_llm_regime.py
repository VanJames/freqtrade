from __future__ import annotations

import pytest

from trading_system.llm_regime import LLMRegimeReviewer, RegimeReview
from trading_system.llm_regime import RegimeReviewInput
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


def test_direct_api_key_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    reviewer = LLMRegimeReviewer(api_key="sk-direct", api_key_env="OPENAI_API_KEY")

    assert reviewer.api_key() == "sk-direct"


def test_llm_cache_key_ignores_small_price_noise() -> None:
    reviewer = LLMRegimeReviewer(enabled=True)
    base_features = {
        "adx": 24.24,
        "plus_di": 28.11,
        "minus_di": 18.19,
        "atr_pct": 0.01123,
        "range_amplitude_4h": 0.08221,
        "ema20_1h": 100.0,
        "ema60_1h": 95.0,
        "range_high_4h": 120.0,
        "range_low_4h": 80.0,
        "close_1h": 104.0,
        "volatility_tier": "HIGH",
    }

    first = reviewer.cache_key(
        RegimeReviewInput("BTC/USDT:USDT", Regime.SHOCK_TREND_UP, base_features)
    )
    second_features = {**base_features, "close_1h": 104.2}
    second = reviewer.cache_key(
        RegimeReviewInput("BTC/USDT:USDT", Regime.SHOCK_TREND_UP, second_features)
    )

    assert first == second


@pytest.mark.asyncio
async def test_llm_review_is_globally_rate_limited(monkeypatch) -> None:
    reviewer = LLMRegimeReviewer(enabled=True, min_interval_seconds=900, cache_ttl_seconds=0)
    calls = 0

    def fake_call_model(item: RegimeReviewInput) -> RegimeReview:
        nonlocal calls
        calls += 1
        return RegimeReview(
            action="KEEP",
            proposed_regime=item.rule_regime,
            confidence=0.9,
            allow_trade=True,
            risk_multiplier=1.0,
            reasons=["tested"],
            missing_evidence=[],
        )

    monkeypatch.setattr(reviewer, "_call_model", fake_call_model)

    features = {
        "adx": 30,
        "plus_di": 25,
        "minus_di": 20,
        "atr_pct": 0.01,
        "range_amplitude_4h": 0.08,
        "ema20_1h": 100,
        "ema60_1h": 95,
        "range_high_4h": 120,
        "range_low_4h": 80,
        "close_1h": 105,
        "volatility_tier": "HIGH",
    }
    first = RegimeReviewInput("BTC/USDT:USDT", Regime.SHOCK_TREND_UP, features)
    second = RegimeReviewInput("ETH/USDT:USDT", Regime.SHOCK_TREND_UP, {**features, "close_1h": 106})

    assert (await reviewer.review(first)).reasons == ["tested"]
    assert (await reviewer.review(second)).reasons == ["llm_rate_limited"]
    assert calls == 1
