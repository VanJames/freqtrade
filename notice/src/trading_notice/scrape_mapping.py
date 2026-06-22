"""Versioned CoinGlass SVG coordinate mapping helpers."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trading_notice.models import AxisAnchor, CoordinateCalibration


class ScrapeMappingError(ValueError):
    """Raised when a configured page mapping cannot be trusted."""


@dataclass(frozen=True)
class ScreenshotValidation:
    min_bytes: int = 32
    min_width: int = 16
    min_height: int = 16
    min_unique_bytes: int = 4


@dataclass(frozen=True)
class ScrapeMappingConfiguration:
    version: str
    page_variant: str
    required_selectors: tuple[str, ...]
    screenshot_regions: dict[str, str]
    render_wait: tuple[str, ...]
    screenshot_validation: ScreenshotValidation = field(default_factory=ScreenshotValidation)
    axis_tick_selectors: tuple[str, ...] = (".price-tick",)
    heatmap_bar_selectors: tuple[str, ...] = (".liq-bar",)
    expected_invariants: tuple[str, ...] = ("heatmap-svg",)
    created_at: str = "2026-06-21"


DEFAULT_MAPPING = ScrapeMappingConfiguration(
    version="coinglass-free-heatmap-v1",
    page_variant="free_liquidation_heatmap",
    required_selectors=('[data-testid="liquidation-heatmap"]', '[data-testid="heatmap-svg"]'),
    screenshot_regions={"heatmap": '[data-testid="liquidation-heatmap"]'},
    render_wait=('[data-testid="heatmap-svg"]', ".price-tick", ".liq-bar"),
)


def get_mapping(version: str) -> ScrapeMappingConfiguration:
    if version != DEFAULT_MAPPING.version:
        raise ScrapeMappingError(f"Unsupported scrape mapping version: {version}")
    return DEFAULT_MAPPING


def parse_price_label(label: str) -> float:
    cleaned = label.strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        raise ScrapeMappingError("Price tick label is not numeric")
    return float(match.group(0))


def calibrate_axis(
    anchors: list[AxisAnchor] | tuple[AxisAnchor, ...],
    mapping_version: str,
    *,
    now: datetime | None = None,
) -> CoordinateCalibration:
    distinct = tuple(anchors)
    if len(distinct) < 2:
        raise ScrapeMappingError("At least two axis anchors are required")

    first = distinct[0]
    last = distinct[-1]
    dy = last.svg_y - first.svg_y
    if dy == 0:
        raise ScrapeMappingError("Axis anchor SVG positions must be distinct")

    slope = (last.price - first.price) / dy
    if slope == 0:
        raise ScrapeMappingError("Axis anchor prices must be distinct")

    ordered = sorted(distinct, key=lambda item: item.svg_y)
    slopes = []
    for left, right in zip(ordered, ordered[1:]):
        if right.svg_y == left.svg_y:
            raise ScrapeMappingError("Axis anchor SVG positions must be distinct")
        slopes.append((right.price - left.price) / (right.svg_y - left.svg_y))
    if any((item > 0) != (slopes[0] > 0) for item in slopes):
        raise ScrapeMappingError("Axis anchors must imply a monotonic price axis")

    intercept = first.price - slope * first.svg_y
    return CoordinateCalibration(
        mapping_version=mapping_version,
        axis_anchors=distinct,
        slope=slope,
        intercept=intercept,
        axis_direction="svg_y_increases_price" if slope > 0 else "svg_y_decreases_price",
        calibrated_at=now or datetime.now(timezone.utc),
    )


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return struct.unpack(">II", data[16:24])


def screenshot_is_valid(data: bytes, validation: ScreenshotValidation | None = None) -> bool:
    validation = validation or ScreenshotValidation()
    if len(data) < validation.min_bytes:
        return False
    dimensions = png_dimensions(data)
    if dimensions is not None:
        width, height = dimensions
        if width < validation.min_width or height < validation.min_height:
            return False
    return len(set(data)) >= validation.min_unique_bytes
