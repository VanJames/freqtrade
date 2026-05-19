from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from trading_system.models import Regime


ReviewAction = Literal["KEEP", "OVERRIDE", "DOWNGRADE_TO_SHOCK", "BLOCK_TRADE"]


class RegimeReview(BaseModel):
    action: ReviewAction
    proposed_regime: Regime
    confidence: float = Field(ge=0.0, le=1.0)
    allow_trade: bool
    risk_multiplier: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    missing_evidence: list[str]


@dataclass(slots=True)
class RegimeReviewInput:
    symbol: str
    rule_regime: Regime
    features: dict[str, Any]


@dataclass(slots=True)
class CachedRegimeReview:
    review: RegimeReview
    created_at: float


class LLMRegimeReviewer:
    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        provider: str = "openai",
        api_key_env: str | None = None,
        base_url: str | None = None,
        knowledge_path: Path = Path("knowledge/regime_rules.md"),
        min_override_confidence: float = 0.72,
        enabled: bool = False,
        cache_ttl_seconds: int = 900,
        min_interval_seconds: int = 120,
    ) -> None:
        self.model = model
        self.provider = provider.lower()
        self.api_key_env = api_key_env or ("DEEPSEEK_API_KEY" if self.provider == "deepseek" else "OPENAI_API_KEY")
        self.base_url = base_url or ("https://api.deepseek.com" if self.provider == "deepseek" else None)
        self.knowledge_path = knowledge_path
        self.min_override_confidence = min_override_confidence
        self.enabled = enabled
        self.cache_ttl_seconds = cache_ttl_seconds
        self.min_interval_seconds = min_interval_seconds
        self._rules_cache: str | None = None
        self._review_cache: dict[str, CachedRegimeReview] = {}
        self._last_call_at_by_symbol: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def review(self, item: RegimeReviewInput) -> RegimeReview:
        if not self.enabled:
            return self.default_review(item.rule_regime)
        cache_key = self.cache_key(item)
        now = time.monotonic()
        async with self._lock:
            cached = self._review_cache.get(cache_key)
            if cached and now - cached.created_at <= self.cache_ttl_seconds:
                return self.cached_review(cached.review)
            last_call_at = self._last_call_at_by_symbol.get(item.symbol, 0.0)
            if now - last_call_at < self.min_interval_seconds:
                return RegimeReview(
                    action="KEEP",
                    proposed_regime=item.rule_regime,
                    confidence=1.0,
                    allow_trade=True,
                    risk_multiplier=1.0,
                    reasons=["llm_rate_limited"],
                    missing_evidence=[],
                )
            self._last_call_at_by_symbol[item.symbol] = now
        try:
            review = await asyncio.to_thread(self._call_model, item)
        except Exception as exc:
            return RegimeReview(
                action="KEEP",
                proposed_regime=item.rule_regime,
                confidence=0.0,
                allow_trade=True,
                risk_multiplier=1.0,
                reasons=[f"llm_review_failed:{type(exc).__name__}"],
                missing_evidence=[],
            )
        guarded = self.guardrail(item.rule_regime, review)
        async with self._lock:
            self._review_cache[cache_key] = CachedRegimeReview(review=guarded, created_at=time.monotonic())
        return guarded

    def default_review(self, regime: Regime) -> RegimeReview:
        return RegimeReview(
            action="KEEP",
            proposed_regime=regime,
            confidence=1.0,
            allow_trade=True,
            risk_multiplier=1.0,
            reasons=["llm_disabled"],
            missing_evidence=[],
        )

    def guardrail(self, rule_regime: Regime, review: RegimeReview) -> RegimeReview:
        if review.action == "OVERRIDE" and review.confidence < self.min_override_confidence:
            review.action = "KEEP"
            review.proposed_regime = rule_regime
            review.reasons.append("override_rejected_low_confidence")
        if review.action == "DOWNGRADE_TO_SHOCK":
            review.proposed_regime = Regime.SHOCK
        if review.action == "BLOCK_TRADE":
            review.proposed_regime = rule_regime
            review.allow_trade = False
        if review.proposed_regime == Regime.TREND_SHORT and review.confidence < 0.80:
            review.action = "KEEP" if rule_regime != Regime.TREND_SHORT else "BLOCK_TRADE"
            review.proposed_regime = rule_regime
            review.allow_trade = False
            review.risk_multiplier = min(review.risk_multiplier, 0.5)
            review.reasons.append("trend_short_requires_high_confidence")
        return review

    def cached_review(self, review: RegimeReview) -> RegimeReview:
        reasons = [*review.reasons, "llm_cached"]
        return review.model_copy(update={"reasons": reasons})

    def cache_key(self, item: RegimeReviewInput) -> str:
        features = item.features
        fingerprint = {
            "symbol": item.symbol,
            "rule_regime": item.rule_regime.value,
            "volatility_tier": features.get("volatility_tier"),
            "adx": self.round_float(features.get("adx"), 1),
            "plus_di": self.round_float(features.get("plus_di"), 1),
            "minus_di": self.round_float(features.get("minus_di"), 1),
            "atr_pct": self.round_float(features.get("atr_pct"), 4),
            "range_amplitude_4h": self.round_float(features.get("range_amplitude_4h"), 4),
            "ema_state": self.ema_state(features),
            "range_position": self.range_position(features),
        }
        return json.dumps(fingerprint, sort_keys=True, ensure_ascii=True)

    @staticmethod
    def round_float(value: Any, digits: int) -> float | None:
        try:
            return round(float(value), digits)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def ema_state(features: dict[str, Any]) -> str:
        close = features.get("close_1h")
        ema20 = features.get("ema20_1h")
        ema60 = features.get("ema60_1h")
        try:
            close_f = float(close)
            ema20_f = float(ema20)
            ema60_f = float(ema60)
        except (TypeError, ValueError):
            return "unknown"
        if close_f >= ema20_f >= ema60_f:
            return "bull"
        if close_f <= ema20_f <= ema60_f:
            return "bear"
        return "mixed"

    @staticmethod
    def range_position(features: dict[str, Any]) -> int | None:
        try:
            close = float(features.get("close_1h"))
            high = float(features.get("range_high_4h"))
            low = float(features.get("range_low_4h"))
        except (TypeError, ValueError):
            return None
        if high <= low:
            return None
        return max(0, min(10, int(((close - low) / (high - low)) * 10)))

    def _call_model(self, item: RegimeReviewInput) -> RegimeReview:
        if self.provider == "deepseek":
            return self._call_deepseek(item)
        return self._call_openai(item)

    def _call_openai(self, item: RegimeReviewInput) -> RegimeReview:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key(), base_url=self.base_url)
        response = client.responses.parse(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a constrained market-regime reviewer. "
                        "Use only the provided rule knowledge base and numeric evidence. "
                        "Return structured JSON only. Do not invent missing market data."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "knowledge_base": self.rules_text(),
                            "symbol": item.symbol,
                            "rule_regime": item.rule_regime.value,
                            "features": item.features,
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            text_format=RegimeReview,
        )
        return response.output_parsed

    def _call_deepseek(self, item: RegimeReviewInput) -> RegimeReview:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key(), base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a constrained market-regime reviewer. "
                        "Use only the provided rule knowledge base and numeric evidence. "
                        "Return one valid JSON object matching this schema: "
                        f"{json.dumps(RegimeReview.model_json_schema(), ensure_ascii=True)}. "
                        "Do not wrap it in markdown. Do not invent missing market data."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "knowledge_base": self.rules_text(),
                            "symbol": item.symbol,
                            "rule_regime": item.rule_regime.value,
                            "features": item.features,
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("empty_deepseek_response")
        return RegimeReview.model_validate_json(content)

    def rules_text(self) -> str:
        if self._rules_cache is None:
            self._rules_cache = self.knowledge_path.read_text(encoding="utf-8")
        return self._rules_cache

    def api_key(self) -> str | None:
        if self.api_key_env.startswith(("sk-", "sk_")):
            return self.api_key_env
        return os.getenv(self.api_key_env)
