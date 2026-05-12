import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from datetime import datetime
from typing import Optional

import numpy as np
import talib.abstract as ta
import pandas as pd
from pandas import DataFrame
from technical import qtpylib

from freqtrade.enums import RunMode
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, Trade, informative


logger = logging.getLogger(__name__)


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
        self._email_warned = False
        self._freqai_warned = False

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

        trend_long_entry = long_trend & long_trigger
        trend_short_entry = short_trend & short_trigger
        dataframe.loc[trend_long_entry, ["enter_long", "enter_tag"]] = (1, "trend_long")
        dataframe.loc[long_range, ["enter_long", "enter_tag"]] = (1, "meanrev_long")
        dataframe.loc[trend_short_entry, ["enter_short", "enter_tag"]] = (1, "trend_short")
        dataframe.loc[short_range, ["enter_short", "enter_tag"]] = (1, "meanrev_short")

        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
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
        if self.config.get("runmode") not in (RunMode.DRY_RUN, RunMode.LIVE):
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
        if self.config.get("runmode") not in (RunMode.DRY_RUN, RunMode.LIVE):
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
        if self.config.get("runmode") not in (RunMode.DRY_RUN, RunMode.LIVE):
            return
        if dataframe.empty:
            return

        signal_col = f"{action}_{side}"
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
