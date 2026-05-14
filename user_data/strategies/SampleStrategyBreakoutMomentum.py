try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class SampleStrategyBreakoutMomentum(SampleStrategy):
    """
    Breakout strategy for volatility expansion.

    It trades fresh 5m range breaks only when volume and higher-timeframe
    context agree, complementing the pullback and mean-reversion candidates.
    """

    can_short = True
    minimal_roi = {
        "0": 0.030,
        "25": 0.016,
        "70": 0.006,
        "150": 0.0,
    }
    stoploss = -0.060
    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.026
    trailing_only_offset_is_reached = False

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        recent_high = dataframe["high"].rolling(36).max().shift(1)
        recent_low = dataframe["low"].rolling(36).min().shift(1)
        volatility_expanding = (
            (dataframe["volatility_ratio"] > 1.08)
            | (dataframe["bb_width"] > dataframe["bb_width_sma"] * 1.10)
        )
        volume_breakout = dataframe["volume_ratio"] > 1.35
        ai_long_ok = pd.Series(True, index=dataframe.index)
        ai_short_ok = pd.Series(True, index=dataframe.index)
        if self._freqai_enabled() and "&-future_return" in dataframe.columns and "do_predict" in dataframe.columns:
            ai_long_ok = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] >= -0.003)
            ai_short_ok = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] <= 0.003)

        breakout_long = (
            (dataframe["trend_up_1h"] | dataframe["trend_up_15m"])
            & volatility_expanding
            & volume_breakout
            & (dataframe["close"] > recent_high)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["rsi"] > 54)
            & (dataframe["macdhist"] > 0)
            & ai_long_ok
        )
        breakout_short = (
            (dataframe["trend_down_1h"] | dataframe["trend_down_15m"])
            & volatility_expanding
            & volume_breakout
            & (dataframe["close"] < recent_low)
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["rsi"] < 46)
            & (dataframe["macdhist"] < 0)
            & ai_short_ok
        )

        if self._new_entries_disabled():
            breakout_long = pd.Series(False, index=dataframe.index)
            breakout_short = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        breakout_long, _, breakout_short, _ = self._apply_direction_advisor_gate(
            breakout_long,
            false_series,
            breakout_short,
            false_series,
        )

        dataframe.loc[breakout_long, ["enter_long", "enter_tag"]] = (1, "breakout_long")
        dataframe.loc[breakout_short, ["enter_short", "enter_tag"]] = (1, "breakout_short")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
