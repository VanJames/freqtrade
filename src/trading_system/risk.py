from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging import getLogger

from trading_system.config import Settings
from trading_system.indicators import ohlcv_frame
from trading_system.models import PositionSide, Regime, SignalType, TradeSignal
from trading_system.position_sizing import adjusted_risk_multiplier

logger = getLogger(__name__)


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    size: float = 0.0
    min_reward_risk: float = 1.5


@dataclass
class RiskManager:
    settings: Settings
    starting_equity: float | None = None
    fused_until: datetime | None = None
    symbol_locks: dict[str, datetime] = field(default_factory=dict)
    direction_risk: dict[PositionSide, float] = field(
        default_factory=lambda: {PositionSide.LONG: 0.0, PositionSide.SHORT: 0.0}
    )

    def fused(self) -> bool:
        return self.fused_until is not None and self.fused_until > datetime.now(timezone.utc)

    def update_equity(self, equity: float) -> bool:
        if self.starting_equity is None:
            self.starting_equity = equity
        if self.starting_equity and equity <= self.starting_equity * (1 - self.settings.daily_drawdown_limit):
            self.fused_until = datetime.now(timezone.utc) + timedelta(hours=24)
            logger.critical("daily drawdown fuse triggered equity=%s starting=%s", equity, self.starting_equity)
            return True
        return False

    def inspect_spike(self, symbol: str, candles_5m: list[list[float]]) -> None:
        df = ohlcv_frame(candles_5m)
        if df.empty:
            return
        last = df.iloc[-1]
        amplitude = (float(last.high) - float(last.low)) / float(last.low) if last.low else 0
        if amplitude >= self.settings.spike_amplitude_threshold:
            self.symbol_locks[symbol] = datetime.now(timezone.utc) + timedelta(seconds=self.settings.spike_lock_seconds)
            logger.warning("symbol locked by 5m spike symbol=%s amplitude=%.4f", symbol, amplitude)

    def assess(
        self,
        signal: TradeSignal,
        equity: float,
        funding_rate: float,
    ) -> RiskDecision:
        if self.fused():
            return RiskDecision(False, "global_fuse_active")

        lock_until = self.symbol_locks.get(signal.symbol)
        if lock_until and lock_until > datetime.now(timezone.utc):
            return RiskDecision(False, "symbol_spike_lock_active")

        if signal.signal_type == SignalType.HEDGE_TRANSITION:
            return RiskDecision(True, "hedge_bypass_position_sizing", size=float(signal.metadata["hedge_contracts"]))
        if signal.signal_type in {SignalType.EXIT, SignalType.RELEASE_HEDGE}:
            return RiskDecision(True, "exit_bypass_position_sizing", size=float(signal.metadata["contracts"]))

        min_live_equity = float(getattr(self.settings, "min_live_equity_to_order", 0.0) or 0.0)
        if min_live_equity > 0 and equity < min_live_equity:
            return RiskDecision(False, "low_equity")

        stop_distance = abs(signal.price - signal.stop_loss)
        if stop_distance <= 0:
            return RiskDecision(False, "invalid_stop_distance")

        risk_multiplier = self.signal_risk_multiplier(signal)
        risk_budget = self.settings.risk_percent * risk_multiplier
        current_direction_risk = self.direction_risk.get(signal.position_side, 0.0)
        if current_direction_risk + risk_budget > self.settings.same_direction_risk_limit:
            return RiskDecision(False, "same_direction_risk_limit")

        min_reward_risk = 1.5
        if signal.regime in {Regime.TREND_LONG, Regime.TREND_SHORT} and funding_rate >= self.settings.funding_block_threshold:
            min_reward_risk = 2.5
        if signal.signal_type == SignalType.ENTER_GRID and funding_rate >= self.settings.funding_block_threshold:
            return RiskDecision(False, "grid_blocked_by_funding_rate")
        if signal.take_profit is not None:
            reward = abs(signal.take_profit - signal.price)
            if reward / stop_distance < min_reward_risk:
                return RiskDecision(False, "reward_risk_below_funding_threshold")

        size = (equity * risk_budget) / stop_distance
        leverage_limit = (
            self.settings.shock_leverage_limit
            if signal.regime == Regime.SHOCK
            else self.settings.trend_symbol_leverage_limit
        )
        max_size = (equity * leverage_limit) / signal.price
        size = min(size, max_size)
        if size <= 0:
            return RiskDecision(False, "zero_position_size")
        return RiskDecision(True, size=size, min_reward_risk=min_reward_risk)

    def reserve_risk(self, side: PositionSide, risk_multiplier: float = 1.0) -> None:
        self.direction_risk[side] = self.direction_risk.get(side, 0.0) + self.settings.risk_percent * risk_multiplier

    def release_risk(self, side: PositionSide, risk_multiplier: float = 1.0) -> None:
        current = self.direction_risk.get(side, 0.0)
        released = self.settings.risk_percent * risk_multiplier
        self.direction_risk[side] = max(0.0, current - released)

    def signal_risk_multiplier(self, signal: TradeSignal) -> float:
        return adjusted_risk_multiplier(signal, self.settings)
