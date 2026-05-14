try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class SampleStrategyPullbackLong(SampleStrategy):
    """
    Long-only pullback strategy for grinding uptrends.

    It is the mirror candidate of SampleStrategyPullbackShort, used when the
    selector detects an uptrend with orderly pullbacks into EMA20/EMA50.
    """

    can_short = False
    minimal_roi = {
        "0": 0.018,
        "20": 0.010,
        "45": 0.004,
        "90": 0.0,
    }
    stoploss = -0.045
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.016
    trailing_only_offset_is_reached = False

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        bullish_1h = dataframe["trend_up_1h"] | (
            (dataframe["ema20_1h"] > dataframe["ema50_1h"])
            & (dataframe["rsi_1h"] > 46)
        )
        bullish_15m = dataframe["trend_up_15m"] | (
            (dataframe["ema20_15m"] > dataframe["ema50_15m"])
            & (dataframe["rsi_15m"] > 46)
        )
        pullback_zone = (
            (dataframe["close"] <= dataframe["ema20"] * 1.004)
            | (dataframe["low"] <= dataframe["ema20"] * 1.001)
            | (dataframe["close"] <= dataframe["ema50"] * 1.008)
        )
        ai_not_against = pd.Series(True, index=dataframe.index)
        if self._freqai_enabled() and "&-future_return" in dataframe.columns and "do_predict" in dataframe.columns:
            ai_not_against = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] >= -0.006)

        pullback_long = (
            bullish_1h
            & bullish_15m
            & pullback_zone
            & (dataframe["adx"] > 12)
            & (dataframe["volume_ratio"] > 0.70)
            & dataframe["rsi"].between(36, 62)
            & (dataframe["close"] > dataframe["ema200"])
            & ai_not_against
        )

        if self._new_entries_disabled():
            pullback_long = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        pullback_long, _, _, _ = self._apply_direction_advisor_gate(
            pullback_long,
            false_series,
            false_series,
            false_series,
        )

        dataframe.loc[pullback_long, ["enter_long", "enter_tag"]] = (1, "pullback_long")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        return dataframe
