from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

from trading_system.indicators import atr, directional_indicators, ema, macd, ohlcv_frame, rsi
from trading_system.llm_regime import LLMRegimeReviewer, RegimeReviewInput
from trading_system.models import PositionSide, Regime


TIMEFRAME_MS = {
    "5m": 5 * 60 * 1000,
}


@dataclass(slots=True)
class BacktestConfig:
    symbols: list[str]
    days: int = 30
    warmup_days: int = 10
    initial_equity: float = 10_000.0
    risk_percent: float = 0.01
    fee_rate: float = 0.0002
    slippage_rate: float = 0.0001
    output_dir: Path = Path("reports")
    cache_dir: Path = Path(".cache/okx_ohlcv")
    shock_stop_atr: float = 1.0
    shock_take_profit_atr: float = 1.8
    shock_long_zone_min: float = 0.20
    shock_long_zone_max: float = 0.45
    shock_short_zone_min: float = 0.55
    shock_short_zone_max: float = 0.80
    shock_long_rsi_5m_max: float = 48.0
    shock_short_rsi_5m_min: float = 52.0
    classifier_mode: str = "dev"
    min_take_profit_pct: float = 0.004
    trailing_gap_pct: float = 0.0025
    max_stop_loss_pct: float = 0.012
    shock_reward_risk: float = 1.15
    trend_reward_risk: float = 1.8
    min_trailing_activate_r: float = 1.0
    enable_trend_short: bool = True
    trend_short_risk_multiplier: float = 0.5
    defensive_risk_multiplier: float = 0.7
    shock_trend_risk_multiplier: float = 0.5
    shock_trend_down_risk_multiplier: float = 0.35
    llm_regime_review_enabled: bool = False
    llm_regime_provider: str = "openai"
    llm_regime_model: str = "gpt-4.1-mini"
    llm_regime_base_url: str = ""
    llm_regime_api_key_env: str = ""
    llm_max_calls: int = 50


@dataclass(slots=True)
class SimTrade:
    symbol: str
    side: PositionSide
    regime: Regime
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct_equity: float
    reason: str


@dataclass(slots=True)
class RegimeHit:
    symbol: str
    timestamp: pd.Timestamp
    regime: Regime
    correct: bool


