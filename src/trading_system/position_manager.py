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
            breakeven_activate_r=float(signal.metadata.get("breakeven_activate_r", 0.0) or 0.0),
            breakeven_buffer_pct=float(signal.metadata.get("breakeven_buffer_pct", 0.0) or 0.0),
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
        signal_hints: dict[tuple[str, PositionSide], dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        recovered: list[dict[str, object]] = []
        signal_hints = signal_hints or {}
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
                state = self.trailing[key]
                if not state.active and (
                    self._position_entry_changed(state.entry_price, pos.entry_price) or self._state_stop_breached(state, price)
                ):
                    state = self._recovered_state(pos, price=price, atr_value=atr_value, signal_hint=signal_hints.get(key))
                    self.trailing[key] = state
                    info = self._recovered_info(pos, state, regime)
                    self.recovered_positions[key] = info
                    recovered.append(info)
                continue
            state = self._recovered_state(pos, price=price, atr_value=atr_value, signal_hint=signal_hints.get(key))
            self.trailing[key] = state
            info = self._recovered_info(pos, state, regime)
            self.recovered_positions[key] = info
            recovered.append(info)
        return recovered

    @staticmethod
    def _position_entry_changed(state_entry: float, position_entry: float) -> bool:
        if position_entry <= 0:
            return False
        if state_entry <= 0:
            return True
        return abs(state_entry - position_entry) / position_entry > 0.001

    @staticmethod
    def _state_stop_breached(state: TrailingState, price: float) -> bool:
        if state.stop_loss <= 0 or price <= 0:
            return False
        if state.position_side == PositionSide.LONG:
            return price <= state.stop_loss
        return price >= state.stop_loss

    @staticmethod
    def _recovered_info(pos: Position, state: TrailingState, regime: Regime) -> dict[str, object]:
        return {
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
                    if (
                        state.breakeven_activate_r > 0
                        and risk > 0
                        and state.highest_price - state.entry_price >= state.breakeven_activate_r * risk
                    ):
                        state.stop_loss = max(state.stop_loss, state.entry_price * (1 + state.breakeven_buffer_pct))
                        state.active = True
                    if risk > 0 and state.highest_price - state.entry_price >= state.min_trailing_activate_r * risk:
                        state.stop_loss = max(state.stop_loss, state.highest_price * (1 - state.trailing_gap_pct))
                        state.active = True
                if price - state.entry_price > 2.0 * state.atr:
                    state.active = True
                    state.stop_loss = max(state.stop_loss, state.highest_price - 1.5 * state.atr)
                if state.stop_loss > 0 and price <= state.stop_loss:
                    reason = "long_trailing_stop" if state.active else "long_stop_loss"
                    signals.append(
                        self._exit_signal(
                            symbol,
                            Side.SELL,
                            PositionSide.LONG,
                            price,
                            pos.contracts,
                            reason,
                            state.risk_multiplier,
                            state.entry_price,
                        )
                    )
                elif state.take_profit is not None and price >= state.take_profit:
                    signals.append(
                        self._exit_signal(
                            symbol,
                            Side.SELL,
                            PositionSide.LONG,
                            price,
                            pos.contracts,
                            "long_take_profit",
                            state.risk_multiplier,
                            state.entry_price,
                        )
                    )
            else:
                state.lowest_price = min(state.lowest_price or price, price)
                if state.trailing_gap_pct and state.stop_loss > 0:
                    risk = abs(state.stop_loss - state.entry_price)
                    if (
                        state.breakeven_activate_r > 0
                        and risk > 0
                        and state.entry_price - state.lowest_price >= state.breakeven_activate_r * risk
                    ):
                        state.stop_loss = min(state.stop_loss, state.entry_price * (1 - state.breakeven_buffer_pct))
                        state.active = True
                    if risk > 0 and state.entry_price - state.lowest_price >= state.min_trailing_activate_r * risk:
                        state.stop_loss = min(state.stop_loss, state.lowest_price * (1 + state.trailing_gap_pct))
                        state.active = True
                if state.entry_price - price > 2.0 * state.atr:
                    state.active = True
                    state.stop_loss = min(state.stop_loss, state.lowest_price + 1.5 * state.atr)
                if state.stop_loss > 0 and price >= state.stop_loss:
                    reason = "short_trailing_stop" if state.active else "short_stop_loss"
                    signals.append(
                        self._exit_signal(
                            symbol,
                            Side.BUY,
                            PositionSide.SHORT,
                            price,
                            pos.contracts,
                            reason,
                            state.risk_multiplier,
                            state.entry_price,
                        )
                    )
                elif state.take_profit is not None and price <= state.take_profit:
                    signals.append(
                        self._exit_signal(
                            symbol,
                            Side.BUY,
                            PositionSide.SHORT,
                            price,
                            pos.contracts,
                            "short_take_profit",
                            state.risk_multiplier,
                            state.entry_price,
                        )
                    )
        return signals

    def trailing_snapshot(self) -> dict[str, dict[str, object]]:
        return {
            self._key_string(symbol, side): {
                "symbol": state.symbol,
                "position_side": state.position_side.value,
                "entry_price": state.entry_price,
                "atr": state.atr,
                "stop_loss": state.stop_loss,
                "take_profit": state.take_profit,
                "trailing_gap_pct": state.trailing_gap_pct,
                "min_trailing_activate_r": state.min_trailing_activate_r,
                "breakeven_activate_r": state.breakeven_activate_r,
                "breakeven_buffer_pct": state.breakeven_buffer_pct,
                "risk_multiplier": state.risk_multiplier,
                "highest_price": state.highest_price,
                "lowest_price": state.lowest_price,
                "active": state.active,
            }
            for (symbol, side), state in self.trailing.items()
        }

    def restore_trailing_snapshot(self, raw: dict[str, dict[str, object]]) -> None:
        for key, item in raw.items():
            symbol = str(item.get("symbol") or key.rsplit(":", 1)[0])
            side = PositionSide(str(item.get("position_side") or key.rsplit(":", 1)[1]))
            self.trailing[(symbol, side)] = TrailingState(
                symbol=symbol,
                position_side=side,
                entry_price=self._float_item(item, "entry_price"),
                atr=self._float_item(item, "atr"),
                stop_loss=self._float_item(item, "stop_loss"),
                take_profit=self._optional_float_item(item, "take_profit"),
                trailing_gap_pct=self._optional_float_item(item, "trailing_gap_pct"),
                min_trailing_activate_r=self._float_item(item, "min_trailing_activate_r", 1.0),
                breakeven_activate_r=self._float_item(item, "breakeven_activate_r"),
                breakeven_buffer_pct=self._float_item(item, "breakeven_buffer_pct"),
                risk_multiplier=self._float_item(item, "risk_multiplier", 1.0),
                highest_price=self._float_item(item, "highest_price"),
                lowest_price=self._float_item(item, "lowest_price"),
                active=bool(item.get("active", False)),
            )

    def _recovered_state(
        self,
        pos: Position,
        *,
        price: float,
        atr_value: float,
        signal_hint: dict[str, object] | None = None,
    ) -> TrailingState:
        entry_price = pos.entry_price or price
        atr_value = atr_value or max(entry_price * self.min_stop_loss_pct, 0.0)
        signal_hint = signal_hint or {}
        stop_loss = self._metadata_float(pos.metadata, "stop_loss") or self._float_item(signal_hint, "stop_loss")
        take_profit = self._metadata_float(pos.metadata, "take_profit") or self._float_item(signal_hint, "take_profit")
        if stop_loss <= 0:
            stop_loss = self._protective_stop(entry_price, atr_value, pos.side)
        stop_loss = self._move_breached_stop_outside_market(stop_loss, entry_price, price, atr_value, pos.side)
        return TrailingState(
            symbol=pos.symbol,
            position_side=pos.side,
            entry_price=entry_price,
            atr=atr_value,
            stop_loss=stop_loss,
            take_profit=take_profit if take_profit > 0 else None,
            trailing_gap_pct=self._optional_float_item(signal_hint, "trailing_gap_pct") or self.trailing_gap_pct,
            min_trailing_activate_r=self._float_item(signal_hint, "min_trailing_activate_r", self.min_trailing_activate_r),
            breakeven_activate_r=self._float_item(signal_hint, "breakeven_activate_r"),
            breakeven_buffer_pct=self._float_item(signal_hint, "breakeven_buffer_pct"),
            risk_multiplier=self._metadata_float(
                pos.metadata,
                "risk_multiplier",
                fallback=self._float_item(signal_hint, "risk_multiplier", self.recovered_risk_multiplier),
            ),
            highest_price=max(entry_price, price) if pos.side == PositionSide.LONG else entry_price,
            lowest_price=min(entry_price, price) if pos.side == PositionSide.SHORT else entry_price,
        )

    def _protective_stop(self, entry_price: float, atr_value: float, side: PositionSide) -> float:
        distance = self._protective_distance(entry_price, atr_value)
        if side == PositionSide.LONG:
            return entry_price - distance
        return entry_price + distance

    def _move_breached_stop_outside_market(
        self,
        stop_loss: float,
        entry_price: float,
        price: float,
        atr_value: float,
        side: PositionSide,
    ) -> float:
        if price <= 0:
            return stop_loss
        distance = self._protective_distance(entry_price, atr_value)
        if side == PositionSide.LONG and stop_loss >= price:
            return price - distance
        if side == PositionSide.SHORT and stop_loss <= price:
            return price + distance
        return stop_loss

    def _protective_distance(self, entry_price: float, atr_value: float) -> float:
        min_distance = entry_price * self.min_stop_loss_pct
        max_distance = entry_price * self.max_stop_loss_pct
        return min(max(atr_value * 1.5, min_distance), max_distance)

    @staticmethod
    def _metadata_float(metadata: dict[str, object], key: str, fallback: float = 0.0) -> float:
        value = metadata.get(key)
        if value is None and isinstance(metadata.get("info"), dict):
            value = metadata["info"].get(key)  # type: ignore[index]
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _float_item(item: dict[str, object], key: str, fallback: float = 0.0) -> float:
        try:
            return float(item.get(key, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback

    @classmethod
    def _optional_float_item(cls, item: dict[str, object], key: str) -> float | None:
        value = cls._float_item(item, key)
        return value if value > 0 else None

    @staticmethod
    def _key_string(symbol: str, side: PositionSide) -> str:
        return f"{symbol}:{side.value}"

    def _exit_signal(
        self,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        price: float,
        contracts: float,
        reason: str,
        risk_multiplier: float,
        entry_price: float,
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
            metadata={
                "contracts": contracts,
                "reduce_only": True,
                "risk_multiplier": risk_multiplier,
                "entry_price": entry_price,
            },
        )
