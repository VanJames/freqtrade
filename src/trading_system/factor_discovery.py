from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from trading_system.indicators import atr, ema, macd, rsi
from trading_system.models import MarketFeatures, PositionSide, Regime


FACTOR_COLUMNS = [
    "rsi_oversold",
    "rsi_overbought",
    "macd_crossover",
    "macd_crossunder",
    "bb_breakout",
    "support_resistance",
    "volume_spike",
    "mean_reversion",
    "trend_alignment",
]


@dataclass(slots=True)
class FactorMetrics:
    win_count: int = 0
    loss_count: int = 0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total else 0.0

    @property
    def expectancy(self) -> float:
        total = self.win_count + self.loss_count
        if total == 0:
            return 0.0
        return self.total_return / total


class AdaptiveFactorDiscovery:
    def __init__(self, window_days: int = 30) -> None:
        self.window_days = window_days
        self.factor_metrics: dict[str, FactorMetrics] = {}

    def register_factor(self, factor_name: str) -> None:
        self.factor_metrics.setdefault(factor_name, FactorMetrics())

    def update_factor_performance(
        self,
        factor_name: str,
        signal_triggered: bool,
        trade_return: float,
        drawdown: float,
    ) -> None:
        if not signal_triggered:
            return
        metrics = self.factor_metrics.setdefault(factor_name, FactorMetrics())
        if trade_return > 0:
            metrics.win_count += 1
        else:
            metrics.loss_count += 1
        metrics.total_return += trade_return
        metrics.max_drawdown = min(metrics.max_drawdown, drawdown)
        metrics.last_updated = datetime.now()

    def get_regime_factors(self, regime: Regime | str, volatility_level: str, atr_ratio: float) -> dict[str, float]:
        regime_value = regime.value if isinstance(regime, Regime) else str(regime)
        if volatility_level == "EXTREME" or atr_ratio > 0.02:
            weights = {
                "macd_crossover": 0.22,
                "macd_crossunder": 0.22,
                "bb_breakout": 0.18,
                "volume_spike": 0.18,
                "trend_alignment": 0.20,
            }
        elif volatility_level == "HIGH" or atr_ratio > 0.015:
            weights = {
                "macd_crossover": 0.18,
                "macd_crossunder": 0.18,
                "volume_spike": 0.22,
                "mean_reversion": 0.14,
                "trend_alignment": 0.28,
            }
        elif regime_value in {Regime.SHOCK.value, "RANGE_BOUND"}:
            weights = {
                "rsi_oversold": 0.18,
                "rsi_overbought": 0.18,
                "support_resistance": 0.24,
                "mean_reversion": 0.28,
                "volume_spike": 0.12,
            }
        else:
            weights = {
                "rsi_oversold": 0.12,
                "rsi_overbought": 0.12,
                "macd_crossover": 0.16,
                "macd_crossunder": 0.16,
                "support_resistance": 0.14,
                "volume_spike": 0.12,
                "trend_alignment": 0.18,
            }
        side_filtered = weights.copy()
        if regime_value in {Regime.TREND_LONG.value, Regime.SHOCK_TREND_UP.value}:
            side_filtered.pop("rsi_overbought", None)
            side_filtered.pop("macd_crossunder", None)
        if regime_value in {Regime.TREND_SHORT.value, Regime.SHOCK_TREND_DOWN.value}:
            side_filtered.pop("rsi_oversold", None)
            side_filtered.pop("macd_crossover", None)
        return normalize_weights(side_filtered)

    def calculate_composite_signal(
        self,
        factors_dict: dict[str, float],
        dataframe: pd.DataFrame,
        lookback_rows: int = 1,
    ) -> float:
        if dataframe.empty or not factors_dict:
            return 0.0
        index = max(0, len(dataframe) - lookback_rows)
        score = 0.0
        total_weight = 0.0
        for factor_name, weight in factors_dict.items():
            if factor_name not in dataframe.columns:
                continue
            value = dataframe[factor_name].iloc[index]
            try:
                factor_value = float(np.clip(value, 0.0, 1.0))
            except (TypeError, ValueError):
                continue
            score += factor_value * weight
            total_weight += weight
        return min(score / total_weight, 1.0) if total_weight else 0.0

    def learn_from_backtest(self, backtest_results: pd.DataFrame, factor_columns: list[str]) -> dict[str, float]:
        if backtest_results.empty or "profitable" not in backtest_results.columns:
            return {}
        valid = [column for column in factor_columns if column in backtest_results.columns]
        if not valid:
            return {}
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            return self._simple_correlation_analysis(backtest_results, valid)
        data = backtest_results[valid + ["profitable"]].fillna(0)
        if len(data) < 20 or data["profitable"].nunique() < 2:
            return self._simple_correlation_analysis(data, valid)
        model = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42)
        model.fit(data[valid].astype(float), data["profitable"].astype(int))
        return dict(sorted(zip(valid, model.feature_importances_), key=lambda item: item[1], reverse=True))

    def _simple_correlation_analysis(self, backtest_results: pd.DataFrame, factor_columns: list[str]) -> dict[str, float]:
        correlations: dict[str, float] = {}
        target = pd.to_numeric(backtest_results["profitable"], errors="coerce")
        for column in factor_columns:
            series = pd.to_numeric(backtest_results[column], errors="coerce")
            corr = series.corr(target)
            correlations[column] = 0.0 if pd.isna(corr) else abs(float(corr))
        return dict(sorted(correlations.items(), key=lambda item: item[1], reverse=True))

    def get_top_factors(self, importance_dict: dict[str, float], top_n: int = 5) -> dict[str, float]:
        return normalize_weights(dict(list(importance_dict.items())[:top_n]))

    def get_factor_stats(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "factor": name,
                    "win_rate": metrics.win_rate,
                    "expectancy": metrics.expectancy,
                    "total_return": metrics.total_return,
                    "max_drawdown": metrics.max_drawdown,
                    "last_updated": metrics.last_updated,
                }
                for name, metrics in self.factor_metrics.items()
            ]
        )


