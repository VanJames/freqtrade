#!/usr/bin/env python3
"""
Build an advisory strategy/factor registry from rolling backtests.

This process is intentionally read-only for live trading. It does not generate
strategy code, modify config.json, or place orders. It produces a registry that
other advisory processes can consume when deciding which reviewed strategy,
side, and factor profile currently deserves more weight.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import auto_optimize, build_market_snapshot, direction_advisor, openai_market_advisor


USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_OUTPUT = AUTOOPT_DIR / "strategy_registry.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "strategy_researcher.jsonl"
DEFAULT_EXECUTION_LEDGER = USER_DATA / "execution_ledger.jsonl"
DEFAULT_TRADE_DB = USER_DATA / "tradesv3.sqlite"
DEFAULT_AI_CANDIDATES = AUTOOPT_DIR / "ai_strategy_candidates.json"
DEFAULT_KRONOS_FORECAST = AUTOOPT_DIR / "kronos_forecast.json"
DEFAULT_PAIR_SELECTION = AUTOOPT_DIR / "pair_selection.json"
DEFAULT_FORMAL_POOL = AUTOOPT_DIR / "formal_strategy_pool.json"
DEFAULT_WINDOWS = [1, 2, 7]
DEFAULT_BASE_STRATEGIES = [
    "AIGeneratedLongTrendContinuationRunner2xSelective",
    "AIGeneratedLongTrendContinuationRunner2x",
]
DEFAULT_FORMAL_POOL_PRIORITY_STREAK_THRESHOLD = 2
DEFAULT_FORMAL_POOL_PRIORITY_STEP = 0.05
DEFAULT_FORMAL_POOL_PRIORITY_MAX_BONUS = 0.2
DEFAULT_REAL_TRADE_PRIORITY_MIN_SAMPLES = 3
DEFAULT_REAL_TRADE_PRIORITY_STEP = 0.03
DEFAULT_REAL_TRADE_PRIORITY_MAX_BONUS = 0.12
VALID_RISK = {"reduce", "normal", "increase"}
VALID_ENTRY = {"stricter", "normal", "looser"}
VALID_EXIT = {"faster", "normal", "slower"}


@dataclass
class FactorCandidate:
    name: str
    description: str
    market_score: float
    side_bias: str
    parameter_bias: dict[str, str]


@dataclass
class WindowEvaluation:
    strategy: str
    window_days: int
    timerange: str
    side: str
    trades: int
    profit_total_pct: float
    profit_total_abs: float
    winrate: float
    score: float
    passed: bool
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build strategy/factor candidate registry.")
    parser.add_argument("--backend", choices=["compose", "local"], default="compose")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--strategy-path", default=str(STRATEGY_DIR))
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=DEFAULT_BASE_STRATEGIES,
    )
    parser.add_argument("--windows", nargs="+", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--min-trades", type=int, default=1)
    parser.add_argument("--min-profit-pct", type=float, default=0.0)
    parser.add_argument("--slippage-buffer-pct-per-trade", type=float, default=0.04)
    parser.add_argument("--min-net-profit-pct", type=float, default=0.0)
    parser.add_argument("--min-pass-rate", type=float, default=0.5)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--snapshot-limit", type=int, default=220)
    parser.add_argument("--use-llm-advisor", action="store_true")
    parser.add_argument("--advisor-model", default="deepseek-chat")
    parser.add_argument("--advisor-base-url", default="")
    parser.add_argument("--advisor-timeout", type=int, default=60)
    parser.add_argument("--include-ai-generated", action="store_true")
    parser.add_argument("--ai-candidates", default=str(DEFAULT_AI_CANDIDATES))
    parser.add_argument("--kronos-forecast", default=str(DEFAULT_KRONOS_FORECAST))
    parser.add_argument("--kronos-return-threshold-pct", type=float, default=0.12)
    parser.add_argument("--disable-kronos-scoring", action="store_true")
    parser.add_argument("--pair-selection-output", default=str(DEFAULT_PAIR_SELECTION))
    parser.add_argument("--formal-pool-output", default=str(DEFAULT_FORMAL_POOL))
    parser.add_argument("--formal-pool-required-window-days", nargs="+", type=int, default=[30, 60])
    parser.add_argument("--formal-pool-min-profit-pct", type=float, default=0.0)
    parser.add_argument("--formal-pool-min-trades", type=int, default=5)
    parser.add_argument("--formal-pool-max-strategies", type=int, default=6)
    parser.add_argument(
        "--formal-pool-priority-streak-threshold",
        type=int,
        default=DEFAULT_FORMAL_POOL_PRIORITY_STREAK_THRESHOLD,
    )
    parser.add_argument(
        "--formal-pool-priority-step",
        type=float,
        default=DEFAULT_FORMAL_POOL_PRIORITY_STEP,
    )
    parser.add_argument(
        "--formal-pool-priority-max-bonus",
        type=float,
        default=DEFAULT_FORMAL_POOL_PRIORITY_MAX_BONUS,
    )
    parser.add_argument("--pair-selection-window-days", type=int, default=30)
    parser.add_argument("--pair-selection-min-trades", type=int, default=1)
    parser.add_argument("--pair-selection-min-profit-pct", type=float, default=0.0)
    parser.add_argument("--pair-selection-max-pairs", type=int, default=6)
    parser.add_argument("--pair-selection-min-hold-runs", type=int, default=2)
    parser.add_argument("--pair-selection-remove-below-profit-pct", type=float, default=-0.15)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--execution-ledger", default=str(DEFAULT_EXECUTION_LEDGER))
    parser.add_argument("--trade-db", default=str(DEFAULT_TRADE_DB))
    parser.add_argument("--real-trade-lookback", type=int, default=300)
    parser.add_argument(
        "--real-trade-priority-min-samples",
        type=int,
        default=DEFAULT_REAL_TRADE_PRIORITY_MIN_SAMPLES,
    )
    parser.add_argument(
        "--real-trade-priority-step",
        type=float,
        default=DEFAULT_REAL_TRADE_PRIORITY_STEP,
    )
    parser.add_argument(
        "--real-trade-priority-max-bonus",
        type=float,
        default=DEFAULT_REAL_TRADE_PRIORITY_MAX_BONUS,
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=360.0)
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def read_jsonl_tail(path: Path, limit: int = 500) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except (FileNotFoundError, OSError):
        return []
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_ai_strategy_names(path: Path) -> list[str]:
    payload = read_json(path, {})
    names = payload.get("strategy_names", []) if isinstance(payload, dict) else []
    cleaned = []
    for name in names:
        text = str(name or "").strip()
        if text and re.match(r"^AIGenerated[A-Za-z0-9_]+$", text):
            cleaned.append(text)
    return sorted(set(cleaned))


def latest_market_snapshot(config_path: Path, limit: int) -> dict[str, Any]:
    config = build_market_snapshot.load_config(str(config_path))
    exchange = config.get("exchange", {}).get("name", "")
    datadir = build_market_snapshot.default_datadir(str(config_path), exchange)
    return build_market_snapshot.build_snapshot(config, limit=limit, datadir=datadir)


def infer_factor_candidates(snapshot: dict[str, Any]) -> list[FactorCandidate]:
    summary = snapshot.get("summary", {}) if isinstance(snapshot, dict) else {}
    pair_count = max(int(summary.get("pair_count") or 0), 1)
    trend_up = float(summary.get("trend_up_count") or 0) / pair_count
    trend_down = float(summary.get("trend_down_count") or 0) / pair_count
    median_atr = float(summary.get("median_atr_pct") or 0.0)
    median_bb = float(summary.get("median_bb_width") or 0.0)

    trend_strength = max(trend_up, trend_down)
    trend_side = "long" if trend_up > trend_down else "short" if trend_down > trend_up else "hold"
    volatility_score = min((median_atr * 100.0) + (median_bb * 20.0), 2.0)
    chop_score = max(0.0, 1.0 - trend_strength) + max(0.0, 0.015 - median_bb) * 20.0

    return [
        FactorCandidate(
            name="trend_momentum",
            description="EMA alignment plus recent return continuation.",
            market_score=round(trend_strength + volatility_score * 0.2, 4),
            side_bias=trend_side,
            parameter_bias={"risk": "normal", "entry": "normal", "exit": "normal"},
        ),
        FactorCandidate(
            name="volatility_breakout",
            description="ATR/Bollinger expansion with volume confirmation.",
            market_score=round(volatility_score, 4),
            side_bias=trend_side,
            parameter_bias={"risk": "reduce", "entry": "stricter", "exit": "faster"},
        ),
        FactorCandidate(
            name="mean_reversion",
            description="Low-trend consolidation profile for fading stretched moves.",
            market_score=round(chop_score, 4),
            side_bias="hold",
            parameter_bias={"risk": "reduce", "entry": "stricter", "exit": "faster"},
        ),
    ]


def best_direction_for_window(
    strategy: str,
    stats: dict,
    *,
    min_trades: int,
    min_profit_pct: float,
) -> tuple[direction_advisor.DirectionMetrics | None, str]:
    metrics = direction_advisor.extract_direction_metrics(strategy, stats)
    viable = [
        item
        for item in metrics
        if item.trades >= min_trades and item.profit_total_pct > min_profit_pct
    ]
    if not viable:
        return None, "no side passed gates"
    return max(viable, key=lambda item: (item.score, item.profit_total_pct, item.trades)), "passed"


def evaluate_strategy_windows(
    *,
    strategy: str,
    windows: list[int],
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
    min_trades: int,
    min_profit_pct: float,
) -> tuple[list[WindowEvaluation], dict[int, list[dict[str, Any]]]]:
    evaluations: list[WindowEvaluation] = []
    pair_attributions: dict[int, list[dict[str, Any]]] = {}
    for days in windows:
        timerange, _ = auto_optimize.build_walk_forward_timeranges(days, 0)
        window_dir = run_dir / strategy / f"{days}d"
        try:
            stats = direction_advisor.run_strategy_backtest(
                strategy=strategy,
                strategy_path=strategy_path,
                config=config,
                timerange=timerange,
                run_dir=window_dir,
                freqaimodel=freqaimodel,
                backend=backend,
            )
            best, reason = best_direction_for_window(
                strategy,
                stats,
                min_trades=min_trades,
                min_profit_pct=min_profit_pct,
            )
            pair_attributions[days] = extract_pair_attribution(strategy, stats)
            if best is None:
                evaluations.append(
                    WindowEvaluation(
                        strategy=strategy,
                        window_days=days,
                        timerange=timerange,
                        side="hold",
                        trades=0,
                        profit_total_pct=0.0,
                        profit_total_abs=0.0,
                        winrate=0.0,
                        score=-999999.0,
                        passed=False,
                        reason=reason,
                    )
                )
            else:
                evaluations.append(
                    WindowEvaluation(
                        strategy=strategy,
                        window_days=days,
                        timerange=timerange,
                        side=best.side,
                        trades=best.trades,
                        profit_total_pct=best.profit_total_pct,
                        profit_total_abs=best.profit_total_abs,
                        winrate=best.winrate,
                        score=best.score,
                        passed=True,
                        reason=reason,
                    )
                )
        except Exception as exc:
            evaluations.append(
                WindowEvaluation(
                    strategy=strategy,
                    window_days=days,
                    timerange=timerange,
                    side="hold",
                    trades=0,
                    profit_total_pct=0.0,
                    profit_total_abs=0.0,
                    winrate=0.0,
                    score=-999999.0,
                    passed=False,
                    reason=str(exc)[-500:],
                )
            )
    return evaluations, pair_attributions


def aggregate_strategy_score(
    evaluations: list[WindowEvaluation],
    *,
    slippage_buffer_pct_per_trade: float = 0.04,
    min_net_profit_pct: float = 0.0,
    min_pass_rate: float = 0.5,
) -> dict[str, Any]:
    if not evaluations:
        return {
            "strategy": "",
            "score": -999999.0,
            "passed_windows": 0,
            "side": "hold",
            "reason": "no windows",
        }
    weights = {1: 0.45, 2: 0.35, 7: 0.20}
    total_score = 0.0
    total_weight = 0.0
    passed = 0
    side_scores = {"long": 0.0, "short": 0.0}
    for item in evaluations:
        weight = weights.get(item.window_days, 1.0 / max(len(evaluations), 1))
        total_weight += weight
        if item.passed:
            passed += 1
            total_score += item.score * weight
            if item.side in side_scores:
                side_scores[item.side] += item.score * weight
        else:
            no_trade = item.trades == 0 and item.reason == "no side passed gates"
            total_score -= (0.25 if no_trade else 2.0) * weight
    side = max(side_scores, key=side_scores.get)
    if passed == 0 or side_scores[side] <= 0:
        side = "hold"
    trades = sum(item.trades for item in evaluations if item.passed)
    profit_total_pct = round(sum(item.profit_total_pct for item in evaluations if item.passed), 4)
    pass_rate = round(passed / max(len(evaluations), 1), 4)
    cost_buffer_pct = round(trades * max(slippage_buffer_pct_per_trade, 0.0), 4)
    net_profit_pct = round(profit_total_pct - cost_buffer_pct, 4)
    active_windows = [item for item in evaluations if item.passed and item.trades > 0]
    avg_profit_per_trade_pct = round(profit_total_pct / max(trades, 1), 4)
    min_window_profit_pct = round(
        min((item.profit_total_pct for item in active_windows), default=0.0),
        4,
    )
    quality_passed = passed > 0 and pass_rate >= min_pass_rate and net_profit_pct > min_net_profit_pct
    quality_penalty = 0.0
    if not quality_passed:
        quality_penalty += 1.0
    if avg_profit_per_trade_pct < slippage_buffer_pct_per_trade * 2.0 and trades > 0:
        quality_penalty += 0.5
    adjusted_score = round(total_score / max(total_weight, 1e-9) - quality_penalty, 4)
    return {
        "strategy": evaluations[0].strategy,
        "score": adjusted_score,
        "raw_score": round(total_score / max(total_weight, 1e-9), 4),
        "passed_windows": passed,
        "total_windows": len(evaluations),
        "side": side,
        "side_scores": {key: round(value, 4) for key, value in side_scores.items()},
        "profit_total_pct": profit_total_pct,
        "trades": trades,
        "quality_gate": {
            "passed": quality_passed,
            "pass_rate": pass_rate,
            "min_pass_rate": min_pass_rate,
            "cost_buffer_pct": cost_buffer_pct,
            "net_profit_pct": net_profit_pct,
            "min_net_profit_pct": min_net_profit_pct,
            "avg_profit_per_trade_pct": avg_profit_per_trade_pct,
            "min_window_profit_pct": min_window_profit_pct,
            "slippage_buffer_pct_per_trade": slippage_buffer_pct_per_trade,
            "penalty": round(quality_penalty, 4),
        },
    }


def extract_pair_attribution(strategy: str, stats: dict) -> list[dict[str, Any]]:
    strategy_stats = stats.get("strategy", {}).get(strategy, {})
    rows = []
    for row in strategy_stats.get("results_per_pair", []) or []:
        if not isinstance(row, dict) or row.get("key") == "TOTAL":
            continue
        rows.append(
            {
                "pair": str(row.get("key") or ""),
                "trades": int(row.get("trades") or 0),
                "profit_total_pct": round(float(row.get("profit_total", 0.0)) * 100.0, 4),
                "profit_total_abs": round(float(row.get("profit_total_abs", 0.0)), 8),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
            }
        )
    return sorted(rows, key=lambda item: (item["profit_total_pct"], item["trades"]), reverse=True)


def build_pair_selection_payload(
    pair_window_attributions: dict[str, dict[int, list[dict[str, Any]]]],
    *,
    source_window_days: int,
    min_trades: int,
    min_profit_pct: float,
    max_pairs: int,
    previous_payload: dict[str, Any] | None = None,
    min_hold_runs: int = 2,
    remove_below_profit_pct: float = -0.15,
    pair_execution_feedback: dict[str, dict[str, dict[str, Any]]] | None = None,
    pair_real_trade_min_samples: int = 2,
) -> dict[str, Any]:
    previous_strategies = (
        previous_payload.get("strategies", {}) if isinstance(previous_payload, dict) else {}
    )
    pair_execution_feedback = (
        pair_execution_feedback if isinstance(pair_execution_feedback, dict) else {}
    )
    strategies: dict[str, dict[str, Any]] = {}
    for strategy, windows in pair_window_attributions.items():
        rows = list(windows.get(source_window_days) or [])
        row_map = {str(row.get("pair") or ""): dict(row) for row in rows}
        strategy_pair_feedback = pair_execution_feedback.get(strategy, {})

        for pair, feedback in strategy_pair_feedback.items():
            if pair not in row_map:
                row_map[pair] = {
                    "pair": pair,
                    "trades": 0,
                    "profit_total_pct": 0.0,
                    "profit_total_abs": 0.0,
                    "wins": 0,
                    "losses": 0,
                }
            row_map[pair]["execution_feedback"] = feedback
            samples = int(feedback.get("samples", 0) or 0)
            avg_profit = float(feedback.get("avg_profit_pct", 0.0) or 0.0)
            positive_rate = float(feedback.get("positive_rate", 0.0) or 0.0)
            if samples >= max(int(pair_real_trade_min_samples), 1):
                if avg_profit > 0 and positive_rate >= 0.5:
                    row_map[pair]["profit_total_pct"] = round(
                        float(row_map[pair].get("profit_total_pct", 0.0)) + min(avg_profit, 0.3),
                        4,
                    )
                elif avg_profit < 0 or positive_rate < 0.5:
                    row_map[pair]["profit_total_pct"] = round(
                        float(row_map[pair].get("profit_total_pct", 0.0)) + max(avg_profit, -0.3),
                        4,
                    )
        rows = sorted(
            row_map.values(),
            key=lambda item: (
                float(item.get("profit_total_pct", 0.0)),
                int(item.get("trades", 0) or 0),
            ),
            reverse=True,
        )
        candidate_allowed = [
            row["pair"]
            for row in rows
            if int(row.get("trades", 0)) >= min_trades
            and float(row.get("profit_total_pct", 0.0)) > min_profit_pct
        ][: max(max_pairs, 1)]
        previous_strategy = previous_strategies.get(strategy, {}) if isinstance(previous_strategies, dict) else {}
        previous_state = previous_strategy.get("pair_state", {}) if isinstance(previous_strategy, dict) else {}
        retained_pairs: list[str] = []
        for pair, state in previous_state.items():
            if not isinstance(state, dict):
                continue
            age_runs = int(state.get("age_runs", 0) or 0)
            row = row_map.get(pair, {})
            current_profit = float(row.get("profit_total_pct", 0.0))
            current_trades = int(row.get("trades", 0) or 0)
            if pair in candidate_allowed:
                continue
            if age_runs < max(min_hold_runs, 0) and (
                current_trades == 0 or current_profit > remove_below_profit_pct
            ):
                retained_pairs.append(pair)

        allowed: list[str] = []
        for pair in [*candidate_allowed, *retained_pairs]:
            if pair not in allowed:
                allowed.append(pair)
            if len(allowed) >= max(max_pairs, 1):
                break

        pair_state: dict[str, dict[str, Any]] = {}
        previous_allowed = set(previous_strategy.get("allowed_pairs", [])) if isinstance(previous_strategy, dict) else set()
        for pair in allowed:
            prev = previous_state.get(pair, {}) if isinstance(previous_state, dict) else {}
            prev_age = int(prev.get("age_runs", 0) or 0)
            row = row_map.get(pair, {})
            pair_state[pair] = {
                "age_runs": prev_age + 1 if pair in previous_allowed else 1,
                "profit_total_pct": round(float(row.get("profit_total_pct", 0.0)), 4),
                "trades": int(row.get("trades", 0) or 0),
                "execution_feedback": row.get("execution_feedback", {}),
            }

        strategies[strategy] = {
            "source_window_days": source_window_days,
            "min_trades": min_trades,
            "min_profit_pct": min_profit_pct,
            "max_pairs": max_pairs,
            "min_hold_runs": min_hold_runs,
            "remove_below_profit_pct": remove_below_profit_pct,
            "pair_real_trade_min_samples": max(int(pair_real_trade_min_samples), 1),
            "allowed_pairs": allowed,
            "blocked_pairs": [row["pair"] for row in rows if row["pair"] not in set(allowed)],
            "pair_scores": rows,
            "pair_state": pair_state,
        }
    return {
        "record_type": "pair_selection",
        "created_at": datetime.now(UTC).isoformat(),
        "source_window_days": source_window_days,
        "strategies": strategies,
    }


def build_formal_strategy_pool(
    strategy_scores: list[dict[str, Any]],
    window_records: list[dict[str, Any]],
    *,
    required_window_days: list[int],
    min_profit_pct: float,
    min_trades: int,
    max_strategies: int,
    previous_payload: dict[str, Any] | None = None,
    priority_streak_threshold: int = DEFAULT_FORMAL_POOL_PRIORITY_STREAK_THRESHOLD,
    priority_step: float = DEFAULT_FORMAL_POOL_PRIORITY_STEP,
    priority_max_bonus: float = DEFAULT_FORMAL_POOL_PRIORITY_MAX_BONUS,
    execution_feedback: dict[str, dict[str, Any]] | None = None,
    real_trade_priority_min_samples: int = DEFAULT_REAL_TRADE_PRIORITY_MIN_SAMPLES,
    real_trade_priority_step: float = DEFAULT_REAL_TRADE_PRIORITY_STEP,
    real_trade_priority_max_bonus: float = DEFAULT_REAL_TRADE_PRIORITY_MAX_BONUS,
) -> dict[str, Any]:
    required_days = sorted({int(day) for day in required_window_days if int(day) > 0})
    window_map: dict[str, dict[int, dict[str, Any]]] = {}
    for row in window_records:
        if not isinstance(row, dict):
            continue
        strategy = str(row.get("strategy") or "")
        day = int(row.get("window_days", 0) or 0)
        if not strategy or day <= 0:
            continue
        window_map.setdefault(strategy, {})[day] = row

    previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
    execution_feedback = execution_feedback if isinstance(execution_feedback, dict) else {}
    previous_entries = previous_payload.get("strategies", [])
    previous_streaks: dict[str, int] = {}
    if isinstance(previous_entries, list):
        for item in previous_entries:
            if not isinstance(item, dict):
                continue
            strategy = str(item.get("strategy") or "")
            if strategy:
                previous_streaks[strategy] = int(item.get("streak_runs", 0) or 0)

    candidates: list[dict[str, Any]] = []
    for score in strategy_scores:
        if not isinstance(score, dict):
            continue
        strategy = str(score.get("strategy") or "")
        if not strategy:
            continue
        quality_gate = score.get("quality_gate", {})
        if isinstance(quality_gate, dict) and not bool(quality_gate.get("passed", True)):
            continue
        windows = window_map.get(strategy, {})
        required_checks = []
        passed = True
        for day in required_days:
            row = windows.get(day)
            if not isinstance(row, dict):
                passed = False
                required_checks.append({"window_days": day, "passed": False, "reason": "missing"})
                continue
            ok = (
                bool(row.get("passed"))
                and int(row.get("trades", 0) or 0) >= min_trades
                and float(row.get("profit_total_pct", 0.0) or 0.0) > min_profit_pct
            )
            if not ok:
                passed = False
            required_checks.append(
                {
                    "window_days": day,
                    "passed": ok,
                    "trades": int(row.get("trades", 0) or 0),
                    "profit_total_pct": round(float(row.get("profit_total_pct", 0.0) or 0.0), 4),
                    "reason": str(row.get("reason") or ""),
                }
            )
        if not passed:
            continue
        streak_runs = previous_streaks.get(strategy, 0) + 1
        priority_bonus = 0.0
        if strategy.startswith("AIGenerated") and streak_runs >= max(int(priority_streak_threshold), 1):
            bonus_steps = streak_runs - max(int(priority_streak_threshold), 1) + 1
            priority_bonus = min(max(float(priority_step), 0.0) * bonus_steps, max(float(priority_max_bonus), 0.0))
        feedback = execution_feedback.get(strategy, {})
        real_trade_bonus = 0.0
        samples = int(feedback.get("samples", 0) or 0)
        avg_profit = float(feedback.get("avg_profit_pct", 0.0) or 0.0)
        positive_rate = float(feedback.get("positive_rate", 0.0) or 0.0)
        min_live_samples = max(int(real_trade_priority_min_samples), 1)
        if samples >= min_live_samples:
            live_steps = min(samples - min_live_samples + 1, 3)
            live_delta = min(
                max(float(real_trade_priority_step), 0.0) * live_steps,
                max(float(real_trade_priority_max_bonus), 0.0),
            )
            if avg_profit > 0 and positive_rate >= 0.5:
                real_trade_bonus = live_delta
            elif avg_profit < 0 or positive_rate < 0.5:
                real_trade_bonus = -live_delta
        candidates.append(
            {
                "strategy": strategy,
                "score": round(float(score.get("score", -999999.0)), 4),
                "priority_bonus": round(priority_bonus, 4),
                "real_trade_bonus": round(real_trade_bonus, 4),
                "priority_score": round(float(score.get("score", -999999.0)) + priority_bonus + real_trade_bonus, 4),
                "side": str(score.get("side") or "hold"),
                "profit_total_pct": round(float(score.get("profit_total_pct", 0.0) or 0.0), 4),
                "trades": int(score.get("trades", 0) or 0),
                "passed_windows": int(score.get("passed_windows", 0) or 0),
                "streak_runs": streak_runs,
                "execution_feedback": feedback,
                "required_windows": required_checks,
            }
        )
    candidates.sort(
        key=lambda item: (
            float(item.get("priority_score", -999999.0)),
            int(item.get("passed_windows", 0)),
            float(item.get("profit_total_pct", 0.0)),
            int(item.get("trades", 0)),
        ),
        reverse=True,
    )
    selected = candidates[: max(int(max_strategies), 1)]
    return {
        "record_type": "formal_strategy_pool",
        "created_at": datetime.now(UTC).isoformat(),
        "required_window_days": required_days,
        "min_profit_pct": min_profit_pct,
        "min_trades": min_trades,
        "max_strategies": max_strategies,
        "allowed_strategies": [item["strategy"] for item in selected],
        "priority_scores": {item["strategy"]: item["priority_score"] for item in selected},
        "strategies": selected,
        "candidate_count": len(candidates),
        "priority_policy": {
            "ai_only": True,
            "streak_threshold": max(int(priority_streak_threshold), 1),
            "priority_step": round(max(float(priority_step), 0.0), 4),
            "priority_max_bonus": round(max(float(priority_max_bonus), 0.0), 4),
            "real_trade_priority_min_samples": max(int(real_trade_priority_min_samples), 1),
            "real_trade_priority_step": round(max(float(real_trade_priority_step), 0.0), 4),
            "real_trade_priority_max_bonus": round(max(float(real_trade_priority_max_bonus), 0.0), 4),
        },
    }


def _record_strategy(record: dict) -> str:
    return str(
        record.get("strategy")
        or record.get("recommended_strategy")
        or record.get("strategy_name")
        or ""
    )


def _record_profit_pct(record: dict) -> float | None:
    for key in ("profit_pct", "profit_total_pct", "realized_profit_pct", "profit_ratio"):
        if key not in record:
            continue
        try:
            value = float(record[key])
        except (TypeError, ValueError):
            continue
        return value * 100.0 if key == "profit_ratio" else value
    return None


def build_execution_feedback(records: list[dict]) -> dict[str, dict[str, Any]]:
    feedback: dict[str, dict[str, Any]] = {}
    for record in records:
        strategy = _record_strategy(record)
        if not strategy:
            continue
        profit_pct = _record_profit_pct(record)
        if profit_pct is None:
            continue
        bucket = feedback.setdefault(
            strategy,
            {
                "samples": 0,
                "positive": 0,
                "profit_total_pct": 0.0,
            },
        )
        bucket["samples"] += 1
        bucket["profit_total_pct"] += profit_pct
        if profit_pct > 0:
            bucket["positive"] += 1
    for bucket in feedback.values():
        samples = max(int(bucket["samples"]), 1)
        bucket["avg_profit_pct"] = round(float(bucket["profit_total_pct"]) / samples, 4)
        bucket["positive_rate"] = round(float(bucket["positive"]) / samples, 4)
        bucket["profit_total_pct"] = round(float(bucket["profit_total_pct"]), 4)
    return feedback


def trade_db_feedback_records(db_path: Path, limit: int = 300) -> list[dict]:
    if not db_path.exists() or limit <= 0:
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
            }
            if "trades" not in tables:
                return []
            rows = conn.execute(
                """
                select strategy, close_profit, close_profit_abs, close_date, pair
                from trades
                where is_open = 0
                  and strategy is not null
                  and close_date is not null
                order by close_date desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []

    records: list[dict] = []
    for strategy, close_profit, close_profit_abs, close_date, pair in rows:
        if not strategy:
            continue
        try:
            profit_pct = float(close_profit or 0.0) * 100.0
        except (TypeError, ValueError):
            profit_pct = 0.0
        try:
            profit_abs = float(close_profit_abs or 0.0)
        except (TypeError, ValueError):
            profit_abs = 0.0
        records.append(
            {
                "record_type": "trade_db_feedback",
                "strategy": str(strategy),
                "profit_total_pct": round(profit_pct, 4),
                "profit_abs": round(profit_abs, 8),
                "close_date": close_date,
                "pair": pair,
            }
        )
    return records


