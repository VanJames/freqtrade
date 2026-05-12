import json

import pandas as pd

from scripts import build_market_snapshot


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_pair_to_okx_inst_id_futures():
    assert build_market_snapshot.pair_to_okx_inst_id("BTC/USDT:USDT", "futures") == "BTC-USDT-SWAP"


def test_fetch_okx_klines_uses_okx_swap_and_sorts_oldest_first(monkeypatch):
    urls = []

    def fake_urlopen(url, timeout):
        urls.append(url)
        return _Response(
            {
                "code": "0",
                "data": [
                    ["3000", "3", "4", "2", "3.5", "30"],
                    ["2000", "2", "3", "1", "2.5", "20"],
                    ["1000", "1", "2", "0.5", "1.5", "10"],
                ],
            }
        )

    monkeypatch.setattr(build_market_snapshot, "urlopen", fake_urlopen)

    frame = build_market_snapshot.fetch_klines(
        "okx", "BTC/USDT:USDT", "5m", 3, "futures"
    )

    assert "https://www.okx.com/api/v5/market/candles" in urls[0]
    assert "instId=BTC-USDT-SWAP" in urls[0]
    assert list(frame["open_time"]) == [1000, 2000, 3000]
    assert list(frame["volume"]) == [10, 20, 30]


def test_build_snapshot_uses_configured_exchange(monkeypatch):
    calls = []

    def fake_fetch(exchange, pair, interval, limit, trading_mode):
        calls.append((exchange, pair, interval, limit, trading_mode))
        return pd.DataFrame(
            {
                "open": list(range(1, 221)),
                "high": list(range(2, 222)),
                "low": list(range(0, 220)),
                "close": list(range(1, 221)),
                "volume": [100.0] * 220,
            }
        )

    monkeypatch.setattr(build_market_snapshot, "fetch_klines", fake_fetch)
    snapshot = build_market_snapshot.build_snapshot(
        {
            "exchange": {"name": "okx", "pair_whitelist": ["BTC/USDT:USDT"]},
            "trading_mode": "futures",
            "stake_currency": "USDT",
            "timeframe": "5m",
            "freqai": {"feature_parameters": {"include_timeframes": ["5m"]}},
        },
        limit=220,
    )

    assert calls == [("okx", "BTC/USDT:USDT", "5m", 220, "futures")]
    assert snapshot["exchange"] == "okx"
    assert snapshot["per_pair"][0]["last_close"] == 220.0
