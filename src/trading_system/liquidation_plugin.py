from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from trading_system.models import PositionSide

try:  # The notice package is included as an optional runtime plugin boundary.
    from trading_notice.decision import decide_strategy
    from trading_notice.kline import _signal_from_candles
    from trading_notice.liquidation_scraper import ScrapeRuntimeState, scrape_liquidation_signal
    from trading_notice.models import (
        AnalysisConfiguration,
        KlineSignal,
        LiquidationSignal,
        PriceSanityBounds,
        StrategyDecision,
        ThresholdSet,
    )
except Exception:  # pragma: no cover - exercised when plugin deps are absent.
    AnalysisConfiguration = None  # type: ignore[assignment]
    KlineSignal = None  # type: ignore[assignment]
    LiquidationSignal = None  # type: ignore[assignment]
    PriceSanityBounds = None  # type: ignore[assignment]
    ScrapeRuntimeState = None  # type: ignore[assignment]
    StrategyDecision = None  # type: ignore[assignment]
    ThresholdSet = None  # type: ignore[assignment]
    decide_strategy = None  # type: ignore[assignment]
    scrape_liquidation_signal = None  # type: ignore[assignment]
    _signal_from_candles = None  # type: ignore[assignment]


PluginStatus = Literal["long", "short", "no_trade", "failure", "disabled"]


@dataclass(slots=True)
class LiquidationPluginResult:
    symbol: str
    status: PluginStatus
    decision: Any | None = None
    liquidation: Any | None = None
    weight: float = 0.65
    reason: str = ""
    checked_at: datetime | None = None

    @property
    def position_side(self) -> PositionSide | None:
        if self.status == "long":
            return PositionSide.LONG
        if self.status == "short":
            return PositionSide.SHORT
        return None

    def as_context(self) -> dict[str, Any]:
        decision = self.decision
        liquidation = self.liquidation
        strongest_upper = getattr(liquidation, "strongest_upper_candidate", None)
        strongest_lower = getattr(liquidation, "strongest_lower_candidate", None)
        return {
            "liquidation_plugin": {
                "status": self.status,
                "weight": self.weight,
                "reason": self.reason,
                "matched_scenario": getattr(decision, "matched_scenario", "none"),
                "suggested_entry_price": getattr(decision, "suggested_entry_price", None),
                "stop_loss": getattr(decision, "stop_loss", None),
                "target_take_profit": getattr(decision, "target_take_profit", None),
                "trigger_condition": getattr(decision, "trigger_condition", None),
                "predicted_liquidation_side": getattr(
                    decision, "predicted_liquidation_side", "none"
                ),
                "basis": getattr(decision, "basis", ""),
                "no_trade_reason": getattr(decision, "no_trade_reason", None),
                "mapping_version": getattr(liquidation, "mapping_version", None),
                "near_tie": bool(getattr(liquidation, "near_tie", False)),
                "upper_strength": getattr(strongest_upper, "strength", None),
                "upper_price_low": getattr(strongest_upper, "price_low", None),
                "upper_price_high": getattr(strongest_upper, "price_high", None),
                "lower_strength": getattr(strongest_lower, "strength", None),
                "lower_price_low": getattr(strongest_lower, "price_low", None),
                "lower_price_high": getattr(strongest_lower, "price_high", None),
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            }
        }


class LiquidationDirectionPlugin:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.weight = max(0.61, float(settings.liquidation_plugin_weight))
        self._states: dict[str, Any] = {}
        self._scrape_lock = asyncio.Lock()

    async def evaluate(
        self,
        symbol: str,
        *,
        current_price: float,
        candles_15m: list[list[float]],
        candles_1h: list[list[float]],
        candles_4h: list[list[float]],
        candles_1d: list[list[float]],
    ) -> LiquidationPluginResult:
        now = datetime.now(timezone.utc)
        if not self.settings.enable_liquidation_plugin:
            return LiquidationPluginResult(
                symbol=symbol,
                status="disabled",
                weight=self.weight,
                reason="liquidation plugin disabled",
                checked_at=now,
            )
        if AnalysisConfiguration is None or scrape_liquidation_signal is None:
            return LiquidationPluginResult(
                symbol=symbol,
                status="failure",
                weight=self.weight,
                reason="trading_notice package or plugin dependencies are unavailable",
                checked_at=now,
            )
        if not self.settings.coinglass_heatmap_url:
            return LiquidationPluginResult(
                symbol=symbol,
                status="failure",
                weight=self.weight,
                reason="coinglass_heatmap_url is required when liquidation plugin is enabled",
                checked_at=now,
            )

        config = self._config(symbol, current_price)
        klines = self._kline_signals(
            config,
            {
                "15m": candles_15m,
                "1h": candles_1h,
                "4h": candles_4h,
                "1d": candles_1d,
            },
        )
        state = self._states.setdefault(symbol, ScrapeRuntimeState())
        async with self._scrape_lock:
            liquidation = await asyncio.to_thread(
                scrape_liquidation_signal,
                config,
                now=now,
                state=state,
                current_market_price=current_price,
            )
        decision = decide_strategy(config, liquidation, klines, now.strftime("%Y%m%dT%H%M%SZ"))
        reason = getattr(decision, "basis", "") or getattr(decision, "no_trade_reason", "")
        return LiquidationPluginResult(
            symbol=symbol,
            status=decision.status,
            decision=decision,
            liquidation=liquidation,
            weight=self.weight,
            reason=reason,
            checked_at=now,
        )

    def _config(self, symbol: str, current_price: float) -> Any:
        thresholds = ThresholdSet(
            liquidation_sufficiency=float(
                self.settings.liquidation_plugin_liquidation_sufficiency
            ),
            upper_lower_near_tie=float(self.settings.liquidation_plugin_near_tie),
            stood_above_confirmation=float(self.settings.liquidation_plugin_stood_above),
            broke_below_confirmation=float(self.settings.liquidation_plugin_broke_below),
            volume_confirmation=float(self.settings.liquidation_plugin_volume_confirmation),
        )
        return AnalysisConfiguration(
            symbol=symbol,
            analysis_interval=str(self.settings.liquidation_plugin_analysis_interval),
            recipients=(),
            coinglass_heatmap_url=str(self.settings.coinglass_heatmap_url),
            coinglass_heatmap_ranges=tuple(self.settings.coinglass_heatmap_ranges),
            kline_exchange_id="okx",
            kline_periods=("15m", "1h", "4h", "1d"),
            scrape_min_interval_seconds=int(self.settings.liquidation_plugin_scrape_min_seconds),
            max_scrape_staleness_seconds=int(
                self.settings.liquidation_plugin_max_staleness_seconds
            ),
            liquidation_price_move_trigger=float(
                self.settings.liquidation_plugin_price_move_trigger
            ),
            liquidation_price_move_cooldown_seconds=int(
                self.settings.liquidation_plugin_price_move_cooldown_seconds
            ),
            price_sanity_bounds=PriceSanityBounds(
                max_relative_distance=float(
                    self.settings.liquidation_plugin_price_sanity_max_distance
                )
            ),
            thresholds=thresholds,
            coinglass_session_path=self.settings.coinglass_session_path or None,
            current_market_price=current_price,
            screenshot_dir=self.settings.coinglass_screenshot_dir or None,
        )

    @staticmethod
    def _kline_signals(config: Any, candles_by_period: dict[str, list[list[float]]]) -> list[Any]:
        signals: list[Any] = []
        for period, candles in candles_by_period.items():
            signals.append(_signal_from_candles(config, period, candles))
        return signals
