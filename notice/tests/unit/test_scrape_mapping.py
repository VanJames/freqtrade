from pathlib import Path

import pytest

from trading_notice.models import AxisAnchor
from trading_notice.scrape_mapping import (
    ScrapeMappingError,
    calibrate_axis,
    get_mapping,
    parse_price_label,
    screenshot_is_valid,
)


def test_get_mapping_requires_version():
    assert get_mapping("coinglass-free-heatmap-v1").version == "coinglass-free-heatmap-v1"
    with pytest.raises(ScrapeMappingError):
        get_mapping("stale-version")


def test_calibrate_axis_maps_svg_y_to_price():
    calibration = calibrate_axis(
        [
            AxisAnchor(price=1600.0, svg_y=260.0),
            AxisAnchor(price=1800.0, svg_y=60.0),
        ],
        "coinglass-free-heatmap-v1",
    )

    assert calibration.axis_direction == "svg_y_decreases_price"
    assert calibration.price_for_svg_y(160.0) == pytest.approx(1700.0)


def test_calibrate_axis_rejects_non_distinct_positions():
    with pytest.raises(ScrapeMappingError):
        calibrate_axis(
            [
                AxisAnchor(price=1700.0, svg_y=160.0),
                AxisAnchor(price=1800.0, svg_y=160.0),
            ],
            "coinglass-free-heatmap-v1",
        )


def test_parse_price_label_accepts_currency_text():
    assert parse_price_label("$1,700.50") == 1700.50


def test_blank_screenshot_fixture_is_invalid():
    data = Path("tests/fixtures/blank_heatmap.png").read_bytes()
    assert screenshot_is_valid(data) is False