@dataclass(slots=True)
class BacktestResult:
    started_at: datetime
    ended_at: datetime
    trades: list[SimTrade] = field(default_factory=list)
    regime_hits: list[RegimeHit] = field(default_factory=list)
    candles_by_symbol: dict[str, int] = field(default_factory=dict)
    regime_counts: dict[str, int] = field(default_factory=dict)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return sum(trade.pnl for trade in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.pnl > 0) / len(self.trades)

    @property
    def regime_accuracy(self) -> float:
        if not self.regime_hits:
            return 0.0
        return sum(1 for hit in self.regime_hits if hit.correct) / len(self.regime_hits)

    @property
    def avg_win(self) -> float:
        wins = [trade.pnl for trade in self.trades if trade.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [trade.pnl for trade in self.trades if trade.pnl <= 0]
        return sum(losses) / len(losses) if losses else 0.0


class OKXBacktester:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.exchange = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        self.llm_reviewer = LLMRegimeReviewer(
            model=config.llm_regime_model,
            provider=config.llm_regime_provider,
            base_url=config.llm_regime_base_url or None,
            api_key_env=config.llm_regime_api_key_env or None,
            enabled=config.llm_regime_review_enabled,
        )
        self.llm_review_count = 0

    def run(self) -> tuple[BacktestResult, Path]:
        end_ms = self.exchange.milliseconds()
        start_ms = end_ms - self.config.days * 24 * 60 * 60 * 1000
        fetch_start_ms = start_ms - self.config.warmup_days * 24 * 60 * 60 * 1000
        result = BacktestResult(
            started_at=datetime.fromtimestamp(start_ms / 1000, timezone.utc),
            ended_at=datetime.fromtimestamp(end_ms / 1000, timezone.utc),
        )

        for symbol in self.config.symbols:
            candles_5m = self.fetch_ohlcv(symbol, fetch_start_ms, end_ms)
            result.candles_by_symbol[symbol] = len(candles_5m)
            if len(candles_5m) < 1000:
                continue
            trades, hits, curve, counts = self.backtest_symbol(symbol, candles_5m, start_ms)
            result.trades.extend(trades)
            result.regime_hits.extend(hits)
            result.equity_curve.extend(curve)
            for regime, count in counts.items():
                result.regime_counts[regime] = result.regime_counts.get(regime, 0) + count

        report_path = self.write_report(result)
        return result, report_path

    def fetch_ohlcv(self, symbol: str, since_ms: int, end_ms: int) -> list[list[float]]:
        cache_path = self.cache_path(symbol, since_ms, end_ms)
        if cache_path.exists():
            frame = pd.read_csv(cache_path)
            return frame[["ts", "open", "high", "low", "close", "volume"]].values.tolist()
        cached = self.covering_cache_path(symbol, since_ms, end_ms)
        if cached:
            frame = pd.read_csv(cached)
            frame = frame[(frame.ts >= since_ms) & (frame.ts <= end_ms)]
            return frame[["ts", "open", "high", "low", "close", "volume"]].values.tolist()

        rows: list[list[float]] = []
        partial_path = self.partial_cache_path(symbol, since_ms, end_ms)
        if partial_path.exists():
            frame = pd.read_csv(partial_path)
            rows = frame[["ts", "open", "high", "low", "close", "volume"]].values.tolist()
        cursor = since_ms
        if rows:
            cursor = int(rows[-1][0]) + TIMEFRAME_MS["5m"]
        while cursor < end_ms:
            batch = self.fetch_ohlcv_with_retries(symbol, cursor)
            if not batch:
                break
            rows.extend(batch)
            rows = [row for row in rows if since_ms <= int(row[0]) <= end_ms]
            unique_partial = {int(row[0]): row for row in rows}
            rows = [unique_partial[key] for key in sorted(unique_partial)]
            self.cache_dir().mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).to_csv(partial_path, index=False)
            next_cursor = int(batch[-1][0]) + TIMEFRAME_MS["5m"]
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 2:
                break
        unique = {int(row[0]): row for row in rows if since_ms <= int(row[0]) <= end_ms}
        result = [unique[key] for key in sorted(unique)]
        self.cache_dir().mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result, columns=["ts", "open", "high", "low", "close", "volume"]).to_csv(cache_path, index=False)
        if partial_path.exists():
            partial_path.unlink()
        return result

    def fetch_ohlcv_with_retries(self, symbol: str, cursor: int) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                return self.exchange.fetch_ohlcv(symbol, "5m", since=cursor, limit=300)
            except Exception as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 20))
        if last_error:
            raise last_error
        return []

    def cache_dir(self) -> Path:
        return self.config.cache_dir

    def cache_path(self, symbol: str, since_ms: int, end_ms: int) -> Path:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return self.cache_dir() / f"{safe_symbol}_5m_{since_ms}_{end_ms}.csv"

    def partial_cache_path(self, symbol: str, since_ms: int, end_ms: int) -> Path:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return self.cache_dir() / f"{safe_symbol}_5m_{since_ms}_{end_ms}.partial.csv"

    def covering_cache_path(self, symbol: str, since_ms: int, end_ms: int) -> Path | None:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        for path in sorted(self.cache_dir().glob(f"{safe_symbol}_5m_*.csv"), reverse=True):
            parts = path.stem.split("_")
            try:
                cached_since = int(parts[-2])
                cached_end = int(parts[-1])
            except ValueError:
                continue
            if cached_since <= since_ms and cached_end >= end_ms:
                return path
        return None

    def backtest_symbol(
        self,
        symbol: str,
        candles_5m: list[list[float]],
        start_ms: int,
    ) -> tuple[list[SimTrade], list[RegimeHit], list[tuple[pd.Timestamp, float]], dict[str, int]]:
        df5 = ohlcv_frame(candles_5m)
        df5["dt"] = pd.to_datetime(df5.ts, unit="ms", utc=True)
        df5 = df5.set_index("dt")
        df1h = self.resample(df5, "1h")
        df4h = self.resample(df5, "4h")

        equity = self.config.initial_equity
        trades: list[SimTrade] = []
        hits: list[RegimeHit] = []
        curve: list[tuple[pd.Timestamp, float]] = []
        regime_counts: dict[str, int] = {}
        open_position: dict[str, Any] | None = None
        last_regime_check: pd.Timestamp | None = None

        for idx in range(240, len(df5)):
            now = df5.index[idx]
            row = df5.iloc[idx]
            if int(row.ts) < start_ms:
                continue

            history_5m = df5.iloc[: idx + 1]
            history_1h = df1h[df1h.index <= now].iloc[:-1]
            history_4h = df4h[df4h.index <= now].iloc[:-1]
            if len(history_1h) < 80 or len(history_4h) < 50:
                continue

            if self.config.classifier_mode == "user_4h":
                regime, features = classify_user_4h_rules(history_1h, history_4h)
            else:
                regime, features = classify_kline_only(history_1h, history_4h)
            if last_regime_check is None or now - last_regime_check >= pd.Timedelta(hours=1):
                regime_counts[regime.value] = regime_counts.get(regime.value, 0) + 1
                hit = evaluate_regime_hit(symbol, now, regime, features, df5, idx)
                if hit:
                    hits.append(hit)
                last_regime_check = now

            if open_position:
                exit_price, reason = maybe_exit(open_position, row)
                if exit_price is not None:
                    trade = close_position(symbol, open_position, now, exit_price, reason, equity, self.config)
                    equity += trade.pnl
                    trades.append(trade)
                    curve.append((now, equity))
                    open_position = None
                continue

            if self.config.classifier_mode == "user_4h":
                signal = build_user_rule_signal(symbol, regime, features, history_5m, self.config)
            else:
                signal = build_signal(symbol, regime, features, history_5m, self.config)
            if signal is None:
                continue
            review = self.review_regime_sync(symbol, regime, features)
            if not review.allow_trade or review.proposed_regime != regime:
                continue

            stop_distance = abs(signal["entry"] - signal["stop"])
            if stop_distance <= 0:
                continue
            risk_percent = self.config.risk_percent * float(signal.get("risk_multiplier", 1.0))
            qty = (equity * risk_percent) / stop_distance
            open_position = {
                **signal,
                "qty": qty,
                "entry_time": now,
                "highest": signal["entry"],
                "lowest": signal["entry"],
                "atr": features["atr_1h"],
                "min_trailing_activate_r": self.config.min_trailing_activate_r,
            }

        if open_position:
            now = df5.index[-1]
            exit_price = float(df5.close.iloc[-1])
            trade = close_position(symbol, open_position, now, exit_price, "end_of_backtest", equity, self.config)
            trades.append(trade)

        return trades, hits, curve, regime_counts

    def resample(self, df: pd.DataFrame, rule: str) -> pd.DataFrame:
        frame = df.resample(rule).agg(
            {
                "ts": "last",
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        return frame.dropna()

    def write_report(self, result: BacktestResult) -> Path:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.config.output_dir / f"okx_backtest_{result.ended_at:%Y%m%d_%H%M%S}.md"
        by_symbol: dict[str, list[SimTrade]] = {}
        for trade in result.trades:
            by_symbol.setdefault(trade.symbol, []).append(trade)

        lines = [
            "# OKX 1 个月 K 线回测报告",
            "",
            f"- 回测区间: `{result.started_at.isoformat()}` 至 `{result.ended_at.isoformat()}`",
            f"- 初始权益: `{self.config.initial_equity:.2f} USDT`",
            f"- 品种: `{', '.join(self.config.symbols)}`",
            f"- 手续费假设: maker `{self.config.fee_rate:.4%}` 每边，滑点 `{self.config.slippage_rate:.4%}` 每边",
            f"- SHOCK 参数: stop `{self.config.shock_stop_atr} ATR`, take_profit `{self.config.shock_take_profit_atr} ATR`, "
            f"long zone `{self.config.shock_long_zone_min:.0%}-{self.config.shock_long_zone_max:.0%}`, "
            f"short zone `{self.config.shock_short_zone_min:.0%}-{self.config.shock_short_zone_max:.0%}`",
            f"- 行情判断模式: `{self.config.classifier_mode}`",
            "- 数据说明: 本次按用户要求只使用 K 线，爆仓/OI 前瞻因子未纳入历史验证；趋势分类采用 K线-only 回测模式。",
            f"- LLM 复核: `{self.config.llm_regime_review_enabled}`, provider `{self.config.llm_regime_provider}`, model `{self.config.llm_regime_model}`",
            f"- LLM 复核调用次数: `{self.llm_review_count}`",
            "",
            "## 汇总",
            "",
            f"- K线数量: `{result.candles_by_symbol}`",
            f"- 下单/交易次数: `{len(result.trades)}`",
            f"- 盈利交易数: `{sum(1 for trade in result.trades if trade.pnl > 0)}`",
            f"- 亏损交易数: `{sum(1 for trade in result.trades if trade.pnl <= 0)}`",
            f"- 盈利比例/胜率: `{result.win_rate:.2%}`",
            f"- 总盈利: `{result.total_pnl:.2f} USDT`",
            f"- 收益率: `{result.total_pnl / self.config.initial_equity:.2%}`",
            f"- 平均盈利: `{result.avg_win:.2f} USDT`",
            f"- 平均亏损: `{result.avg_loss:.2f} USDT`",
            f"- 行情趋势准确度: `{result.regime_accuracy:.2%}`，样本 `{len(result.regime_hits)}`",
            f"- 行情状态计数: `{result.regime_counts}`",
            "",
            "## 行情状态次数",
            "",
            "| regime | 次数 | 占比 |",
            "|---|---:|---:|",
        ]
        total_regimes = sum(result.regime_counts.values()) or 1
        for regime, count in sorted(result.regime_counts.items(), key=lambda item: item[0]):
            lines.append(f"| {regime} | {count} | {count / total_regimes:.2%} |")
        lines.extend(
            [
                "",
            "## 行情分类准确度",
            "",
            "| regime | 样本数 | 准确率 |",
            "|---|---:|---:|",
            ]
        )
        for regime in [Regime.TREND_LONG, Regime.TREND_SHORT, Regime.SHOCK, Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN, Regime.UNKNOWN]:
            hits = [hit for hit in result.regime_hits if hit.regime == regime]
            if not hits:
                continue
            accuracy = sum(1 for hit in hits if hit.correct) / len(hits)
            lines.append(f"| {regime.value} | {len(hits)} | {accuracy:.2%} |")
        lines.extend(
            [
                "",
                "## 交易表现按行情",
                "",
                "| regime | 交易次数 | 胜率 | 净利润 |",
                "|---|---:|---:|---:|",
            ]
        )
        for regime in [Regime.TREND_LONG, Regime.TREND_SHORT, Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN, Regime.SHOCK]:
            trades = [trade for trade in result.trades if trade.regime == regime]
            if not trades:
                continue
            wins = sum(1 for trade in trades if trade.pnl > 0)
            pnl = sum(trade.pnl for trade in trades)
            lines.append(f"| {regime.value} | {len(trades)} | {wins / len(trades):.2%} | {pnl:.2f} |")
        lines.extend(
            [
                "",
            "## 分品种",
            "",
            ]
        )
        for symbol in self.config.symbols:
            trades = by_symbol.get(symbol, [])
            pnl = sum(trade.pnl for trade in trades)
            wins = sum(1 for trade in trades if trade.pnl > 0)
            lines.extend(
                [
                    f"### {symbol}",
                    "",
                    f"- 交易次数: `{len(trades)}`",
                    f"- 胜率: `{wins / len(trades):.2%}`" if trades else "- 胜率: `0.00%`",
                    f"- 净利润: `{pnl:.2f} USDT`",
                    "",
                ]
            )
        lines.extend(["## 交易明细", ""])
        if not result.trades:
            lines.append("本次回测未触发交易。")
        else:
            lines.append("| symbol | side | regime | entry | exit | pnl | reason |")
            lines.append("|---|---:|---|---:|---:|---:|---|")
            for trade in result.trades:
                lines.append(
                    f"| {trade.symbol} | {trade.side.value} | {trade.regime.value} | "
                    f"{trade.entry_price:.4f} | {trade.exit_price:.4f} | {trade.pnl:.2f} | {trade.reason} |"
                )
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    def review_regime_sync(self, symbol: str, regime: Regime, features: dict[str, Any]):
        if not self.config.llm_regime_review_enabled:
            return self.llm_reviewer.default_review(regime)
        if self.llm_review_count >= self.config.llm_max_calls:
            return self.llm_reviewer.default_review(regime)
        self.llm_review_count += 1
        return asyncio_run_review(
            self.llm_reviewer,
            RegimeReviewInput(symbol=symbol, rule_regime=regime, features=features),
        )


def classify_kline_only(history_1h: pd.DataFrame, history_4h: pd.DataFrame) -> tuple[Regime, dict[str, float]]:
    adx_series, plus_di, minus_di = directional_indicators(history_1h.high, history_1h.low, history_1h.close)
    atr_1h = atr(history_1h.high, history_1h.low, history_1h.close)
    atr_4h = atr(history_4h.high, history_4h.low, history_4h.close)
    ema20 = ema(history_1h.close, 20)
    ema60 = ema(history_1h.close, 60)
    rsi_1h = rsi(history_1h.close, 14)
    range_4h = history_4h.iloc[-42:]
    high_42 = float(range_4h.high.max())
    low_42 = float(range_4h.low.min())
    close = float(history_1h.close.iloc[-1])
    amplitude = (high_42 - low_42) / low_42 if low_42 else 0.0
    recent_3 = history_1h.iloc[-3:]
    features = {
        "adx": float(adx_series.iloc[-1]),
        "plus_di": float(plus_di.iloc[-1]),
        "minus_di": float(minus_di.iloc[-1]),
        "atr_1h": float(atr_1h.iloc[-1]),
        "atr_4h": float(atr_4h.iloc[-1]),
        "ema20": float(ema20.iloc[-1]),
        "ema60": float(ema60.iloc[-1]),
        "rsi_1h": float(rsi_1h.iloc[-1]),
        "high_42": high_42,
        "low_42": low_42,
        "close": close,
        "amplitude": amplitude,
    }
    if close > high_42 and features["adx"] > 25 and features["plus_di"] > features["minus_di"]:
        return Regime.TREND_LONG, features
    if close < low_42 and features["adx"] > 25 and features["minus_di"] > features["plus_di"]:
        return Regime.TREND_SHORT, features
    if (
        amplitude <= 0.08
        and float(recent_3.high.max()) <= high_42
        and float(recent_3.low.min()) >= low_42
        and features["adx"] < 18
    ):
        return Regime.SHOCK, features
    if amplitude <= 0.12 and features["adx"] < 25 and features["ema20"] > features["ema60"]:
        return Regime.SHOCK_TREND_UP, features
    if amplitude <= 0.12 and features["adx"] < 25 and features["ema20"] < features["ema60"]:
        return Regime.SHOCK_TREND_DOWN, features
    return Regime.UNKNOWN, features


def classify_user_4h_rules(history_1h: pd.DataFrame, history_4h: pd.DataFrame) -> tuple[Regime, dict[str, float]]:
    window = history_4h.iloc[-42:]
    recent_3 = history_4h.iloc[-3:]
    recent_6 = history_4h.iloc[-6:]
    high_42 = float(window.high.max())
    low_42 = float(window.low.min())
    high_pos = len(window) - 1 - int(window.high.iloc[::-1].reset_index(drop=True).idxmax())
    low_pos = len(window) - 1 - int(window.low.iloc[::-1].reset_index(drop=True).idxmin())
    high_in_last_2 = high_pos >= len(window) - 2
    low_in_last_2 = low_pos >= len(window) - 2
    last_close = float(history_1h.close.iloc[-1])
    atr_1h_value = float(atr(history_1h.high, history_1h.low, history_1h.close).iloc[-1])
    features = {
        "high_42": high_42,
        "low_42": low_42,
        "midpoint": (high_42 + low_42) / 2,
        "atr_1h": atr_1h_value,
        "current_4h_low": float(history_4h.low.iloc[-1]),
        "current_4h_high": float(history_4h.high.iloc[-1]),
        "previous_1h_low": float(history_1h.low.iloc[-1]),
        "previous_1h_high": float(history_1h.high.iloc[-1]),
        "ema20_1h": float(ema(history_1h.close, 20).iloc[-1]),
        "ema60_1h": float(ema(history_1h.close, 60).iloc[-1]),
        "last_4h_close": float(history_4h.close.iloc[-1]),
        "prev_4h_close": float(history_4h.close.iloc[-2]),
        "close": last_close,
    }

    up_3 = bool(
        recent_3.low.iloc[1] > recent_3.low.iloc[0]
        and recent_3.high.iloc[1] > recent_3.high.iloc[0]
        and recent_3.low.iloc[2] > recent_3.low.iloc[1]
        and recent_3.high.iloc[2] > recent_3.high.iloc[1]
        and (float(recent_3.high.max()) >= high_42 or high_in_last_2)
    )
    down_3 = bool(
        recent_3.low.iloc[1] < recent_3.low.iloc[0]
        and recent_3.high.iloc[1] < recent_3.high.iloc[0]
        and recent_3.low.iloc[2] < recent_3.low.iloc[1]
        and recent_3.high.iloc[2] < recent_3.high.iloc[1]
        and (float(recent_3.low.min()) <= low_42 or low_in_last_2)
    )
    if up_3:
        return Regime.TREND_LONG, features
    if down_3:
        return Regime.TREND_SHORT, features

    alternating = count_4h_direction_changes(recent_6) >= 3
    rising_high_steps = sum(
        1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) > float(left)
    )
    rising_low_steps = sum(
        1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) > float(left)
    )
    falling_high_steps = sum(
        1 for left, right in zip(recent_6.high.iloc[:-1], recent_6.high.iloc[1:]) if float(right) < float(left)
    )
    falling_low_steps = sum(
        1 for left, right in zip(recent_6.low.iloc[:-1], recent_6.low.iloc[1:]) if float(right) < float(left)
    )
    ema20_1h = features["ema20_1h"]
    ema60_1h = features["ema60_1h"]
    if alternating and rising_high_steps >= 3 and rising_low_steps >= 2 and ema20_1h > ema60_1h:
        return Regime.SHOCK_TREND_UP, features
    if alternating and falling_low_steps >= 3 and falling_high_steps >= 2 and ema20_1h < ema60_1h:
        return Regime.SHOCK_TREND_DOWN, features

    no_breakout = not high_in_last_2 and not low_in_last_2
    if no_breakout:
        return Regime.SHOCK, features
    return Regime.UNKNOWN, features