def build_pair_execution_feedback(records: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
    feedback: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        strategy = _record_strategy(record)
        pair = str(record.get("pair") or "")
        if not strategy or not pair:
            continue
        profit_pct = _record_profit_pct(record)
        if profit_pct is None:
            continue
        bucket = feedback.setdefault(strategy, {}).setdefault(
            pair,
            {
                "samples": 0,
                "positive": 0,
                "profit_total_pct": 0.0,
            },
        )
        bucket["samples"] += 1
        bucket["profit_total_pct"] += profit_pct
        if profit_pct > 0:
            bucket["positive"] += 1
    for pair_map in feedback.values():
        for bucket in pair_map.values():
            samples = max(int(bucket["samples"]), 1)
            bucket["avg_profit_pct"] = round(float(bucket["profit_total_pct"]) / samples, 4)
            bucket["positive_rate"] = round(float(bucket["positive"]) / samples, 4)
            bucket["profit_total_pct"] = round(float(bucket["profit_total_pct"]), 4)
    return feedback


def apply_execution_feedback(
    strategy_scores: list[dict[str, Any]], feedback: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    adjusted = []
    for item in strategy_scores:
        copied = dict(item)
        data = feedback.get(str(copied.get("strategy", "")), {})
        penalty = 0.0
        if data:
            avg_profit = float(data.get("avg_profit_pct", 0.0))
            positive_rate = float(data.get("positive_rate", 0.0))
            if avg_profit < 0:
                penalty += abs(avg_profit) * 2.0
            if positive_rate < 0.5:
                penalty += (0.5 - positive_rate) * 2.0
        copied["execution_feedback"] = data
        copied["feedback_penalty"] = round(penalty, 4)
        copied["score"] = round(float(copied.get("score", -999999.0)) - penalty, 4)
        adjusted.append(copied)
    return adjusted


def kronos_signal(forecast: dict[str, Any], return_threshold_pct: float) -> dict[str, Any]:
    summary = forecast.get("summary", {}) if isinstance(forecast, dict) else {}
    bias = str(summary.get("market_bias") or "hold").lower()
    try:
        avg_confidence = float(summary.get("avg_confidence") or 0.0)
    except (TypeError, ValueError):
        avg_confidence = 0.0
    try:
        avg_return = float(summary.get("avg_pred_return_pct") or 0.0)
    except (TypeError, ValueError):
        avg_return = 0.0
    if bias not in {"long", "short"}:
        if avg_return >= return_threshold_pct:
            bias = "long"
        elif avg_return <= -return_threshold_pct:
            bias = "short"
        else:
            bias = "hold"
    strength = min(max(avg_confidence, abs(avg_return) / 2.0), 1.0)
    return {
        "side": bias,
        "avg_confidence": round(avg_confidence, 4),
        "avg_pred_return_pct": round(avg_return, 4),
        "strength": round(strength, 4),
        "return_threshold_pct": return_threshold_pct,
        "source_created_at": forecast.get("created_at") if isinstance(forecast, dict) else None,
    }


def apply_kronos_scoring(strategy_scores: list[dict[str, Any]], signal: dict[str, Any]) -> list[dict[str, Any]]:
    side = str(signal.get("side") or "hold")
    if side not in {"long", "short"}:
        return strategy_scores
    strength = float(signal.get("strength") or 0.0)
    adjusted = []
    for item in strategy_scores:
        copied = dict(item)
        strategy_side = str(copied.get("side") or "hold")
        delta = 0.0
        effect = "none"
        if strategy_side == side:
            delta = 0.12 + strength * 0.30
            effect = "confirmed"
        elif strategy_side in {"long", "short"}:
            delta = -(0.20 + strength * 0.45)
            effect = "opposed"
        copied["kronos_scoring"] = {
            **signal,
            "effect": effect,
            "score_delta": round(delta, 4),
        }
        copied["score"] = round(float(copied.get("score", -999999.0)) + delta, 4)
        adjusted.append(copied)
    return adjusted


def normalize_llm_config(payload: dict) -> dict:
    def norm(value: Any, allowed: set[str], default: str) -> str:
        text = str(value or default).strip().lower()
        return text if text in allowed else default

    bias = payload.get("parameter_bias", {})
    if not isinstance(bias, dict):
        bias = {}
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "enabled": True,
        "strategy_prior": str(payload.get("strategy_prior") or "")[:120],
        "factor_prior": str(payload.get("factor_prior") or "")[:120],
        "side_prior": norm(payload.get("side_prior"), {"long", "short", "hold"}, "hold"),
        "confidence": max(0.0, min(confidence, 1.0)),
        "parameter_bias": {
            "risk": norm(bias.get("risk"), VALID_RISK, "normal"),
            "entry": norm(bias.get("entry"), VALID_ENTRY, "normal"),
            "exit": norm(bias.get("exit"), VALID_EXIT, "normal"),
        },
        "reason": str(payload.get("reason") or "")[:500],
    }


def llm_candidate_review(registry: dict, *, model: str, base_url: str, timeout: int) -> dict:
    api_key = openai_market_advisor.os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"enabled": False, "error": "OPENAI_API_KEY is not set."}
    prompt = json.dumps(
        {
            "task": "Review the strategy researcher registry and return strict JSON only.",
            "schema": {
                "strategy_prior": "one known strategy or empty",
                "factor_prior": "one known factor or empty",
                "side_prior": "long|short|hold",
                "confidence": "0..1",
                "parameter_bias": {
                    "risk": "reduce|normal|increase",
                    "entry": "stricter|normal|looser",
                    "exit": "faster|normal|slower",
                },
                "reason": "short reason",
            },
            "constraints": [
                "Do not override failed backtest windows.",
                "Prefer hold when evidence is weak.",
                "Use the provided candidate names only.",
            ],
            "registry": registry,
        },
        ensure_ascii=False,
    )
    response = openai_market_advisor.call_openai(
        base_url=openai_market_advisor.infer_base_url(model, base_url),
        api_key=api_key,
        model=model,
        prompt=prompt,
        timeout=timeout,
    )
    result = openai_market_advisor.normalize_payload(response)
    return normalize_llm_config(result if isinstance(result, dict) else {})


