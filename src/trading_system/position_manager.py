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
                    highest_price=pos.entry_price,
                    lowest_price=pos.entry_price,
                )
                self.trailing[key] = state
            state.atr = atr_value or state.atr
            if pos.side == PositionSide.LONG:
                state.highest_price = max(state.highest_price, price)
                if price - state.entry_price > 2.0 * state.atr:
                    state.active = True
                trigger = state.highest_price - 1.5 * state.atr
                if state.active and price <= trigger:
                    signals.append(self._exit_signal(symbol, Side.SELL, PositionSide.LONG, price, pos.contracts, "long_trailing_stop"))
            else:
                state.lowest_price = min(state.lowest_price or price, price)
                if state.entry_price - price > 2.0 * state.atr:
                    state.active = True
                trigger = state.lowest_price + 1.5 * state.atr
                if state.active and price >= trigger:
                    signals.append(self._exit_signal(symbol, Side.BUY, PositionSide.SHORT, price, pos.contracts, "short_trailing_stop"))
        return signals

    def _exit_signal(
        self,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        price: float,
        contracts: float,
        reason: str,
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
            metadata={"contracts": contracts, "reduce_only": True},
        )
