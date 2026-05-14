try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class AIGeneratedAltLongMomentum2x(SampleStrategy):
    """
    Altcoin long-momentum candidate for short-term trend bursts.

    This is intentionally limited to liquid non-core OKX swaps.  The core
    BTC/ETH/SOL profile is already covered by AIGeneratedLongTrendContinuation.
    """

    can_short = False
    minimal_roi = {
        "0": 0.020,
        "18": 0.010,
        "50": 0.004,
        "120": 0.0,
    }
    stoploss = -0.035
    trailing_stop = True
    trailing_stop_positive = 0.009
    trailing_stop_positive_offset = 0.020
    trailing_only_offset_is_reached = False

    alt_pairs = {
        "XRP/USDT:USDT",
        "BCH/USDT:USDT",
        "LINK/USDT:USDT",
        "DOGE/USDT:USDT",
        "XLM/USDT:USDT",
        "UNI/USDT:USDT",
    }

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return min(2.0, max_leverage)

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        pair = metadata.get("pair", "")
        if pair not in self.alt_pairs:
            return dataframe

        bullish_1h = dataframe["trend_up_1h"] | (
            (dataframe["ema20_1h"] > dataframe["ema50_1h"])
            & (dataframe["rsi_1h"] > 45)
        )
        bullish_15m = dataframe["trend_up_15m"] | (
            (dataframe["ema20_15m"] > dataframe["ema50_15m"])
            & (dataframe["rsi_15m"] > 45)
        )

        recent_high = dataframe["high"].rolling(24).max().shift(1)
        recent_low = dataframe["low"].rolling(18).min().shift(1)
        range_pct = (recent_high - recent_low) / dataframe["close"]
        range_expanding = (
            (dataframe["volatility_ratio"] > 0.92)
            | (dataframe["bb_width"] > dataframe["bb_width_sma"] * 0.96)
            | (range_pct > 0.006)
        )
        reclaim_ema20 = (
            (dataframe["low"] <= dataframe["ema20"] * 1.006)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["close"] > dataframe["open"])
        )
        breakout = dataframe["close"] > recent_high

        ai_not_bearish = pd.Series(True, index=dataframe.index)
        if self._freqai_enabled() and "&-future_return" in dataframe.columns and "do_predict" in dataframe.columns:
            ai_not_bearish = (dataframe["do_predict"] != 1) | (dataframe["&-future_return"] >= -0.020)

        long_signal = (
            (bullish_1h | bullish_15m)
            & range_expanding
            & (breakout | reclaim_ema20)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["adx"] > 8)
            & dataframe["rsi"].between(42, 72)
            & ((dataframe["macdhist"] > 0) | breakout)
            & (dataframe["volume_ratio"] > 0.15)
            & ai_not_bearish
        )

        if self._new_entries_disabled():
            long_signal = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        long_signal, _, _, _ = self._apply_direction_advisor_gate(
            long_signal,
            false_series,
            false_series,
            false_series,
        )

        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, "alt_long_momentum_2x")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        return dataframe
