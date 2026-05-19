from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Regime(StrEnum):
    TREND_LONG = "TREND_LONG"
    TREND_SHORT = "TREND_SHORT"
    SHOCK_TREND_UP = "SHOCK_TREND_UP"
    SHOCK_TREND_DOWN = "SHOCK_TREND_DOWN"
    SHOCK = "SHOCK"
    UNKNOWN = "UNKNOWN"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class SignalType(StrEnum):
    ENTER_TREND = "ENTER_TREND"
    ENTER_GRID = "ENTER_GRID"
    HEDGE_TRANSITION = "HEDGE_TRANSITION"
    EXIT = "EXIT"
    GRID_ADD = "GRID_ADD"
    RELEASE_HEDGE = "RELEASE_HEDGE"


@dataclass(slots=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_ohlcv(cls, row: list[float] | tuple[float, ...]) -> "Candle":
        return cls(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))

    def to_ohlcv(self) -> list[float]:
        return [self.ts, self.open, self.high, self.low, self.close, self.volume]


@dataclass(slots=True)
class MarketFeatures:
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    atr_1h: float = 0.0
    atr_4h: float = 0.0
    ema20_1h: float = 0.0
    ema60_1h: float = 0.0
    rsi_1h: float = 50.0
    range_high_4h: float = 0.0
    range_low_4h: float = 0.0
    range_amplitude_4h: float = 0.0
    close_1h: float = 0.0
    ret_24h: float = 0.0
    ret_72h: float = 0.0
    range_24h: float = 0.0
    range_72h: float = 0.0
    close_position_72h: float = 0.5
    oi_change_4h: float = 0.0
    short_liq_p95_hit: bool = False
    long_liq_p95_hit: bool = False


@dataclass(slots=True)
class TradeSignal:
    symbol: str
    signal_type: SignalType
    side: Side
    position_side: PositionSide
    regime: Regime
    price: float
    stop_loss: float
    take_profit: float | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class Position:
    symbol: str
    side: PositionSide
    contracts: float
    entry_price: float
    unrealized_pnl: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderResult:
    id: str
    symbol: str
    side: Side
    position_side: PositionSide
    amount: float
    price: float
    status: str
    filled: float = 0.0
    remaining: float = 0.0
    fee: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HedgeLock:
    symbol: str
    grid_side: PositionSide
    hedge_side: PositionSide
    contracts: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active: bool = True


@dataclass(slots=True)
class TrailingState:
    symbol: str
    position_side: PositionSide
    entry_price: float
    atr: float
    highest_price: float = 0.0
    lowest_price: float = 0.0
    active: bool = False
