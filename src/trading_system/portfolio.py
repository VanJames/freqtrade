from __future__ import annotations

from trading_system.indicators import ohlcv_frame
from trading_system.models import PositionSide, Regime, SignalType, TradeSignal


class AlphaFilter:
    def filter(
        self,
        signals: list[TradeSignal],
        candles_1h: dict[str, list[list[float]]],
        btc_symbol: str = "BTC/USDT:USDT",
        keep_per_direction: int = 2,
    ) -> list[TradeSignal]:
        passthrough = [
            signal
            for signal in signals
            if signal.signal_type not in {SignalType.ENTER_TREND, SignalType.ENTER_GRID}
        ]
        candidates = [
            signal
            for signal in signals
            if signal.signal_type in {SignalType.ENTER_TREND, SignalType.ENTER_GRID}
        ]
        if not candidates:
            return passthrough

        btc_return = self._return(candles_1h.get(btc_symbol, []))
        longs = [signal for signal in candidates if signal.position_side == PositionSide.LONG]
        shorts = [signal for signal in candidates if signal.position_side == PositionSide.SHORT]

        scored_longs = sorted(
            longs,
            key=lambda signal: self._return(candles_1h.get(signal.symbol, [])) - btc_return,
            reverse=True,
        )
        scored_shorts = sorted(
            shorts,
            key=lambda signal: self._return(candles_1h.get(signal.symbol, [])) - btc_return,
        )
        return passthrough + scored_longs[:keep_per_direction] + scored_shorts[:keep_per_direction]

    def _return(self, candles: list[list[float]], lookback: int = 24) -> float:
        df = ohlcv_frame(candles)
        if len(df) < 2:
            return 0.0
        window = df.iloc[-lookback:] if len(df) >= lookback else df
        start = float(window.close.iloc[0])
        end = float(window.close.iloc[-1])
        return (end - start) / start if start else 0.0


class GridPlanner:
    def orders_for_signal(self, signal: TradeSignal, base_size: float, atr_value: float) -> list[tuple[float, float]]:
        layers = signal.metadata.get("grid_layers", [1.0])
        spacing = signal.metadata.get("grid_spacing_atr", [])
        planned: list[tuple[float, float]] = []
        for index, ratio in enumerate(layers):
            if index == 0:
                planned.append((signal.price, base_size * float(ratio)))
                continue
            distance = float(spacing[min(index - 1, len(spacing) - 1)]) * atr_value if spacing else atr_value
            if signal.position_side == PositionSide.LONG:
                price = signal.price - distance
            else:
                price = signal.price + distance
            planned.append((price, base_size * float(ratio)))
        return planned

