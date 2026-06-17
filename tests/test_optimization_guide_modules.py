from __future__ import annotations

import pandas as pd

from trading_system.adaptive_parameters import AdaptiveParameterTuner
from trading_system.expectancy_optimizer import ExpectancyOptimizer
from trading_system.factor_discovery import AdaptiveFactorDiscovery, FACTOR_COLUMNS, add_factor_columns
from trading_system.ml_factor_selector import MLFactorSelector
from trading_system.multi_timeframe_fusion import MultiTimeframeFusion


def sample_frame(rows: int = 80) -> pd.DataFrame:
    prices = [100 + index * 0.1 for index in range(rows)]
    return pd.DataFrame(
        {
            "ts": [index * 300_000 for index in range(rows)],
            "open": prices,
            "high": [price + 0.2 for price in prices],
            "low": [price - 0.2 for price in prices],
            "close": [price + 0.05 for price in prices],
            "volume": [10 + index % 7 for index in range(rows)],
        }
    )


def test_factor_discovery_adds_expected_columns_and_scores() -> None:
    discovery = AdaptiveFactorDiscovery()
    frame = add_factor_columns(sample_frame())
    weights = discovery.get_regime_factors("TREND_LONG", "NORMAL", 0.01)
    score = discovery.calculate_composite_signal(weights, frame)

    assert set(FACTOR_COLUMNS).issubset(frame.columns)
    assert 0.0 <= score <= 1.0


def test_multi_timeframe_fusion_returns_structured_signal() -> None:
    fusion = MultiTimeframeFusion()
    signal = fusion.generate_fused_signal(sample_frame(), sample_frame(90), sample_frame(60))

    assert signal.signal in {"BUY", "SELL", "HOLD"}
    assert 0.0 <= signal.confidence <= 1.0
    assert "4h_direction" in signal.timeframe_details


def test_adaptive_parameter_tuner_adjusts_and_validates() -> None:
    tuner = AdaptiveParameterTuner()
    condition = tuner.analyze_market_condition(sample_frame())

    assert condition is not None
    params = tuner.adjust_parameters(condition)
    assert tuner.validate_parameters(params)


def test_expectancy_optimizer_metrics() -> None:
    trades = pd.DataFrame({"pnl": [10.0, -4.0, 3.0]})
    metrics = ExpectancyOptimizer.calculate_metrics(trades)

    assert metrics["total_trades"] == 3.0
    assert metrics["expectancy"] == 3.0
    assert metrics["profit_factor"] > 1.0


def test_ml_factor_selector_uses_correlation_fallback_for_small_samples() -> None:
    selector = MLFactorSelector(model_path="/tmp/nonexistent_factor_model.json")
    frame = pd.DataFrame(
        {
            "risk_multiplier": [0.2, 0.8, 0.3, 1.0],
            "entry_ret_24h": [0.01, -0.02, 0.015, -0.03],
            "profitable": [1, 0, 1, 0],
        }
    )
    importance = selector.train_factor_importance(frame, ["risk_multiplier", "entry_ret_24h"])

    assert set(importance) == {"risk_multiplier", "entry_ret_24h"}

