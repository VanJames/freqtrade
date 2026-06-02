from __future__ import annotations

from trading_system.models import Regime
from trading_system.regime import MarketRegimeClassifier


def make_candles(
    count: int,
    start: float = 100.0,
    step: float = 0.01,
    interval_ms: int = 60 * 60 * 1000,
) -> list[list[float]]:
    rows = []
    for idx in range(count):
        close = start + idx * step
        rows.append([idx * interval_ms, close * 0.999, close * 1.002, close * 0.998, close, 1000.0])
    return rows


def test_classifier_detects_shock_when_range_is_tight() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)

    regime, features = classifier.classify("BTC/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK
    assert features.range_amplitude_4h <= 0.08


def test_classifier_uses_user_4h_trend_long_without_foresight() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.6)
    candles_4h = make_candles(60, 100.0, 0.01, interval_ms=4 * 60 * 60 * 1000)
    for offset, row in enumerate(candles_4h[-12:]):
        base = 108.0 + offset
        row[1] = base
        row[2] = base + 2.0
        row[3] = base - 1.0
        row[4] = base + 1.0
    candles_1h[-1][4] = 130.0
    candles_1h[-1][2] = 131.0

    regime, _ = classifier.classify("BTC/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.TREND_LONG


def test_classifier_does_not_infer_trend_from_1h_breakout_only() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.6)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)
    candles_1h[-1][4] = 130.0
    candles_1h[-1][2] = 131.0

    regime, _ = classifier.classify("BTC/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK


def test_classifier_treats_wide_directional_drop_as_shock_trend_down() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, -0.12)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)

    regime, _ = classifier.classify("SOL/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK_TREND_DOWN


def test_classifier_does_not_treat_recent_4h_higher_highs_as_shock() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)
    for offset, row in enumerate(candles_4h[-6:]):
        row[2] = 100.2 + offset * 0.2

    regime, _ = classifier.classify("SOL/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK_TREND_UP


def test_classifier_corrects_shock_trend_up_when_short_term_and_daily_turn_down() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)
    candles_5m = make_candles(240, 100.0, 0.0, interval_ms=5 * 60 * 1000)

    for offset, row in enumerate(candles_4h[-6:]):
        row[1] = 110.0
        row[2] = 111.0 + offset
        row[3] = 94.0
        row[4] = 96.0

    for offset, row in enumerate(candles_5m[-12:]):
        base = 100.0 - offset * 0.2
        row[1] = base + 0.1
        row[2] = base + 0.2
        row[3] = base - 0.2
        row[4] = base - 0.1

    regime, _ = classifier.classify("ETH/USDT:USDT", candles_1h, candles_4h, candles_5m)

    assert regime == Regime.SHOCK_TREND_DOWN


def test_classifier_uses_current_daily_candle_for_short_term_correction() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)

    for offset, row in enumerate(candles_4h[-6:]):
        row[1] = 101.0
        row[2] = 102.0 + offset
        row[3] = 99.5
        row[4] = 100.5
    for offset, row in enumerate(candles_1h[-2:]):
        base = 101.0 - offset
        row[1] = base
        row[2] = base + 0.2
        row[3] = base - 1.2
        row[4] = base - 1.0

    regime, _ = classifier.classify("ETH/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK_TREND_DOWN


def test_classifier_corrects_shock_trend_down_when_two_1h_and_daily_turn_up() -> None:
    classifier = MarketRegimeClassifier()
    candles_1h = make_candles(80, 100.0, 0.0)
    candles_4h = make_candles(60, 100.0, 0.0, interval_ms=4 * 60 * 60 * 1000)

    for offset, row in enumerate(candles_4h[-6:]):
        row[1] = 90.0
        row[2] = 106.0
        row[3] = 99.0 - offset
        row[4] = 104.0
    for offset, row in enumerate(candles_1h[-2:]):
        base = 100.0 + offset
        row[1] = base
        row[2] = base + 1.5
        row[3] = base - 0.2
        row[4] = base + 1.0

    regime, _ = classifier.classify("ETH/USDT:USDT", candles_1h, candles_4h)

    assert regime == Regime.SHOCK_TREND_UP
