from __future__ import annotations

from trading_system.indicators import atr, crossed_above, crossed_below, ema, macd, ohlcv_frame, rsi
from trading_system.models import HedgeLock, MarketFeatures, Position, PositionSide, Regime, Side, SignalType, TradeSignal


class StrategyEngine:
    def __init__(
        self,
        max_stop_loss_pct: float = 0.012,
        shock_reward_risk: float = 1.15,
        trend_reward_risk: float = 1.8,
        min_take_profit_pct: float = 0.004,
    ) -> None:
        self.max_stop_loss_pct = max_stop_loss_pct
        self.shock_reward_risk = shock_reward_risk
        self.trend_reward_risk = trend_reward_risk
        self.min_take_profit_pct = min_take_profit_pct

    def build_signals(
        self,
        symbol: str,
        regime: Regime,
        previous_regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
        positions: list[Position],
    ) -> list[TradeSignal]:
        if regime == Regime.UNKNOWN:
            return []

        signals: list[TradeSignal] = []
        signals.extend(self._transition_hedges(symbol, regime, previous_regime, features.close_1h, positions))
        if signals:
            return signals

        if regime in {Regime.TREND_LONG, Regime.TREND_SHORT}:
            signal = self._trend_signal(symbol, regime, features, candles_5m)
            return [signal] if signal else []

        if regime == Regime.SHOCK:
            signal = self._grid_signal(symbol, features, candles_5m)
            return [signal] if signal else []

        return []

    def hedge_release_signals(
        self,
        symbol: str,
        lock: HedgeLock | None,
        candles_5m: list[list[float]],
        positions: list[Position],
    ) -> list[TradeSignal]:
        if not lock or not lock.active:
            return []
        df = ohlcv_frame(candles_5m)
        if len(df) < 20:
            return []
        rsi_5m = rsi(df.close, 14)
        price = float(df.close.iloc[-1])
        release = False
        if lock.hedge_side == PositionSide.LONG:
            release = bool(rsi_5m.iloc[-2] > 70 and rsi_5m.iloc[-1] < rsi_5m.iloc[-2])
        elif lock.hedge_side == PositionSide.SHORT:
            release = bool(rsi_5m.iloc[-2] < 30 and rsi_5m.iloc[-1] > rsi_5m.iloc[-2])
        if not release:
            return []

        signals: list[TradeSignal] = []
        for pos in positions:
            if pos.side not in {lock.grid_side, lock.hedge_side} or pos.contracts <= 0:
                continue
            side = Side.SELL if pos.side == PositionSide.LONG else Side.BUY
            signals.append(
                TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.RELEASE_HEDGE,
                    side=side,
                    position_side=pos.side,
                    regime=Regime.UNKNOWN,
                    price=price,
                    stop_loss=price,
                    reason="hedge_release_5m_pullback",
                    metadata={"contracts": pos.contracts, "reduce_only": True},
                )
            )
        return signals

    def _transition_hedges(
        self,
        symbol: str,
        regime: Regime,
        previous_regime: Regime,
        price: float,
        positions: list[Position],
    ) -> list[TradeSignal]:
        if previous_regime != Regime.SHOCK:
            return []
        signals: list[TradeSignal] = []
        if regime == Regime.TREND_LONG:
            for pos in positions:
                if pos.side == PositionSide.SHORT and pos.contracts > 0:
                    signals.append(
                        TradeSignal(
                            symbol=symbol,
                            signal_type=SignalType.HEDGE_TRANSITION,
                            side=Side.BUY,
                            position_side=PositionSide.LONG,
                            regime=regime,
                            price=price,
                            stop_loss=0.0,
                            reason="shock_to_trend_long_hedge_short_grid",
                            metadata={"hedge_contracts": pos.contracts},
                        )
                    )
        if regime == Regime.TREND_SHORT:
            for pos in positions:
                if pos.side == PositionSide.LONG and pos.contracts > 0:
                    signals.append(
                        TradeSignal(
                            symbol=symbol,
                            signal_type=SignalType.HEDGE_TRANSITION,
                            side=Side.SELL,
                            position_side=PositionSide.SHORT,
                            regime=regime,
                            price=price,
                            stop_loss=0.0,
                            reason="shock_to_trend_short_hedge_long_grid",
                            metadata={"hedge_contracts": pos.contracts},
                        )
                    )
        return signals

    def _trend_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 60 or features.atr_1h <= 0:
            return None
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        macd_line, signal_line, _ = macd(df.close)
        price = float(df.close.iloc[-1])
        last_rsi = float(rsi_5m.iloc[-1])

        if regime == Regime.TREND_LONG:
            pullback = price >= float(ema20_5m.iloc[-1]) and 45 <= last_rsi <= 55
            if pullback and crossed_above(macd_line, signal_line):
                stop = self._cap_stop(price, max(float(df.low.iloc[-12:].min()), price - 1.2 * features.atr_1h), PositionSide.LONG)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.BUY,
                    position_side=PositionSide.LONG,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price + 2.5 * abs(price - stop),
                    reason="trend_long_pullback_macd_cross",
                )

        if regime == Regime.TREND_SHORT:
            pullback = price <= float(ema20_5m.iloc[-1]) and 45 <= last_rsi <= 55
            if pullback and crossed_below(macd_line, signal_line):
                stop = self._cap_stop(price, min(float(df.high.iloc[-12:].max()), price + 1.2 * features.atr_1h), PositionSide.SHORT)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.SELL,
                    position_side=PositionSide.SHORT,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price - 2.5 * abs(stop - price),
                    reason="trend_short_pullback_macd_cross",
                )
        return None

    def _grid_signal(
        self,
        symbol: str,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 35 or features.atr_1h <= 0:
            return None
        midpoint = (features.range_high_4h + features.range_low_4h) / 2
        range_width = features.range_high_4h - features.range_low_4h
        long_zone_low = features.range_low_4h + 0.20 * range_width
        long_zone_high = features.range_low_4h + 0.45 * range_width
        short_zone_low = features.range_low_4h + 0.55 * range_width
        short_zone_high = features.range_low_4h + 0.80 * range_width
        price = float(df.close.iloc[-1])
        macd_line, signal_line, _ = macd(df.close)
        rsi_5m = rsi(df.close, 14)
        last_rsi_5m = float(rsi_5m.iloc[-1])
        atr_5m = float(atr(df.high, df.low, df.close).iloc[-1]) or features.atr_1h

        if (
            long_zone_low <= price <= min(midpoint, long_zone_high)
            and features.rsi_1h < 35
            and last_rsi_5m < 48
            and crossed_above(macd_line, signal_line)
        ):
            stop = price - 1.0 * atr_5m
            stop = self._cap_stop(price, stop, PositionSide.LONG)
            reward = max(price * self.min_take_profit_pct, self.shock_reward_risk * abs(price - stop))
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_GRID,
                side=Side.BUY,
                position_side=PositionSide.LONG,
                regime=Regime.SHOCK,
                price=price,
                stop_loss=stop,
                take_profit=price + reward,
                reason="shock_grid_long_inner_reversal",
                metadata={"grid_layers": [1.0, 0.5, 0.5], "grid_spacing_atr": [1.2, 1.8]},
            )

        if (
            max(midpoint, short_zone_low) <= price <= short_zone_high
            and features.rsi_1h > 65
            and last_rsi_5m > 52
            and crossed_below(macd_line, signal_line)
        ):
            stop = price + 1.0 * atr_5m
            stop = self._cap_stop(price, stop, PositionSide.SHORT)
            reward = max(price * self.min_take_profit_pct, self.shock_reward_risk * abs(stop - price))
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_GRID,
                side=Side.SELL,
                position_side=PositionSide.SHORT,
                regime=Regime.SHOCK,
                price=price,
                stop_loss=stop,
                take_profit=price - reward,
                reason="shock_grid_short_inner_reversal",
                metadata={"grid_layers": [1.0, 0.5, 0.5], "grid_spacing_atr": [1.2, 1.8]},
            )
        return None

    def _cap_stop(self, price: float, raw_stop: float, side: PositionSide) -> float:
        max_distance = price * self.max_stop_loss_pct
        if side == PositionSide.LONG:
            return max(raw_stop, price - max_distance)
        return min(raw_stop, price + max_distance)
