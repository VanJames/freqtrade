from trading_notice.config import load_analysis_config
from trading_notice.kline import analyze_kline_signals


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


def candles(count=120, *, last_close=130.0, last_volume=200.0):
    rows = []
    for i in range(count - 1):
        close = 100.0 + i * 0.1
        rows.append([i, close - 1, close + 1, close - 2, close, 100.0])
    rows.append([count, last_close - 1, last_close + 1, last_close - 2, last_close, last_volume])
    return rows


class FakeExchange:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_analyze_kline_signals_computes_ma_volume_and_key_level_state():
    result = analyze_kline_signals(config(), exchange=FakeExchange(candles()))

    signal = result[0]
    assert signal.failure is None
    assert signal.ma7 is not None
    assert signal.ma25 is not None
    assert signal.ma99 is not None
    assert signal.volume_state == "high_breakout"
    assert signal.key_level_state == "stood_above"
    assert signal.last_closed_price == 130.0


def test_analyze_kline_signals_marks_insufficient_candles():
    result = analyze_kline_signals(config(), exchange=FakeExchange(candles(10)))

    assert result[0].volume_state == "insufficient"
    assert result[0].failure is None


def test_analyze_kline_signals_converts_ccxt_exception_to_failure():
    result = analyze_kline_signals(
        config(), exchange=FakeExchange(error=RuntimeError("api key secret leaked")), max_attempts=1
    )

    assert result[0].failure.category == "ccxt_api"
    assert "secret" not in result[0].failure.safe_message.lower()