def count_4h_direction_changes(frame: pd.DataFrame) -> int:
    directions = []
    for _, row in frame.iterrows():
        if float(row.close) > float(row.open):
            directions.append(1)
        elif float(row.close) < float(row.open):
            directions.append(-1)
        else:
            directions.append(0)
    compact = [item for item in directions if item]
    return sum(1 for left, right in zip(compact, compact[1:]) if left != right)


def build_signal(
    symbol: str,
    regime: Regime,
    features: dict[str, float],
    history_5m: pd.DataFrame,
    config: BacktestConfig,
) -> dict[str, Any] | None:
    if len(history_5m) < 80:
        return None
    price = float(history_5m.close.iloc[-1])
    macd_line, signal_line, _ = macd(history_5m.close)
    ema20_5m = ema(history_5m.close, 20)
    rsi_5m = rsi(history_5m.close, 14)
    crossed_up = bool(macd_line.iloc[-2] < signal_line.iloc[-2] and macd_line.iloc[-1] >= signal_line.iloc[-1])
    crossed_down = bool(macd_line.iloc[-2] > signal_line.iloc[-2] and macd_line.iloc[-1] <= signal_line.iloc[-1])

    if regime == Regime.TREND_LONG and price >= float(ema20_5m.iloc[-1]) and 45 <= float(rsi_5m.iloc[-1]) <= 55 and crossed_up:
        stop = max(float(history_5m.low.iloc[-12:].min()), price - 1.2 * features["atr_1h"])
        return {"side": PositionSide.LONG, "regime": regime, "entry": price, "stop": stop, "take_profit": price + 2.5 * (price - stop)}
    if regime == Regime.TREND_SHORT and price <= float(ema20_5m.iloc[-1]) and 45 <= float(rsi_5m.iloc[-1]) <= 55 and crossed_down:
        stop = min(float(history_5m.high.iloc[-12:].max()), price + 1.2 * features["atr_1h"])
        return {"side": PositionSide.SHORT, "regime": regime, "entry": price, "stop": stop, "take_profit": price - 2.5 * (stop - price)}

    atr_5m = float(atr(history_5m.high, history_5m.low, history_5m.close).iloc[-1]) or features["atr_1h"]
    rsi_5m_value = float(rsi_5m.iloc[-1])
    ema60_5m = ema(history_5m.close, 60)
    midpoint = (features["high_42"] + features["low_42"]) / 2
    range_width = features["high_42"] - features["low_42"]
    long_zone_low = features["low_42"] + config.shock_long_zone_min * range_width
    long_zone_high = features["low_42"] + config.shock_long_zone_max * range_width
    short_zone_low = features["low_42"] + config.shock_short_zone_min * range_width
    short_zone_high = features["low_42"] + config.shock_short_zone_max * range_width
    if (
        regime == Regime.SHOCK
        and long_zone_low <= price <= min(midpoint, long_zone_high)
        and features["rsi_1h"] < 35
        and rsi_5m_value < config.shock_long_rsi_5m_max
        and crossed_up
    ):
        return {
            "side": PositionSide.LONG,
            "regime": regime,
            "entry": price,
            "stop": price - config.shock_stop_atr * atr_5m,
            "take_profit": price + config.shock_take_profit_atr * atr_5m,
        }
    if (
        regime == Regime.SHOCK
        and max(midpoint, short_zone_low) <= price <= short_zone_high
        and features["rsi_1h"] > 65
        and rsi_5m_value > config.shock_short_rsi_5m_min
        and crossed_down
    ):
        return {
            "side": PositionSide.SHORT,
            "regime": regime,
            "entry": price,
            "stop": price + config.shock_stop_atr * atr_5m,
            "take_profit": price - config.shock_take_profit_atr * atr_5m,
        }
    return None


