from datetime import datetime, timedelta, timezone

from trading_notice.config import load_analysis_config
from trading_notice.liquidation_scraper import (
    ScrapeRuntimeState,
    _optional_float,
    scrape_liquidation_signal,
)


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00 "
    b"\x00\x00\x00 "
    b"\x08\x02\x00\x00\x00"
    b"abcdefghijklmno1234567890"
)


class FakeElement:
    def __init__(self, attrs=None, text="", screenshot_bytes=None):
        self.attrs = attrs or {}
        self.text = text
        self.screenshot_bytes = screenshot_bytes or PNG_BYTES

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.text

    def screenshot(self):
        return self.screenshot_bytes


class FakePage:
    def __init__(self, *, screenshot_bytes=PNG_BYTES, ticks=None, bars=None, fail_wait=False):
        self.region = FakeElement(screenshot_bytes=screenshot_bytes)
        self.ticks = ticks or [
            FakeElement({"y": "260"}, "$1600"),
            FakeElement({"y": "160"}, "$1700"),
            FakeElement({"y": "60"}, "$1800"),
        ]
        self.bars = bars or [
            FakeElement(
                {
                    "data-side": "upper_short",
                    "data-period": "1H",
                    "data-strength": "0.82",
                    "x": "412",
                    "y": "80",
                    "width": "9",
                    "height": "42",
                }
            ),
            FakeElement(
                {
                    "data-side": "lower_long",
                    "data-period": "4H",
                    "data-strength": "0.76",
                    "x": "220",
                    "y": "210",
                    "width": "10",
                    "height": "39",
                }
            ),
        ]
        self.fail_wait = fail_wait

    def goto(self, url, wait_until="networkidle", timeout=30000):
        self.url = url

    def wait_for_selector(self, selector, timeout=30000):
        if self.fail_wait:
            raise TimeoutError("missing selector")
        return self.region

    def screenshot(self, **kwargs):
        return self.region.screenshot()

    def query_selector_all(self, selector):
        if selector == ".price-tick":
            return self.ticks
        if selector == ".liq-bar":
            return self.bars
        return []


class CrashingPage(FakePage):
    def goto(self, url, wait_until="networkidle", timeout=30000):
        raise RuntimeError("Page.goto: Page crashed")


class FakeEchartsPage(FakePage):
    def __init__(self, *, empty_echarts_reads=0, formatted_axis=False, **kwargs):
        super().__init__(**kwargs)
        self.empty_echarts_reads = empty_echarts_reads
        self.formatted_axis = formatted_axis
        self.evaluate_calls = 0
        self.selected_range = "24 hour"

    def wait_for_selector(self, selector, timeout=30000):
        if selector == ".echarts-for-react":
            return self.region
        return super().wait_for_selector(selector, timeout=timeout)

    def evaluate(self, script, *args):
        if "some((button)" in script:
            return bool(args) and args[0] == self.selected_range
        if "labels =" in script:
            return True
        if "option.click" in script:
            self.selected_range = args[0]
            return True
        self.evaluate_calls += 1
        if self.evaluate_calls <= self.empty_echarts_reads:
            return {
                "chartFound": True,
                "currentPrice": None,
                "yMin": None,
                "yMax": None,
                "yCount": 0,
                "heatmapPoints": 0,
                "latestColumnPoints": 0,
                "latestUpperTop": [],
                "latestLowerTop": [],
            }
        multiplier = 2 if self.selected_range == "48 hour" else 1
        y_min = "$60,477.7" if self.formatted_axis else 60477.7
        y_max = "66,837.75" if self.formatted_axis else 66837.75
        return {
            "chartFound": True,
            "selectedRange": self.selected_range,
            "currentPrice": 63972.6,
            "latestTime": "21 Jun 2026, 00:55",
            "yMin": y_min,
            "yMax": y_max,
            "yCount": 132,
            "heatmapPoints": 13545,
            "latestColumnPoints": 86,
            "latestUpperTop": [
                {"x": 287, "yIndex": 85, "price": 64604.45, "value": 33817476.38 * multiplier},
                {"x": 287, "yIndex": 91, "price": 64895.75, "value": 31926346.05},
            ],
            "latestLowerTop": [
                {"x": 287, "yIndex": 65, "price": 63633.45, "value": 49129438.84},
                {"x": 287, "yIndex": 47, "price": 62759.55, "value": 46289713.86},
            ],
        }


def config(current="1700"):
    env = {
        "TRADING_SYMBOL": "ETH/USDT",
        "ANALYSIS_INTERVAL": "15m",
        "RECIPIENTS": "trader@example.test",
        "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_FROM": "alerts@example.test",
        "CURRENT_MARKET_PRICE": current,
    }
    return load_analysis_config(env)


