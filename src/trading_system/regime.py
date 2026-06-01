from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger

import pandas as pd

from trading_system.indicators import atr, directional_indicators, ema, ohlcv_frame, rsi
from trading_system.models import MarketFeatures, PositionSide, Regime

logger = getLogger(__name__)


@dataclass(slots=True)
class ForesightData:
    oi_change_4h: float = 0.0
    short_liq_p95_hit: bool = False
    long_liq_p95_hit: bool = False


class MarketRegimeClassifier:
    def __init__(self) -> None:
        self.regimes: dict[str, Regime] = {}

    def classify(
        self,
        symbol: str,
        candles_1h: list[list[float]],
        candles_4h: list[list[float]],
        foresight: ForesightData | None = None,
    ) -> tuple[Regime, MarketFeatures]:
        foresight = foresight or ForesightData()
        df_1h = ohlcv_frame(candles_1h)
        df_4h = ohlcv_frame(candles_4h)
        if len(df_1h) < 60 or len(df_4h) < 45:
            return Regime.UNKNOWN, MarketFeatures()

        regime, features = classify_user_4h_market(df_1h, df_4h, symbol)
        features.oi_change_4h = foresight.oi_change_4h
        features.short_liq_p95_hit = foresight.short_liq_p95_hit
        features.long_liq_p95_hit = foresight.long_liq_p95_hit

        previous = self.regimes.get(symbol)
        if previous != regime:
            logger.info("market regime changed symbol=%s previous=%s current=%s", symbol, previous, regime)
        self.regimes[symbol] = regime
        return regime, features

    def _daily_trend_confirmed(self, df_4h, side: PositionSide) -> bool:
        if not isinstance(df_4h.index, pd.DatetimeIndex) and "ts" in df_4h.columns:
            df_4h = df_4h.copy()
            df_4h.index = pd.to_datetime(df_4h.ts, unit="ms", utc=True)
        daily = (
            df_4h.resample("1D")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
        )
        if len(daily) < 3:
            return False
        last_2 = daily.iloc[-2:]
        previous = daily.iloc[-3]
        current = daily.iloc[-1]
        ema_fast = ema(daily.close.astype(float), 3)
        ema_slow = ema(daily.close.astype(float), 5)
        if side == PositionSide.LONG:
            two_green = bool((last_2.close > last_2.open).all()) and float(current.close) > float(previous.close)
            breaks_previous_high = float(current.close) > float(previous.high)
            ema_confirm = float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1]) and float(current.close) > float(
                previous.close
            )
            return two_green or breaks_previous_high or ema_confirm
        two_red = bool((last_2.close < last_2.open).all()) and float(current.close) < float(previous.close)
        breaks_previous_low = float(current.close) < float(previous.low)
        ema_confirm = float(ema_fast.iloc[-1]) < float(ema_slow.iloc[-1]) and float(current.close) < float(
            previous.close
        )
        return two_red or breaks_previous_low or ema_confirm

    def _structural_4h_drift(self, recent_6) -> Regime | None:
        rising_high_steps = sum(
            1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) > float(left)
        )
        rising_low_steps = sum(
            1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) > float(left)
        )
        falling_high_steps = sum(
            1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) < float(left)
        )
        falling_low_steps = sum(
            1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) < float(left)
        )
        up_structure = rising_high_steps >= 3 or rising_low_steps >= 3
        down_structure = falling_low_steps >= 3 or falling_high_steps >= 3
        if up_structure and not down_structure:
            return Regime.SHOCK_TREND_UP
        if down_structure and not up_structure:
            return Regime.SHOCK_TREND_DOWN
        if up_structure and down_structure:
            return Regime.UNKNOWN
        return None

    def _directional_breakout_regime(
        self,
        df_1h,
        ema20_1h: float,
        ema60_1h: float,
    ) -> Regime | None:
        if len(df_1h) < 72:
            return None
        recent_24h = df_1h.iloc[-24:]
        recent_72h = df_1h.iloc[-72:]
        close = float(df_1h.close.iloc[-1])
        open_24h = float(recent_24h.open.iloc[0])
        open_72h = float(recent_72h.open.iloc[0])
        high_24h = float(recent_24h.high.max())
        low_24h = float(recent_24h.low.min())
        high_72h = float(recent_72h.high.max())
        low_72h = float(recent_72h.low.min())
        ret_24h = (close - open_24h) / open_24h if open_24h else 0.0
        ret_72h = (close - open_72h) / open_72h if open_72h else 0.0
        range_24h = (high_24h - low_24h) / close if close else 0.0
        range_72h = (high_72h - low_72h) / close if close else 0.0
        close_position = (close - low_72h) / (high_72h - low_72h) if high_72h > low_72h else 0.5
        wide_directional = abs(ret_72h) >= 0.04 and range_72h >= 0.07
        short_directional = abs(ret_24h) >= 0.03 and range_24h >= 0.035
        if not (wide_directional or short_directional):
            return None
        if ret_72h > 0 and ret_24h > -0.01 and ema20_1h >= ema60_1h and close_position >= 0.55:
            return Regime.SHOCK_TREND_UP
        if ret_72h < 0 and ret_24h < 0.01 and ema20_1h <= ema60_1h and close_position <= 0.45:
            return Regime.SHOCK_TREND_DOWN
        return None

    def _directional_features(self, df_1h) -> dict[str, float]:
        if len(df_1h) < 72:
            return {
                "ret_24h": 0.0,
                "ret_72h": 0.0,
                "range_24h": 0.0,
                "range_72h": 0.0,
                "close_position_72h": 0.5,
            }
        recent_24h = df_1h.iloc[-24:]
        recent_72h = df_1h.iloc[-72:]
        close = float(df_1h.close.iloc[-1])
        open_24h = float(recent_24h.open.iloc[0])
        open_72h = float(recent_72h.open.iloc[0])
        high_24h = float(recent_24h.high.max())
        low_24h = float(recent_24h.low.min())
        high_72h = float(recent_72h.high.max())
        low_72h = float(recent_72h.low.min())
        return {
            "ret_24h": (close - open_24h) / open_24h if open_24h else 0.0,
            "ret_72h": (close - open_72h) / open_72h if open_72h else 0.0,
            "range_24h": (high_24h - low_24h) / close if close else 0.0,
            "range_72h": (high_72h - low_72h) / close if close else 0.0,
            "close_position_72h": (close - low_72h) / (high_72h - low_72h) if high_72h > low_72h else 0.5,
        }

    def _directional_breakout_active(self, df_1h) -> bool:
        if len(df_1h) < 72:
            return False
        recent_24h = df_1h.iloc[-24:]
        recent_72h = df_1h.iloc[-72:]
        close = float(df_1h.close.iloc[-1])
        open_24h = float(recent_24h.open.iloc[0])
        open_72h = float(recent_72h.open.iloc[0])
        high_24h = float(recent_24h.high.max())
        low_24h = float(recent_24h.low.min())
        high_72h = float(recent_72h.high.max())
        low_72h = float(recent_72h.low.min())
        ret_24h = (close - open_24h) / open_24h if open_24h else 0.0
        ret_72h = (close - open_72h) / open_72h if open_72h else 0.0
        range_24h = (high_24h - low_24h) / close if close else 0.0
        range_72h = (high_72h - low_72h) / close if close else 0.0
        return (abs(ret_72h) >= 0.04 and range_72h >= 0.07) or (
            abs(ret_24h) >= 0.03 and range_24h >= 0.035
        )


