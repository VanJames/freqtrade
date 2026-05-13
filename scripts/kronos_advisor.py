#!/usr/bin/env python3
"""
Publish a Kronos-style market forecast JSON for advisory use.

The script is deliberately read-only. It first tries to use the upstream Kronos
package if it is installed in the runtime. When Kronos is unavailable, it falls
back to a deterministic OHLCV momentum/volatility forecast so the rest of the
trading system can be integrated and tested without blocking on model setup.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_market_snapshot


USER_DATA = ROOT / "user_data"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_OUTPUT = AUTOOPT_DIR / "kronos_forecast.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "kronos_forecast.jsonl"


@dataclass
class PairForecast:
    pair: str
    pred_return_pct: float
    pred_max_drawdown_pct: float
    pred_max_runup_pct: float
    pred_volatility_pct: float
    side_bias: str
    confidence: float
    source: str
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Kronos advisory forecast JSON.")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--timeframe", default="")
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--pred-len", type=int, default=24)
    parser.add_argument("--model", default="NeoQuasar/Kronos-small")
    parser.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--confidence-threshold", type=float, default=0.55)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=30.0)
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def load_pair_frame(
    *, config: dict[str, Any], config_path: Path, pair: str, timeframe: str, lookback: int
) -> pd.DataFrame:
    exchange = config.get("exchange", {}).get("name", "")
    trading_mode = config.get("trading_mode", "spot")
    datadir = build_market_snapshot.default_datadir(str(config_path), exchange)
    return build_market_snapshot.load_or_fetch_klines(
        exchange=exchange,
        pair=pair,
        interval=timeframe,
        limit=max(lookback, 80),
        trading_mode=trading_mode,
        datadir=datadir,
    ).tail(lookback).reset_index(drop=True)


def _timestamp_series(frame: pd.DataFrame) -> pd.Series:
    if "date" in frame.columns:
        return pd.to_datetime(frame["date"], utc=True)
    if "open_time" in frame.columns:
        return pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return pd.Series(pd.date_range(end=datetime.now(UTC), periods=len(frame), freq="5min"))


def _future_timestamps(frame: pd.DataFrame, pred_len: int, timeframe: str) -> pd.Series:
    timestamps = _timestamp_series(frame)
    if timestamps.empty:
        start = pd.Timestamp.now(tz=UTC)
    else:
        start = timestamps.iloc[-1]
    freq = timeframe.replace("m", "min")
    return pd.Series(pd.date_range(start=start, periods=pred_len + 1, freq=freq)[1:])


def _summarize_prediction(pair: str, pred_df: pd.DataFrame, source: str) -> PairForecast:
    close = pred_df.get("close", pd.Series(dtype=float)).astype(float)
    if close.empty or close.iloc[0] == 0:
        return PairForecast(pair, 0.0, 0.0, 0.0, 0.0, "hold", 0.0, source, "empty_prediction")
    pred_return = close.iloc[-1] / close.iloc[0] - 1.0
    drawdown = close.min() / close.iloc[0] - 1.0
    runup = close.max() / close.iloc[0] - 1.0
    volatility = close.pct_change().dropna().std() if len(close) > 1 else 0.0
    confidence = min(abs(pred_return) * 18.0 + _as_float(volatility) * 12.0, 0.95)
    side = "hold"
    if confidence >= 0.55 and pred_return > 0:
        side = "long"
    elif confidence >= 0.55 and pred_return < 0:
        side = "short"
    return PairForecast(
        pair=pair,
        pred_return_pct=round(float(pred_return) * 100.0, 4),
        pred_max_drawdown_pct=round(float(drawdown) * 100.0, 4),
        pred_max_runup_pct=round(float(runup) * 100.0, 4),
        pred_volatility_pct=round(_as_float(volatility) * 100.0, 4),
        side_bias=side,
        confidence=round(confidence, 4),
        source=source,
        reason="forecast_path_summary",
    )


def heuristic_forecast(pair: str, frame: pd.DataFrame, pred_len: int) -> PairForecast:
    close = frame["close"].astype(float)
    if len(close) < 30 or close.iloc[-1] == 0:
        return PairForecast(pair, 0.0, 0.0, 0.0, 0.0, "hold", 0.0, "heuristic", "insufficient_data")
    returns = close.pct_change().dropna()
    recent = close.tail(min(24, len(close)))
    medium = close.tail(min(96, len(close)))
    recent_return = recent.iloc[-1] / recent.iloc[0] - 1.0 if recent.iloc[0] else 0.0
    medium_return = medium.iloc[-1] / medium.iloc[0] - 1.0 if medium.iloc[0] else 0.0
    momentum = recent_return * 0.65 + medium_return * 0.35
    pred_return = momentum * min(pred_len / 24.0, 1.5)
    volatility = returns.tail(min(96, len(returns))).std()
    drawdown = min(pred_return - _as_float(volatility) * math.sqrt(max(pred_len, 1)), 0.0)
    runup = max(pred_return + _as_float(volatility) * math.sqrt(max(pred_len, 1)), 0.0)
    confidence = min(abs(momentum) * 22.0 + _as_float(volatility) * 15.0, 0.9)
    side = "hold"
    if confidence >= 0.55 and pred_return > 0:
        side = "long"
    elif confidence >= 0.55 and pred_return < 0:
        side = "short"
    return PairForecast(
        pair=pair,
        pred_return_pct=round(float(pred_return) * 100.0, 4),
        pred_max_drawdown_pct=round(float(drawdown) * 100.0, 4),
        pred_max_runup_pct=round(float(runup) * 100.0, 4),
        pred_volatility_pct=round(_as_float(volatility) * 100.0, 4),
        side_bias=side,
        confidence=round(confidence, 4),
        source="heuristic",
        reason="kronos_unavailable_momentum_fallback",
    )


def try_kronos_forecast(
    *,
    pair: str,
    frame: pd.DataFrame,
    timeframe: str,
    pred_len: int,
    model_name: str,
    tokenizer_name: str,
    device: str,
) -> PairForecast:
    try:
        from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore
    except Exception:
        return heuristic_forecast(pair, frame, pred_len)

    try:
        tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
        model = Kronos.from_pretrained(model_name)
        predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        input_cols = [col for col in ["open", "high", "low", "close", "volume", "amount"] if col in frame.columns]
        x_df = frame[input_cols].copy()
        x_timestamp = _timestamp_series(frame)
        y_timestamp = _future_timestamps(frame, pred_len, timeframe)
        pred_df = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=pred_len,
            T=1.0,
            top_p=0.9,
            sample_count=1,
        )
        return _summarize_prediction(pair, pred_df, "kronos")
    except Exception as exc:
        forecast = heuristic_forecast(pair, frame, pred_len)
        forecast.reason = f"kronos_failed_fallback:{str(exc)[:160]}"
        return forecast


def summarize_market(forecasts: list[PairForecast], confidence_threshold: float) -> dict[str, Any]:
    strong = [item for item in forecasts if item.confidence >= confidence_threshold]
    long_count = sum(1 for item in strong if item.side_bias == "long")
    short_count = sum(1 for item in strong if item.side_bias == "short")
    hold_count = len(forecasts) - long_count - short_count
    market_bias = "hold"
    if long_count > short_count:
        market_bias = "long"
    elif short_count > long_count:
        market_bias = "short"
    avg_confidence = sum(item.confidence for item in forecasts) / len(forecasts) if forecasts else 0.0
    avg_return = sum(item.pred_return_pct for item in forecasts) / len(forecasts) if forecasts else 0.0
    return {
        "market_bias": market_bias,
        "long_count": long_count,
        "short_count": short_count,
        "hold_count": hold_count,
        "avg_confidence": round(avg_confidence, 4),
        "avg_pred_return_pct": round(avg_return, 4),
        "confidence_threshold": confidence_threshold,
    }


def run_once(args: argparse.Namespace) -> dict:
    config_path = Path(args.config).resolve()
    config = build_market_snapshot.load_config(str(config_path))
    timeframe = args.timeframe or config.get("timeframe", "5m")
    pairs = config.get("exchange", {}).get("pair_whitelist", [])
    forecasts: list[PairForecast] = []
    errors: list[dict[str, str]] = []

    for pair in pairs:
        try:
            frame = load_pair_frame(
                config=config,
                config_path=config_path,
                pair=pair,
                timeframe=timeframe,
                lookback=args.lookback,
            )
            forecasts.append(
                try_kronos_forecast(
                    pair=pair,
                    frame=frame,
                    timeframe=timeframe,
                    pred_len=args.pred_len,
                    model_name=args.model,
                    tokenizer_name=args.tokenizer,
                    device=args.device,
                )
            )
        except Exception as exc:
            errors.append({"pair": str(pair), "error": str(exc)[-500:]})

    payload = {
        "record_type": "kronos_forecast",
        "created_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "tokenizer": args.tokenizer,
        "timeframe": timeframe,
        "lookback": args.lookback,
        "pred_len": args.pred_len,
        "pairs": {item.pair: asdict(item) for item in forecasts},
        "summary": summarize_market(forecasts, args.confidence_threshold),
        "errors": errors,
        "usage_note": "Advisory only. Kronos confirms or weakens statistical signals; it must not open trades by itself.",
    }
    write_json(Path(args.output), payload)
    append_jsonl(Path(args.ledger), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return payload


def main() -> int:
    args = parse_args()
    while True:
        run_once(args)
        if not args.loop:
            return 0
        time.sleep(max(args.interval_minutes, 1.0) * 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