def config_with_ranges(current="1700", ranges="24 hour"):
    env = {
        "TRADING_SYMBOL": "ETH/USDT",
        "ANALYSIS_INTERVAL": "15m",
        "RECIPIENTS": "trader@example.test",
        "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
        "COINGLASS_HEATMAP_RANGES": ranges,
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_FROM": "alerts@example.test",
        "CURRENT_MARKET_PRICE": current,
    }
    return load_analysis_config(env)


def test_echarts_canvas_page_returns_liquidation_candidates():
    signal = scrape_liquidation_signal(
        config(current="63972.6"), page=FakeEchartsPage(), max_attempts=1
    )

    assert signal.failure is None
    assert signal.strongest_upper_candidate.price_low < 64604.45
    assert signal.strongest_upper_candidate.price_high > 64604.45
    assert signal.strongest_upper_candidate.strength == 33817476.38
    assert signal.strongest_lower_candidate.price_low < 63633.45
    assert signal.strongest_lower_candidate.price_high > 63633.45
    assert signal.strongest_lower_candidate.strength == 49129438.84
    assert signal.near_tie is False


def test_echarts_canvas_waits_until_series_data_is_ready(monkeypatch):
    page = FakeEchartsPage(empty_echarts_reads=1)
    monkeypatch.setattr("trading_notice.liquidation_scraper.time.sleep", lambda seconds: None)

    signal = scrape_liquidation_signal(config(current="63972.6"), page=page, max_attempts=1)

    assert signal.failure is None
    assert page.evaluate_calls == 2
    assert signal.strongest_lower_candidate.strength == 49129438.84


def test_echarts_canvas_accepts_formatted_axis_numbers():
    signal = scrape_liquidation_signal(
        config(current="63972.6"), page=FakeEchartsPage(formatted_axis=True), max_attempts=1
    )

    assert signal.failure is None
    assert signal.strongest_upper_candidate.price_low < 64604.45
    assert signal.strongest_lower_candidate.price_high > 63633.45


def test_echarts_canvas_aggregates_configured_heatmap_ranges():
    signal = scrape_liquidation_signal(
        config_with_ranges(current="63972.6", ranges="24 hour,48 hour"),
        page=FakeEchartsPage(),
        max_attempts=1,
    )

    assert signal.failure is None
    assert {item.period for item in signal.upper_accumulations} == {"24 hour", "48 hour"}
    assert signal.strongest_upper_candidate.period == "48 hour"
    assert signal.strongest_upper_candidate.strength == 33817476.38 * 2


def test_render_timeout_returns_failure_state():
    signal = scrape_liquidation_signal(config(), page=FakePage(fail_wait=True), max_attempts=1)

    assert signal.failure.category == "scrape_render"
    assert signal.validation.render_valid is False


def test_page_crash_returns_retryable_failure_state():
    signal = scrape_liquidation_signal(config(), page=CrashingPage(), max_attempts=1)

    assert signal.failure.category == "coinglass_scrape"
    assert signal.failure.retryable is True
    assert "page crashed" in signal.failure.safe_message


def test_blank_screenshot_returns_failure_state():
    signal = scrape_liquidation_signal(
        config(), page=FakePage(screenshot_bytes=b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), max_attempts=1
    )

    assert signal.failure.category == "scrape_screenshot"
    assert signal.validation.screenshot_valid is False


def test_calibration_failure_returns_failure_state():
    bad_ticks = [FakeElement({"y": "160"}, "$1700"), FakeElement({"y": "160"}, "$1800")]

    signal = scrape_liquidation_signal(config(), page=FakePage(ticks=bad_ticks), max_attempts=1)

    assert signal.failure.category in {"scrape_calibration", "scrape_mapping"}
    assert signal.failure.safe_message


def test_sanity_rejection_returns_failure_state():
    signal = scrape_liquidation_signal(config(current="100"), page=FakePage(), max_attempts=1)

    assert signal.failure.category == "scrape_sanity"
    assert signal.validation.price_sanity_valid is False


def test_stale_latest_valid_scrape_returns_failure_without_scraping():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    state = ScrapeRuntimeState(latest_valid_scrape_at=now - timedelta(hours=3))

    signal = scrape_liquidation_signal(config(), page=FakePage(), state=state, now=now)

    assert signal.failure.category == "stale_scrape"
    assert signal.validation.staleness_seconds == 10800


def test_optional_float_accepts_common_chart_number_formatting():
    assert _optional_float("$60,477.70") == 60477.7
    assert _optional_float(" 66,837.75 ") == 66837.75
    assert _optional_float("not-a-number") is None
