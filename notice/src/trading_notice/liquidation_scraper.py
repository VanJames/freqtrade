"""CoinGlass free-page liquidation heatmap scraper."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from trading_notice.models import (
    AnalysisConfiguration,
    AxisAnchor,
    FailureState,
    LiquidationAccumulation,
    LiquidationSignal,
    ScrapeValidationResult,
    SvgCoordinates,
)
from trading_notice.scrape_mapping import (
    ScrapeMappingConfiguration,
    ScrapeMappingError,
    calibrate_axis,
    get_mapping,
    parse_price_label,
    screenshot_is_valid,
)


class BrowserPage(Protocol):
    def goto(self, url: str, wait_until: str = "networkidle", timeout: int = 30000) -> Any: ...

    def wait_for_selector(self, selector: str, timeout: int = 30000) -> Any: ...

    def screenshot(self, **kwargs: Any) -> bytes: ...

    def query_selector_all(self, selector: str) -> list[Any]: ...

    def evaluate(self, script: str, *args: Any) -> Any: ...

    def fill(self, selector: str, value: str) -> Any: ...

    def locator(self, selector: str) -> Any: ...


class BrowserContext(Protocol):
    def new_page(self) -> BrowserPage: ...

    def storage_state(self, **kwargs: Any) -> Any: ...


@dataclass
class ScrapeRuntimeState:
    latest_valid_signal: LiquidationSignal | None = None
    latest_valid_scrape_at: datetime | None = None
    last_scrape_attempt_at: datetime | None = None
    failure_backoff_until: datetime | None = None


class LiquidationScrapeError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def scrape_liquidation_signal(
    config: AnalysisConfiguration,
    *,
    page: BrowserPage | None = None,
    current_market_price: float | None = None,
    now: datetime | None = None,
    state: ScrapeRuntimeState | None = None,
    max_attempts: int = 2,
    sleep_seconds: float = 0.0,
) -> LiquidationSignal:
    """Scrape and normalize CoinGlass liquidation evidence.

    The optional `page` and `state` arguments are dependency-injection seams for
    deterministic tests. Production code can omit them to use Playwright.
    """

    now = now or datetime.now(timezone.utc)
    current_market_price = current_market_price or config.current_market_price

    stale_failure = _stale_failure_if_needed(config, state, now)
    if stale_failure is not None:
        return stale_failure

    attempts = 0
    last_error: LiquidationScrapeError | None = None
    mapping_version = config.scrape_mapping_version
    for attempts in range(1, max_attempts + 1):
        try:
            mapping = get_mapping(mapping_version)
            signal = _scrape_once(config, mapping, page, current_market_price, now)
            if state is not None:
                state.latest_valid_signal = signal
                state.latest_valid_scrape_at = signal.latest_valid_scrape_at
                state.last_scrape_attempt_at = now
                state.failure_backoff_until = None
            return signal
        except ScrapeMappingError as exc:
            last_error = LiquidationScrapeError("scrape_mapping", str(exc), retryable=False)
            break
        except LiquidationScrapeError as exc:
            last_error = exc
            if state is not None:
                state.last_scrape_attempt_at = now
            if not exc.retryable:
                break
            if sleep_seconds:
                time.sleep(sleep_seconds)
    assert last_error is not None
    if state is not None:
        state.failure_backoff_until = now
    return _failure_signal(
        config,
        last_error.category,
        str(last_error),
        attempts=attempts,
        retryable=last_error.retryable,
        current_market_price=current_market_price,
        now=now,
    )


def _scrape_once(
    config: AnalysisConfiguration,
    mapping: ScrapeMappingConfiguration,
    page: BrowserPage | None,
    current_market_price: float | None,
    now: datetime,
) -> LiquidationSignal:
    own_browser = None
    own_playwright = None
    if page is None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment-specific
            raise LiquidationScrapeError("coinglass_scrape", "Playwright is unavailable") from exc
        own_playwright = sync_playwright().start()
        own_browser = own_playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        session_path = Path(config.coinglass_session_path) if config.coinglass_session_path else None
        storage_state = str(session_path) if session_path and session_path.exists() else None
        context = own_browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            storage_state=storage_state,
        )
        page = context.new_page()
        _ensure_coinglass_login_if_configured(config, page, context)

    try:
        page.goto(config.coinglass_heatmap_url, wait_until="load", timeout=45000)
        echarts_signal = _try_scrape_echarts_canvas(
            config, mapping, page, current_market_price, now
        )
        if echarts_signal is not None:
            return echarts_signal

        for selector in mapping.required_selectors:
            page.wait_for_selector(selector, timeout=15000)
        for selector in mapping.render_wait:
            page.wait_for_selector(selector, timeout=15000)

        screenshot = _capture_screenshot(page, mapping)
        if not screenshot_is_valid(screenshot, mapping.screenshot_validation):
            raise LiquidationScrapeError("scrape_screenshot", "Chart screenshot is blank or invalid")
        _save_screenshot(config, screenshot, now)

        anchors = _read_axis_anchors(page, mapping)
        calibration = calibrate_axis(anchors, mapping.version, now=now)
        bars = _read_bars(page, mapping)
        if not bars:
            raise LiquidationScrapeError("parsing", "No liquidation bars found", retryable=False)

        validation = ScrapeValidationResult.ok(mapping.version, current_market_price, now)
        accumulations = [
            _bar_to_accumulation(
                bar,
                config,
                mapping,
                calibration,
                validation,
                current_market_price,
            )
            for bar in bars
        ]
        upper = [item for item in accumulations if item.side == "upper_short"]
        lower = [item for item in accumulations if item.side == "lower_long"]
        strongest_upper = max(upper, key=lambda item: item.strength, default=None)
        strongest_lower = max(lower, key=lambda item: item.strength, default=None)
        if strongest_upper is None or strongest_lower is None:
            raise LiquidationScrapeError(
                "parsing", "Per-side highest liquidation candidates were not identified"
            )
        near_tie = (
            abs(strongest_upper.strength - strongest_lower.strength)
            <= config.thresholds.upper_lower_near_tie
        )
        return LiquidationSignal(
            symbol=config.symbol,
            upper_accumulations=upper,
            lower_accumulations=lower,
            strongest_upper_candidate=strongest_upper,
            strongest_lower_candidate=strongest_lower,
            near_tie=near_tie,
            fetched_at=now,
            latest_valid_scrape_at=now,
            mapping_version=mapping.version,
            validation=validation,
        )
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutError":
            raise LiquidationScrapeError("scrape_render", "Chart render timed out") from exc
        message = str(exc)
        if "Page crashed" in message or "Target closed" in message:
            raise LiquidationScrapeError(
                "coinglass_scrape", "CoinGlass browser page crashed during render"
            ) from exc
        raise
    finally:
        if own_browser is not None:  # pragma: no cover - integration boundary
            own_browser.close()
        if own_playwright is not None:  # pragma: no cover - integration boundary
            own_playwright.stop()


def _try_scrape_echarts_canvas(
    config: AnalysisConfiguration,
    mapping: ScrapeMappingConfiguration,
    page: BrowserPage,
    current_market_price: float | None,
    now: datetime,
) -> LiquidationSignal | None:
    if not hasattr(page, "evaluate"):
        return None
    try:
        page.wait_for_selector(".echarts-for-react", timeout=20000)
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutError":
            return None
        raise

    upper: list[LiquidationAccumulation] = []
    lower: list[LiquidationAccumulation] = []
    reference_price = current_market_price
    validation: ScrapeValidationResult | None = None
    for heatmap_range in config.coinglass_heatmap_ranges:
        _select_heatmap_range(page, heatmap_range)
        payload = _wait_for_echarts_payload(page, reference_price, expected_range=heatmap_range)
        if not isinstance(payload, dict) or not payload.get("chartFound"):
            raise LiquidationScrapeError("parsing", "ECharts liquidation chart was not readable")

        chart_price = _optional_float(payload.get("currentPrice"))
        reference_price = reference_price or chart_price
        if reference_price is None:
            raise LiquidationScrapeError("parsing", "Current market price was not available")

        screenshot = _capture_selector_screenshot(page, ".echarts-for-react")
        if not screenshot_is_valid(screenshot, mapping.screenshot_validation):
            raise LiquidationScrapeError("scrape_screenshot", "Chart screenshot is blank or invalid")
        _save_screenshot(config, screenshot, now, suffix=_range_slug(heatmap_range))

        y_min = _required_float(payload.get("yMin"), "yMin")
        y_max = _required_float(payload.get("yMax"), "yMax")
        y_count = int(payload.get("yCount") or 0)
        if y_count < 2 or y_min <= 0 or y_max <= y_min:
            raise LiquidationScrapeError("scrape_calibration", "Invalid ECharts price axis")

        calibration = calibrate_axis(
            [
                AxisAnchor(price=y_min, svg_y=0.0),
                AxisAnchor(price=y_max, svg_y=float(y_count - 1)),
            ],
            mapping.version,
            now=now,
        )
        validation = ScrapeValidationResult.ok(mapping.version, reference_price, now)

        upper.extend(
            _echarts_rows_to_accumulations(
                payload.get("latestUpperTop"),
                "upper_short",
                heatmap_range,
                config,
                mapping,
                calibration,
                validation,
                reference_price,
            )
        )
        lower.extend(
            _echarts_rows_to_accumulations(
                payload.get("latestLowerTop"),
                "lower_long",
                heatmap_range,
                config,
                mapping,
                calibration,
                validation,
                reference_price,
            )
        )
    strongest_upper = max(upper, key=lambda item: item.strength, default=None)
    strongest_lower = max(lower, key=lambda item: item.strength, default=None)
    if strongest_upper is None or strongest_lower is None:
        raise LiquidationScrapeError(
            "parsing", "Per-side highest liquidation candidates were not identified"
        )
    strength_base = max(strongest_upper.strength, strongest_lower.strength)
    near_tie = (
        strength_base > 0
        and abs(strongest_upper.strength - strongest_lower.strength) / strength_base
        <= config.thresholds.upper_lower_near_tie
    )
    return LiquidationSignal(
        symbol=config.symbol,
        upper_accumulations=upper,
        lower_accumulations=lower,
        strongest_upper_candidate=strongest_upper,
        strongest_lower_candidate=strongest_lower,
        near_tie=near_tie,
        fetched_at=now,
        latest_valid_scrape_at=now,
        mapping_version=mapping.version,
        validation=validation or ScrapeValidationResult.ok(mapping.version, reference_price, now),
    )


def _ensure_coinglass_login_if_configured(
    config: AnalysisConfiguration, page: BrowserPage, context: BrowserContext
) -> None:
    if not config.coinglass_auth_ref:
        return
    if config.coinglass_session_path and _coinglass_session_is_authenticated(page):
        return
    _login_to_coinglass(config, page)
    if config.coinglass_session_path:
        session_path = Path(config.coinglass_session_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(session_path))


def _coinglass_session_is_authenticated(page: BrowserPage) -> bool:
    page.goto("https://www.coinglass.com/login?act=lhm1", wait_until="load", timeout=45000)
    for _ in range(8):
        payload = page.evaluate(
            """
            () => ({
              loginFormVisible: !!document.querySelector('input[name="email"]'),
              text: document.body?.innerText?.slice(0, 800) || "",
            })
            """
        )
        if isinstance(payload, dict) and not payload.get("loginFormVisible"):
            return True
        time.sleep(1)
    return False


def _login_to_coinglass(config: AnalysisConfiguration, page: BrowserPage) -> None:
    email_name = config.coinglass_auth_ref["email"]
    password_name = config.coinglass_auth_ref["password"]
    email = os.environ.get(email_name)
    password = os.environ.get(password_name)
    if not email or not password:
        raise LiquidationScrapeError("configuration", "CoinGlass login credentials are not set")

    page.goto("https://www.coinglass.com/login?act=lhm1", wait_until="load", timeout=45000)
    page.wait_for_selector('input[name="email"]', timeout=20000)
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    button = page.locator("button").filter(has_text="Login").last
    if not button.is_enabled():
        raise LiquidationScrapeError("configuration", "CoinGlass login form was not submittable")
    button.click()
    _wait_for_login_completion(page)


def _wait_for_login_completion(page: BrowserPage) -> None:
    for _ in range(20):
        payload = page.evaluate(
            """
            () => {
              const text = document.body?.innerText || "";
              return {
                url: location.href,
                loginFormVisible: !!document.querySelector('input[name="email"]'),
                verificationRequired: /captcha|verify|verification|code/i.test(text),
              };
            }
            """
        )
        if isinstance(payload, dict) and payload.get("verificationRequired"):
            raise LiquidationScrapeError(
                "configuration", "CoinGlass login requires interactive verification", retryable=False
            )
        if isinstance(payload, dict) and not payload.get("loginFormVisible"):
            return
        time.sleep(1)
    raise LiquidationScrapeError("configuration", "CoinGlass login did not complete")


def _wait_for_echarts_payload(
    page: BrowserPage,
    current_market_price: float | None,
    *,
    expected_range: str | None = None,
    attempts: int = 24,
    sleep_seconds: float = 2.5,
) -> dict[str, Any]:
    last_payload: Any = None
    for _ in range(attempts):
        last_payload = page.evaluate(_ECHARTS_EXTRACTION_SCRIPT)
        if _echarts_payload_is_ready(last_payload, current_market_price, expected_range):
            return last_payload
        time.sleep(sleep_seconds)
    if isinstance(last_payload, dict) and last_payload.get("chartFound"):
        return last_payload
    raise LiquidationScrapeError("parsing", "ECharts liquidation chart was not readable")


def _select_heatmap_range(page: BrowserPage, heatmap_range: str) -> None:
    if not heatmap_range:
        return
    current = page.evaluate(
        """
        (target) => [...document.querySelectorAll('button')]
          .some((button) => button.innerText.trim() === target)
        """,
        heatmap_range,
    )
    if current is True:
        return
    opened = page.evaluate(
        """
        () => {
          const labels = [
            '12 hour', '24 hour', '48 hour', '3 day', '1 week', '2 week',
            '1 month', '3 month', '6 month', '1 Year', '2 Year'
          ];
          const button = [...document.querySelectorAll('button')]
            .find((item) => labels.includes(item.innerText.trim()));
          if (!button) return false;
          button.click();
          return true;
        }
        """
    )
    if opened is not True:
        raise LiquidationScrapeError("scrape_mapping", "CoinGlass heatmap range selector not found")
    time.sleep(0.5)
    selected = page.evaluate(
        """
        (target) => {
          const option = [...document.querySelectorAll('[role="option"], li')]
            .find((item) => item.innerText.trim() === target);
          if (!option) return false;
          option.click();
          return true;
        }
        """,
        heatmap_range,
    )
    if selected is not True:
        raise LiquidationScrapeError(
            "scrape_mapping", f"CoinGlass heatmap range is unsupported: {heatmap_range}"
        )
    time.sleep(1)


def _echarts_payload_is_ready(
    payload: Any, current_market_price: float | None, expected_range: str | None = None
) -> bool:
    if not isinstance(payload, dict) or not payload.get("chartFound"):
        return False
    if expected_range and payload.get("selectedRange") not in {None, expected_range}:
        return False
    y_min = _optional_float(payload.get("yMin"))
    y_max = _optional_float(payload.get("yMax"))
    y_count = int(payload.get("yCount") or 0)
    heatmap_points = int(payload.get("heatmapPoints") or 0)
    latest_points = int(payload.get("latestColumnPoints") or 0)
    price = current_market_price or _optional_float(payload.get("currentPrice"))
    return (
        y_min is not None
        and y_max is not None
        and y_max > y_min
        and y_count >= 2
        and heatmap_points > 0
        and latest_points > 0
        and price is not None
        and isinstance(payload.get("latestUpperTop"), list)
        and isinstance(payload.get("latestLowerTop"), list)
    )


def _capture_selector_screenshot(page: BrowserPage, selector: str) -> bytes:
    locator = page.wait_for_selector(selector, timeout=15000)
    try:
        return locator.screenshot()
    except AttributeError:
        return page.screenshot()


def _capture_screenshot(page: BrowserPage, mapping: ScrapeMappingConfiguration) -> bytes:
    region_selector = mapping.screenshot_regions["heatmap"]
    locator = page.wait_for_selector(region_selector, timeout=15000)
    try:
        return locator.screenshot()
    except AttributeError:
        return page.screenshot()


def _save_screenshot(
    config: AnalysisConfiguration, screenshot: bytes, now: datetime, *, suffix: str | None = None
) -> None:
    if not config.screenshot_dir:
        return
    directory = Path(config.screenshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    suffix_part = f"-{suffix}" if suffix else ""
    (directory / f"coinglass-{config.symbol.replace('/', '-')}-{stamp}{suffix_part}.png").write_bytes(
        screenshot
    )


def _range_slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip(
        "-"
    )


_ECHARTS_EXTRACTION_SCRIPT = r"""
() => {
  let webpackRequire;
  window.webpackChunk_N_E.push([
    [Math.floor(Math.random() * 1000000)],
    {},
    (require) => { webpackRequire = require; },
  ]);
  function resolveEcharts(require) {
    const candidates = ["47958", ...Object.keys(require.m || {})];
    const seen = new Set();
    for (const id of candidates) {
      if (seen.has(id)) continue;
      seen.add(id);
      try {
        const mod = require(id);
        if (mod && typeof mod.getInstanceByDom === "function") return mod;
      } catch (_) {}
    }
    return null;
  }
  const echarts = webpackRequire ? resolveEcharts(webpackRequire) : null;
  if (!echarts) return {chartFound: false, reason: "echarts_module_not_found"};
  const node = document.querySelector(".echarts-for-react");
  if (!node) return {chartFound: false, reason: "echarts_container_not_found"};
  const instance = echarts.getInstanceByDom(node);
  if (!instance) return {chartFound: false, reason: "echarts_instance_not_found"};
  const option = instance.getOption();
  const yPrices = option.yAxis?.[0]?.data || [];
  const xLabels = option.xAxis?.[0]?.data || [];
  const heatmap = (option.series || []).find((item) => item.type === "heatmap");
  const candle = (option.series || []).find((item) => item.type === "candlestick");
  const latestX = xLabels.length - 1;
  const lastCandle = candle?.data?.[candle.data.length - 1] || null;
  const currentPrice = lastCandle ? Number(lastCandle[1]) : null;

  function normalize(point) {
    if (!Array.isArray(point)) return null;
    const x = Number(point[0]);
    const yIndex = Number(point[1]);
    const value = Number(point[2]);
    const price = yPrices[yIndex] === undefined ? Number(yIndex) : Number(yPrices[yIndex]);
    if (![x, yIndex, value, price].every(Number.isFinite)) return null;
    return {x, yIndex, price, value};
  }

  function topRows(rows, predicate) {
    return rows
      .filter(predicate)
      .sort((left, right) => right.value - left.value)
      .slice(0, 8);
  }

  const rows = (heatmap?.data || []).map(normalize).filter(Boolean);
  const latestRows = rows.filter((row) => row.x === latestX);
  const rangeLabels = [
    '12 hour', '24 hour', '48 hour', '3 day', '1 week', '2 week',
    '1 month', '3 month', '6 month', '1 Year', '2 Year'
  ];
  const selectedRange = [...document.querySelectorAll('button')]
    .map((button) => button.innerText.trim())
    .find((text) => rangeLabels.includes(text)) || null;
  return {
    chartFound: true,
    selectedRange,
    currentPrice,
    latestTime: xLabels[latestX] || null,
    yMin: Math.min(...yPrices.map(Number).filter(Number.isFinite)),
    yMax: Math.max(...yPrices.map(Number).filter(Number.isFinite)),
    yCount: yPrices.length,
    heatmapPoints: rows.length,
    latestColumnPoints: latestRows.length,
    latestUpperTop: topRows(latestRows, (row) => row.price > currentPrice),
    latestLowerTop: topRows(latestRows, (row) => row.price < currentPrice),
  };
}
"""


def _echarts_rows_to_accumulations(
    rows: Any,
    side: str,
    period: str,
    config: AnalysisConfiguration,
    mapping: ScrapeMappingConfiguration,
    calibration: Any,
    validation: ScrapeValidationResult,
    current_market_price: float,
) -> list[LiquidationAccumulation]:
    if not isinstance(rows, list):
        raise LiquidationScrapeError("parsing", "ECharts liquidation rows were not a list")
    accumulations: list[LiquidationAccumulation] = []
    for row in rows:
        if not isinstance(row, dict):
            raise LiquidationScrapeError("parsing", "Malformed ECharts liquidation row")
        price = _required_float(row.get("price"), "price")
        strength = _required_float(row.get("value"), "value")
        y_index = _required_float(row.get("yIndex"), "yIndex")
        x_index = _required_float(row.get("x"), "x")
        if not config.price_sanity_bounds.contains(price, current_market_price):
            raise LiquidationScrapeError("scrape_sanity", "Parsed liquidation price is out of bounds")
        interval = abs(calibration.slope) / 2
        coords = SvgCoordinates(x=x_index, y=y_index, width=1.0, height=1.0)
        accumulations.append(
            LiquidationAccumulation(
                side=side,  # type: ignore[arg-type]
                period=period,
                price_low=price - interval,
                price_high=price + interval,
                strength=strength,
                svg_coordinates=coords,
                calibration=calibration,
                source="coinglass_web",
                mapping_version=mapping.version,
                validation=validation,
                evidence=f"{side} liquidation concentration near {price:.2f}",
            )
        )
    return accumulations


def _required_float(value: Any, field: str) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise LiquidationScrapeError("parsing", f"ECharts field {field} was not numeric")
    return parsed


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed):
        return None
    return parsed


def _read_axis_anchors(
    page: BrowserPage, mapping: ScrapeMappingConfiguration
) -> list[AxisAnchor]:
    anchors: list[AxisAnchor] = []
    for selector in mapping.axis_tick_selectors:
        for element in page.query_selector_all(selector):
            text = _element_text(element)
            y = _element_float_attr(element, "y")
            anchors.append(AxisAnchor(price=parse_price_label(text), svg_y=y))
    if len(anchors) < 2:
        raise LiquidationScrapeError("scrape_calibration", "Not enough price-axis anchors")
    return anchors


def _read_bars(page: BrowserPage, mapping: ScrapeMappingConfiguration) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for selector in mapping.heatmap_bar_selectors:
        for element in page.query_selector_all(selector):
            try:
                bars.append(
                    {
                        "side": _element_attr(element, "data-side"),
                        "period": _element_attr(element, "data-period") or "1H",
                        "strength": float(_element_attr(element, "data-strength")),
                        "x": _element_float_attr(element, "x"),
                        "y": _element_float_attr(element, "y"),
                        "width": _element_float_attr(element, "width"),
                        "height": _element_float_attr(element, "height"),
                    }
                )
            except (TypeError, ValueError) as exc:
                raise LiquidationScrapeError("parsing", "Malformed liquidation bar") from exc
    return bars


def _bar_to_accumulation(
    bar: dict[str, Any],
    config: AnalysisConfiguration,
    mapping: ScrapeMappingConfiguration,
    calibration: Any,
    validation: ScrapeValidationResult,
    current_market_price: float | None,
) -> LiquidationAccumulation:
    side = bar["side"]
    if side not in {"upper_short", "lower_long"}:
        raise LiquidationScrapeError("parsing", "Unknown liquidation bar side", retryable=False)
    price_a = calibration.price_for_svg_y(bar["y"])
    price_b = calibration.price_for_svg_y(bar["y"] + bar["height"])
    price_low = min(price_a, price_b)
    price_high = max(price_a, price_b)
    representative = (price_low + price_high) / 2
    if current_market_price is not None and not config.price_sanity_bounds.contains(
        representative, current_market_price
    ):
        raise LiquidationScrapeError("scrape_sanity", "Parsed liquidation price is out of bounds")
    coords = SvgCoordinates(x=bar["x"], y=bar["y"], width=bar["width"], height=bar["height"])
    return LiquidationAccumulation(
        side=side,
        period=bar["period"],
        price_low=price_low,
        price_high=price_high,
        strength=bar["strength"],
        svg_coordinates=coords,
        calibration=calibration,
        source="coinglass_web",
        mapping_version=mapping.version,
        validation=validation,
        evidence=f"{side} liquidation concentration near {price_low:.2f}-{price_high:.2f}",
    )


def _stale_failure_if_needed(
    config: AnalysisConfiguration,
    state: ScrapeRuntimeState | None,
    now: datetime,
) -> LiquidationSignal | None:
    if state is None or state.latest_valid_scrape_at is None:
        return None
    age = (now - state.latest_valid_scrape_at).total_seconds()
    if age <= config.max_scrape_staleness_seconds:
        return None
    return _failure_signal(
        config,
        "stale_scrape",
        "Latest valid liquidation scrape is stale",
        attempts=0,
        retryable=True,
        current_market_price=config.current_market_price,
        now=now,
        staleness_seconds=age,
        latest_valid_scrape_at=state.latest_valid_scrape_at,
    )


def _failure_signal(
    config: AnalysisConfiguration,
    category: str,
    message: str,
    *,
    attempts: int,
    retryable: bool,
    current_market_price: float | None,
    now: datetime,
    staleness_seconds: float | None = None,
    latest_valid_scrape_at: datetime | None = None,
) -> LiquidationSignal:
    validation = ScrapeValidationResult.failed(
        config.scrape_mapping_version,
        message,
        layout_valid=category not in {"scrape_mapping"},
        render_valid=category not in {"scrape_render"},
        screenshot_valid=category not in {"scrape_screenshot"},
        dom_parse_valid=category not in {"parsing"},
        calibration_valid=category not in {"scrape_calibration"},
        per_side_candidate_valid=category not in {"parsing"},
        price_sanity_valid=category not in {"scrape_sanity"},
        current_market_price=current_market_price,
        latest_valid_scrape_at=latest_valid_scrape_at,
        staleness_seconds=staleness_seconds,
    )
    return LiquidationSignal(
        symbol=config.symbol,
        upper_accumulations=[],
        lower_accumulations=[],
        fetched_at=now,
        mapping_version=config.scrape_mapping_version,
        validation=validation,
        latest_valid_scrape_at=latest_valid_scrape_at,
        failure=FailureState(
            category=category,  # type: ignore[arg-type]
            retryable=retryable,
            attempts=attempts,
            source="coinglass_web",
            safe_message=message,
        ),
    )


def _element_attr(element: Any, name: str) -> str:
    if hasattr(element, "get_attribute"):
        value = element.get_attribute(name)
    else:
        value = element[name]
    if value is None:
        raise ValueError(f"Missing attribute {name}")
    return str(value)


def _element_float_attr(element: Any, name: str) -> float:
    return float(_element_attr(element, name))


def _element_text(element: Any) -> str:
    if hasattr(element, "inner_text"):
        return str(element.inner_text())
    return str(element.get("text", ""))
