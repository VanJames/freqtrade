#!/usr/bin/env python3
"""
Build a lightweight market snapshot for OpenAI profile selection.

The snapshot is intentionally small and read-only. It summarizes recent OHLCV
behavior for the configured pairs and timeframes so a model can choose between
conservative, balanced, and aggressive execution profiles.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pandas as pd


TIMEFRAME_TO_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
BINANCE_SPOT_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"


@dataclass
class CandleSummary:
    pair: str
    timeframe: str
    last_close: float
    return_pct: float
    atr_pct: float
    bb_width: float
    ema20_above_ema50: bool
    ema50_above_ema200: bool
    ema20_below_ema50: bool
    ema50_below_ema200: bool
    volume_ratio: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a market snapshot JSON.")
    parser.add_argument("--config", default=str(Path("user_data/config.json")))
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=220)
    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def pair_to_symbol(pair: str) -> str:
    return pair.split(":")[0].replace("/", "")


def klines_url(trading_mode: str) -> str:
    return BINANCE_FUTURES_URL if trading_mode == "futures" else BINANCE_SPOT_URL


def fetch_klines(symbol: str, interval: str, limit: int, trading_mode: str) -> pd.DataFrame:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = klines_url(trading_mode) + "?" + urlencode(params)
    with urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = []
    for row in payload:
        rows.append(
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    return pd.DataFrame(rows)


def empty_summary(pair: str, timeframe: str, error: str) -> dict[str, Any]:
    return {
        "pair": pair,
        "timeframe": timeframe,
        "error": error,
        "last_close": 0.0,
        "return_pct": 0.0,
        "atr_pct": 0.0,
        "bb_width": 0.0,
        "ema20_above_ema50": False,
        "ema50_above_ema200": False,
        "ema20_below_ema50": False,
        "ema50_below_ema200": False,
        "volume_ratio": 0.0,
    }


def summarize_frame(pair: str, timeframe: str, frame: pd.DataFrame) -> CandleSummary:
    if frame.empty:
        raise ValueError(f"No candles returned for {pair} {timeframe}")
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    bb_width = ((mid + 2 * std) - (mid - 2 * std)) / mid
    vol_ratio = volume.iloc[-1] / max(volume.rolling(20).mean().iloc[-1], 1e-9)
    return_pct = close.iloc[-1] / close.iloc[0] - 1 if close.iloc[0] else 0.0

    return CandleSummary(
        pair=pair,
        timeframe=timeframe,
        last_close=float(close.iloc[-1]),
        return_pct=float(return_pct),
        atr_pct=float((atr.iloc[-1] / close.iloc[-1]) if not math.isnan(atr.iloc[-1]) else 0.0),
        bb_width=float(bb_width.iloc[-1] if not math.isnan(bb_width.iloc[-1]) else 0.0),
        ema20_above_ema50=bool(ema20.iloc[-1] > ema50.iloc[-1]),
        ema50_above_ema200=bool(ema50.iloc[-1] > ema200.iloc[-1]),
        ema20_below_ema50=bool(ema20.iloc[-1] < ema50.iloc[-1]),
        ema50_below_ema200=bool(ema50.iloc[-1] < ema200.iloc[-1]),
        volume_ratio=float(vol_ratio),
    )


def build_snapshot(config: dict[str, Any], limit: int) -> dict[str, Any]:
    trading_mode = config.get("trading_mode", "spot")
    pair_whitelist = config.get("exchange", {}).get("pair_whitelist", [])
    timeframes = config.get("freqai", {}).get("feature_parameters", {}).get(
        "include_timeframes", [config.get("timeframe", "5m")]
    )

    per_pair: list[dict[str, Any]] = []
    for pair in pair_whitelist:
        symbol = pair_to_symbol(pair)
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_TO_MINUTES:
                continue
            try:
                frame = fetch_klines(symbol, timeframe, limit, trading_mode)
                summary = summarize_frame(pair, timeframe, frame)
                per_pair.append(
                    {
                        "pair": summary.pair,
                        "timeframe": summary.timeframe,
                        "last_close": summary.last_close,
                        "return_pct": summary.return_pct,
                        "atr_pct": summary.atr_pct,
                        "bb_width": summary.bb_width,
                        "ema20_above_ema50": summary.ema20_above_ema50,
                        "ema50_above_ema200": summary.ema50_above_ema200,
                        "ema20_below_ema50": summary.ema20_below_ema50,
                        "ema50_below_ema200": summary.ema50_below_ema200,
                        "volume_ratio": summary.volume_ratio,
                    }
                )
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                per_pair.append(empty_summary(pair, timeframe, str(exc)))

    atr_values = [item["atr_pct"] for item in per_pair if item["atr_pct"] > 0]
    bb_values = [item["bb_width"] for item in per_pair if item["bb_width"] > 0]
    trend_up_count = sum(
        1
        for item in per_pair
        if item["ema20_above_ema50"] and item["ema50_above_ema200"]
    )
    trend_down_count = sum(
        1
        for item in per_pair
        if item["ema20_below_ema50"] and item["ema50_below_ema200"]
    )

    def median(values: list[float]) -> float:
        return float(pd.Series(values).median()) if values else 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exchange": config.get("exchange", {}).get("name", ""),
        "trading_mode": trading_mode,
        "stake_currency": config.get("stake_currency", "USDT"),
        "timeframes": timeframes,
        "pairs": pair_whitelist,
        "summary": {
            "median_atr_pct": median(atr_values),
            "median_bb_width": median(bb_values),
            "trend_up_count": trend_up_count,
            "trend_down_count": trend_down_count,
            "pair_count": len(pair_whitelist),
            "observations": len(per_pair),
        },
        "per_pair": per_pair,
    }


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    snapshot = build_snapshot(config, args.limit)
    rendered = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)
    Path(args.output).write_text(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
