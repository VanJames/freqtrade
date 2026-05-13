import logging
import hashlib
import json
import os
import subprocess
import smtplib
import ssl
import threading
from email.message import EmailMessage
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import talib.abstract as ta
import pandas as pd
from pandas import DataFrame
from technical import qtpylib

from freqtrade.enums import RunMode
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, Trade, informative


logger = logging.getLogger(__name__)

SIGNAL_DIAGNOSTICS_PATH = Path(
    os.getenv("SIGNAL_DIAGNOSTICS_PATH", "/freqtrade/user_data/signals/signal_diagnostics.jsonl")
)


class SampleStrategy(IStrategy):
    """
    Multi-timeframe trend strategy with long/short support, risk-based sizing,
    ATR stop management, and signal email notifications.
    """

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    startup_candle_count = 600
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    use_custom_stoploss = True
    position_adjustment_enable = False

    minimal_roi = {
        "0": 0.045,
        "30": 0.025,
        "90": 0.012,
        "180": 0.0,
    }

    stoploss = -0.08
    trailing_stop = False

    protections = [
        {
            "method": "CooldownPeriod",
            "stop_duration_candles": 3,
        },
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 72,
            "trade_limit": 2,
            "stop_duration_candles": 12,
            "only_per_pair": True,
        },
        {
            "method": "LowProfitPairs",
            "lookback_period_candles": 144,
            "trade_limit": 4,
            "stop_duration_candles": 24,
            "required_profit": 0.01,
            "only_per_side": False,
        },
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 288,
            "trade_limit": 1,
            "stop_duration_candles": 48,
            "max_allowed_drawdown": 0.12,
        },
    ]

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    adx_threshold = IntParameter(16, 35, default=22, space="buy", optimize=True, load=True)
    long_rsi_trigger = IntParameter(28, 42, default=34, space="buy", optimize=True, load=True)
    short_rsi_trigger = IntParameter(58, 72, default=66, space="sell", optimize=True, load=True)
    volume_ratio_min = DecimalParameter(
        1.00, 2.50, default=1.20, decimals=2, space="buy", optimize=True, load=True
    )
    ai_edge_threshold = DecimalParameter(
        0.0030, 0.0200, default=0.0080, decimals=4, space="buy", optimize=True, load=True
    )
    atr_stop_mult = DecimalParameter(
        1.20, 3.20, default=1.80, decimals=2, space="sell", optimize=True, load=True
    )
    atr_trail_mult = DecimalParameter(
        0.80, 2.00, default=1.20, decimals=2, space="sell", optimize=True, load=True
    )
    risk_per_trade = DecimalParameter(
        0.0025, 0.0100, default=0.0050, decimals=4, space="buy", optimize=True, load=True
    )
    regime_window = IntParameter(48, 288, default=96, space="buy", optimize=True, load=True)
    high_volatility_ratio = DecimalParameter(
        1.05, 1.60, default=1.20, decimals=2, space="buy", optimize=True, load=True
    )
    low_volatility_ratio = DecimalParameter(
        0.65, 0.98, default=0.88, decimals=2, space="buy", optimize=True, load=True
    )

    plot_config = {
        "main_plot": {
            "ema20": {},
            "ema50": {},
            "ema200": {},
            "&-future_return": {"color": "cyan"},
            "bb_lowerband": {"color": "gray"},
            "bb_middleband": {"color": "white"},
            "bb_upperband": {"color": "gray"},
        },
        "subplots": {
            "Momentum": {
                "rsi": {"color": "red"},
                "adx": {"color": "orange"},
                "atr_pct": {"color": "blue"},
            },
            "Volume": {
                "volume_ratio": {"color": "green"},
            },
            "FreqAI": {
                "do_predict": {"color": "brown"},
            },
        },
    }

    def bot_start(self, **kwargs) -> None:
        self._signal_email_cache: set[str] = set()
        self._signal_diagnostic_cache: set[str] = set()
        self._email_warned = False
        self._freqai_warned = False
        self._trading_disabled_warned = False
        self._direction_gate_warned = False

    def _freqai_enabled(self) -> bool:
        return self.config.get("freqai", {}).get("enabled", False)

    def _freqai_long_ok(self, dataframe: DataFrame) -> pd.Series:
        if not self._freqai_enabled():
            return pd.Series(True, index=dataframe.index)
        if "&-future_return" not in dataframe.columns or "do_predict" not in dataframe.columns:
            # FreqAI is an optional edge layer. If the model output is unavailable
            # we fall back to the rule-based signal instead of suppressing all trades.
            return pd.Series(True, index=dataframe.index)

        atr_pct = dataframe.get("atr_pct", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        threshold = pd.Series(self.ai_edge_threshold.value, index=dataframe.index) + (atr_pct * 0.8)
        return (dataframe["do_predict"] == 1) & (dataframe["&-future_return"] > threshold)

    def _freqai_short_ok(self, dataframe: DataFrame) -> pd.Series:
        if not self._freqai_enabled():
            return pd.Series(True, index=dataframe.index)
        if "&-future_return" not in dataframe.columns or "do_predict" not in dataframe.columns:
            return pd.Series(True, index=dataframe.index)

        atr_pct = dataframe.get("atr_pct", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        threshold = pd.Series(self.ai_edge_threshold.value, index=dataframe.index) + (atr_pct * 0.8)
        return (dataframe["do_predict"] == 1) & (dataframe["&-future_return"] < -threshold)

    def _profile_adjusted_value(
        self,
        dataframe: DataFrame,
        base: float,
        conservative_delta: float = 0.0,
        aggressive_delta: float = 0.0,
    ) -> pd.Series:
        values = pd.Series(base, index=dataframe.index, dtype="float64")
        values = values.where(~dataframe["regime_high_vol"], base + conservative_delta)
        values = values.where(~dataframe["regime_low_vol"], base - aggressive_delta)
        return values

    @staticmethod
    def _last_bool(series: pd.Series) -> bool:
        if series.empty:
            return False
        return bool(series.iloc[-1])

    @staticmethod
    def _recent_signal(series: pd.Series, window: int) -> pd.Series:
        return series.fillna(False).astype(int).rolling(window, min_periods=1).max().astype(bool)

    @staticmethod
    def _last_float(row: pd.Series, key: str, default: float = 0.0) -> float:
        try:
            value = float(row.get(key, default) or default)
        except (TypeError, ValueError):
            return default
        if not np.isfinite(value):
            return default
        return value

    def _is_live_or_dry_run(self) -> bool:
        runmode = self.config.get("runmode")
        if runmode in (RunMode.DRY_RUN, RunMode.LIVE):
            return True
        value = getattr(runmode, "value", runmode)
        name = getattr(runmode, "name", "")
        text_values = {str(value).lower(), str(name).lower(), str(runmode).lower()}
        return bool(text_values & {"dry_run", "dry-run", "live", "runmode.dry_run", "runmode.live"})

    def _log_entry_diagnostics(
        self,
        dataframe: DataFrame,
        metadata: dict,
        adx_threshold: pd.Series,
        volume_threshold: pd.Series,
        long_rsi_threshold: pd.Series,
        short_rsi_threshold: pd.Series,
        long_trend: pd.Series,
        long_trigger: pd.Series,
        long_range: pd.Series,
        short_trend: pd.Series,
        short_trigger: pd.Series,
        short_range: pd.Series,
    ) -> None:
        if not self._is_live_or_dry_run():
            logger.info(
                "Signal diagnostics skipped for %s: runmode=%s",
                metadata.get("pair", "unknown"),
                self.config.get("runmode"),
            )
            return
        if dataframe.empty:
            logger.info("Signal diagnostics skipped for %s: empty dataframe", metadata.get("pair", "unknown"))
            return

        last = dataframe.iloc[-1]
        has_enter_long = self._last_bool(dataframe.get("enter_long", pd.Series(dtype=float)))
        has_enter_short = self._last_bool(dataframe.get("enter_short", pd.Series(dtype=float)))
        candle_time = last.get("date")
        if hasattr(candle_time, "isoformat"):
            candle_time = candle_time.isoformat()

        cache_key = f"{metadata.get('pair', '')}:entry_diagnostics:{candle_time}"
        diagnostic_cache = getattr(self, "_signal_diagnostic_cache", set())
        if cache_key in diagnostic_cache:
            return
        diagnostic_cache.add(cache_key)
        self._signal_diagnostic_cache = diagnostic_cache

        close = self._last_float(last, "close")
        ema20 = self._last_float(last, "ema20")
        ema50 = self._last_float(last, "ema50")
        ema200 = self._last_float(last, "ema200")
        rsi = self._last_float(last, "rsi")
        adx = self._last_float(last, "adx")
        macd = self._last_float(last, "macd")
        macdsignal = self._last_float(last, "macdsignal")
        macdhist = self._last_float(last, "macdhist")
        volume_ratio = self._last_float(last, "volume_ratio")
        volatility_ratio = self._last_float(last, "volatility_ratio", 1.0)
        adx_limit = float(adx_threshold.iloc[-1])
        volume_limit = float(volume_threshold.iloc[-1])
        long_rsi_limit = float(long_rsi_threshold.iloc[-1])
        short_rsi_limit = float(short_rsi_threshold.iloc[-1])

        long_blockers: list[str] = []
        if not bool(last.get("trend_context_long", False)):
            long_blockers.append("no_long_trend_context")
        if close <= ema20:
            long_blockers.append("close<=ema20")
        if ema20 <= ema50:
            long_blockers.append("ema20<=ema50")
        if ema50 <= ema200:
            long_blockers.append("ema50<=ema200")
        if adx <= adx_limit:
            long_blockers.append(f"adx {adx:.2f}<={adx_limit:.2f}")
        if not (
            (bool(last.get("trend_up_15m", False)) and volume_ratio > 0.95)
            or volume_ratio > (volume_limit - 0.15)
        ):
            long_blockers.append(f"volume_ratio {volume_ratio:.2f}<={volume_limit - 0.15:.2f}")
        if rsi <= 52:
            long_blockers.append("rsi<=52")
        if macdhist <= 0:
            long_blockers.append("macdhist<=0")
        if self._last_bool(long_trend) and not self._last_bool(long_trigger):
            long_blockers.append(f"no_long_trigger rsi_limit={long_rsi_limit:.2f}")
        if not self._last_bool(long_range):
            range_reasons = []
            if not bool(last.get("range_market_1h", False)):
                range_reasons.append("not_range_1h")
            if not bool(last.get("regime_low_vol", False)):
                range_reasons.append("not_low_vol")
            if rsi >= 34:
                range_reasons.append("rsi>=34")
            if volume_ratio <= 1.0:
                range_reasons.append("volume<=1")
            if range_reasons:
                long_blockers.append("meanrev_long:" + ",".join(range_reasons[:3]))

        short_blockers: list[str] = []
        if not (bool(last.get("trend_down_1h", False)) and bool(last.get("trend_down_15m", False))):
            short_blockers.append("no_short_alignment")
        if close >= ema20:
            short_blockers.append("close>=ema20")
        if ema20 >= ema50:
            short_blockers.append("ema20>=ema50")
        if ema50 >= ema200:
            short_blockers.append("ema50>=ema200")
        if adx <= (adx_limit + 1.0):
            short_blockers.append(f"adx {adx:.2f}<={adx_limit + 1.0:.2f}")
        if volume_ratio <= (volume_limit + 0.05):
            short_blockers.append(f"volume_ratio {volume_ratio:.2f}<={volume_limit + 0.05:.2f}")
        if rsi >= 45:
            short_blockers.append("rsi>=45")
        if macd >= macdsignal:
            short_blockers.append("macd>=signal")
        if macdhist >= 0:
            short_blockers.append("macdhist>=0")
        if self._last_bool(short_trend) and not self._last_bool(short_trigger):
            short_blockers.append(f"no_short_trigger rsi_limit={short_rsi_limit:.2f}")
        if not self._last_bool(short_range):
            range_reasons = []
            if not bool(last.get("range_market_1h", False)):
                range_reasons.append("not_range_1h")
            if not bool(last.get("regime_low_vol", False)):
                range_reasons.append("not_low_vol")
            if rsi <= 66:
                range_reasons.append("rsi<=66")
            if volume_ratio <= 1.0:
                range_reasons.append("volume<=1")
            if range_reasons:
                short_blockers.append("meanrev_short:" + ",".join(range_reasons[:3]))

        if self._freqai_enabled() and "&-future_return" in dataframe.columns:
            future_return = self._last_float(last, "&-future_return")
            do_predict = last.get("do_predict", "n/a")
            long_ai_limit = float(self.ai_edge_threshold.value) + self._last_float(last, "atr_pct") * 0.8
            short_ai_limit = -long_ai_limit
            if not self._last_bool(self._freqai_long_ok(dataframe)):
                long_blockers.append(f"freqai future_return {future_return:.4f}<={long_ai_limit:.4f}")
            if not self._last_bool(self._freqai_short_ok(dataframe)):
                short_blockers.append(
                    f"freqai future_return {future_return:.4f}>={short_ai_limit:.4f}"
                )
        else:
            future_return = None
            do_predict = "n/a"
        do_predict_value = str(do_predict)

        long_blockers_limited = long_blockers[:8] or ["waiting_trigger"]
        short_blockers_limited = short_blockers[:8] or ["waiting_trigger"]
        regime = (
            "high"
            if bool(last.get("regime_high_vol", False))
            else "low"
            if bool(last.get("regime_low_vol", False))
            else "balanced"
        )

        logger.info(
            "Signal diagnostics %s time=%s close=%.8f rsi=%.2f adx=%.2f "
            "volume_ratio=%.2f volatility_ratio=%.2f regime=%s trends="
            "1h_up:%s 1h_down:%s 15m_up:%s 15m_down:%s freqai_do_predict=%s "
            "freqai_future_return=%s long_blockers=%s short_blockers=%s",
            metadata.get("pair", "unknown"),
            candle_time,
            close,
            rsi,
            adx,
            volume_ratio,
            volatility_ratio,
            regime,
            bool(last.get("trend_up_1h", False)),
            bool(last.get("trend_down_1h", False)),
            bool(last.get("trend_up_15m", False)),
            bool(last.get("trend_down_15m", False)),
            do_predict_value,
            f"{future_return:.4f}" if future_return is not None else "n/a",
            long_blockers_limited,
            short_blockers_limited,
        )
        self._append_signal_diagnostic(
            {
                "record_type": "signal_diagnostics",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "pair": metadata.get("pair", "unknown"),
                "time": candle_time,
                "close": close,
                "rsi": rsi,
                "adx": adx,
                "volume_ratio": volume_ratio,
                "volatility_ratio": volatility_ratio,
                "regime": regime,
                "trend_up_1h": bool(last.get("trend_up_1h", False)),
                "trend_down_1h": bool(last.get("trend_down_1h", False)),
                "trend_up_15m": bool(last.get("trend_up_15m", False)),
                "trend_down_15m": bool(last.get("trend_down_15m", False)),
                "freqai_do_predict": do_predict_value,
                "freqai_future_return": future_return,
                "enter_long": has_enter_long,
                "enter_short": has_enter_short,
                "long_blockers": long_blockers_limited,
                "short_blockers": short_blockers_limited,
            }
        )

    def _append_signal_diagnostic(self, payload: dict) -> None:
        try:
            path = Path(os.getenv("SIGNAL_DIAGNOSTICS_PATH", str(SIGNAL_DIAGNOSTICS_PATH)))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        except Exception as exc:  # pragma: no cover - diagnostics must not affect trading
            logger.debug("Failed to write signal diagnostics record: %s", exc)

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        dataframe["%-atr-period"] = ta.ATR(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.0
        )
        dataframe["%-bb_width-period"] = (bollinger["upper"] - bollinger["lower"]) / bollinger["mid"]
        dataframe["%-close_bb_lower-period"] = dataframe["close"] / bollinger["lower"]
        dataframe["%-relative_volume-period"] = dataframe["volume"] / dataframe["volume"].rolling(
            period
        ).mean()
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-pct_change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-future_return"] = dataframe["close"].shift(-label_period) / dataframe["close"] - 1
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if self._freqai_enabled():
            try:
                dataframe = self.freqai.start(dataframe, metadata, self)
            except Exception as exc:
                if not getattr(self, "_freqai_warned", False):
                    logger.warning("FreqAI disabled for this run after start() failed: %s", exc)
                    self._freqai_warned = True

        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]

        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        dataframe["bb_width"] = (dataframe["bb_upperband"] - dataframe["bb_lowerband"]) / dataframe["bb_middleband"]

        dataframe["volume_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_sma"]

        regime_window = int(self.regime_window.value)
        dataframe["atr_pct_sma"] = dataframe["atr_pct"].rolling(regime_window).mean()
        dataframe["bb_width_sma"] = dataframe["bb_width"].rolling(regime_window).mean()
        dataframe["volatility_ratio"] = (
            (dataframe["atr_pct"] / dataframe["atr_pct_sma"].replace(0, np.nan))
            + (dataframe["bb_width"] / dataframe["bb_width_sma"].replace(0, np.nan))
        ) / 2.0
        dataframe["volatility_ratio"] = dataframe["volatility_ratio"].replace([np.inf, -np.inf], np.nan).fillna(1.0)
        dataframe["regime_high_vol"] = dataframe["volatility_ratio"] >= self.high_volatility_ratio.value
        dataframe["regime_low_vol"] = dataframe["volatility_ratio"] <= self.low_volatility_ratio.value
        dataframe["regime_balanced"] = ~(dataframe["regime_high_vol"] | dataframe["regime_low_vol"])

        dataframe["trend_up_1h"] = (
            (dataframe["close_1h"] > dataframe["ema20_1h"]) &
            (dataframe["ema20_1h"] > dataframe["ema50_1h"]) &
            (dataframe["rsi_1h"] > 50) &
            (dataframe["adx_1h"] > 18)
        )
        dataframe["trend_down_1h"] = (
            (dataframe["close_1h"] < dataframe["ema20_1h"]) &
            (dataframe["ema20_1h"] < dataframe["ema50_1h"]) &
            (dataframe["rsi_1h"] < 50) &
            (dataframe["adx_1h"] > 18)
        )
        dataframe["trend_up_15m"] = (
            (dataframe["close_15m"] > dataframe["ema20_15m"]) &
            (dataframe["ema20_15m"] > dataframe["ema50_15m"]) &
            (dataframe["rsi_15m"] > 52) &
            (dataframe["adx_15m"] > 16)
        )
        dataframe["trend_down_15m"] = (
            (dataframe["close_15m"] < dataframe["ema20_15m"]) &
            (dataframe["ema20_15m"] < dataframe["ema50_15m"]) &
            (dataframe["rsi_15m"] < 48) &
            (dataframe["adx_15m"] > 16)
        )
        dataframe["trend_context_long"] = dataframe["trend_up_1h"] | dataframe["trend_up_15m"]
        dataframe["trend_context_short"] = dataframe["trend_down_1h"] | dataframe["trend_down_15m"]
        dataframe["range_market_1h"] = ~(dataframe["trend_up_1h"] | dataframe["trend_down_1h"])
        dataframe["trend_up_1h"] = dataframe["trend_up_1h"].fillna(False)
        dataframe["trend_down_1h"] = dataframe["trend_down_1h"].fillna(False)
        dataframe["trend_up_15m"] = dataframe["trend_up_15m"].fillna(False)
        dataframe["trend_down_15m"] = dataframe["trend_down_15m"].fillna(False)
        dataframe["trend_context_long"] = dataframe["trend_context_long"].fillna(False)
        dataframe["trend_context_short"] = dataframe["trend_context_short"].fillna(False)
        dataframe["range_market_1h"] = dataframe["range_market_1h"].fillna(False)
        dataframe["regime_high_vol"] = dataframe["regime_high_vol"].fillna(False)
        dataframe["regime_low_vol"] = dataframe["regime_low_vol"].fillna(False)
        dataframe["regime_balanced"] = dataframe["regime_balanced"].fillna(True)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        adx_threshold = self._profile_adjusted_value(dataframe, float(self.adx_threshold.value), 4.0, 2.0)
        volume_threshold = self._profile_adjusted_value(
            dataframe, float(self.volume_ratio_min.value), 0.20, 0.10
        )
        long_rsi_threshold = self._profile_adjusted_value(
            dataframe, float(self.long_rsi_trigger.value), 2.0, 2.0
        )
        short_rsi_threshold = self._profile_adjusted_value(
            dataframe, float(self.short_rsi_trigger.value), 2.0, 2.0
        )

        long_trend = (
            dataframe["trend_context_long"]
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
            & (dataframe["adx"] > adx_threshold)
            & (
                (dataframe["trend_up_15m"] & (dataframe["volume_ratio"] > 0.95))
                | (dataframe["volume_ratio"] > (volume_threshold - 0.15))
            )
            & (dataframe["rsi"] > 52)
            & (dataframe["macdhist"] > 0)
        )
        long_trigger = (
            qtpylib.crossed_above(dataframe["rsi"], long_rsi_threshold)
            | ((dataframe["rsi"] > 48) & qtpylib.crossed_above(dataframe["close"], dataframe["ema20"]))
        )
        long_trigger_recent = self._recent_signal(long_trigger, 12)
        long_range = (
            dataframe["range_market_1h"] & dataframe["regime_low_vol"]
            & (dataframe["rsi"] < 34)
            & (
                (dataframe["close"] < dataframe["bb_lowerband"])
                | qtpylib.crossed_below(dataframe["close"], dataframe["bb_lowerband"])
            )
            & (dataframe["volume_ratio"] > 1.00)
            & self._freqai_long_ok(dataframe)
        )

        short_alignment = dataframe["trend_down_1h"] & dataframe["trend_down_15m"]
        short_trend = (
            short_alignment
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["ema50"] < dataframe["ema200"])
            & (dataframe["adx"] > (adx_threshold + 1.0))
            & (dataframe["volume_ratio"] > (volume_threshold + 0.05))
            & (dataframe["rsi"] < 45)
            & (dataframe["macd"] < dataframe["macdsignal"])
            & (dataframe["macdhist"] < 0)
        )
        short_trigger = (
            qtpylib.crossed_below(dataframe["rsi"], short_rsi_threshold)
            | ((dataframe["rsi"] < 52) & qtpylib.crossed_below(dataframe["close"], dataframe["ema20"]))
        )
        short_trigger_recent = self._recent_signal(short_trigger, 12)
        short_range = (
            dataframe["range_market_1h"] & dataframe["regime_low_vol"]
            & (dataframe["rsi"] > 66)
            & (
                (dataframe["close"] > dataframe["bb_upperband"])
                | qtpylib.crossed_above(dataframe["close"], dataframe["bb_upperband"])
            )
            & (dataframe["volume_ratio"] > 1.00)
            & self._freqai_short_ok(dataframe)
        )

        trend_long_entry = long_trend & long_trigger_recent
        trend_short_entry = short_trend & short_trigger_recent
        trading_disabled = self._new_entries_disabled()
        if trading_disabled:
            trend_long_entry = pd.Series(False, index=dataframe.index)
            long_range = pd.Series(False, index=dataframe.index)
            trend_short_entry = pd.Series(False, index=dataframe.index)
            short_range = pd.Series(False, index=dataframe.index)
            if not getattr(self, "_trading_disabled_warned", False):
                logger.warning("New entries are disabled by trade execution settings.")
                self._trading_disabled_warned = True

        trend_long_entry, long_range, trend_short_entry, short_range = self._apply_direction_advisor_gate(
            trend_long_entry,
            long_range,
            trend_short_entry,
            short_range,
        )

        dataframe.loc[trend_long_entry, ["enter_long", "enter_tag"]] = (1, "trend_long")
        dataframe.loc[long_range, ["enter_long", "enter_tag"]] = (1, "meanrev_long")
        dataframe.loc[trend_short_entry, ["enter_short", "enter_tag"]] = (1, "trend_short")
        dataframe.loc[short_range, ["enter_short", "enter_tag"]] = (1, "meanrev_short")

        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        self._log_entry_diagnostics(
            dataframe,
            metadata,
            adx_threshold,
            volume_threshold,
            long_rsi_threshold,
            short_rsi_threshold,
            long_trend,
            long_trigger_recent,
            long_range,
            short_trend,
            short_trigger_recent,
            short_range,
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe["close"], dataframe["ema20"])
                | ((dataframe["rsi"] > 68) & (dataframe["macdhist"] < dataframe["macdhist"].shift(1)))
                | ((dataframe["close"] < dataframe["ema50"]) & (dataframe["macdhist"] < 0))
            )
            & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "long_exit")

        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["close"], dataframe["ema20"])
                | ((dataframe["rsi"] < 32) & (dataframe["macdhist"] > dataframe["macdhist"].shift(1)))
                | ((dataframe["close"] > dataframe["ema50"]) & (dataframe["macdhist"] > 0))
            )
            & (dataframe["volume"] > 0),
            ["exit_short", "exit_tag"],
        ] = (1, "short_exit")

        return dataframe

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        if not self._is_live_or_dry_run():
            return proposed_stake

        if not self.dp:
            return proposed_stake

        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
            if dataframe.empty:
                return proposed_stake

            last = dataframe.iloc[-1]
            atr_pct = float(last.get("atr_pct", 0.03) or 0.03)
            if bool(last.get("regime_high_vol", False)):
                regime_boost = 0.72
            elif bool(last.get("regime_low_vol", False)):
                regime_boost = 1.18
            else:
                regime_boost = 1.0
            trend_boost = 1.15 if (
                (side == "long" and bool(last.get("trend_up_1h", False)))
                or (side == "long" and bool(last.get("trend_up_15m", False)))
                or (side == "short" and bool(last.get("trend_down_1h", False)))
                or (side == "short" and bool(last.get("trend_down_15m", False)))
            ) else 0.80

            wallet = self.wallets.get_total_stake_amount()
            risk_budget = wallet * float(self.risk_per_trade.value)
            stop_distance = max(atr_pct * float(self.atr_stop_mult.value), 0.025)
            stake = (risk_budget / stop_distance) * trend_boost * regime_boost
            stake = min(stake, proposed_stake, max_stake)
            if min_stake is not None:
                stake = max(stake, min_stake)
            return float(stake)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("custom_stake_amount fallback for %s: %s", pair, exc)
            return proposed_stake

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        if not self._is_live_or_dry_run():
            return None
        if current_profit <= 0:
            return None
        if not self.dp:
            return None

        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
            if dataframe.empty:
                return None

            last = dataframe.iloc[-1]
            atr_pct = float(last.get("atr_pct", 0.03) or 0.03)
            profile_mult = float(self.atr_trail_mult.value)
            if bool(last.get("regime_high_vol", False)):
                profile_mult *= 1.20
            elif bool(last.get("regime_low_vol", False)):
                profile_mult *= 0.85
            trail = max(atr_pct * profile_mult, 0.01)
            if current_profit > 0.10:
                trail = min(trail, 0.02)
            elif current_profit > 0.05:
                trail = min(trail, 0.03)
            else:
                trail = min(trail, 0.05)
            return float(trail)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("custom_stoploss fallback for %s: %s", pair, exc)
            return None

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        return 1.0

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        if not self._is_live_or_dry_run():
            return True
        side = "short" if trade.is_short else "long"
        cache_key = f"{pair}:trade_exit:{getattr(trade, 'id', '')}:{exit_reason}:{current_time.isoformat()}"
        if cache_key not in self._signal_email_cache:
            self._signal_email_cache.add(cache_key)
            subject = f"[Freqtrade] {pair} {side.upper()} trade exit"
            body = "\n".join(
                [
                    f"pair: {pair}",
                    "action: exit",
                    f"side: {side}",
                    f"time: {current_time.isoformat()}",
                    f"rate: {rate:.8f}",
                    f"amount: {amount:.8f}",
                    f"order_type: {order_type}",
                    f"time_in_force: {time_in_force}",
                    f"exit_reason: {exit_reason}",
                ]
            )
            self._send_email_async(subject, body)
        return True

    def _emit_signal_email(self, dataframe: DataFrame, metadata: dict, action: str, side: str) -> None:
        if action != "entry":
            logger.debug(
                "Ignoring non-entry signal email for %s action=%s side=%s",
                metadata.get("pair", "unknown"),
                action,
                side,
            )
            return
        if not self._is_live_or_dry_run():
            return
        if dataframe.empty:
            return

        signal_col = f"enter_{side}" if action == "entry" else f"{action}_{side}"
        if signal_col not in dataframe.columns or dataframe[signal_col].iloc[-1] != 1:
            return

        last = dataframe.iloc[-1]
        candle_time = last["date"]
        if hasattr(candle_time, "isoformat"):
            candle_time = candle_time.isoformat()

        cache_key = f"{metadata['pair']}:{action}:{side}:{candle_time}"
        if cache_key in self._signal_email_cache:
            return
        self._signal_email_cache.add(cache_key)

        subject = f"[Freqtrade] {metadata['pair']} {side.upper()} {action}"
        body = "\n".join(
            [
                f"pair: {metadata['pair']}",
                f"action: {action}",
                f"side: {side}",
                f"time: {candle_time}",
                f"close: {float(last['close']):.8f}",
                f"rsi: {float(last.get('rsi', 0.0)):.2f}",
                f"adx: {float(last.get('adx', 0.0)):.2f}",
                f"atr_pct: {float(last.get('atr_pct', 0.0)):.4f}",
                f"volatility_ratio: {float(last.get('volatility_ratio', 1.0)):.2f}",
                f"volume_ratio: {float(last.get('volume_ratio', 0.0)):.2f}",
                f"regime_high_vol: {bool(last.get('regime_high_vol', False))}",
                f"regime_balanced: {bool(last.get('regime_balanced', False))}",
                f"regime_low_vol: {bool(last.get('regime_low_vol', False))}",
                f"trend_up_1h: {bool(last.get('trend_up_1h', False))}",
                f"trend_down_1h: {bool(last.get('trend_down_1h', False))}",
                f"trend_up_15m: {bool(last.get('trend_up_15m', False))}",
                f"trend_down_15m: {bool(last.get('trend_down_15m', False))}",
            ]
        )
        self._send_email_async(subject, body)
        if action == "entry":
            self._dispatch_hotcoin_signal(metadata["pair"], side, last, candle_time)

    def _dispatch_hotcoin_signal(
        self, pair: str, side: str, last: pd.Series, candle_time: str
    ) -> None:
        bridge_settings = self._load_trade_execution_settings()
        enabled_raw = bridge_settings.get(
            "hotcoin_signal_bridge_enabled",
            os.getenv("HOTCOIN_SIGNAL_BRIDGE_ENABLED", "false"),
        )
        enabled = str(enabled_raw).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            return

        amount_raw = bridge_settings.get("hotcoin_order_amount", os.getenv("HOTCOIN_ORDER_AMOUNT", "0"))
        try:
            amount = float(amount_raw)
        except ValueError:
            logger.warning("Invalid HOTCOIN_ORDER_AMOUNT=%s; Hotcoin signal skipped.", amount_raw)
            return
        if amount <= 0:
            logger.warning("HOTCOIN_ORDER_AMOUNT must be > 0; Hotcoin signal skipped.")
            return

        script = bridge_settings.get(
            "hotcoin_adapter_path",
            os.getenv("HOTCOIN_ADAPTER_PATH", "/freqtrade/scripts/hotcoin_adapter.py"),
        )
        symbol = pair.split(":")[0]
        order_side = "open_long" if side == "long" else "open_short"
        order_type = bridge_settings.get("hotcoin_order_type", os.getenv("HOTCOIN_ORDER_TYPE", "market"))
        mode = bridge_settings.get("hotcoin_mode", os.getenv("HOTCOIN_MODE", "web"))
        execute_raw = bridge_settings.get(
            "hotcoin_signal_execute",
            os.getenv("HOTCOIN_SIGNAL_EXECUTE", "false"),
        )
        execute = str(execute_raw).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        close = float(last["close"])
        signal_id = self._hotcoin_signal_id(pair, side, candle_time, close)
        duplicate_reason = self._hotcoin_duplicate_reason(bridge_settings, signal_id, pair, side)
        if duplicate_reason:
            self._append_execution_ledger(
                bridge_settings,
                {
                    "record_type": "hotcoin_execution",
                    "status": "skipped",
                    "reason": duplicate_reason,
                    "signal_id": signal_id,
                    "pair": pair,
                    "side": side,
                    "candle_time": candle_time,
                    "execute": execute,
                },
            )
            logger.info("Hotcoin signal skipped for %s %s %s: %s", pair, side, candle_time, duplicate_reason)
            return
        atr = float(last.get("atr", close * 0.02) or close * 0.02)
        stop_mult = float(
            bridge_settings.get("hotcoin_atr_stop_mult", os.getenv("HOTCOIN_ATR_STOP_MULT", str(self.atr_stop_mult.value)))
        )
        reward_mult = float(
            bridge_settings.get("hotcoin_reward_risk_mult", os.getenv("HOTCOIN_REWARD_RISK_MULT", "1.6"))
        )
        stop_distance = atr * stop_mult
        if side == "long":
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_mult)
        else:
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_mult)

        command = [
            "python",
            script,
            "order",
            "--mode",
            mode,
            "--symbol",
            symbol,
            "--side",
            order_side,
            "--amount",
            f"{amount:g}",
            "--type",
            order_type,
            "--stop-loss",
            f"{stop_loss:.8f}",
            "--take-profit",
            f"{take_profit:.8f}",
        ]
        if order_type == "limit":
            command.extend(["--price", f"{close:.8f}"])
        if execute:
            command.append("--yes")

        self._append_execution_ledger(
            bridge_settings,
            {
                "record_type": "hotcoin_execution",
                "status": "submitted",
                "signal_id": signal_id,
                "pair": pair,
                "side": side,
                "candle_time": candle_time,
                "symbol": symbol,
                "order_side": order_side,
                "order_type": order_type,
                "amount": amount,
                "price": close,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "execute": execute,
            },
        )
        threading.Thread(
            target=self._run_hotcoin_bridge,
            args=(command, pair, side, candle_time, execute, bridge_settings, signal_id),
            daemon=True,
        ).start()

    def _load_trade_execution_settings(self) -> dict:
        path = os.getenv("TRADE_EXECUTION_SETTINGS_PATH", "/freqtrade/user_data/trade_execution.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Failed to load trade execution settings from %s: %s", path, exc)
            return {}

    def _new_entries_disabled(self) -> bool:
        env_value = os.getenv("LIVE_TRADING_DISABLED", "")
        if env_value.lower() in {"1", "true", "yes", "on"}:
            return True
        settings = self._load_trade_execution_settings()
        return bool(settings.get("live_trading_disabled", False))

    @staticmethod
    def _truthy(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _direction_gate_config(self) -> dict:
        settings = self._load_trade_execution_settings()
        enabled = self._truthy(os.getenv("DIRECTION_ADVISOR_GATE_ENABLED")) or self._truthy(
            settings.get("direction_advisor_gate_enabled", False)
        )
        try:
            max_age_minutes = float(
                os.getenv(
                    "DIRECTION_ADVISOR_GATE_MAX_AGE_MINUTES",
                    settings.get("direction_advisor_gate_max_age_minutes", 90),
                )
            )
        except (TypeError, ValueError):
            max_age_minutes = 90.0
        stale_policy = str(
            os.getenv(
                "DIRECTION_ADVISOR_GATE_STALE_POLICY",
                settings.get("direction_advisor_gate_stale_policy", "normal"),
            )
        ).strip().lower()
        if stale_policy not in {"normal", "hold"}:
            stale_policy = "normal"
        return {
            "enabled": enabled,
            "path": str(
                os.getenv(
                    "DIRECTION_ADVISOR_PATH",
                    settings.get(
                        "direction_advisor_path",
                        "/freqtrade/user_data/autoopt/direction_advisor.json",
                    ),
                )
            ),
            "max_age_minutes": max(max_age_minutes, 1.0),
            "stale_policy": stale_policy,
        }

    def _load_direction_advisor_action(self) -> tuple[str, str]:
        gate = self._direction_gate_config()
        if not gate["enabled"]:
            return "normal", "disabled"
        try:
            with open(gate["path"], "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return gate["stale_policy"], "missing"
        except Exception as exc:
            logger.warning("Failed to load direction advisor from %s: %s", gate["path"], exc)
            return gate["stale_policy"], "invalid"

        created_at = payload.get("created_at")
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_seconds = (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()
        except Exception:
            return gate["stale_policy"], "invalid_created_at"
        if age_seconds > gate["max_age_minutes"] * 60:
            return gate["stale_policy"], "stale"

        action = str(payload.get("final_action") or payload.get("action") or "hold").strip().lower()
        if action not in {"long", "short", "hold"}:
            return "hold", "invalid_action"
        return action, "fresh"

    def _apply_direction_advisor_gate(
        self,
        trend_long_entry: pd.Series,
        long_range: pd.Series,
        trend_short_entry: pd.Series,
        short_range: pd.Series,
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        action, reason = self._load_direction_advisor_action()
        if action == "normal":
            return trend_long_entry, long_range, trend_short_entry, short_range

        false_series = pd.Series(False, index=trend_long_entry.index)
        if action == "long":
            trend_short_entry = false_series
            short_range = false_series
        elif action == "short":
            trend_long_entry = false_series
            long_range = false_series
        else:
            trend_long_entry = false_series
            long_range = false_series
            trend_short_entry = false_series
            short_range = false_series

        if not getattr(self, "_direction_gate_warned", False):
            logger.info("Direction advisor gate applied: action=%s reason=%s", action, reason)
            self._direction_gate_warned = True
        return trend_long_entry, long_range, trend_short_entry, short_range

    def _run_hotcoin_bridge(
        self,
        command: list[str],
        pair: str,
        side: str,
        candle_time: str,
        execute: bool,
        bridge_settings: dict,
        signal_id: str,
    ) -> None:
        try:
            env = os.environ.copy()
            if "hotcoin_session_path" in bridge_settings:
                env["HOTCOIN_SESSION_PATH"] = str(bridge_settings["hotcoin_session_path"])
            if "hotcoin_mode" in bridge_settings:
                env["HOTCOIN_MODE"] = str(bridge_settings["hotcoin_mode"])
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                self._append_execution_ledger(
                    bridge_settings,
                    {
                        "record_type": "hotcoin_execution",
                        "status": "failed",
                        "signal_id": signal_id,
                        "pair": pair,
                        "side": side,
                        "candle_time": candle_time,
                        "execute": execute,
                        "error": error,
                    },
                )
                self._maybe_disable_entries_after_hotcoin_failures(bridge_settings)
                logger.warning(
                    "Hotcoin bridge failed for %s %s %s: %s",
                    pair,
                    side,
                    candle_time,
                    error,
                )
                return
            self._append_execution_ledger(
                bridge_settings,
                {
                    "record_type": "hotcoin_execution",
                    "status": "succeeded",
                    "signal_id": signal_id,
                    "pair": pair,
                    "side": side,
                    "candle_time": candle_time,
                    "execute": execute,
                    "stdout": result.stdout.strip()[:2000],
                },
            )
            self._verify_hotcoin_position(command, bridge_settings, signal_id, pair, side, candle_time)
            logger.info(
                "Hotcoin bridge %s for %s %s %s: %s",
                "executed" if execute else "dry-run",
                pair,
                side,
                candle_time,
                result.stdout.strip()[:1000],
            )
        except Exception as exc:
            self._append_execution_ledger(
                bridge_settings,
                {
                    "record_type": "hotcoin_execution",
                    "status": "failed",
                    "signal_id": signal_id,
                    "pair": pair,
                    "side": side,
                    "candle_time": candle_time,
                    "execute": execute,
                    "error": str(exc),
                },
            )
            self._maybe_disable_entries_after_hotcoin_failures(bridge_settings)
            logger.warning("Hotcoin bridge exception for %s %s: %s", pair, side, exc)

    @staticmethod
    def _command_value(command: list[str], key: str, default: str = "") -> str:
        try:
            return command[command.index(key) + 1]
        except (ValueError, IndexError):
            return default

    def _verify_hotcoin_position(
        self,
        command: list[str],
        bridge_settings: dict,
        signal_id: str,
        pair: str,
        side: str,
        candle_time: str,
    ) -> None:
        enabled = self._truthy(
            bridge_settings.get(
                "hotcoin_verify_after_order",
                os.getenv("HOTCOIN_VERIFY_AFTER_ORDER", "true"),
            )
        )
        if not enabled:
            return
        script = command[1] if len(command) > 1 else str(
            bridge_settings.get("hotcoin_adapter_path", "/freqtrade/scripts/hotcoin_adapter.py")
        )
        mode = self._command_value(command, "--mode", str(bridge_settings.get("hotcoin_mode", "web")))
        symbol = self._command_value(command, "--symbol", pair.split(":")[0])
        verify_command = ["python", script, "positions", "--mode", mode, "--symbol", symbol]
        try:
            env = os.environ.copy()
            if "hotcoin_session_path" in bridge_settings:
                env["HOTCOIN_SESSION_PATH"] = str(bridge_settings["hotcoin_session_path"])
            if "hotcoin_mode" in bridge_settings:
                env["HOTCOIN_MODE"] = str(bridge_settings["hotcoin_mode"])
            result = subprocess.run(verify_command, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode == 0:
                self._append_execution_ledger(
                    bridge_settings,
                    {
                        "record_type": "hotcoin_execution",
                        "status": "position_snapshot",
                        "signal_id": signal_id,
                        "pair": pair,
                        "side": side,
                        "candle_time": candle_time,
                        "stdout": result.stdout.strip()[:4000],
                    },
                )
                return
            self._append_execution_ledger(
                bridge_settings,
                {
                    "record_type": "hotcoin_execution",
                    "status": "position_check_failed",
                    "signal_id": signal_id,
                    "pair": pair,
                    "side": side,
                    "candle_time": candle_time,
                    "error": result.stderr.strip() or result.stdout.strip(),
                },
            )
        except Exception as exc:
            self._append_execution_ledger(
                bridge_settings,
                {
                    "record_type": "hotcoin_execution",
                    "status": "position_check_failed",
                    "signal_id": signal_id,
                    "pair": pair,
                    "side": side,
                    "candle_time": candle_time,
                    "error": str(exc),
                },
            )

    def _hotcoin_signal_id(self, pair: str, side: str, candle_time: str, close: float) -> str:
        raw = f"{pair}|{side}|{candle_time}|{close:.8f}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _execution_ledger_path(self, settings: dict) -> Path:
        return Path(
            str(
                settings.get(
                    "execution_ledger_path",
                    os.getenv("EXECUTION_LEDGER_PATH", "/freqtrade/user_data/execution_ledger.jsonl"),
                )
            )
        )

    def _append_execution_ledger(self, settings: dict, payload: dict) -> None:
        path = self._execution_ledger_path(settings)
        record = {"created_at": datetime.now(UTC).isoformat(), **payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        except Exception as exc:
            logger.warning("Failed to append execution ledger %s: %s", path, exc)

    def _read_execution_ledger(self, settings: dict, limit: int = 300) -> list[dict]:
        path = self._execution_ledger_path(settings)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning("Failed to read execution ledger %s: %s", path, exc)
            return []
        records = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _hotcoin_duplicate_reason(self, settings: dict, signal_id: str, pair: str, side: str) -> str:
        records = self._read_execution_ledger(settings)
        active_statuses = {"submitted", "succeeded"}
        for record in reversed(records):
            if record.get("signal_id") == signal_id and record.get("status") in active_statuses:
                return "duplicate_signal_id"

        try:
            cooldown_minutes = float(
                settings.get(
                    "hotcoin_order_cooldown_minutes",
                    os.getenv("HOTCOIN_ORDER_COOLDOWN_MINUTES", "30"),
                )
            )
        except (TypeError, ValueError):
            cooldown_minutes = 30.0
        if cooldown_minutes <= 0:
            return ""
        now = datetime.now(UTC)
        for record in reversed(records):
            if record.get("pair") != pair or record.get("side") != side:
                continue
            if record.get("status") not in active_statuses:
                continue
            try:
                created = datetime.fromisoformat(str(record.get("created_at")).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
            except Exception:
                continue
            if (now - created.astimezone(UTC)).total_seconds() < cooldown_minutes * 60:
                return "pair_side_cooldown"
        return ""

    def _maybe_disable_entries_after_hotcoin_failures(self, settings: dict) -> None:
        auto_disable = self._truthy(
            settings.get(
                "auto_disable_on_hotcoin_failures",
                os.getenv("AUTO_DISABLE_ON_HOTCOIN_FAILURES", "true"),
            )
        )
        if not auto_disable:
            return
        try:
            max_failures = int(
                settings.get(
                    "max_consecutive_hotcoin_failures",
                    os.getenv("MAX_CONSECUTIVE_HOTCOIN_FAILURES", "3"),
                )
            )
        except (TypeError, ValueError):
            max_failures = 3
        if max_failures <= 0:
            return
        consecutive = 0
        for record in reversed(self._read_execution_ledger(settings, limit=100)):
            status = record.get("status")
            if status == "failed":
                consecutive += 1
                continue
            if status in {"submitted", "succeeded"}:
                break
        if consecutive < max_failures:
            return
        self._merge_trade_execution_settings(
            {
                "live_trading_disabled": True,
                "risk_fuse_reason": f"Hotcoin bridge failed {consecutive} consecutive times",
                "risk_fuse_updated_at": datetime.now(UTC).isoformat(),
            }
        )
        logger.warning("New entries disabled after %s consecutive Hotcoin failures.", consecutive)

    def _merge_trade_execution_settings(self, updates: dict) -> None:
        path = Path(os.getenv("TRADE_EXECUTION_SETTINGS_PATH", "/freqtrade/user_data/trade_execution.json"))
        try:
            current = {}
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            current.update(updates)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
        except Exception as exc:
            logger.warning("Failed to update trade execution settings %s: %s", path, exc)

    def _send_email_async(self, subject: str, body: str) -> None:
        host = os.getenv("FT_EMAIL_HOST", "smtp.qq.com")
        port = int(os.getenv("FT_EMAIL_PORT", "465"))
        username = os.getenv("FT_EMAIL_USER", "")
        password = os.getenv("FT_EMAIL_PASSWORD", "")
        sender = os.getenv("FT_EMAIL_FROM", username)
        recipient = os.getenv("FT_EMAIL_TO", "746439274@qq.com")

        if not host or not username or not password or not sender or not recipient:
            if not self._email_warned:
                logger.info(
                    "Email alerts disabled. Set FT_EMAIL_HOST/FT_EMAIL_USER/FT_EMAIL_PASSWORD "
                    "and optionally FT_EMAIL_FROM/FT_EMAIL_TO."
                )
                self._email_warned = True
            return

        threading.Thread(
            target=self._send_email,
            args=(host, port, username, password, sender, recipient, subject, body),
            daemon=True,
        ).start()

    def _send_email(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        try:
            message = EmailMessage()
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = subject
            message.set_content(body)

            context = ssl.create_default_context()
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, context=context, timeout=10)
            else:
                server = smtplib.SMTP(host, port, timeout=10)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            with server:
                server.login(username, password)
                server.send_message(message)
        except Exception as exc:  # pragma: no cover - external dependency
            logger.warning("Failed to send signal email: %s", exc)
