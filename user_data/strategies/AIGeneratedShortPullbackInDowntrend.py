try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class AIGeneratedShortPullbackInDowntrend(SampleStrategy):
    """
    AI strategy-lab generated candidate.

    The candidate DSL is rendered by scripts/ai_strategy_lab.py.  The LLM never
    writes executable Python directly.
    """

    can_short = True
    minimal_roi = {"0": 0.04, "30": 0.02, "90": 0.0}
    stoploss = -0.025
    trailing_stop = True
    trailing_stop_positive = 0.006
    trailing_stop_positive_offset = 0.006
    trailing_only_offset_is_reached = False
    ai_candidate = {
    "adx_min": 20.0,
    "archetype": "pullback",
    "bb_tolerance": 0.01,
    "class_name": "AIGeneratedShortPullbackInDowntrend",
    "generated_by": "llm",
    "name": "short_pullback_in_downtrend",
    "reason": "After sharp declines (e.g., ETH -4.5% on 1h, UNI -6.5% on 15m), expect brief bounces to moving averages as resistance. Entering short on 5m/15m bounces to EMA20/EMA50 in overall downtrend provides favorable risk/reward. Use ADX>20 to ensure trend is established.",
    "risk_profile": "balanced",
    "roi_0": 0.04,
    "roi_30": 0.02,
    "rsi_long_max": 50.0,
    "rsi_short_min": 40.0,
    "side": "short",
    "stoploss": -0.025,
    "trailing_offset": 0.006,
    "trailing_positive": 0.006,
    "volume_min": 0.8
}

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        side = self.ai_candidate["side"]
        archetype = self.ai_candidate["archetype"]
        adx_min = float(self.ai_candidate["adx_min"])
        volume_min = float(self.ai_candidate["volume_min"])
        rsi_long_max = float(self.ai_candidate["rsi_long_max"])
        rsi_short_min = float(self.ai_candidate["rsi_short_min"])
        bb_tolerance = float(self.ai_candidate["bb_tolerance"])

        volume_ok = dataframe["volume_ratio"] > volume_min
        long_context = dataframe["trend_up_1h"] | dataframe["trend_up_15m"]
        short_context = dataframe["trend_down_1h"] | dataframe["trend_down_15m"]
        trend_long = (
            long_context
            & (dataframe["adx"] > adx_min)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["macdhist"] > 0)
            & (dataframe["rsi"] < max(rsi_long_max, 52))
            & volume_ok
        )
        trend_short = (
            short_context
            & (dataframe["adx"] > adx_min)
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["macdhist"] < 0)
            & (dataframe["rsi"] > min(rsi_short_min, 48))
            & volume_ok
        )

        pullback_long = (
            long_context
            & (dataframe["adx"] > max(adx_min - 4, 8))
            & (dataframe["low"] <= dataframe["ema20"] * (1 + bb_tolerance))
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["rsi"] < rsi_long_max)
            & volume_ok
        )
        pullback_short = (
            short_context
            & (dataframe["adx"] > max(adx_min - 4, 8))
            & (dataframe["high"] >= dataframe["ema20"] * (1 - bb_tolerance))
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["rsi"] > rsi_short_min)
            & volume_ok
        )

        recent_high = dataframe["high"].rolling(36).max().shift(1)
        recent_low = dataframe["low"].rolling(36).min().shift(1)
        volatility_expanding = (
            (dataframe["volatility_ratio"] > 1.05)
            | (dataframe["bb_width"] > dataframe["bb_width_sma"] * 1.08)
        )
        breakout_long = (
            long_context
            & volatility_expanding
            & (dataframe["close"] > recent_high)
            & (dataframe["rsi"] > 52)
            & volume_ok
        )
        breakout_short = (
            short_context
            & volatility_expanding
            & (dataframe["close"] < recent_low)
            & (dataframe["rsi"] < 48)
            & volume_ok
        )

        range_context = dataframe["range_market_1h"] | (
            (dataframe["adx"] < adx_min) & (dataframe["volatility_ratio"] < 1.35)
        )
        meanrev_long = (
            range_context
            & (dataframe["close"] <= dataframe["bb_lowerband"] * (1 + bb_tolerance))
            & (dataframe["rsi"] < rsi_long_max)
            & volume_ok
        )
        meanrev_short = (
            range_context
            & (dataframe["close"] >= dataframe["bb_upperband"] * (1 - bb_tolerance))
            & (dataframe["rsi"] > rsi_short_min)
            & volume_ok
        )

        long_map = {
            "trend": trend_long,
            "pullback": pullback_long,
            "breakout": breakout_long,
            "mean_reversion": meanrev_long,
        }
        short_map = {
            "trend": trend_short,
            "pullback": pullback_short,
            "breakout": breakout_short,
            "mean_reversion": meanrev_short,
        }
        long_signal = long_map.get(archetype, trend_long) if side in {"long", "both"} else pd.Series(False, index=dataframe.index)
        short_signal = short_map.get(archetype, trend_short) if side in {"short", "both"} else pd.Series(False, index=dataframe.index)

        if self._new_entries_disabled():
            long_signal = pd.Series(False, index=dataframe.index)
            short_signal = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        long_signal, _, short_signal, _ = self._apply_direction_advisor_gate(
            long_signal,
            false_series,
            short_signal,
            false_series,
        )

        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, f"ai_{archetype}_long")
        dataframe.loc[short_signal, ["enter_short", "enter_tag"]] = (1, f"ai_{archetype}_short")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
