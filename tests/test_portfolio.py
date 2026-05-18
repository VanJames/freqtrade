from __future__ import annotations

from trading_system.models import PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.portfolio import AlphaFilter, GridPlanner


def candles(start: float, end: float) -> list[list[float]]:
    rows = []
    for idx in range(24):
        close = start + (end - start) * idx / 23
        rows.append([idx, close, close * 1.001, close * 0.999, close, 1000])
    return rows


def signal(symbol: str, side: PositionSide = PositionSide.LONG) -> TradeSignal:
    trade_side = Side.BUY if side == PositionSide.LONG else Side.SELL
    return TradeSignal(
        symbol=symbol,
        signal_type=SignalType.ENTER_TREND,
        side=trade_side,
        position_side=side,
        regime=Regime.TREND_LONG if side == PositionSide.LONG else Regime.TREND_SHORT,
        price=100,
        stop_loss=95 if side == PositionSide.LONG else 105,
    )


def test_alpha_filter_keeps_two_strongest_longs() -> None:
    selected = AlphaFilter().filter(
        [signal("A/USDT:USDT"), signal("B/USDT:USDT"), signal("C/USDT:USDT")],
        {
            "BTC/USDT:USDT": candles(100, 101),
            "A/USDT:USDT": candles(100, 104),
            "B/USDT:USDT": candles(100, 103),
            "C/USDT:USDT": candles(100, 102),
        },
    )

    assert [item.symbol for item in selected] == ["A/USDT:USDT", "B/USDT:USDT"]


def test_grid_planner_builds_three_non_martingale_layers() -> None:
    grid = signal("BTC/USDT:USDT")
    grid.signal_type = SignalType.ENTER_GRID
    grid.regime = Regime.SHOCK
    grid.metadata = {"grid_layers": [1.0, 0.5, 0.5], "grid_spacing_atr": [1.0, 1.5]}

    planned = GridPlanner().orders_for_signal(grid, base_size=10, atr_value=2)

    assert planned == [(100, 10), (98, 5), (97.0, 5)]

