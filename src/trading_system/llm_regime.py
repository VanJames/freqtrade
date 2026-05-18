from __future__ import annotations

import asyncio
import json
import os
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
    ) -> None:
        self.model = model
        self.provider = provider.lower()
        self.api_key_env = api_key_env or ("DEEPSEEK_API_KEY" if self.provider == "deepseek" else "OPENAI_API_KEY")
        self.base_url = base_url or ("https://api.deepseek.com" if self.provider == "deepseek" else None)
        self.knowledge_path = knowledge_path
        self.min_override_confidence = min_override_confidence
        self.enabled = enabled
        self._rules_cache: str | None = None

    async def review(self, item: RegimeReviewInput) -> RegimeReview:
        if not self.enabled:
            return self.default_review(item.rule_regime)
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
        return self.guardrail(item.rule_regime, review)

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
