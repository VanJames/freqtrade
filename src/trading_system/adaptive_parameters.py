from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_system.indicators import atr, ema


@dataclass(frozen=True, slots=True)
class MarketCondition:
    volatility: float
    trend_strength: float
    momentum: float
    regime: str
    time_of_day: str = "GENERAL"


class AdaptiveParameterTuner:
    def __init__(self) -> None:
        self.base_parameters: dict[str, float] = {
            "risk_multiplier_scale": 1.0,
            "take_profit_scale": 1.0,
            "stop_loss_scale": 1.0,
            "max_open_trades": 4.0,
            "atr_multiplier": 1.0,
        }
        self.parameter_ranges: dict[str, tuple[float, float]] = {
            "risk_multiplier_scale": (0.25, 1.50),
            "take_profit_scale": (0.75, 1.80),
            "stop_loss_scale": (0.75, 1.60),
            "max_open_trades": (1.0, 8.0),
            "atr_multiplier": (0.75, 1.50),
        }

    def analyze_market_condition(self, dataframe: pd.DataFrame) -> MarketCondition | None:
        if len(dataframe) < 30:
            return None
        close = dataframe["close"]
        volatility = float((atr(dataframe["high"], dataframe["low"], close, 14) / close.replace(0, pd.NA)).iloc[-1] or 0.0)
        ema9 = ema(close, 9)
        ema21 = ema(close, 21)
        trend_strength = float(abs(ema9.iloc[-1] - ema21.iloc[-1]) / close.iloc[-1]) if close.iloc[-1] else 0.0
        momentum = float((close.iloc[-1] - close.iloc[-20]) / close.iloc[-20]) if close.iloc[-20] else 0.0
        if volatility > 0.02:
            regime = "VOLATILE"
        elif trend_strength > 0.01 and abs(momentum) > 0.01:
            regime = "TREND"
        elif volatility < 0.005:
            regime = "LOW_VOL"
        else:
            regime = "RANGE"
        return MarketCondition(volatility=volatility, trend_strength=trend_strength, momentum=momentum, regime=regime)

    def adjust_parameters(self, condition: MarketCondition) -> dict[str, float]:
        params = self.base_parameters.copy()
        if condition.regime == "VOLATILE":
            params.update(
                risk_multiplier_scale=0.55,
                take_profit_scale=0.90,
                stop_loss_scale=1.25,
                max_open_trades=2.0,
                atr_multiplier=1.30,
            )
        elif condition.regime == "TREND":
            params.update(
                risk_multiplier_scale=1.15,
                take_profit_scale=1.25,
                stop_loss_scale=1.10,
                max_open_trades=5.0,
                atr_multiplier=1.10,
            )
        elif condition.regime == "RANGE":
            params.update(
                risk_multiplier_scale=0.80,
                take_profit_scale=0.85,
                stop_loss_scale=1.15,
                max_open_trades=4.0,
                atr_multiplier=1.00,
            )
        else:
            params.update(risk_multiplier_scale=0.65, max_open_trades=2.0)
        return params

    def validate_parameters(self, params: dict[str, Any]) -> bool:
        for key, value in params.items():
            if key not in self.parameter_ranges:
                continue
            low, high = self.parameter_ranges[key]
            if not low <= float(value) <= high:
                return False
        return True

    def generate_parameter_report(self, condition: MarketCondition, adjusted_params: dict[str, Any]) -> str:
        lines = [
            "# 参数自适应调整报告",
            "",
            "## 市场条件",
            f"- 波动率: `{condition.volatility:.4f}`",
            f"- 趋势强度: `{condition.trend_strength:.4f}`",
            f"- 动量: `{condition.momentum:.4f}`",
            f"- 行情模式: `{condition.regime}`",
            "",
            "## 参数调整",
            "",
            "| 参数 | 原始值 | 调整值 |",
            "|---|---:|---:|",
        ]
        for key, value in adjusted_params.items():
            lines.append(f"| {key} | {self.base_parameters.get(key, 0):.4f} | {float(value):.4f} |")
        return "\n".join(lines) + "\n"

