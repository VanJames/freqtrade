from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_system.factor_discovery import add_factor_columns
from trading_system.indicators import ema, rsi


@dataclass(frozen=True, slots=True)
class FusedSignal:
    signal: str
    confidence: float
    reason: str
    timeframe_details: dict[str, Any]


class MultiTimeframeFusion:
    def get_4h_trend_direction(self, dataframe_4h: pd.DataFrame) -> str:
        if len(dataframe_4h) < 21:
            return "NEUTRAL"
        close = dataframe_4h["close"]
        ema9 = ema(close, 9)
        ema21 = ema(close, 21)
        latest_close = float(close.iloc[-1])
        latest_long = float(ema21.iloc[-1])
        if latest_long <= 0:
            return "NEUTRAL"
        trend_score = (latest_close - latest_long) / latest_long
        if trend_score > 0.05 and ema9.iloc[-1] > ema21.iloc[-1]:
            return "STRONG_UP"
        if trend_score > 0.015:
            return "UP"
        if trend_score < -0.05 and ema9.iloc[-1] < ema21.iloc[-1]:
            return "STRONG_DOWN"
        if trend_score < -0.015:
            return "DOWN"
        return "NEUTRAL"

    def get_1h_entry_signals(self, dataframe_1h: pd.DataFrame, direction: str = "LONG") -> dict[str, float]:
        if len(dataframe_1h) < 30:
            return {"oversold_signal": 0.0, "support_signal": 0.0, "momentum_signal": 0.0, "composite": 0.0}
        enriched = add_factor_columns(dataframe_1h)
        latest = enriched.iloc[-1]
        rsi_latest = float(rsi(dataframe_1h["close"], 14).iloc[-1])
        if direction == "SHORT":
            oversold_signal = max(0.0, (rsi_latest - 65) / 35)
            support_signal = float(latest["support_resistance"])
            momentum_signal = float(latest["macd_crossunder"] or latest["trend_alignment"])
        else:
            oversold_signal = max(0.0, (35 - rsi_latest) / 35)
            support_signal = float(latest["support_resistance"])
            momentum_signal = float(latest["macd_crossover"] or latest["trend_alignment"])
        composite = oversold_signal * 0.30 + support_signal * 0.35 + min(momentum_signal, 1.0) * 0.35
        return {
            "oversold_signal": max(0.0, min(1.0, oversold_signal)),
            "support_signal": max(0.0, min(1.0, support_signal)),
            "momentum_signal": max(0.0, min(1.0, momentum_signal)),
            "composite": max(0.0, min(1.0, composite)),
        }

    def get_15m_confirmation(self, dataframe_15m: pd.DataFrame, direction: str = "LONG") -> bool:
        if len(dataframe_15m) < 24:
            return False
        recent = dataframe_15m.iloc[-24:]
        price = float(recent.close.iloc[-1])
        volume_sma = float(recent.volume.rolling(20).mean().iloc[-1] or 0.0)
        volume_ok = volume_sma > 0 and float(recent.volume.iloc[-1]) > volume_sma * 1.25
        if direction == "SHORT":
            breakout_ok = price < float(recent.low.iloc[:-1].min()) * 0.999
            impulse_ok = price < float(recent.close.iloc[-4])
        else:
            breakout_ok = price > float(recent.high.iloc[:-1].max()) * 1.001
            impulse_ok = price > float(recent.close.iloc[-4])
        return bool((breakout_ok or volume_ok) and impulse_ok)

    def generate_fused_signal(
        self,
        dataframe_1h: pd.DataFrame,
        dataframe_4h: pd.DataFrame,
        dataframe_15m: pd.DataFrame | None = None,
        allow_short: bool = True,
    ) -> FusedSignal:
        if dataframe_1h.empty or dataframe_4h.empty:
            return FusedSignal("HOLD", 0.0, "insufficient_data", {})
        direction_4h = self.get_4h_trend_direction(dataframe_4h)
        short_direction = direction_4h in {"STRONG_DOWN", "DOWN"}
        direction = "SHORT" if short_direction else "LONG"
        details: dict[str, Any] = {"4h_direction": direction_4h}
        signals_1h = self.get_1h_entry_signals(dataframe_1h, direction)
        details["1h_signals"] = signals_1h
        confirmation = True
        if dataframe_15m is not None:
            confirmation = self.get_15m_confirmation(dataframe_15m, direction)
            details["15m_confirmation"] = confirmation
        if direction_4h in {"STRONG_UP", "UP"} and signals_1h["composite"] >= 0.50 and confirmation:
            return FusedSignal("BUY", signals_1h["composite"], f"4h:{direction_4h}+1h:{signals_1h['composite']:.2f}", details)
        if allow_short and short_direction and signals_1h["composite"] >= 0.50 and confirmation:
            return FusedSignal("SELL", signals_1h["composite"], f"4h:{direction_4h}+1h:{signals_1h['composite']:.2f}", details)
        if direction_4h == "NEUTRAL" and signals_1h["support_signal"] >= 0.70:
            return FusedSignal("BUY", signals_1h["support_signal"] * 0.8, "range_support_bounce", details)
        return FusedSignal("HOLD", signals_1h["composite"], "no_fused_edge", details)

    def detect_signal_conflict(self, signals_1h: dict[str, Any], signals_4h: dict[str, Any]) -> bool:
        return (
            signals_1h.get("direction") == "UP" and signals_4h.get("direction") == "DOWN"
        ) or (
            signals_1h.get("direction") == "DOWN" and signals_4h.get("direction") == "UP"
        )

