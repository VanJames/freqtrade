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

        adx_series, plus_di, minus_di = directional_indicators(df_1h.high, df_1h.low, df_1h.close)
        atr_1h = atr(df_1h.high, df_1h.low, df_1h.close)
        atr_4h = atr(df_4h.high, df_4h.low, df_4h.close)
        ema20 = ema(df_1h.close, 20)
        ema60 = ema(df_1h.close, 60)
        rsi_1h = rsi(df_1h.close, 14)

        range_4h = df_4h.iloc[-42:]
        high_42 = float(range_4h.high.max())
        low_42 = float(range_4h.low.min())
        amplitude = (high_42 - low_42) / low_42 if low_42 else 0.0
        close = float(df_1h.close.iloc[-1])
        last_3 = df_1h.iloc[-3:]
        recent_not_breaking = bool(last_3.high.max() <= high_42 and last_3.low.min() >= low_42)
        structural_regime = self._structural_4h_drift(df_4h.iloc[-6:])

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
            oi_change_4h=foresight.oi_change_4h,
            short_liq_p95_hit=foresight.short_liq_p95_hit,
            long_liq_p95_hit=foresight.long_liq_p95_hit,
        )

        raw_trend_long = (
            close > high_42
            and features.adx > 25
            and features.plus_di > features.minus_di
            and foresight.short_liq_p95_hit
            and foresight.oi_change_4h > 0.15
        )
        raw_trend_short = (
            close < low_42
            and features.adx > 25
            and features.minus_di > features.plus_di
            and foresight.long_liq_p95_hit
            and foresight.oi_change_4h > 0.15
        )
        trend_long = raw_trend_long and self._daily_trend_confirmed(df_4h, PositionSide.LONG)
        trend_short = raw_trend_short and self._daily_trend_confirmed(df_4h, PositionSide.SHORT)
        directional_regime = (
            self._directional_breakout_regime(
                df_1h,
                features.ema20_1h,
                features.ema60_1h,
            )
            if symbol.startswith("SOL/")
            else None
        )
        shock = amplitude <= 0.08 and recent_not_breaking and features.adx < 18
        shock_trend = amplitude <= 0.12 and features.adx < 25

        if trend_long:
            regime = Regime.TREND_LONG
        elif trend_short:
            regime = Regime.TREND_SHORT
        elif raw_trend_long:
            regime = Regime.SHOCK_TREND_UP
        elif raw_trend_short:
            regime = Regime.SHOCK_TREND_DOWN
        elif directional_regime is not None:
            regime = directional_regime
        elif symbol.startswith("SOL/") and self._directional_breakout_active(df_1h):
            regime = Regime.UNKNOWN
        elif structural_regime is not None:
            regime = structural_regime
        elif shock:
            regime = Regime.SHOCK
        elif shock_trend and features.ema20_1h > features.ema60_1h:
            regime = Regime.SHOCK_TREND_UP
        elif shock_trend and features.ema20_1h < features.ema60_1h:
            regime = Regime.SHOCK_TREND_DOWN
        else:
            regime = Regime.UNKNOWN

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
