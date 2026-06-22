"""Structured models shared across trading notice modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


FailureCategory = Literal[
    "coinglass_scrape",
    "scrape_mapping",
    "scrape_render",
    "scrape_screenshot",
    "scrape_calibration",
    "scrape_sanity",
    "stale_scrape",
    "ccxt_api",
    "smtp",
    "configuration",
    "parsing",
    "scheduler",
]


@dataclass(frozen=True)
class ThresholdSet:
    liquidation_sufficiency: float = 0.7
    upper_lower_near_tie: float = 0.05
    stood_above_confirmation: float = 0.001
    broke_below_confirmation: float = 0.001
    volume_confirmation: float = 1.2
    source_basis: str = "industry_common_heuristic"
    pending_backtest_validation: bool = True


@dataclass(frozen=True)
class PriceSanityBounds:
    max_relative_distance: float = 0.50
    source_basis: str = "planning_input_engineering_guardrail"
    pending_backtest_validation: bool = True

    def contains(self, price: float, current_market_price: float) -> bool:
        if price <= 0 or current_market_price <= 0:
            return False
        return abs(price - current_market_price) / current_market_price <= self.max_relative_distance


@dataclass(frozen=True)
class AnalysisConfiguration:
    symbol: str
    analysis_interval: str
    recipients: tuple[str, ...]
    coinglass_heatmap_url: str
    scrape_mapping_version: str = "coinglass-free-heatmap-v1"
    coinglass_heatmap_ranges: tuple[str, ...] = ("24 hour",)
    liquidation_periods: tuple[str, ...] = ("15M", "1H", "4H", "1D")
    kline_periods: tuple[str, ...] = ("15m", "1h", "4h", "1d")
    kline_exchange_id: str = "okx"
    kline_check_interval_seconds: int = 60
    scrape_min_interval_seconds: int = 3600
    scrape_failure_backoff_seconds: int = 300
    max_scrape_staleness_seconds: int = 7200
    liquidation_price_move_trigger: float = 0.01
    liquidation_price_move_cooldown_seconds: int = 900
    price_sanity_bounds: PriceSanityBounds = field(default_factory=PriceSanityBounds)
    thresholds: ThresholdSet = field(default_factory=ThresholdSet)
    failure_cooldown_seconds: int = 900
    smtp_settings_ref: dict[str, str] = field(default_factory=dict)
    coinglass_auth_ref: dict[str, str] = field(default_factory=dict)
    coinglass_session_path: str | None = None
    current_market_price: float | None = None
    screenshot_dir: str | None = None


@dataclass(frozen=True)
class FailureState:
    category: FailureCategory
    retryable: bool
    attempts: int
    source: str
    safe_message: str


@dataclass(frozen=True)
class AxisAnchor:
    price: float
    svg_y: float


@dataclass(frozen=True)
class SvgCoordinates:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class CoordinateCalibration:
    mapping_version: str
    axis_anchors: tuple[AxisAnchor, ...]
    slope: float
    intercept: float
    axis_direction: Literal["svg_y_increases_price", "svg_y_decreases_price"]
    calibrated_at: datetime
    valid: bool = True
    failure_reason: str | None = None

    def price_for_svg_y(self, svg_y: float) -> float:
        return self.slope * svg_y + self.intercept


@dataclass(frozen=True)
class ScrapeValidationResult:
    mapping_version: str
    layout_valid: bool
    render_valid: bool
    screenshot_valid: bool
    dom_parse_valid: bool
    calibration_valid: bool
    per_side_candidate_valid: bool
    price_sanity_valid: bool
    current_market_price: float | None = None
    latest_valid_scrape_at: datetime | None = None
    staleness_seconds: float | None = None
    failure_reason: str | None = None

    @classmethod
    def ok(
        cls,
        mapping_version: str,
        current_market_price: float | None,
        latest_valid_scrape_at: datetime | None = None,
    ) -> "ScrapeValidationResult":
        return cls(
            mapping_version=mapping_version,
            layout_valid=True,
            render_valid=True,
            screenshot_valid=True,
            dom_parse_valid=True,
            calibration_valid=True,
            per_side_candidate_valid=True,
            price_sanity_valid=True,
            current_market_price=current_market_price,
            latest_valid_scrape_at=latest_valid_scrape_at,
        )

    @classmethod
    def failed(
        cls,
        mapping_version: str,
        reason: str,
        *,
        layout_valid: bool = True,
        render_valid: bool = True,
        screenshot_valid: bool = True,
        dom_parse_valid: bool = True,
        calibration_valid: bool = True,
        per_side_candidate_valid: bool = True,
        price_sanity_valid: bool = True,
        current_market_price: float | None = None,
        latest_valid_scrape_at: datetime | None = None,
        staleness_seconds: float | None = None,
    ) -> "ScrapeValidationResult":
        return cls(
            mapping_version=mapping_version,
            layout_valid=layout_valid,
            render_valid=render_valid,
            screenshot_valid=screenshot_valid,
            dom_parse_valid=dom_parse_valid,
            calibration_valid=calibration_valid,
            per_side_candidate_valid=per_side_candidate_valid,
            price_sanity_valid=price_sanity_valid,
            current_market_price=current_market_price,
            latest_valid_scrape_at=latest_valid_scrape_at,
            staleness_seconds=staleness_seconds,
            failure_reason=reason,
        )


@dataclass(frozen=True)
class LiquidationAccumulation:
    side: Literal["upper_short", "lower_long"]
    period: str
    price_low: float
    price_high: float
    strength: float
    svg_coordinates: SvgCoordinates
    calibration: CoordinateCalibration
    source: str
    mapping_version: str
    validation: ScrapeValidationResult
    evidence: str


@dataclass(frozen=True)
class LiquidationSignal:
    symbol: str
    upper_accumulations: list[LiquidationAccumulation]
    lower_accumulations: list[LiquidationAccumulation]
    fetched_at: datetime
    mapping_version: str
    validation: ScrapeValidationResult
    strongest_upper_candidate: LiquidationAccumulation | None = None
    strongest_lower_candidate: LiquidationAccumulation | None = None
    near_tie: bool = False
    latest_valid_scrape_at: datetime | None = None
    failure: FailureState | None = None

    def as_dict_shape(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "upper_accumulations": [vars(item) for item in self.upper_accumulations],
            "lower_accumulations": [vars(item) for item in self.lower_accumulations],
        }


@dataclass(frozen=True)
class KlineSignal:
    symbol: str
    period: str
    last_closed_price: float | None = None
    ma7: float | None = None
    ma25: float | None = None
    ma99: float | None = None
    ma_position: str = "unknown"
    volume_state: str = "unknown"
    key_level: float | None = None
    key_level_state: str = "unknown"
    evidence: str = ""
    failure: FailureState | None = None


@dataclass(frozen=True)
class StrategyDecision:
    cycle_id: str
    symbol: str
    status: Literal["long", "short", "no_trade", "failure"]
    matched_scenario: str = "none"
    trigger_condition: str | None = None
    suggested_entry_price: float | None = None
    stop_loss: float | None = None
    target_take_profit: float | None = None
    predicted_liquidation_side: Literal["longs", "shorts", "none"] = "none"
    basis: str = ""
    no_trade_reason: str | None = None
    failure: FailureState | None = None


@dataclass(frozen=True)
class StrategyEmailReport:
    cycle_id: str
    subject: str
    recipients: tuple[str, ...]
    direction: Literal["long", "short"]
    trigger_condition: str
    suggested_entry_price: float
    stop_loss: float
    target_take_profit: float
    predicted_liquidation_side: Literal["longs", "shorts"]
    technical_basis: str
    body_html: str = ""
    send_status: str = "pending"


@dataclass(frozen=True)
class FailureNotification:
    cycle_id: str
    failure_category: FailureCategory
    message: str
    recipients: tuple[str, ...]
    send_status: str = "pending"
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    recovered_at: datetime | None = None
    cooldown_expires_at: datetime | None = None