def classify_user_4h_market(
    history_1h: pd.DataFrame,
    history_4h: pd.DataFrame,
    symbol: str = "",
) -> tuple[Regime, MarketFeatures]:
    adx_series, plus_di, minus_di = directional_indicators(history_1h.high, history_1h.low, history_1h.close)
    atr_1h = atr(history_1h.high, history_1h.low, history_1h.close)
    atr_4h = atr(history_4h.high, history_4h.low, history_4h.close)
    ema20 = ema(history_1h.close, 20)
    ema60 = ema(history_1h.close, 60)
    rsi_1h = rsi(history_1h.close, 14)

    window = history_4h.iloc[-42:]
    recent_3 = history_4h.iloc[-3:]
    recent_6 = history_4h.iloc[-6:]
    high_42 = float(window.high.max())
    low_42 = float(window.low.min())
    high_in_last_2 = bool(float(window.high.iloc[-2:].max()) > float(window.high.iloc[:-2].max()))
    low_in_last_2 = bool(float(window.low.iloc[-2:].min()) < float(window.low.iloc[:-2].min()))
    close = float(history_1h.close.iloc[-1])
    amplitude = (high_42 - low_42) / low_42 if low_42 else 0.0
    directional = directional_breakout_features(history_1h)

    features = MarketFeatures(
        adx=float(adx_series.iloc[-1]),
        plus_di=float(plus_di.iloc[-1]),
        minus_di=float(minus_di.iloc[-1]),
        atr_1h=float(atr_1h.iloc[-1]),
        atr_4h=float(atr_4h.iloc[-1]),
        ema20_1h=float(ema20.iloc[-1]),
        ema60_1h=float(ema60.iloc[-1]),
        rsi_1h=float(rsi_1h.iloc[-1]),
        range_high_4h=high_42,
        range_low_4h=low_42,
        range_amplitude_4h=amplitude,
        close_1h=close,
        current_4h_low=float(history_4h.low.iloc[-1]),
        current_4h_high=float(history_4h.high.iloc[-1]),
        previous_1h_low=float(history_1h.low.iloc[-1]),
        previous_1h_high=float(history_1h.high.iloc[-1]),
        last_4h_close=float(history_4h.close.iloc[-1]),
        prev_4h_close=float(history_4h.close.iloc[-2]),
        ret_24h=directional["ret_24h"],
        ret_72h=directional["ret_72h"],
        range_24h=directional["range_24h"],
        range_72h=directional["range_72h"],
        close_position_72h=directional["close_position_72h"],
    )

    up_3 = bool(
        recent_3.low.iloc[1] > recent_3.low.iloc[0]
        and recent_3.high.iloc[1] > recent_3.high.iloc[0]
        and recent_3.low.iloc[2] > recent_3.low.iloc[1]
        and recent_3.high.iloc[2] > recent_3.high.iloc[1]
        and (float(recent_3.high.max()) >= high_42 or high_in_last_2)
    )
    down_3 = bool(
        recent_3.low.iloc[1] < recent_3.low.iloc[0]
        and recent_3.high.iloc[1] < recent_3.high.iloc[0]
        and recent_3.low.iloc[2] < recent_3.low.iloc[1]
        and recent_3.high.iloc[2] < recent_3.high.iloc[1]
        and (float(recent_3.low.min()) <= low_42 or low_in_last_2)
    )
    if up_3 and daily_trend_confirmed(history_4h, PositionSide.LONG):
        return Regime.TREND_LONG, features
    if up_3:
        return Regime.SHOCK_TREND_UP, features
    if down_3 and daily_trend_confirmed(history_4h, PositionSide.SHORT):
        return Regime.TREND_SHORT, features
    if down_3:
        return Regime.SHOCK_TREND_DOWN, features

    alternating = count_4h_direction_changes(recent_6) >= 3
    rising_high_steps = sum(
        1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) > float(left)
    )
    rising_low_steps = sum(
        1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) > float(left)
    )
    falling_high_steps = sum(
        1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) < float(left)
    )
    falling_low_steps = sum(
        1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) < float(left)
    )
    if alternating and rising_high_steps >= 3 and rising_low_steps >= 2 and features.ema20_1h > features.ema60_1h:
        return Regime.SHOCK_TREND_UP, features
    if alternating and falling_low_steps >= 3 and falling_high_steps >= 2 and features.ema20_1h < features.ema60_1h:
        return Regime.SHOCK_TREND_DOWN, features

    structural_regime = classify_structural_4h_drift(
        rising_high_steps,
        rising_low_steps,
        falling_high_steps,
        falling_low_steps,
    )
    if structural_regime is not None:
        return structural_regime, features

    directional_regime = classify_directional_breakout(directional, features.ema20_1h, features.ema60_1h)
    if directional_regime is not None:
        return directional_regime, features

    if not high_in_last_2 and not low_in_last_2:
        return Regime.SHOCK, features
    return Regime.UNKNOWN, features


