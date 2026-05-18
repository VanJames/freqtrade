from __future__ import annotations

import numpy as np
import pandas as pd


OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def ohlcv_frame(rows: list[list[float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    for column in OHLCV_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna().reset_index(drop=True)


def ema(values: pd.Series | np.ndarray, period: int) -> pd.Series:
    return pd.Series(values, dtype="float64").ewm(span=period, adjust=False).mean()


def rsi(values: pd.Series | np.ndarray, period: int = 14) -> pd.Series:
    series = pd.Series(values, dtype="float64")
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().fillna(0)


def directional_indicators(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0))
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0))

    tr = true_range(high, low, close)
    atr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def macd(values: pd.Series | np.ndarray) -> tuple[pd.Series, pd.Series, pd.Series]:
    series = pd.Series(values, dtype="float64")
    macd_line = ema(series, 12) - ema(series, 26)
    signal_line = ema(macd_line, 9)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def crossed_above(left: pd.Series, right: pd.Series) -> bool:
    return len(left) >= 2 and bool(left.iloc[-2] < right.iloc[-2] and left.iloc[-1] >= right.iloc[-1])


def crossed_below(left: pd.Series, right: pd.Series) -> bool:
    return len(left) >= 2 and bool(left.iloc[-2] > right.iloc[-2] and left.iloc[-1] <= right.iloc[-1])

