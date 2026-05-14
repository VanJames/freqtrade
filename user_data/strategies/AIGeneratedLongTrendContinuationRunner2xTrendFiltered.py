try:
    from user_data.strategies.AIGeneratedLongTrendContinuationRunner2x import (
        AIGeneratedLongTrendContinuationRunner2x,
    )
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuationRunner2x import AIGeneratedLongTrendContinuationRunner2x

import pandas as pd


class AIGeneratedLongTrendContinuationRunner2xTrendFiltered(
    AIGeneratedLongTrendContinuationRunner2x
):
    """
    Runner2x variant with stricter trend-quality filters.

    Goal:
    - Keep the same overall structure and risk model
    - Reduce weak long continuation entries that later decay into a single
      small stop/exit loss
    """

    ai_candidate = {
        **AIGeneratedLongTrendContinuationRunner2x.ai_candidate,
        "class_name": "AIGeneratedLongTrendContinuationRunner2xTrendFiltered",
        "reason": "Trend-filtered continuation variant with stronger multi-timeframe alignment.",
        "adx_min": 24.0,
        "volume_min": 0.95,
        "rsi_long_max": 46,
    }

    minimal_roi = {"0": 0.03, "45": 0.012, "150": 0.0}
    stoploss = -0.04
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.025

    def populate_entry_trend(self, dataframe, metadata):
        dataframe = super().populate_entry_trend(dataframe, metadata)

        stronger_long_context = (
            dataframe["trend_up_1h"]
            & dataframe["trend_up_15m"]
            & (dataframe["close"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
            & (dataframe["macd"] > dataframe["macdsignal"])
            & (dataframe["volume_ratio"] > 0.95)
            & (dataframe["volatility_ratio"] > 0.95)
            & (dataframe["rsi"] > 42)
            & (dataframe["rsi"] < 58)
        )

        dataframe.loc[~stronger_long_context, "enter_long"] = 0
        long_tag = dataframe.get("enter_tag")
        if long_tag is not None:
            dataframe.loc[dataframe["enter_long"] == 1, "enter_tag"] = "ai_trend_long_filtered"

        dataframe["enter_short"] = 0
        return dataframe