def market_features_to_backtest_dict(features: MarketFeatures, history_1h: pd.DataFrame, history_4h: pd.DataFrame) -> dict[str, float]:
    return {
        "adx": features.adx,
        "plus_di": features.plus_di,
        "minus_di": features.minus_di,
        "atr_1h": features.atr_1h,
        "atr_4h": features.atr_4h,
        "ema20_1h": features.ema20_1h,
        "ema60_1h": features.ema60_1h,
        "rsi_1h": features.rsi_1h,
        "high_42": features.range_high_4h,
        "low_42": features.range_low_4h,
        "midpoint": (features.range_high_4h + features.range_low_4h) / 2,
        "amplitude": features.range_amplitude_4h,
        "current_4h_low": features.current_4h_low,
        "current_4h_high": features.current_4h_high,
        "previous_1h_low": features.previous_1h_low,
        "previous_1h_high": features.previous_1h_high,
        "last_4h_close": features.last_4h_close,
        "prev_4h_close": features.prev_4h_close,
        "close": features.close_1h,
        "ret_24h": features.ret_24h,
        "ret_72h": features.ret_72h,
        "range_24h": features.range_24h,
        "range_72h": features.range_72h,
        "close_position_72h": features.close_position_72h,
    }


def classify_structural_4h_drift(
    rising_high_steps: int,
    rising_low_steps: int,
    falling_high_steps: int,
    falling_low_steps: int,
) -> Regime | None:
    up_structure = rising_high_steps >= 3 or rising_low_steps >= 3
    down_structure = falling_low_steps >= 3 or falling_high_steps >= 3
    if up_structure and not down_structure:
        return Regime.SHOCK_TREND_UP
    if down_structure and not up_structure:
        return Regime.SHOCK_TREND_DOWN
    if up_structure and down_structure:
        return Regime.UNKNOWN
    return None


