from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger

from trading_system.indicators import atr, directional_indicators, ema, ohlcv_frame, rsi
from trading_system.models import MarketFeatures, Regime

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

        trend_long = (
            close > high_42
            and features.adx > 25
            and features.plus_di > features.minus_di
            and foresight.short_liq_p95_hit
            and foresight.oi_change_4h > 0.15
        )
        trend_short = (
            close < low_42
            and features.adx > 25
            and features.minus_di > features.plus_di
            and foresight.long_liq_p95_hit
            and foresight.oi_change_4h > 0.15
        )
        shock = amplitude <= 0.08 and recent_not_breaking and features.adx < 18
        shock_trend = amplitude <= 0.12 and features.adx < 25

        if trend_long:
            regime = Regime.TREND_LONG
        elif trend_short:
            regime = Regime.TREND_SHORT
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
