try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class AIGeneratedLongTrendContinuation(SampleStrategy):
    """
    AI strategy-lab generated candidate.

    The candidate DSL is rendered by scripts/ai_strategy_lab.py.  The LLM never
    writes executable Python directly.
    """

    can_short = True
    minimal_roi = {"0": 0.026, "30": 0.012, "90": 0.0}
    stoploss = -0.05
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.016
    trailing_only_offset_is_reached = False
    ai_candidate = {
    "adx_min": 20.0,
    "archetype": "trend",
    "bb_tolerance": 0.002,
    "class_name": "AIGeneratedLongTrendContinuation",
    "generated_by": "heuristic",
    "name": "long_trend_continuation",
    "reason": "Heuristic trend continuation candidate from market breadth.",
    "risk_profile": "balanced",
    "roi_0": 0.026,
    "roi_30": 0.012,
    "rsi_long_max": 48,
    "rsi_short_min": 52,
    "side": "long",
    "stoploss": -0.05,
    "trailing_offset": 0.016,
    "trailing_positive": 0.008,
    "volume_min": 0.8
}

    def _build_long_blockers(
        self,
        dataframe,
        archetype,
        side,
        adx_min,
        volume_min,
        rsi_long_max,
        bb_tolerance,
        long_context,
        trend_long,
        pullback_long,
        breakout_long,
        meanrev_long,
        volatility_expanding,
        recent_high,
        volume_ok,
    ):
        if side not in {"long", "both"}:
            return ["side_disabled"]
        strategy_signal = {
            "trend": trend_long,
            "pullback": pullback_long,
            "breakout": breakout_long,
            "mean_reversion": meanrev_long,
        }.get(archetype, trend_long)
        if bool(strategy_signal.iloc[-1]):
            return ["passed"]

        last = dataframe.iloc[-1]
        blockers = []
        if archetype in {"trend", "pullback", "breakout"} and not bool(long_context.iloc[-1]):
            blockers.append("no_long_trend_context")
        if not bool(volume_ok.iloc[-1]):
            blockers.append(f"volume_ratio<={volume_min:.2f}")
        if self._last_float(last, "adx") <= adx_min:
            blockers.append(f"adx<={adx_min:.2f}")
        if archetype == "trend":
            if self._last_float(last, "close") <= self._last_float(last, "ema20"):
                blockers.append("close<=ema20")
            if self._last_float(last, "ema20") <= self._last_float(last, "ema50"):
                blockers.append("ema20<=ema50")
            if self._last_float(last, "macdhist") <= 0:
                blockers.append("macdhist<=0")
            if self._last_float(last, "rsi") >= max(rsi_long_max, 52):
                blockers.append(f"rsi>={max(rsi_long_max, 52):.0f}")
        elif archetype == "pullback":
            if self._last_float(last, "low") > self._last_float(last, "ema20") * (1 + bb_tolerance):
                blockers.append("low>ema20_pullback")
            if self._last_float(last, "close") <= self._last_float(last, "ema20"):
                blockers.append("close<=ema20")
            if self._last_float(last, "rsi") >= rsi_long_max:
                blockers.append(f"rsi>={rsi_long_max:.0f}")
        elif archetype == "breakout":
            if not bool(volatility_expanding.iloc[-1]):
                blockers.append("no_volatility_expansion")
            if pd.isna(recent_high.iloc[-1]) or self._last_float(last, "close") <= float(recent_high.iloc[-1]):
                blockers.append("close<=recent_high")
            if self._last_float(last, "rsi") <= 52:
                blockers.append("rsi<=52")
        else:
            if not bool(last.get("range_market_1h", False)) and self._last_float(last, "adx") >= adx_min:
                blockers.append("not_range_context")
            if self._last_float(last, "close") > self._last_float(last, "bb_lowerband") * (1 + bb_tolerance):
                blockers.append("close>bb_lowerband")
            if self._last_float(last, "rsi") >= rsi_long_max:
                blockers.append(f"rsi>={rsi_long_max:.0f}")
        return blockers or ["waiting_trigger"]

    def _build_short_blockers(
        self,
        dataframe,
        archetype,
        side,
        adx_min,
        volume_min,
        rsi_short_min,
        bb_tolerance,
        short_context,
        trend_short,
        pullback_short,
        breakout_short,
        meanrev_short,
        volatility_expanding,
        recent_low,
        volume_ok,
    ):
        if side not in {"short", "both"}:
            return ["side_disabled"]
        strategy_signal = {
            "trend": trend_short,
            "pullback": pullback_short,
            "breakout": breakout_short,
            "mean_reversion": meanrev_short,
        }.get(archetype, trend_short)
        if bool(strategy_signal.iloc[-1]):
            return ["passed"]

        last = dataframe.iloc[-1]
        blockers = []
        if archetype in {"trend", "pullback", "breakout"} and not bool(short_context.iloc[-1]):
            blockers.append("no_short_trend_context")
        if not bool(volume_ok.iloc[-1]):
            blockers.append(f"volume_ratio<={volume_min:.2f}")
        if self._last_float(last, "adx") <= adx_min:
            blockers.append(f"adx<={adx_min:.2f}")
        if archetype == "trend":
            if self._last_float(last, "close") >= self._last_float(last, "ema20"):
                blockers.append("close>=ema20")
            if self._last_float(last, "ema20") >= self._last_float(last, "ema50"):
                blockers.append("ema20>=ema50")
            if self._last_float(last, "macdhist") >= 0:
                blockers.append("macdhist>=0")
            if self._last_float(last, "rsi") <= min(rsi_short_min, 48):
                blockers.append(f"rsi<={min(rsi_short_min, 48):.0f}")
        elif archetype == "pullback":
            if self._last_float(last, "high") < self._last_float(last, "ema20") * (1 - bb_tolerance):
                blockers.append("high<ema20_pullback")
            if self._last_float(last, "close") >= self._last_float(last, "ema20"):
                blockers.append("close>=ema20")
            if self._last_float(last, "rsi") <= rsi_short_min:
                blockers.append(f"rsi<={rsi_short_min:.0f}")
        elif archetype == "breakout":
            if not bool(volatility_expanding.iloc[-1]):
                blockers.append("no_volatility_expansion")
            if pd.isna(recent_low.iloc[-1]) or self._last_float(last, "close") >= float(recent_low.iloc[-1]):
                blockers.append("close>=recent_low")
            if self._last_float(last, "rsi") >= 48:
                blockers.append("rsi>=48")
        else:
            if not bool(last.get("range_market_1h", False)) and self._last_float(last, "adx") >= adx_min:
                blockers.append("not_range_context")
            if self._last_float(last, "close") < self._last_float(last, "bb_upperband") * (1 - bb_tolerance):
                blockers.append("close<bb_upperband")
            if self._last_float(last, "rsi") <= rsi_short_min:
                blockers.append(f"rsi<={rsi_short_min:.0f}")
        return blockers or ["waiting_trigger"]

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        if not dataframe.empty:
            last = dataframe.iloc[-1]
            candle_time = last.get("date")
            if hasattr(candle_time, "isoformat"):
                candle_time = candle_time.isoformat()
            self._append_strategy_runtime_probe(
                {
                    "record_type": "strategy_runtime_probe",
                    "created_at": pd.Timestamp.utcnow().isoformat(),
                    "strategy": self.__class__.__name__,
                    "pair": metadata.get("pair", "unknown"),
                    "time": candle_time,
                    "stage": "populate_entry_trend_start",
                }
            )

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
        self._append_custom_entry_diagnostics(
            dataframe,
            metadata,
            self._build_long_blockers(
                dataframe,
                archetype,
                side,
                adx_min,
                volume_min,
                rsi_long_max,
                bb_tolerance,
                long_context,
                trend_long,
                pullback_long,
                breakout_long,
                meanrev_long,
                volatility_expanding,
                recent_high,
                volume_ok,
            ),
            self._build_short_blockers(
                dataframe,
                archetype,
                side,
                adx_min,
                volume_min,
                rsi_short_min,
                bb_tolerance,
                short_context,
                trend_short,
                pullback_short,
                breakout_short,
                meanrev_short,
                volatility_expanding,
                recent_low,
                volume_ok,
            ),
        )
        if not dataframe.empty:
            last = dataframe.iloc[-1]
            candle_time = last.get("date")
            if hasattr(candle_time, "isoformat"):
                candle_time = candle_time.isoformat()
            self._append_strategy_runtime_probe(
                {
                    "record_type": "strategy_runtime_probe",
                    "created_at": pd.Timestamp.utcnow().isoformat(),
                    "strategy": self.__class__.__name__,
                    "pair": metadata.get("pair", "unknown"),
                    "time": candle_time,
                    "stage": "populate_entry_trend_end",
                    "enter_long": self._last_bool(dataframe.get("enter_long", pd.Series(dtype=float))),
                    "enter_short": self._last_bool(dataframe.get("enter_short", pd.Series(dtype=float))),
                }
            )
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
