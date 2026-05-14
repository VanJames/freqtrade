try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class SampleStrategyRangeMeanReversion(SampleStrategy):
    """
    Range strategy for sideways markets.

    It fades Bollinger-band extensions only when the market is not trending
    strongly. This fills the gap where trend and pullback strategies stay idle.
    """

    can_short = True
    minimal_roi = {
        "0": 0.014,
        "18": 0.008,
        "45": 0.003,
        "90": 0.0,
    }
    stoploss = -0.035
    trailing_stop = True
    trailing_stop_positive = 0.006
    trailing_stop_positive_offset = 0.012
    trailing_only_offset_is_reached = False

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        range_context = (
            dataframe["range_market_1h"]
            | (
                (dataframe["adx"] < 22)
                & (dataframe["adx_15m"] < 24)
                & (dataframe["volatility_ratio"] < 1.25)
            )
        )
        volume_ok = dataframe["volume_ratio"].between(0.45, 2.80)
        ai_long_ok = pd.Series(True, index=dataframe.index)
        ai_short_ok = pd.Series(True, index=dataframe.index)
        if self._freqai_enabled() and "&-future_return" in dataframe.columns and "do_predict" in dataframe.columns:
            ai_long_ok = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] >= -0.008)
            ai_short_ok = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] <= 0.008)

        range_long = (
            range_context
            & volume_ok
            & (
                (dataframe["close"] <= dataframe["bb_lowerband"] * 1.002)
                | (dataframe["low"] <= dataframe["bb_lowerband"])
            )
            & (dataframe["rsi"] < 38)
            & ai_long_ok
        )
        range_short = (
            range_context
            & volume_ok
            & (
                (dataframe["close"] >= dataframe["bb_upperband"] * 0.998)
                | (dataframe["high"] >= dataframe["bb_upperband"])
            )
            & (dataframe["rsi"] > 62)
            & ai_short_ok
        )

        if self._new_entries_disabled():
            range_long = pd.Series(False, index=dataframe.index)
            range_short = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        range_long, _, range_short, _ = self._apply_direction_advisor_gate(
            range_long,
            false_series,
            range_short,
            false_series,
        )

        dataframe.loc[range_long, ["enter_long", "enter_tag"]] = (1, "range_long")
        dataframe.loc[range_short, ["enter_short", "enter_tag"]] = (1, "range_short")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
