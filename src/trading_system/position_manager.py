from __future__ import annotations

from trading_system.models import Position, PositionSide, Regime, Side, SignalType, TradeSignal, TrailingState


class PositionManager:
    def __init__(self) -> None:
        self.trailing: dict[tuple[str, PositionSide], TrailingState] = {}

    def register_entry(self, signal: TradeSignal, atr_value: float) -> None:
        if signal.regime not in {
            Regime.TREND_LONG,
            Regime.TREND_SHORT,
            Regime.SHOCK_TREND_UP,
            Regime.SHOCK_TREND_DOWN,
        }:
            return
        key = (signal.symbol, signal.position_side)
        self.trailing[key] = TrailingState(
            symbol=signal.symbol,
            position_side=signal.position_side,
            entry_price=signal.price,
            atr=atr_value,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            trailing_gap_pct=signal.metadata.get("trailing_gap_pct"),
            min_trailing_activate_r=float(signal.metadata.get("min_trailing_activate_r", 1.0)),
            risk_multiplier=float(signal.metadata.get("risk_multiplier", 1.0)),
            highest_price=signal.price,
            lowest_price=signal.price,
        )

    def exit_signals(
        self,
        symbol: str,
        price: float,
        atr_value: float,
        positions: list[Position],
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for pos in positions:
            key = (pos.symbol, pos.side)
            state = self.trailing.get(key)
            if not state:
                state = TrailingState(
                    symbol=pos.symbol,
                    position_side=pos.side,
                    entry_price=pos.entry_price,
                    atr=atr_value,
                    stop_loss=pos.metadata.get("stop_loss", 0.0),
                    take_profit=pos.metadata.get("take_profit"),
                    risk_multiplier=float(pos.metadata.get("risk_multiplier", 1.0)),
                    highest_price=pos.entry_price,
                    lowest_price=pos.entry_price,
                )
                self.trailing[key] = state
            state.atr = atr_value or state.atr
            if pos.side == PositionSide.LONG:
                state.highest_price = max(state.highest_price, price)
                if state.trailing_gap_pct and state.stop_loss > 0:
                    risk = abs(state.entry_price - state.stop_loss)
                    if risk > 0 and state.highest_price - state.entry_price >= state.min_trailing_activate_r * risk:
                        state.stop_loss = max(state.stop_loss, state.highest_price * (1 - state.trailing_gap_pct))
                        state.active = True
                if price - state.entry_price > 2.0 * state.atr:
                    state.active = True
                    state.stop_loss = max(state.stop_loss, state.highest_price - 1.5 * state.atr)
                if state.stop_loss > 0 and price <= state.stop_loss:
                    reason = "long_trailing_stop" if state.active else "long_stop_loss"
                    signals.append(self._exit_signal(symbol, Side.SELL, PositionSide.LONG, price, pos.contracts, reason, state.risk_multiplier))
                elif state.take_profit is not None and price >= state.take_profit:
                    signals.append(self._exit_signal(symbol, Side.SELL, PositionSide.LONG, price, pos.contracts, "long_take_profit", state.risk_multiplier))
            else:
                state.lowest_price = min(state.lowest_price or price, price)
                if state.trailing_gap_pct and state.stop_loss > 0:
                    risk = abs(state.stop_loss - state.entry_price)
                    if risk > 0 and state.entry_price - state.lowest_price >= state.min_trailing_activate_r * risk:
                        state.stop_loss = min(state.stop_loss, state.lowest_price * (1 + state.trailing_gap_pct))
                        state.active = True
                if state.entry_price - price > 2.0 * state.atr:
                    state.active = True
                    state.stop_loss = min(state.stop_loss, state.lowest_price + 1.5 * state.atr)
                if state.stop_loss > 0 and price >= state.stop_loss:
                    reason = "short_trailing_stop" if state.active else "short_stop_loss"
                    signals.append(self._exit_signal(symbol, Side.BUY, PositionSide.SHORT, price, pos.contracts, reason, state.risk_multiplier))
                elif state.take_profit is not None and price <= state.take_profit:
                    signals.append(self._exit_signal(symbol, Side.BUY, PositionSide.SHORT, price, pos.contracts, "short_take_profit", state.risk_multiplier))
        return signals

    def _exit_signal(
        self,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        price: float,
        contracts: float,
        reason: str,
        risk_multiplier: float,
    ) -> TradeSignal:
        return TradeSignal(
            symbol=symbol,
            signal_type=SignalType.EXIT,
            side=side,
            position_side=position_side,
            regime=Regime.UNKNOWN,
            price=price,
            stop_loss=price,
            reason=reason,
            metadata={"contracts": contracts, "reduce_only": True, "risk_multiplier": risk_multiplier},
        )