def build_user_rule_signal(
    symbol: str,
    regime: Regime,
    features: dict[str, float],
    history_5m: pd.DataFrame,
    config: BacktestConfig,
) -> dict[str, Any] | None:
    if len(history_5m) < 35:
        return None
    price = float(history_5m.close.iloc[-1])
    previous = history_5m.iloc[-2]
    current = history_5m.iloc[-1]
    macd_line, signal_line, _ = macd(history_5m.close)
    crossed_up = bool(macd_line.iloc[-2] < signal_line.iloc[-2] and macd_line.iloc[-1] >= signal_line.iloc[-1])
    crossed_down = bool(macd_line.iloc[-2] > signal_line.iloc[-2] and macd_line.iloc[-1] <= signal_line.iloc[-1])

    if regime == Regime.SHOCK:
        if price < features["midpoint"] and crossed_up:
            stop = cap_stop(price, float(features["low_42"]), PositionSide.LONG, config)
            take_profit = price + max(price * config.min_take_profit_pct, config.shock_reward_risk * abs(price - stop))
            return {
                "side": PositionSide.LONG,
                "regime": regime,
                "entry": price,
                "stop": stop,
                "take_profit": take_profit,
                "risk_multiplier": config.defensive_risk_multiplier,
            }
        if price > features["midpoint"] and crossed_down:
            stop = cap_stop(price, float(features["high_42"]), PositionSide.SHORT, config)
            take_profit = price - max(price * config.min_take_profit_pct, config.shock_reward_risk * abs(stop - price))
            return {
                "side": PositionSide.SHORT,
                "regime": regime,
                "entry": price,
                "stop": stop,
                "take_profit": take_profit,
                "risk_multiplier": config.defensive_risk_multiplier,
            }

    if regime == Regime.TREND_LONG and float(current.close) < float(current.open):
        stop = cap_stop(price, float(features["previous_1h_low"]), PositionSide.LONG, config)
        return {
            "side": PositionSide.LONG,
            "regime": regime,
            "entry": price,
            "stop": stop,
            "take_profit": price + config.trend_reward_risk * abs(price - stop),
            "trailing_gap_pct": config.trailing_gap_pct,
        }
    if (
        config.enable_trend_short
        and regime == Regime.TREND_SHORT
        and features["ema20_1h"] < features["ema60_1h"]
        and features["last_4h_close"] < features["prev_4h_close"]
        and features["last_4h_close"] <= features["low_42"] * 1.01
        and price < features["low_42"] * 1.003
        and float(current.close) > float(current.open)
        and crossed_down
    ):
        stop = cap_stop(price, float(features["previous_1h_high"]), PositionSide.SHORT, config)
        return {
            "side": PositionSide.SHORT,
            "regime": regime,
            "entry": price,
            "stop": stop,
            "take_profit": price - config.trend_reward_risk * abs(stop - price),
            "trailing_gap_pct": config.trailing_gap_pct,
            "risk_multiplier": config.trend_short_risk_multiplier,
        }

    if regime == Regime.SHOCK_TREND_UP and crossed_up:
        stop = cap_stop(price, float(features["current_4h_low"]), PositionSide.LONG, config)
        return {
            "side": PositionSide.LONG,
            "regime": regime,
            "entry": price,
            "stop": stop,
            "take_profit": price + config.trend_reward_risk * abs(price - stop),
            "trailing_gap_pct": config.trailing_gap_pct,
            "risk_multiplier": config.shock_trend_risk_multiplier,
        }
    if regime == Regime.SHOCK_TREND_DOWN and crossed_down:
        stop = cap_stop(price, float(features["current_4h_high"]), PositionSide.SHORT, config)
        return {
            "side": PositionSide.SHORT,
            "regime": regime,
            "entry": price,
            "stop": stop,
            "take_profit": price - config.trend_reward_risk * abs(stop - price),
            "trailing_gap_pct": config.trailing_gap_pct,
            "risk_multiplier": config.shock_trend_down_risk_multiplier,
        }
    return None


