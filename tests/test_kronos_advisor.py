import pandas as pd

from scripts import kronos_advisor


def frame_from_closes(closes):
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        }
    )


def test_heuristic_forecast_detects_short_bias():
    frame = frame_from_closes([100 - i * 0.2 for i in range(120)])

    forecast = kronos_advisor.heuristic_forecast("BTC/USDT:USDT", frame, pred_len=24)

    assert forecast.side_bias == "short"
    assert forecast.pred_return_pct < 0
    assert forecast.confidence > 0


def test_summarize_market_counts_strong_biases():
    forecasts = [
        kronos_advisor.PairForecast("A", 1, 0, 1, 0.1, "long", 0.7, "heuristic", ""),
        kronos_advisor.PairForecast("B", -1, -1, 0, 0.1, "short", 0.8, "heuristic", ""),
        kronos_advisor.PairForecast("C", 0, 0, 0, 0.1, "hold", 0.1, "heuristic", ""),
    ]

    summary = kronos_advisor.summarize_market(forecasts, confidence_threshold=0.55)

    assert summary["long_count"] == 1
    assert summary["short_count"] == 1
    assert summary["hold_count"] == 1
    assert summary["market_bias"] == "hold"


def test_summarize_prediction_outputs_bias():
    pred = frame_from_closes([100, 100.5, 101.0, 101.5])

    forecast = kronos_advisor._summarize_prediction("BTC/USDT:USDT", pred, "kronos")

    assert forecast.source == "kronos"
    assert forecast.pred_return_pct > 0
