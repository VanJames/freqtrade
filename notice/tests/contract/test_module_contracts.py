from datetime import datetime, timezone

from trading_notice.config import load_analysis_config
from trading_notice.liquidation_scraper import scrape_liquidation_signal


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
    def __init__(self):
        self.region = FakeElement(screenshot_bytes=PNG_BYTES)
        self.ticks = [
            FakeElement({"y": "260"}, "$1600"),
            FakeElement({"y": "160"}, "$1700"),
            FakeElement({"y": "60"}, "$1800"),
        ]
        self.bars = [
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

    def goto(self, url, wait_until="networkidle", timeout=30000):
        self.url = url

    def wait_for_selector(self, selector, timeout=30000):
        return self.region

    def screenshot(self, **kwargs):
        return PNG_BYTES

    def query_selector_all(self, selector):
        if selector == ".price-tick":
            return self.ticks
        if selector == ".liq-bar":
            return self.bars
        return []


def config():
    env = {
        "TRADING_SYMBOL": "ETH/USDT",
        "ANALYSIS_INTERVAL": "15m",
        "RECIPIENTS": "trader@example.test",
        "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_FROM": "alerts@example.test",
        "CURRENT_MARKET_PRICE": "1700",
    }
    return load_analysis_config(env)


def test_liquidation_scraper_success_contract_shape():
    signal = scrape_liquidation_signal(
        config(),
        page=FakePage(),
        now=datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc),
    )

    assert signal.failure is None
    assert signal.mapping_version == "coinglass-free-heatmap-v1"
    assert len(signal.upper_accumulations) == 1
    assert len(signal.lower_accumulations) == 1
    assert signal.strongest_upper_candidate is signal.upper_accumulations[0]
    assert signal.strongest_lower_candidate is signal.lower_accumulations[0]
    assert signal.validation.layout_valid is True
    assert signal.validation.screenshot_valid is True
    assert signal.validation.calibration_valid is True
    assert signal.strongest_upper_candidate.calibration.axis_anchors
    assert signal.as_dict_shape()["upper_accumulations"]