def cap_stop(price: float, raw_stop: float, side: PositionSide, config: BacktestConfig) -> float:
    max_distance = price * config.max_stop_loss_pct
    if side == PositionSide.LONG:
        return max(raw_stop, price - max_distance)
    return min(raw_stop, price + max_distance)


def maybe_exit(position: dict[str, Any], row: pd.Series) -> tuple[float | None, str]:
    high = float(row.high)
    low = float(row.low)
    close = float(row.close)
    if position["side"] == PositionSide.LONG:
        position["highest"] = max(float(position["highest"]), high)
        trailing = None
        risk = abs(float(position["entry"]) - float(position["stop"]))
        if position.get("trailing_gap_pct") and risk > 0:
            if position["highest"] - position["entry"] >= position.get("min_trailing_activate_r", 1.0) * risk:
                position["stop"] = max(position["stop"], position["highest"] * (1 - position["trailing_gap_pct"]))
                trailing = position["stop"]
        if position["highest"] - position["entry"] > 2.0 * position["atr"]:
            trailing = position["highest"] - 1.5 * position["atr"]
            position["stop"] = max(position["stop"], trailing)
        if low <= position["stop"]:
            return float(position["stop"]), "stop_loss" if trailing is None else "trailing_stop"
        if position.get("take_profit") is not None and high >= position["take_profit"]:
            return float(position["take_profit"]), "take_profit"
    else:
        position["lowest"] = min(float(position["lowest"]), low)
        trailing = None
        risk = abs(float(position["stop"]) - float(position["entry"]))
        if position.get("trailing_gap_pct") and risk > 0:
            if position["entry"] - position["lowest"] >= position.get("min_trailing_activate_r", 1.0) * risk:
                position["stop"] = min(position["stop"], position["lowest"] * (1 + position["trailing_gap_pct"]))
                trailing = position["stop"]
        if position["entry"] - position["lowest"] > 2.0 * position["atr"]:
            trailing = position["lowest"] + 1.5 * position["atr"]
            position["stop"] = min(position["stop"], trailing)
        if high >= position["stop"]:
            return float(position["stop"]), "stop_loss" if trailing is None else "trailing_stop"
        if position.get("take_profit") is not None and low <= position["take_profit"]:
            return float(position["take_profit"]), "take_profit"
    return None, ""


