from datetime import datetime, timezone

from trading_notice.config import load_analysis_config
from trading_notice.decision import decide_strategy
from trading_notice.models import (
    AxisAnchor,
    CoordinateCalibration,
    FailureState,
    KlineSignal,
    LiquidationAccumulation,
    LiquidationSignal,
    ScrapeValidationResult,
    SvgCoordinates,
)


def config():
    return load_analysis_config(
        {
            "TRADING_SYMBOL": "ETH/USDT",
            "ANALYSIS_INTERVAL": "15m",
            "RECIPIENTS": "trader@example.test",
            "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_FROM": "alerts@example.test",
        }
    )


def liquidation(*, near_tie=False, failure=None):
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    validation = ScrapeValidationResult.ok("coinglass-free-heatmap-v1", 1700.0, now)
    calibration = CoordinateCalibration(
        mapping_version="coinglass-free-heatmap-v1",
        axis_anchors=(AxisAnchor(1600.0, 260.0), AxisAnchor(1800.0, 60.0)),
        slope=-1.0,
        intercept=1860.0,
        axis_direction="svg_y_decreases_price",
        calibrated_at=now,
    )
    upper = LiquidationAccumulation(
        side="upper_short",
        period="1H",
        price_low=1740.0,
        price_high=1780.0,
        strength=0.82,
        svg_coordinates=SvgCoordinates(1, 1, 1, 1),
        calibration=calibration,
        source="coinglass_web",
        mapping_version="coinglass-free-heatmap-v1",
        validation=validation,
        evidence="upper",
    )
    lower = LiquidationAccumulation(
        side="lower_long",
        period="1H",
        price_low=1600.0,
        price_high=1640.0,
        strength=0.80,
        svg_coordinates=SvgCoordinates(1, 1, 1, 1),
        calibration=calibration,
        source="coinglass_web",
        mapping_version="coinglass-free-heatmap-v1",
        validation=validation,
        evidence="lower",
    )
    return LiquidationSignal(
        symbol="ETH/USDT",
        upper_accumulations=[] if failure else [upper],
        lower_accumulations=[] if failure else [lower],
        strongest_upper_candidate=None if failure else upper,
        strongest_lower_candidate=None if failure else lower,
        near_tie=near_tie,
        fetched_at=now,
        latest_valid_scrape_at=now,
        mapping_version="coinglass-free-heatmap-v1",
        validation=validation,
        failure=failure,
    )


def kline(state, volume, *, higher=None):
    items = [
        KlineSignal(
            symbol="ETH/USDT",
            period="15M",
            last_closed_price=1700.0,
            ma25=1680.0,
            key_level=1680.0,
            key_level_state=state,
            volume_state=volume,
            evidence=f"{state} {volume}",
        )
    ]
    if higher:
        items.extend(higher)
    return items


def htf(period, state, volume, ma_position="unknown"):
    return KlineSignal(
        symbol="ETH/USDT",
        period=period,
        last_closed_price=1700.0,
        ma25=1680.0,
        key_level=1680.0,
        key_level_state=state,
        volume_state=volume,
        ma_position=ma_position,
        evidence=f"{period} {state} {volume} {ma_position}",
    )


def test_decide_strategy_scenario_a_breakdown_short():
    decision = decide_strategy(config(), liquidation(), kline("broke_below", "high_breakdown"), "c1")

    assert decision.status == "short"
    assert decision.matched_scenario == "A_breakdown_short"
    assert "broke below" in decision.trigger_condition
    assert decision.suggested_entry_price == 1700.0
    assert decision.stop_loss > decision.suggested_entry_price


def test_decide_strategy_scenario_b_rebound_trap_short():
    decision = decide_strategy(config(), liquidation(), kline("stalled_at_resistance", "low_rebound"), "c1")

    assert decision.status == "short"
    assert decision.matched_scenario == "B_rebound_trap_short"
    assert decision.suggested_entry_price == 1700.0
    assert decision.stop_loss > decision.suggested_entry_price


def test_decide_strategy_short_blocked_by_4h_strong_long():
    decision = decide_strategy(
        config(),
        liquidation(),
        kline(
            "stalled_at_resistance",
            "low_rebound",
            higher=[htf("4h", "stood_above", "high_breakout", "above_ma25")],
        ),
        "c1",
    )

    assert decision.status == "no_trade"
    assert "Short blocked by higher timeframe" in decision.no_trade_reason


def test_decide_strategy_short_blocked_by_1d_stood_above():
    decision = decide_strategy(
        config(),
        liquidation(),
        kline("broke_below", "high_breakdown", higher=[htf("1d", "stood_above", "normal")]),
        "c1",
    )

    assert decision.status == "no_trade"
    assert "1d stood_above" in decision.no_trade_reason


def test_decide_strategy_scenario_c_confirmed_long():
    decision = decide_strategy(config(), liquidation(), kline("stood_above", "high_breakout"), "c1")

    assert decision.status == "long"
    assert decision.matched_scenario == "C_confirmed_long"
    assert decision.predicted_liquidation_side == "shorts"
    assert decision.suggested_entry_price == 1700.0
    assert decision.stop_loss < decision.suggested_entry_price


def test_decide_strategy_long_blocked_by_4h_strong_short():
    decision = decide_strategy(
        config(),
        liquidation(),
        kline(
            "stood_above",
            "high_breakout",
            higher=[htf("4h", "broke_below", "high_breakdown", "below_ma25")],
        ),
        "c1",
    )

    assert decision.status == "no_trade"
    assert "Long blocked by higher timeframe" in decision.no_trade_reason


def test_decide_strategy_near_tie_is_no_trade():
    decision = decide_strategy(config(), liquidation(near_tie=True), kline("stood_above", "high_breakout"), "c1")

    assert decision.status == "no_trade"
    assert "near-tied" in decision.no_trade_reason


def test_decide_strategy_dependency_failure_propagates():
    failure = FailureState("stale_scrape", True, 0, "coinglass_web", "stale")
    decision = decide_strategy(config(), liquidation(failure=failure), kline("stood_above", "high_breakout"), "c1")

    assert decision.status == "failure"
    assert decision.failure.category == "stale_scrape"


def test_threshold_defaults_keep_source_basis_and_pending_flag():
    cfg = config()

    assert cfg.thresholds.source_basis
    assert cfg.thresholds.pending_backtest_validation is True