def run_once(args: argparse.Namespace) -> dict:
    config = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = AUTOOPT_DIR / "strategy-researcher" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    snapshot = latest_market_snapshot(config, args.snapshot_limit)
    factor_candidates = infer_factor_candidates(snapshot)
    strategy_summaries: list[dict[str, Any]] = []
    window_records: list[dict[str, Any]] = []
    pair_window_attributions: dict[str, dict[int, list[dict[str, Any]]]] = {}
    strategies = list(dict.fromkeys(args.strategies))
    ai_strategy_names: list[str] = []
    if args.include_ai_generated:
        ai_strategy_names = load_ai_strategy_names(Path(args.ai_candidates))
        strategies = list(dict.fromkeys([*strategies, *ai_strategy_names]))

    for strategy in strategies:
        strategy_file = strategy_path / f"{strategy}.py"
        if not strategy_file.is_file():
            strategy_summaries.append(
                {
                    "strategy": strategy,
                    "score": -999999.0,
                    "passed_windows": 0,
                    "side": "hold",
                    "reason": f"strategy file not found: {strategy_file}",
                }
            )
            continue
        evaluations, pair_attributions = evaluate_strategy_windows(
            strategy=strategy,
            windows=list(dict.fromkeys(args.windows)),
            strategy_path=strategy_path,
            config=config,
            run_dir=run_dir,
            freqaimodel=args.freqaimodel,
            backend=args.backend,
            min_trades=args.min_trades,
            min_profit_pct=args.min_profit_pct,
        )
        window_records.extend(asdict(item) for item in evaluations)
        pair_window_attributions[strategy] = pair_attributions
        strategy_summaries.append(
            aggregate_strategy_score(
                evaluations,
                slippage_buffer_pct_per_trade=args.slippage_buffer_pct_per_trade,
                min_net_profit_pct=args.min_net_profit_pct,
                min_pass_rate=args.min_pass_rate,
            )
        )

    ledger_feedback_records = read_jsonl_tail(Path(args.execution_ledger))
    trade_db_records = trade_db_feedback_records(
        Path(getattr(args, "trade_db", DEFAULT_TRADE_DB)),
        limit=getattr(args, "real_trade_lookback", 300),
    )
    combined_feedback_records = [*ledger_feedback_records, *trade_db_records]
    execution_feedback = build_execution_feedback(combined_feedback_records)
    pair_execution_feedback = build_pair_execution_feedback(combined_feedback_records)
    strategy_summaries = apply_execution_feedback(strategy_summaries, execution_feedback)
    kronos_payload = read_json(Path(args.kronos_forecast), {})
    kronos = kronos_signal(kronos_payload, args.kronos_return_threshold_pct)
    if not args.disable_kronos_scoring:
        strategy_summaries = apply_kronos_scoring(strategy_summaries, kronos)
    viable = [
        item
        for item in strategy_summaries
        if item.get("passed_windows", 0) > 0
        and item.get("quality_gate", {}).get("passed", True)
    ]
    best_strategy = max(viable, key=lambda item: (item["score"], item["passed_windows"])) if viable else None
    best_factor = max(factor_candidates, key=lambda item: item.market_score)
    recommended_side = best_strategy.get("side", "hold") if best_strategy else "hold"
    kronos_confirms_side = kronos.get("side") == recommended_side and recommended_side in {"long", "short"}
    if (
        best_factor.side_bias in {"long", "short"}
        and recommended_side != best_factor.side_bias
        and not kronos_confirms_side
    ):
        recommended_side = "hold"

    registry = {
        "record_type": "strategy_registry",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "windows": list(dict.fromkeys(args.windows)),
        "strategy_candidates": sorted(set(strategies)),
        "ai_generated_strategy_candidates": ai_strategy_names,
        "factor_candidates": [asdict(item) for item in factor_candidates],
        "strategy_scores": sorted(strategy_summaries, key=lambda item: item["score"], reverse=True),
        "window_evaluations": window_records,
        "execution_feedback": execution_feedback,
        "execution_feedback_sources": {
            "ledger_records": len(ledger_feedback_records),
            "trade_db_records": len(trade_db_records),
            "trade_db_path": str(Path(getattr(args, "trade_db", DEFAULT_TRADE_DB))),
        },
        "kronos_signal": kronos,
        "recommended": {
            "strategy": best_strategy.get("strategy") if best_strategy else None,
            "side": recommended_side,
            "factor": best_factor.name,
            "score": best_strategy.get("score") if best_strategy else -999999.0,
            "parameter_bias": best_factor.parameter_bias,
        },
        "market_summary": snapshot.get("summary", {}),
        "usage_note": "Advisory only. Freqtrade remains the execution and risk-control layer.",
    }
    previous_pair_selection = read_json(Path(args.pair_selection_output), {})
    pair_selection = build_pair_selection_payload(
        pair_window_attributions,
        source_window_days=args.pair_selection_window_days,
        min_trades=args.pair_selection_min_trades,
        min_profit_pct=args.pair_selection_min_profit_pct,
        max_pairs=args.pair_selection_max_pairs,
        previous_payload=previous_pair_selection,
        min_hold_runs=args.pair_selection_min_hold_runs,
        remove_below_profit_pct=args.pair_selection_remove_below_profit_pct,
        pair_execution_feedback=pair_execution_feedback,
        pair_real_trade_min_samples=2,
    )
    previous_formal_pool = read_json(Path(args.formal_pool_output), {})
    formal_pool = build_formal_strategy_pool(
        strategy_summaries,
        window_records,
        required_window_days=args.formal_pool_required_window_days,
        min_profit_pct=args.formal_pool_min_profit_pct,
        min_trades=args.formal_pool_min_trades,
        max_strategies=args.formal_pool_max_strategies,
        previous_payload=previous_formal_pool,
        priority_streak_threshold=getattr(
            args,
            "formal_pool_priority_streak_threshold",
            DEFAULT_FORMAL_POOL_PRIORITY_STREAK_THRESHOLD,
        ),
        priority_step=getattr(
            args,
            "formal_pool_priority_step",
            DEFAULT_FORMAL_POOL_PRIORITY_STEP,
        ),
        priority_max_bonus=getattr(
            args,
            "formal_pool_priority_max_bonus",
            DEFAULT_FORMAL_POOL_PRIORITY_MAX_BONUS,
        ),
        execution_feedback=execution_feedback,
        real_trade_priority_min_samples=getattr(
            args,
            "real_trade_priority_min_samples",
            DEFAULT_REAL_TRADE_PRIORITY_MIN_SAMPLES,
        ),
        real_trade_priority_step=getattr(
            args,
            "real_trade_priority_step",
            DEFAULT_REAL_TRADE_PRIORITY_STEP,
        ),
        real_trade_priority_max_bonus=getattr(
            args,
            "real_trade_priority_max_bonus",
            DEFAULT_REAL_TRADE_PRIORITY_MAX_BONUS,
        ),
    )
    pair_selection["recommended_strategy"] = best_strategy.get("strategy") if best_strategy else None
    pair_selection["recommended_allowed_pairs"] = (
        pair_selection.get("strategies", {})
        .get(str(best_strategy.get("strategy") if best_strategy else ""), {})
        .get("allowed_pairs", [])
    )
    registry["pair_selection"] = pair_selection
    registry["formal_strategy_pool"] = formal_pool
    if args.use_llm_advisor:
        try:
            registry["llm_candidate_review"] = llm_candidate_review(
                registry,
                model=args.advisor_model,
                base_url=args.advisor_base_url,
                timeout=args.advisor_timeout,
            )
        except Exception as exc:
            registry["llm_candidate_review"] = {"enabled": True, "error": str(exc)}
    write_json(Path(args.output), registry)
    write_json(Path(args.pair_selection_output), pair_selection)
    write_json(Path(args.formal_pool_output), formal_pool)
    append_jsonl(Path(args.ledger), registry)
    print(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return registry


def main() -> int:
    args = parse_args()
    while True:
        run_once(args)
        if not args.loop:
            return 0
        time.sleep(max(args.interval_minutes, 1.0) * 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
