try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class SampleStrategyPullbackShort(SampleStrategy):
    """
    Short-only pullback strategy for grinding downtrends.

    It targets weak rebounds into EMA20/EMA50 during a bearish higher-timeframe
    context instead of waiting for the stricter trend-breakdown signal used by
    SampleStrategy.
    """

    can_short = True
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

        bearish_1h = dataframe["trend_down_1h"] | (
            (dataframe["ema20_1h"] < dataframe["ema50_1h"])
            & (dataframe["rsi_1h"] < 54)
        )
        bearish_15m = dataframe["trend_down_15m"] | (
            (dataframe["ema20_15m"] < dataframe["ema50_15m"])
            & (dataframe["rsi_15m"] < 54)
        )
        pullback_zone = (
            (dataframe["close"] >= dataframe["ema20"] * 0.996)
            | (dataframe["high"] >= dataframe["ema20"] * 0.999)
            | (dataframe["close"] >= dataframe["ema50"] * 0.992)
        )
        ai_not_against = pd.Series(True, index=dataframe.index)
        if self._freqai_enabled() and "&-future_return" in dataframe.columns and "do_predict" in dataframe.columns:
            ai_not_against = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] <= 0.006)

        pullback_short = (
            bearish_1h
            & bearish_15m
            & pullback_zone
            & (dataframe["adx"] > 12)
            & (dataframe["volume_ratio"] > 0.70)
            & dataframe["rsi"].between(38, 64)
            & (dataframe["close"] < dataframe["ema200"])
            & ai_not_against
        )

        if self._new_entries_disabled():
            pullback_short = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        _, _, pullback_short, _ = self._apply_direction_advisor_gate(
            false_series,
            false_series,
            pullback_short,
            false_series,
        )

        dataframe.loc[pullback_short, ["enter_short", "enter_tag"]] = (1, "pullback_short")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