def daily_trend_confirmed(history_4h: pd.DataFrame, side: PositionSide) -> bool:
    daily = resample_history(history_4h, "1D")
    if len(daily) < 3:
        return False
    last_2 = daily.iloc[-2:]
    previous = daily.iloc[-3]
    current = daily.iloc[-1]
    close_series = daily.close.astype(float)
    ema_fast = ema(close_series, 3)
    ema_slow = ema(close_series, 5)
    if side == PositionSide.LONG:
        two_green = bool((last_2.close > last_2.open).all()) and float(current.close) > float(previous.close)
        breaks_previous_high = float(current.close) > float(previous.high)
        ema_confirm = float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1]) and float(current.close) > float(
            previous.close
        )
        return two_green or breaks_previous_high or ema_confirm
    two_red = bool((last_2.close < last_2.open).all()) and float(current.close) < float(previous.close)
    breaks_previous_low = float(current.close) < float(previous.low)
    ema_confirm = float(ema_fast.iloc[-1]) < float(ema_slow.iloc[-1]) and float(current.close) < float(
        previous.close
    )
    return two_red or breaks_previous_low or ema_confirm


def resample_history(history: pd.DataFrame, rule: str) -> pd.DataFrame:
    frame = history.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.ts, unit="ms", utc=True)
    return frame.resample(rule).agg(
        {
            "ts": "last",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()


def directional_breakout_features(history_1h: pd.DataFrame) -> dict[str, float]:
    if len(history_1h) < 72:
        return {
            "ret_24h": 0.0,
            "ret_72h": 0.0,
            "range_24h": 0.0,
            "range_72h": 0.0,
            "close_position_72h": 0.5,
        }
    recent_24h = history_1h.iloc[-24:]
    recent_72h = history_1h.iloc[-72:]
    close = float(history_1h.close.iloc[-1])
    open_24h = float(recent_24h.open.iloc[0])
    open_72h = float(recent_72h.open.iloc[0])
    high_24h = float(recent_24h.high.max())
    low_24h = float(recent_24h.low.min())
    high_72h = float(recent_72h.high.max())
    low_72h = float(recent_72h.low.min())
    return {
        "ret_24h": (close - open_24h) / open_24h if open_24h else 0.0,
        "ret_72h": (close - open_72h) / open_72h if open_72h else 0.0,
        "range_24h": (high_24h - low_24h) / close if close else 0.0,
        "range_72h": (high_72h - low_72h) / close if close else 0.0,
        "close_position_72h": (close - low_72h) / (high_72h - low_72h) if high_72h > low_72h else 0.5,
    }


def classify_directional_breakout(
    features: dict[str, float],
    ema20_1h: float,
    ema60_1h: float,
) -> Regime | None:
    if not directional_breakout_active(features):
        return None
    if (
        features["ret_72h"] > 0
        and features["ret_24h"] > -0.01
        and ema20_1h >= ema60_1h
        and features["close_position_72h"] >= 0.55
    ):
        return Regime.SHOCK_TREND_UP
    if (
        features["ret_72h"] < 0
        and features["ret_24h"] < 0.01
        and ema20_1h <= ema60_1h
        and features["close_position_72h"] <= 0.45
    ):
        return Regime.SHOCK_TREND_DOWN
    return None


def directional_breakout_active(features: dict[str, float]) -> bool:
    wide_directional = abs(features["ret_72h"]) >= 0.04 and features["range_72h"] >= 0.07
    short_directional = abs(features["ret_24h"]) >= 0.03 and features["range_24h"] >= 0.035
    return wide_directional or short_directional


def count_4h_direction_changes(frame: pd.DataFrame) -> int:
    directions = []
    for _, row in frame.iterrows():
        if float(row.close) > float(row.open):
            directions.append(1)
        elif float(row.close) < float(row.open):
            directions.append(-1)
        else:
            directions.append(0)
    compact = [item for item in directions if item]
    return sum(1 for left, right in zip(compact, compact[1:]) if left != right)
