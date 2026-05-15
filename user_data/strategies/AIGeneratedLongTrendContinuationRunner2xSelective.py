try:
    from user_data.strategies.AIGeneratedLongTrendContinuationRunner2x import (
        AIGeneratedLongTrendContinuationRunner2x,
    )
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuationRunner2x import AIGeneratedLongTrendContinuationRunner2x

import json
from pathlib import Path

import pandas as pd


class AIGeneratedLongTrendContinuationRunner2xSelective(
    AIGeneratedLongTrendContinuationRunner2x
):
    """
    Restrict the runner to a tighter pair subset and bias execution short-first.

    The live diagnostics show the broad market is currently failing the long
    trend gate while the advisor repeatedly leans short.  Rather than waiting
    for the formal pool to recover, this wrapper narrows the live universe and
    flips the inherited AI candidate toward bearish breakout continuation.
    """

    ai_candidate = {
        **AIGeneratedLongTrendContinuationRunner2x.ai_candidate,
        "name": "short_breakout_selective",
        "reason": "Live diagnostics show weak long trend structure and repeated short-side pressure.",
        "side": "short",
        "archetype": "breakout",
        "adx_min": 14.0,
        "volume_min": 0.35,
        "rsi_short_min": 40.0,
    }
    fallback_pairs = {
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "LINK/USDT:USDT",
    }
    pair_selection_path = Path(__file__).resolve().parents[1] / "autoopt" / "pair_selection.json"

    def _load_allowed_pairs(self) -> set[str]:
        try:
            payload = json.loads(self.pair_selection_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set(self.fallback_pairs)

        strategies = payload.get("strategies", {}) if isinstance(payload, dict) else {}
        current = strategies.get(self.__class__.__name__, {}) if isinstance(strategies, dict) else {}
        allowed = current.get("allowed_pairs", []) if isinstance(current, dict) else []
        cleaned = {str(item).strip() for item in allowed if str(item).strip()}
        return cleaned or set(self.fallback_pairs)

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        side = "short"
        archetype = "breakout"
        adx_min = 14.0
        volume_min = 0.35
        rsi_long_max = float(self.ai_candidate["rsi_long_max"])
        rsi_short_min = 40.0
        bb_tolerance = float(self.ai_candidate["bb_tolerance"])

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

        volume_ok = dataframe["volume_ratio"] > max(volume_min, 0.22)
        long_context = (
            dataframe["trend_up_1h"]
            | dataframe["trend_up_15m"]
            | (
                (dataframe["adx"] > max(adx_min - 5, 9))
                & (dataframe["close"] > dataframe["ema20"] * 0.995)
                & (dataframe["rsi"] > 47)
            )
        )
        short_context = (
            dataframe["trend_down_1h"]
            | dataframe["trend_down_15m"]
            | (
                (dataframe["adx"] > max(adx_min - 5, 9))
                & (dataframe["close"] < dataframe["ema20"] * 1.005)
                & (dataframe["rsi"] < 53)
            )
        )
        trend_long = (
            long_context
            & (dataframe["adx"] > max(adx_min - 4, 9))
            & (dataframe["close"] > dataframe["ema20"] * 0.995)
            & (dataframe["ema20"] > dataframe["ema50"] * 0.995)
            & (dataframe["macdhist"] > -0.035)
            & (dataframe["rsi"] < max(rsi_long_max, 60))
            & volume_ok
        )
        trend_short = (
            short_context
            & (dataframe["adx"] > max(adx_min - 4, 9))
            & (dataframe["close"] < dataframe["ema20"] * 1.005)
            & (dataframe["ema20"] < dataframe["ema50"] * 1.005)
            & (dataframe["macdhist"] < 0.03)
            & (dataframe["rsi"] > min(rsi_short_min, 40))
            & volume_ok
        )

        pullback_long = (
            long_context
            & (dataframe["adx"] > max(adx_min - 6, 7))
            & (dataframe["low"] <= dataframe["ema20"] * (1 + bb_tolerance + 0.002))
            & (dataframe["close"] > dataframe["ema20"] * 0.995)
            & (dataframe["rsi"] < max(rsi_long_max, 58))
            & volume_ok
        )
        pullback_short = (
            short_context
            & (dataframe["adx"] > max(adx_min - 6, 7))
            & (dataframe["high"] >= dataframe["ema20"] * (1 - bb_tolerance - 0.002))
            & (dataframe["close"] < dataframe["ema20"] * 1.005)
            & (dataframe["rsi"] > min(rsi_short_min, 40))
            & volume_ok
        )

        recent_high = dataframe["high"].rolling(36).max().shift(1)
        recent_low = dataframe["low"].rolling(36).min().shift(1)
        volatility_expanding = (
            (dataframe["volatility_ratio"] > 0.98)
            | (dataframe["bb_width"] > dataframe["bb_width_sma"] * 1.02)
        )
        breakout_long = (
            long_context
            & volatility_expanding
            & (dataframe["close"] > recent_high.fillna(dataframe["close"] * 1.002))
            & (dataframe["rsi"] > 50)
            & volume_ok
        )
        breakout_short = (
            short_context
            & volatility_expanding
            & (dataframe["close"] < recent_low.fillna(dataframe["close"] * 0.998))
            & (dataframe["rsi"] < 52)
            & volume_ok
        )

        range_context = dataframe["range_market_1h"] | (
            (dataframe["adx"] < max(adx_min + 2, 20)) & (dataframe["volatility_ratio"] < 1.45)
        )
        meanrev_long = (
            range_context
            & (dataframe["close"] <= dataframe["bb_lowerband"] * (1 + bb_tolerance + 0.002))
            & (dataframe["rsi"] < max(rsi_long_max, 58))
            & volume_ok
        )
        meanrev_short = (
            range_context
            & (dataframe["close"] >= dataframe["bb_upperband"] * (1 - bb_tolerance - 0.002))
            & (dataframe["rsi"] > min(rsi_short_min, 40))
            & volume_ok
        )

        long_signal = pd.Series(False, index=dataframe.index)
        short_signal = breakout_short | trend_short | pullback_short

        if self._new_entries_disabled():
            short_signal = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        long_signal, _, short_signal, _ = self._apply_direction_advisor_gate(
            long_signal,
            false_series,
            short_signal,
            false_series,
        )

        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, "ai_breakout_long")
        dataframe.loc[short_signal, ["enter_short", "enter_tag"]] = (1, "ai_breakout_short")

        long_blockers = self._build_long_blockers(
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
        )
        short_blockers = self._build_short_blockers(
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
        )
        self._append_custom_entry_diagnostics(dataframe, metadata, long_blockers, short_blockers)

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
                    "long_blockers": long_blockers[:5],
                    "short_blockers": short_blockers[:5],
                }
            )

        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        if metadata.get("pair") not in self._load_allowed_pairs():
            dataframe["enter_long"] = 0
            dataframe["enter_short"] = 0
        return dataframe
