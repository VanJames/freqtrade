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
DEFAULT_WINDOWS = [1, 2, 7]
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
        default=[
            "SampleStrategy",
            "SampleStrategyActive",
            "SampleStrategyScalp",
            "SampleStrategyPullbackShort",
            "SampleStrategyPullbackLong",
            "SampleStrategyLongOnly",
            "SampleStrategyShortOnly",
        ],
    )
    parser.add_argument("--windows", nargs="+", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--min-trades", type=int, default=1)
    parser.add_argument("--min-profit-pct", type=float, default=0.0)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--snapshot-limit", type=int, default=220)
    parser.add_argument("--use-llm-advisor", action="store_true")
    parser.add_argument("--advisor-model", default="deepseek-chat")
    parser.add_argument("--advisor-base-url", default="")
    parser.add_argument("--advisor-timeout", type=int, default=60)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--execution-ledger", default=str(DEFAULT_EXECUTION_LEDGER))
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
) -> list[WindowEvaluation]:
    evaluations: list[WindowEvaluation] = []
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
    return evaluations


def aggregate_strategy_score(evaluations: list[WindowEvaluation]) -> dict[str, Any]:
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
            total_score -= 2.0 * weight
    side = max(side_scores, key=side_scores.get)
    if passed == 0 or side_scores[side] <= 0:
        side = "hold"
    return {
        "strategy": evaluations[0].strategy,
        "score": round(total_score / max(total_weight, 1e-9), 4),
        "passed_windows": passed,
        "total_windows": len(evaluations),
        "side": side,
        "side_scores": {key: round(value, 4) for key, value in side_scores.items()},
        "profit_total_pct": round(sum(item.profit_total_pct for item in evaluations if item.passed), 4),
        "trades": sum(item.trades for item in evaluations if item.passed),
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

    for strategy in dict.fromkeys(args.strategies):
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
        evaluations = evaluate_strategy_windows(
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
        strategy_summaries.append(aggregate_strategy_score(evaluations))

    execution_feedback = build_execution_feedback(read_jsonl_tail(Path(args.execution_ledger)))
    strategy_summaries = apply_execution_feedback(strategy_summaries, execution_feedback)
    viable = [item for item in strategy_summaries if item.get("passed_windows", 0) > 0]
    best_strategy = max(viable, key=lambda item: (item["score"], item["passed_windows"])) if viable else None
    best_factor = max(factor_candidates, key=lambda item: item.market_score)
    recommended_side = best_strategy.get("side", "hold") if best_strategy else "hold"
    if best_factor.side_bias in {"long", "short"} and recommended_side != best_factor.side_bias:
        recommended_side = "hold"

    registry = {
        "record_type": "strategy_registry",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "windows": list(dict.fromkeys(args.windows)),
        "strategy_candidates": sorted(set(args.strategies)),
        "factor_candidates": [asdict(item) for item in factor_candidates],
        "strategy_scores": sorted(strategy_summaries, key=lambda item: item["score"], reverse=True),
        "window_evaluations": window_records,
        "execution_feedback": execution_feedback,
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
