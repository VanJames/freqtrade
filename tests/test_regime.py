from __future__ import annotations

from trading_system.regime import ForesightData, MarketRegimeClassifier
from trading_system.models import Regime


def make_candles(count: int, start: float = 100.0, step: float = 0.01) -> list[list[float]]:
    rows = []
    for idx in range(count):
        close = start + idx * step
        rows.append([idx * 60_000, close * 0.999, close * 1.002, close * 0.998, close, 1000.0])
    return rows


def test_classifier_detects_shock_when_range_is_tight() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0)

    regime, features = classifier.classify("BTC/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK
    assert features.range_amplitude_4h <= 0.08


def test_classifier_requires_foresight_for_trend_long() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.6)
    candles_4h = make_candles(60, 100.0, 0.01)
    candles_1h[-1][4] = 130.0
    candles_1h[-1][2] = 131.0

    no_foresight, _ = classifier.classify("BTC/USDT:USDT", candles_1h, candles_4h)
    with_foresight, _ = classifier.classify(
        "BTC/USDT:USDT",
        candles_1h,
        candles_4h,
        ForesightData(oi_change_4h=0.2, short_liq_p95_hit=True),
    )

    assert no_foresight != Regime.TREND_LONG
    assert with_foresight == Regime.TREND_LONG