def add_factor_columns(frame: pd.DataFrame, side: PositionSide | None = None) -> pd.DataFrame:
    result = frame.copy()
    if len(result) < 30:
        for column in FACTOR_COLUMNS:
            result[column] = 0.0
        return result
    close = result["close"]
    high = result["high"]
    low = result["low"]
    volume = result["volume"]
    rsi_14 = rsi(close, 14)
    macd_line, signal_line, _ = macd(close)
    ema20 = ema(close, 20)
    ema60 = ema(close, 60)
    mid = close.rolling(20).mean()
    std = close.rolling(20).std().fillna(0)
    upper = mid + 2 * std
    lower = mid - 2 * std
    width = (upper - lower).replace(0, np.nan)
    recent_low = low.rolling(20).min()
    recent_high = high.rolling(20).max()
    avg_volume = volume.rolling(20).mean().replace(0, np.nan)
    result["rsi_oversold"] = ((35 - rsi_14) / 35).clip(0, 1)
    result["rsi_overbought"] = ((rsi_14 - 65) / 35).clip(0, 1)
    result["macd_crossover"] = ((macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))).astype(float)
    result["macd_crossunder"] = ((macd_line < signal_line) & (macd_line.shift(1) >= signal_line.shift(1))).astype(float)
    result["bb_breakout"] = ((close - mid).abs() / width).clip(0, 1).fillna(0)
    support_distance = ((close - recent_low).abs() / close.replace(0, np.nan)).fillna(1)
    resistance_distance = ((recent_high - close).abs() / close.replace(0, np.nan)).fillna(1)
    result["support_resistance"] = (1 - (pd.concat([support_distance, resistance_distance], axis=1).min(axis=1) / 0.02)).clip(0, 1)
    result["volume_spike"] = ((volume / avg_volume - 1.2) / 1.8).clip(0, 1).fillna(0)
    result["mean_reversion"] = ((close - mid).abs() / (close * 0.03).replace(0, np.nan)).clip(0, 1).fillna(0)
    if side == PositionSide.SHORT:
        trend = ((ema60 - ema20) / close.replace(0, np.nan) / 0.01).clip(0, 1)
    elif side == PositionSide.LONG:
        trend = ((ema20 - ema60) / close.replace(0, np.nan) / 0.01).clip(0, 1)
    else:
        trend = ((ema20 - ema60).abs() / close.replace(0, np.nan) / 0.01).clip(0, 1)
    result["trend_alignment"] = trend.fillna(0)
    return result.fillna(0)


def factor_snapshot_from_features(features: MarketFeatures, side: PositionSide) -> dict[str, float]:
    atr_ratio = features.atr_1h / features.close_1h if features.close_1h else 0.0
    trend_long = features.ema20_1h >= features.ema60_1h
    return {
        "atr_ratio": atr_ratio,
        "rsi_oversold": max(0.0, min(1.0, (35 - features.rsi_1h) / 35)),
        "rsi_overbought": max(0.0, min(1.0, (features.rsi_1h - 65) / 35)),
        "trend_alignment": float(trend_long if side == PositionSide.LONG else not trend_long),
        "range_edge": abs(features.close_position_72h - 0.5) * 2,
        "momentum_24h": abs(features.ret_24h),
        "momentum_72h": abs(features.ret_72h),
    }


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in weights.values())
    if total <= 0:
        return {}
    return {key: max(0.0, float(value)) / total for key, value in weights.items()}