def close_position(
    symbol: str,
    position: dict[str, Any],
    exit_time: pd.Timestamp,
    raw_exit_price: float,
    reason: str,
    equity: float,
    config: BacktestConfig,
) -> SimTrade:
    if position["side"] == PositionSide.LONG:
        entry = position["entry"] * (1 + config.slippage_rate)
        exit_price = raw_exit_price * (1 - config.slippage_rate)
        gross = (exit_price - entry) * position["qty"]
    else:
        entry = position["entry"] * (1 - config.slippage_rate)
        exit_price = raw_exit_price * (1 + config.slippage_rate)
        gross = (entry - exit_price) * position["qty"]
    fees = (entry + exit_price) * position["qty"] * config.fee_rate
    pnl = gross - fees
    return SimTrade(
        symbol=symbol,
        side=position["side"],
        regime=position["regime"],
        entry_time=position["entry_time"],
        exit_time=exit_time,
        entry_price=entry,
        exit_price=exit_price,
        qty=float(position["qty"]),
        pnl=pnl,
        pnl_pct_equity=pnl / equity if equity else 0.0,
        reason=reason,
    )


def evaluate_regime_hit(
    symbol: str,
    now: pd.Timestamp,
    regime: Regime,
    features: dict[str, float],
    df5: pd.DataFrame,
    idx: int,
    horizon_bars: int = 12 * 12,
) -> RegimeHit | None:
    if regime not in {Regime.TREND_LONG, Regime.TREND_SHORT, Regime.SHOCK, Regime.SHOCK_TREND_UP, Regime.SHOCK_TREND_DOWN}:
        return None
    future = df5.iloc[idx + 1 : idx + 1 + horizon_bars]
    if future.empty:
        return None
    close = float(df5.close.iloc[idx])
    future_close = float(future.close.iloc[-1])
    ret = (future_close - close) / close if close else 0.0
    atr_pct = features["atr_1h"] / close if close else 0.0
    if regime in {Regime.TREND_LONG, Regime.SHOCK_TREND_UP}:
        correct = ret > max(0.003, 0.5 * atr_pct)
    elif regime in {Regime.TREND_SHORT, Regime.SHOCK_TREND_DOWN}:
        correct = ret < -max(0.003, 0.5 * atr_pct)
    else:
        correct = abs(ret) <= max(0.01, atr_pct)
    return RegimeHit(symbol=symbol, timestamp=now, regime=regime, correct=correct)


def asyncio_run_review(reviewer: LLMRegimeReviewer, item: RegimeReviewInput):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(reviewer.review(item))
    raise RuntimeError("backtest LLM review cannot run inside an active event loop")
