from __future__ import annotations

from trading_system.models import Position, PositionSide, Regime, Side, SignalType, TradeSignal, TrailingState


class PositionManager:
    def __init__(
        self,
        *,
        min_stop_loss_pct: float = 0.002,
        max_stop_loss_pct: float = 0.012,
        trailing_gap_pct: float = 0.0025,
        min_trailing_activate_r: float = 1.0,
        recovered_risk_multiplier: float = 0.3,
    ) -> None:
        self.trailing: dict[tuple[str, PositionSide], TrailingState] = {}
        self.recovered_positions: dict[tuple[str, PositionSide], dict[str, object]] = {}
        self.min_stop_loss_pct = min_stop_loss_pct
        self.max_stop_loss_pct = max_stop_loss_pct
        self.trailing_gap_pct = trailing_gap_pct
        self.min_trailing_activate_r = min_trailing_activate_r
        self.recovered_risk_multiplier = recovered_risk_multiplier

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
        self.recovered_positions.pop(key, None)

    def recover_missing_states(
        self,
        symbol: str,
        positions: list[Position],
        *,
        price: float,
        atr_value: float,
        regime: Regime,
    ) -> list[dict[str, object]]:
        recovered: list[dict[str, object]] = []
        active_keys = {(pos.symbol, pos.side) for pos in positions if pos.contracts > 0}
        for key in list(self.trailing):
            if key[0] == symbol and key not in active_keys:
                self.trailing.pop(key, None)
                self.recovered_positions.pop(key, None)

        for pos in positions:
            if pos.contracts <= 0:
                continue
            key = (pos.symbol, pos.side)
            if key in self.trailing:
                continue
            state = self._recovered_state(pos, price=price, atr_value=atr_value)
            self.trailing[key] = state
            info = {
                "symbol": pos.symbol,
                "side": pos.side.value,
                "contracts": pos.contracts,
                "entry_price": pos.entry_price,
                "stop_loss": state.stop_loss,
                "take_profit": state.take_profit,
                "atr": state.atr,
                "regime": regime.value,
                "risk_multiplier": state.risk_multiplier,
            }
            self.recovered_positions[key] = info
            recovered.append(info)
        return recovered

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
                state = self._recovered_state(pos, price=price, atr_value=atr_value)
                self.trailing[key] = state
                self.recovered_positions[key] = {
                    "symbol": pos.symbol,
                    "side": pos.side.value,
                    "contracts": pos.contracts,
                    "entry_price": pos.entry_price,
                    "stop_loss": state.stop_loss,
                    "take_profit": state.take_profit,
                    "atr": state.atr,
                    "regime": Regime.UNKNOWN.value,
                    "risk_multiplier": state.risk_multiplier,
                }
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

    def _recovered_state(self, pos: Position, *, price: float, atr_value: float) -> TrailingState:
        entry_price = pos.entry_price or price
        atr_value = atr_value or max(entry_price * self.min_stop_loss_pct, 0.0)
        stop_loss = self._metadata_float(pos.metadata, "stop_loss")
        take_profit = self._metadata_float(pos.metadata, "take_profit")
        if stop_loss <= 0:
            stop_loss = self._protective_stop(entry_price, atr_value, pos.side)
        return TrailingState(
            symbol=pos.symbol,
            position_side=pos.side,
            entry_price=entry_price,
            atr=atr_value,
            stop_loss=stop_loss,
            take_profit=take_profit if take_profit > 0 else None,
            trailing_gap_pct=self.trailing_gap_pct,
            min_trailing_activate_r=self.min_trailing_activate_r,
            risk_multiplier=self._metadata_float(
                pos.metadata,
                "risk_multiplier",
                fallback=self.recovered_risk_multiplier,
            ),
            highest_price=max(entry_price, price) if pos.side == PositionSide.LONG else entry_price,
            lowest_price=min(entry_price, price) if pos.side == PositionSide.SHORT else entry_price,
        )

    def _protective_stop(self, entry_price: float, atr_value: float, side: PositionSide) -> float:
        min_distance = entry_price * self.min_stop_loss_pct
        max_distance = entry_price * self.max_stop_loss_pct
        distance = min(max(atr_value * 1.5, min_distance), max_distance)
        if side == PositionSide.LONG:
            return entry_price - distance
        return entry_price + distance

    @staticmethod
    def _metadata_float(metadata: dict[str, object], key: str, fallback: float = 0.0) -> float:
        value = metadata.get(key)
        if value is None and isinstance(metadata.get("info"), dict):
            value = metadata["info"].get(key)  # type: ignore[index]
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

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
