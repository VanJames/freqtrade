"""CCXT-backed K-line signal analyzer."""

from __future__ import annotations

import time
from typing import Any

from trading_notice.models import AnalysisConfiguration, FailureState, KlineSignal


def analyze_kline_signals(
    config: AnalysisConfiguration,
    *,
    exchange: Any | None = None,
    limit: int = 120,
    max_attempts: int = 2,
    sleep_seconds: float = 0.0,
) -> list[KlineSignal]:
    own_exchange = None
    if exchange is None:
        try:
            import ccxt
        except Exception:
            return [_failure_signal(config.symbol, period, "ccxt import failed", 1) for period in config.kline_periods]
        try:
            exchange_cls = getattr(ccxt, config.kline_exchange_id)
        except AttributeError:
            return [
                _failure_signal(
                    config.symbol,
                    period,
                    f"unsupported ccxt exchange {config.kline_exchange_id}",
                    1,
                    source=f"ccxt.{config.kline_exchange_id}.fetch_ohlcv",
                )
                for period in config.kline_periods
            ]
        own_exchange = exchange_cls(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        )
        exchange = own_exchange

    results: list[KlineSignal] = []
    symbol = _exchange_symbol(config)
    for period in config.kline_periods:
        attempts = 0
        last_error = ""
        for attempts in range(1, max_attempts + 1):
            try:
                candles = exchange.fetch_ohlcv(symbol, timeframe=period.lower(), limit=limit)
                results.append(_signal_from_candles(config, period, candles))
                break
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        else:
            results.append(
                _failure_signal(
                    config.symbol,
                    period,
                    last_error,
                    attempts,
                    source=f"ccxt.{config.kline_exchange_id}.fetch_ohlcv",
                )
            )
    if own_exchange is not None and hasattr(own_exchange, "close"):
        own_exchange.close()
    return results


def _signal_from_candles(config: AnalysisConfiguration, period: str, candles: list[list[float]]) -> KlineSignal:
    if len(candles) < 25:
        return KlineSignal(
            symbol=config.symbol,
            period=period,
            volume_state="insufficient",
            key_level_state="unknown",
            evidence="Insufficient closed candles for K-line confirmation",
        )
    closes = [float(row[4]) for row in candles]
    volumes = [float(row[5]) for row in candles]
    last_close = closes[-1]
    prev_close = closes[-2]
    ma7 = _mean(closes[-7:])
    ma25 = _mean(closes[-25:])
    ma99 = _mean(closes[-99:]) if len(closes) >= 99 else None
    baseline_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else _mean(volumes[:-1])
    volume_ratio = volumes[-1] / baseline_volume if baseline_volume else 1.0

    ma_position = "above_ma25" if last_close >= ma25 else "below_ma25"
    if ma99 is not None:
        ma_position += "_above_ma99" if last_close >= ma99 else "_below_ma99"

    threshold = config.thresholds.volume_confirmation
    if volume_ratio >= threshold and last_close >= prev_close:
        volume_state = "high_breakout"
    elif volume_ratio >= threshold and last_close < prev_close:
        volume_state = "high_breakdown"
    elif volume_ratio <= 1 / threshold:
        volume_state = "low_rebound"
    else:
        volume_state = "normal"

    key_level = ma25
    stood = config.thresholds.stood_above_confirmation
    broke = config.thresholds.broke_below_confirmation
    if last_close >= key_level * (1 + stood):
        key_level_state = "stood_above"
    elif last_close <= key_level * (1 - broke):
        key_level_state = "broke_below"
    elif last_close < prev_close and volume_state == "low_rebound":
        key_level_state = "stalled_at_resistance"
    else:
        key_level_state = "near_level_unconfirmed"

    return KlineSignal(
        symbol=config.symbol,
        period=period,
        last_closed_price=last_close,
        ma7=ma7,
        ma25=ma25,
        ma99=ma99,
        ma_position=ma_position,
        volume_state=volume_state,
        key_level=key_level,
        key_level_state=key_level_state,
        evidence=f"{period} close {last_close:.2f}, {ma_position}, {volume_state}, {key_level_state}",
    )


def _exchange_symbol(config: AnalysisConfiguration) -> str:
    symbol = config.symbol
    if config.kline_exchange_id == "okx" and ":" not in symbol:
        return f"{symbol}:USDT"
    return symbol


def _failure_signal(
    symbol: str,
    period: str,
    message: str,
    attempts: int,
    *,
    source: str = "ccxt.fetch_ohlcv",
) -> KlineSignal:
    return KlineSignal(
        symbol=symbol,
        period=period,
        volume_state="insufficient",
        key_level_state="unknown",
        evidence="CCXT K-line fetch failed",
        failure=FailureState(
            category="ccxt_api",
            retryable=True,
            attempts=attempts,
            source=source,
            safe_message=_safe_message(message),
        ),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _safe_message(message: str) -> str:
    lowered = message.lower()
    if "key" in lowered or "secret" in lowered or "password" in lowered or "token" in lowered:
        return "CCXT request failed"
    return message[:160]
