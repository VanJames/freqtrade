from __future__ import annotations

from dataclasses import dataclass

from trading_system.regime import ForesightData


@dataclass(slots=True)
class ForesightPoint:
    timestamp_ms: int
    long_liquidation: float = 0.0
    short_liquidation: float = 0.0
    open_interest: float = 0.0
    price: float = 0.0


class ForesightProvider:
    def __init__(self) -> None:
        self.points: dict[str, list[ForesightPoint]] = {}

    def update(self, symbol: str, point: ForesightPoint) -> None:
        series = self.points.setdefault(symbol, [])
        series.append(point)
        del series[:-480]

    def get(self, symbol: str) -> ForesightData:
        series = self.points.get(symbol, [])
        if len(series) < 480:
            return ForesightData()
        recent_4h = series[-16:]
        lookback_5d = series[-480:]
        short_threshold = percentile(rolling_sums([item.short_liquidation for item in lookback_5d], 16), 0.95)
        long_threshold = percentile(rolling_sums([item.long_liquidation for item in lookback_5d], 16), 0.95)
        short_liq_hit = sum(item.short_liquidation for item in recent_4h) > short_threshold
        long_liq_hit = sum(item.long_liquidation for item in recent_4h) > long_threshold
        oi_start = recent_4h[0].open_interest
        oi_end = recent_4h[-1].open_interest
        oi_change = (oi_end - oi_start) / oi_start if oi_start else 0.0
        return ForesightData(
            oi_change_4h=oi_change,
            short_liq_p95_hit=short_liq_hit,
            long_liq_p95_hit=long_liq_hit,
        )


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def rolling_sums(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return values
    return [sum(values[index : index + window]) for index in range(0, len(values) - window + 1)]
