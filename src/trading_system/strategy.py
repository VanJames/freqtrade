from __future__ import annotations

from trading_system.indicators import atr, crossed_above, crossed_below, ema, macd, ohlcv_frame, rsi
from trading_system.models import HedgeLock, MarketFeatures, Position, PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.volatility import build_volatility_policy


class StrategyEngine:
    def __init__(
        self,
        min_stop_loss_pct: float = 0.002,
        high_vol_min_stop_loss_pct: float = 0.004,
        extreme_vol_min_stop_loss_pct: float = 0.005,
        max_stop_loss_pct: float = 0.012,
        shock_reward_risk: float = 1.15,
        trend_reward_risk: float = 1.8,
        min_take_profit_pct: float = 0.004,
    ) -> None:
        self.min_stop_loss_pct = min_stop_loss_pct
        self.high_vol_min_stop_loss_pct = high_vol_min_stop_loss_pct
        self.extreme_vol_min_stop_loss_pct = extreme_vol_min_stop_loss_pct
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

        if regime in {Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN}:
            signal = self._shock_trend_signal(symbol, regime, features, candles_5m)
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
        volatility_policy = self._volatility_policy(price, features, df)

        if regime == Regime.TREND_LONG:
            near_breakout = price >= features.range_high_4h - volatility_policy.breakout_retrace_atr * features.atr_1h
            pullback = (
                (not volatility_policy.require_near_breakout or near_breakout)
                and (not volatility_policy.require_multi_timeframe or self._multi_timeframe_aligned(df, PositionSide.LONG))
                and price >= float(ema20_5m.iloc[-1])
                and 45 <= last_rsi <= 55
            )
            if pullback and crossed_above(macd_line, signal_line):
                raw_stop = max(float(df.low.iloc[-12:].min()), price - 1.2 * features.atr_1h)
                raw_stop = self._trend_stop(price, raw_stop, features.atr_1h, PositionSide.LONG, volatility_policy)
                stop = self._cap_stop(price, raw_stop, PositionSide.LONG, volatility_policy)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.BUY,
                    position_side=PositionSide.LONG,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price + self._reward(price, stop, 2.5),
                    reason="trend_long_pullback_macd_cross",
                    metadata={"volatility_tier": volatility_policy.tier},
                )

        if regime == Regime.TREND_SHORT:
            near_breakout = price <= features.range_low_4h + volatility_policy.breakout_retrace_atr * features.atr_1h
            pullback = (
                (not volatility_policy.require_near_breakout or near_breakout)
                and (not volatility_policy.require_multi_timeframe or self._multi_timeframe_aligned(df, PositionSide.SHORT))
                and price <= float(ema20_5m.iloc[-1])
                and 45 <= last_rsi <= 55
            )
            if pullback and crossed_below(macd_line, signal_line):
                raw_stop = min(float(df.high.iloc[-12:].max()), price + 1.2 * features.atr_1h)
                raw_stop = self._trend_stop(price, raw_stop, features.atr_1h, PositionSide.SHORT, volatility_policy)
                stop = self._cap_stop(price, raw_stop, PositionSide.SHORT, volatility_policy)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.SELL,
                    position_side=PositionSide.SHORT,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price - self._reward(price, stop, 2.5),
                    reason="trend_short_pullback_macd_cross",
                    metadata={"volatility_tier": volatility_policy.tier},
                )
        return None

    def _shock_trend_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 60:
            return None
        macd_line, signal_line, _ = macd(df.close)
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        price = float(df.close.iloc[-1])
        current = df.iloc[-1]
        recent_pullback_down = bool((df.close.iloc[-4:-1] < df.open.iloc[-4:-1]).any())
        recent_pullback_up = bool((df.close.iloc[-4:-1] > df.open.iloc[-4:-1]).any())
        ema_now = float(ema20_5m.iloc[-1])
        ema_prev = float(ema20_5m.iloc[-4])
        last_rsi = float(rsi_5m.iloc[-1])
        current_4h = df.iloc[-48:]
        frame_4h = self._aggregate_bars(df, 48)
        last_4h_up = len(frame_4h) >= 2 and float(frame_4h.close.iloc[-1]) > float(frame_4h.close.iloc[-2])
        last_4h_down = len(frame_4h) >= 2 and float(frame_4h.close.iloc[-1]) < float(frame_4h.close.iloc[-2])
        trend_up_aligned = self._multi_timeframe_aligned(df, PositionSide.LONG)
        trend_down_aligned = self._multi_timeframe_aligned(df, PositionSide.SHORT)

        if (
            regime == Regime.SHOCK_TREND_UP
            and trend_up_aligned
            and features.ema20_1h >= features.ema60_1h
            and price >= features.ema20_1h * 0.998
            and last_4h_up
            and recent_pullback_down
            and float(current.close) > float(current.open)
            and crossed_above(macd_line, signal_line)
            and price >= ema_now
            and ema_now >= ema_prev
            and 45 <= last_rsi <= 62
        ):
            stop = self._cap_stop(price, float(current_4h.low.min()), PositionSide.LONG)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_TREND,
                side=Side.BUY,
                position_side=PositionSide.LONG,
                regime=regime,
                price=price,
                stop_loss=stop,
                take_profit=price + self._reward(price, stop, self.trend_reward_risk),
                reason="shock_trend_up_pullback_confirmed",
            )

        if (
            regime == Regime.SHOCK_TREND_DOWN
            and trend_down_aligned
            and features.ema20_1h <= features.ema60_1h
            and price <= features.ema20_1h * 1.002
            and last_4h_down
            and recent_pullback_up
            and float(current.close) < float(current.open)
            and crossed_below(macd_line, signal_line)
            and price <= ema_now
            and ema_now <= ema_prev
            and 38 <= last_rsi <= 55
        ):
            stop = self._cap_stop(price, float(current_4h.high.max()), PositionSide.SHORT)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_TREND,
                side=Side.SELL,
                position_side=PositionSide.SHORT,
                regime=regime,
                price=price,
                stop_loss=stop,
                take_profit=price - self._reward(price, stop, self.trend_reward_risk),
                reason="shock_trend_down_pullback_confirmed",
            )
        return None

    def _multi_timeframe_aligned(self, df_5m, side: PositionSide) -> bool:
        frame_15m = self._aggregate_bars(df_5m, 3)
        frame_1h = self._aggregate_bars(df_5m, 12)
        return (
            self._timeframe_aligned(df_5m, side, min_bars=30)
            and self._timeframe_aligned(frame_15m, side, min_bars=12)
            and self._timeframe_aligned(frame_1h, side, min_bars=6)
        )

    def _aggregate_bars(self, frame, group_size: int):
        groups = []
        rows = frame.tail((len(frame) // group_size) * group_size)
        for start in range(0, len(rows), group_size):
            chunk = rows.iloc[start : start + group_size]
            if chunk.empty:
                continue
            groups.append(
                [
                    float(chunk.ts.iloc[-1]),
                    float(chunk.open.iloc[0]),
                    float(chunk.high.max()),
                    float(chunk.low.min()),
                    float(chunk.close.iloc[-1]),
                    float(chunk.volume.sum()),
                ]
            )
        return ohlcv_frame(groups)

    def _timeframe_aligned(self, frame, side: PositionSide, min_bars: int) -> bool:
        if len(frame) < min_bars:
            return False
        closes = frame.close.astype(float)
        ema_line = ema(closes, min(20, max(3, len(closes) // 2)))
        ema_now = float(ema_line.iloc[-1])
        ema_prev = float(ema_line.iloc[-min(4, len(ema_line))])
        last_close = float(closes.iloc[-1])
        reference = float(closes.iloc[-min(4, len(closes))])
        recent_return = (last_close - reference) / reference if reference else 0.0
        if side == PositionSide.LONG:
            return last_close >= ema_now and ema_now >= ema_prev and recent_return >= -0.003
        return last_close <= ema_now and ema_now <= ema_prev and recent_return <= 0.003

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
            reward = self._reward(price, stop, self.shock_reward_risk)
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
            reward = self._reward(price, stop, self.shock_reward_risk)
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

    def _volatility_policy(self, price: float, features: MarketFeatures, df_5m):
        ret_24h = 0.0
        range_24h = 0.0
        if len(df_5m) >= 288:
            recent_24h = df_5m.iloc[-288:]
            open_24h = float(recent_24h.open.iloc[0])
            high_24h = float(recent_24h.high.max())
            low_24h = float(recent_24h.low.min())
            ret_24h = (price - open_24h) / open_24h if open_24h else 0.0
            range_24h = (high_24h - low_24h) / price if price else 0.0
        return build_volatility_policy(
            price=price,
            atr_1h=features.atr_1h,
            base_min_stop_loss_pct=self.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=self.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=self.extreme_vol_min_stop_loss_pct,
            ret_24h=ret_24h,
            range_24h=range_24h,
            range_72h=features.range_amplitude_4h,
        )

    def _trend_stop(self, price: float, raw_stop: float, atr_1h: float, side: PositionSide, volatility_policy) -> float:
        if volatility_policy.stop_buffer_atr > 0:
            buffer = volatility_policy.stop_buffer_atr * atr_1h
            if side == PositionSide.LONG:
                return min(raw_stop - buffer, price - buffer)
            return max(raw_stop + buffer, price + buffer)
        return raw_stop

    def _cap_stop(self, price: float, raw_stop: float, side: PositionSide, volatility_policy=None) -> float:
        min_stop_loss_pct = volatility_policy.min_stop_loss_pct if volatility_policy else self.min_stop_loss_pct
        min_distance = price * min_stop_loss_pct
        max_distance = price * self.max_stop_loss_pct
        if side == PositionSide.LONG:
            stop = min(raw_stop, price - min_distance)
            return max(stop, price - max_distance)
        stop = max(raw_stop, price + min_distance)
        return min(stop, price + max_distance)

    def _reward(self, price: float, stop: float, reward_risk: float) -> float:
        return max(price * self.min_take_profit_pct, reward_risk * abs(price - stop))
