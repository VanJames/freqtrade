#!/usr/bin/env python3
"""
Continuously evaluate strategy + direction performance and publish an advisory JSON.

This script is deliberately advisory-only. It does not place orders and does not
modify the active Freqtrade config. It answers: which reviewed strategy, side
(long/short), and recent market prices look strongest under rolling backtests.
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

from freqtrade.data.btanalysis.bt_fileutils import load_backtest_stats

from scripts import auto_optimize, build_market_snapshot, openai_market_advisor


USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_OUTPUT = AUTOOPT_DIR / "direction_advisor.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "direction_advisor.jsonl"
DEFAULT_CANDIDATE_REGISTRY = AUTOOPT_DIR / "strategy_registry.json"
DEFAULT_KRONOS_FORECAST = AUTOOPT_DIR / "kronos_forecast.json"
VALID_ACTIONS = {"long", "short", "hold"}
VALID_PARAMETER_BIAS = {
    "risk": {"reduce", "normal", "increase"},
    "entry": {"stricter", "normal", "looser"},
    "exit": {"faster", "normal", "slower"},
}
LLM_OPPOSITE_VETO_CONFIDENCE = 0.65
LLM_HOLD_VETO_CONFIDENCE = 0.80
KRONOS_CONFIRM_CONFIDENCE = 0.55
KRONOS_STRONG_OPPOSE_CONFIDENCE = 0.75


@dataclass
class DirectionMetrics:
    strategy: str
    side: str
    trades: int
    wins: int
    losses: int
    winrate: float
    profit_total_pct: float
    profit_total_abs: float
    score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate best strategy side recommendation.")
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
            "SampleStrategyRangeMeanReversion",
            "SampleStrategyBreakoutMomentum",
            "SampleStrategyLongOnly",
            "SampleStrategyShortOnly",
        ],
    )
    parser.add_argument("--candidate-registry", default=str(DEFAULT_CANDIDATE_REGISTRY))
    parser.add_argument("--kronos-forecast", default=str(DEFAULT_KRONOS_FORECAST))
    parser.add_argument("--disable-kronos-confirmation", action="store_true")
    parser.add_argument("--backtest-days", type=int, default=2)
    parser.add_argument("--min-side-trades", type=int, default=1)
    parser.add_argument("--min-side-profit-pct", type=float, default=0.0)
    parser.add_argument("--snapshot-limit", type=int, default=220)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--use-llm-advisor", action="store_true")
    parser.add_argument("--advisor-model", default="deepseek-chat")
    parser.add_argument("--advisor-base-url", default="")
    parser.add_argument("--advisor-timeout", type=int, default=60)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=30.0)
    return parser.parse_args()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def latest_prices(config_path: Path, limit: int) -> dict[str, Any]:
    config = build_market_snapshot.load_config(str(config_path))
    exchange = config.get("exchange", {}).get("name", "")
    datadir = build_market_snapshot.default_datadir(str(config_path), exchange)
    snapshot = build_market_snapshot.build_snapshot(config, limit=limit, datadir=datadir)
    prices: dict[str, Any] = {}
    for item in snapshot.get("per_pair", []):
        if not isinstance(item, dict):
            continue
        pair = item.get("pair")
        timeframe = item.get("timeframe")
        if timeframe != config.get("timeframe", "5m") or not pair:
            continue
        prices[str(pair)] = {
            "last_close": item.get("last_close", 0.0),
            "return_pct": item.get("return_pct", 0.0),
            "atr_pct": item.get("atr_pct", 0.0),
            "volume_ratio": item.get("volume_ratio", 0.0),
        }
    return {
        "generated_at": snapshot.get("generated_at"),
        "exchange": snapshot.get("exchange"),
        "timeframe": config.get("timeframe", "5m"),
        "prices": prices,
        "summary": snapshot.get("summary", {}),
    }


def side_from_tag(key: Any) -> str | None:
    text = str(key).lower()
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    return None


def extract_direction_metrics(strategy: str, stats: dict) -> list[DirectionMetrics]:
    strategy_stats = stats["strategy"][strategy]
    buckets = {
        "long": {"trades": 0, "wins": 0, "losses": 0, "profit_total": 0.0, "profit_abs": 0.0},
        "short": {"trades": 0, "wins": 0, "losses": 0, "profit_total": 0.0, "profit_abs": 0.0},
    }
    for row in strategy_stats.get("results_per_enter_tag", []) or []:
        if not isinstance(row, dict) or row.get("key") == "TOTAL":
            continue
        side = side_from_tag(row.get("key"))
        if side not in buckets:
            continue
        bucket = buckets[side]
        bucket["trades"] += _as_int(row.get("trades"))
        bucket["wins"] += _as_int(row.get("wins"))
        bucket["losses"] += _as_int(row.get("losses"))
        bucket["profit_total"] += _as_float(row.get("profit_total"))
        bucket["profit_abs"] += _as_float(row.get("profit_total_abs"))

    results: list[DirectionMetrics] = []
    overall_drawdown_pct = _as_float(strategy_stats.get("max_drawdown_account")) * 100.0
    for side, bucket in buckets.items():
        trades = int(bucket["trades"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        profit_pct = round(float(bucket["profit_total"]) * 100.0, 4)
        winrate = wins / trades if trades else 0.0
        score = round(profit_pct - overall_drawdown_pct * 0.35 + winrate * 0.25, 4)
        results.append(
            DirectionMetrics(
                strategy=strategy,
                side=side,
                trades=trades,
                wins=wins,
                losses=losses,
                winrate=round(winrate, 4),
                profit_total_pct=profit_pct,
                profit_total_abs=round(float(bucket["profit_abs"]), 8),
                score=score,
            )
        )
    return results


def run_strategy_backtest(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> dict:
    auto_optimize.backtest(
        strategy=strategy,
        strategy_path=strategy_path,
        config=config,
        timerange=timerange,
        backtest_dir=run_dir,
        freqaimodel=freqaimodel,
        logfile=run_dir / "backtest.log",
        backend=backend,
    )
    return load_backtest_stats(run_dir)


def choose_recommendation(
    metrics: list[DirectionMetrics], min_trades: int, min_profit_pct: float
) -> tuple[str, DirectionMetrics | None, str]:
    viable = [
        item
        for item in metrics
        if item.trades >= min_trades and item.profit_total_pct > min_profit_pct
    ]
    if not viable:
        return "hold", None, "No strategy side passed positive-profit and trade-count gates."
    best = max(viable, key=lambda item: (item.score, item.profit_total_pct, item.trades))
    return best.side, best, "Best side passed positive-profit and trade-count gates."


def build_llm_prompt(payload: dict) -> str:
    compact = {
        "task": "Review short-term crypto futures direction advisor output.",
        "constraints": [
            "Return strict JSON only.",
            "Do not invent trades or ignore backtest gates.",
            "If statistical action is hold, do not force a trade.",
            "Focus on 1-2 day short-term execution.",
        ],
        "schema": {
            "action": "long|short|hold",
            "confidence": "0..1",
            "reason": "short reason",
            "parameter_bias": {
                "risk": "reduce|normal|increase",
                "entry": "stricter|normal|looser",
                "exit": "faster|normal|slower",
            },
            "warnings": ["short strings"],
        },
        "advisor_payload": payload,
    }
    return json.dumps(compact, ensure_ascii=False)


def llm_review(payload: dict, *, model: str, base_url: str, timeout: int) -> dict:
    api_key = openai_market_advisor.os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"enabled": False, "error": "OPENAI_API_KEY is not set."}
    prompt = build_llm_prompt(payload)
    response = openai_market_advisor.call_openai(
        base_url=openai_market_advisor.infer_base_url(model, base_url),
        api_key=api_key,
        model=model,
        prompt=prompt,
        timeout=timeout,
    )
    result = openai_market_advisor.normalize_payload(response)
    if not isinstance(result, dict):
        result = {"raw_output": result}
    return normalize_llm_review(result)


def normalize_llm_review(review: dict) -> dict:
    action = str(review.get("action", "hold")).strip().lower()
    if action not in VALID_ACTIONS:
        action = "hold"
    try:
        confidence = float(review.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    parameter_bias = review.get("parameter_bias", {})
    if not isinstance(parameter_bias, dict):
        parameter_bias = {}
    normalized_bias = {}
    for key, allowed in VALID_PARAMETER_BIAS.items():
        value = str(parameter_bias.get(key, "normal")).strip().lower()
        normalized_bias[key] = value if value in allowed else "normal"
    warnings = review.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {
        "enabled": True,
        "action": action,
        "confidence": max(0.0, min(confidence, 1.0)),
        "reason": str(review.get("reason", ""))[:500],
        "parameter_bias": normalized_bias,
        "warnings": [str(item)[:200] for item in warnings[:5]],
    }


def apply_llm_confirmation(payload: dict, review: dict) -> dict:
    statistical_action = payload.get("action", "hold")
    llm_action = str(review.get("action", "")).lower()
    confidence = _as_float(review.get("confidence"), 0.0)
    if not review.get("enabled"):
        payload["final_action"] = statistical_action
        payload["final_reason"] = payload.get("reason", "")
        return payload
    if statistical_action == "hold":
        payload["final_action"] = "hold"
        payload["final_reason"] = "Statistical gate is hold; LLM is not allowed to override it."
        return payload
    if llm_action == "hold" and confidence >= LLM_HOLD_VETO_CONFIDENCE:
        payload["final_action"] = "hold"
        payload["final_reason"] = (
            f"LLM requested hold with high confidence {confidence:.2f}; statistical action "
            f"{statistical_action} was vetoed."
        )
        return payload
    if (
        llm_action in {"long", "short"}
        and llm_action != statistical_action
        and confidence >= LLM_OPPOSITE_VETO_CONFIDENCE
    ):
        payload["final_action"] = "hold"
        payload["final_reason"] = (
            f"LLM disagreed with statistical action {statistical_action} "
            f"with confidence {confidence:.2f}."
        )
        return payload
    payload["final_action"] = statistical_action
    payload["final_reason"] = "Statistical gate passed; LLM did not provide a high-confidence veto."
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def load_jsonl_tail(path: Path, limit: int = 100) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError:
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


def load_registry_strategies(path: Path, fallback: list[str]) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback
    candidates = payload.get("strategy_candidates", [])
    if not isinstance(candidates, list):
        return fallback
    names = [str(item) for item in candidates if str(item).strip()]
    return list(dict.fromkeys([*fallback, *names]))


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def apply_kronos_confirmation(payload: dict, forecast: dict[str, Any]) -> dict:
    if not forecast:
        payload["kronos_confirmation"] = {"enabled": False, "reason": "forecast_missing"}
        return payload
    action = str(payload.get("final_action") or payload.get("action") or "hold").lower()
    summary = forecast.get("summary", {}) if isinstance(forecast.get("summary"), dict) else {}
    market_bias = str(summary.get("market_bias", "hold")).lower()
    confidence = _as_float(summary.get("avg_confidence"), 0.0)
    confirmation = {
        "enabled": True,
        "forecast_created_at": forecast.get("created_at"),
        "market_bias": market_bias if market_bias in VALID_ACTIONS else "hold",
        "confidence": confidence,
        "effect": "none",
        "reason": "kronos_neutral_or_missing_bias",
    }
    if action == "hold":
        confirmation["reason"] = "statistical_action_hold"
        payload["kronos_confirmation"] = confirmation
        return payload
    if market_bias == action and confidence >= KRONOS_CONFIRM_CONFIDENCE:
        confirmation["effect"] = "confirmed"
        confirmation["reason"] = "kronos_confirmed_statistical_action"
        payload["kronos_confirmation"] = confirmation
        return payload
    if (
        market_bias in {"long", "short"}
        and market_bias != action
        and confidence >= KRONOS_STRONG_OPPOSE_CONFIDENCE
    ):
        payload["final_action"] = "hold"
        payload["final_reason"] = (
            f"Kronos strongly opposed statistical action {action} with "
            f"{market_bias} confidence {confidence:.2f}."
        )
        confirmation["effect"] = "vetoed"
        confirmation["reason"] = "kronos_strong_opposition"
        payload["kronos_confirmation"] = confirmation
        return payload
    if market_bias in {"long", "short"} and market_bias != action:
        confirmation["effect"] = "weakened"
        confirmation["reason"] = "kronos_low_confidence_opposition"
        if "llm_advisor" not in payload:
            payload["final_reason"] = (
                payload.get("final_reason")
                or "Statistical gate passed; Kronos disagreement was below veto threshold."
            )
        payload["kronos_confirmation"] = confirmation
        return payload
    payload["kronos_confirmation"] = confirmation
    return payload


def build_feedback_summary(records: list[dict]) -> dict[str, Any]:
    buckets = {
        "long": {"count": 0, "positive": 0, "profit_total_pct": 0.0},
        "short": {"count": 0, "positive": 0, "profit_total_pct": 0.0},
        "hold": {"count": 0, "positive": 0, "profit_total_pct": 0.0},
    }
    for record in records:
        action = str(record.get("final_action") or record.get("action") or "hold").lower()
        if action not in buckets:
            action = "hold"
        best = record.get("best") or {}
        profit = _as_float(best.get("profit_total_pct"))
        buckets[action]["count"] += 1
        buckets[action]["profit_total_pct"] += profit
        if profit > 0:
            buckets[action]["positive"] += 1
    for item in buckets.values():
        count = item["count"]
        item["positive_rate"] = round(item["positive"] / count, 4) if count else 0.0
        item["avg_profit_total_pct"] = round(item["profit_total_pct"] / count, 4) if count else 0.0
        item["profit_total_pct"] = round(item["profit_total_pct"], 4)
    return {
        "sample_size": len(records),
        "by_action": buckets,
        "last_action": str(records[-1].get("final_action", "")) if records else "",
        "last_run_id": str(records[-1].get("run_id", "")) if records else "",
    }


def run_once(args: argparse.Namespace) -> dict:
    config = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    train_timerange, _ = auto_optimize.build_walk_forward_timeranges(args.backtest_days, 0)
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = AUTOOPT_DIR / "direction-advisor" / run_id
    strategies = load_registry_strategies(Path(args.candidate_registry), list(args.strategies))

    all_metrics: list[DirectionMetrics] = []
    errors: list[dict[str, str]] = []
    for strategy in dict.fromkeys(strategies):
        if not (strategy_path / f"{strategy}.py").is_file():
            errors.append({"strategy": strategy, "error": "strategy file not found"})
            continue
        try:
            stats = run_strategy_backtest(
                strategy=strategy,
                strategy_path=strategy_path,
                config=config,
                timerange=train_timerange,
                run_dir=run_dir / strategy,
                freqaimodel=args.freqaimodel,
                backend=args.backend,
            )
            all_metrics.extend(extract_direction_metrics(strategy, stats))
        except Exception as exc:
            errors.append({"strategy": strategy, "error": str(exc)[-2000:]})

    action, best, reason = choose_recommendation(
        all_metrics,
        min_trades=args.min_side_trades,
        min_profit_pct=args.min_side_profit_pct,
    )
    if not all_metrics and errors:
        reason = "All strategy backtests failed; holding until advisor can evaluate fresh data."
    market = latest_prices(config, args.snapshot_limit)
    feedback_summary = build_feedback_summary(load_jsonl_tail(Path(args.ledger), limit=100))
    payload = {
        "record_type": "direction_advisor",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "timerange": train_timerange,
        "action": action,
        "final_action": action,
        "recommended_strategy": best.strategy if best else None,
        "recommended_side": best.side if best else "hold",
        "reason": reason,
        "best": asdict(best) if best else None,
        "metrics": [asdict(item) for item in sorted(all_metrics, key=lambda x: x.score, reverse=True)],
        "errors": errors,
        "candidate_registry": str(Path(args.candidate_registry)),
        "evaluated_strategies": list(dict.fromkeys(strategies)),
        "market": market,
        "feedback_summary": feedback_summary,
        "usage_note": (
            "Advisory only. Trade only when live strategy entry signal agrees with recommended_side."
        ),
    }
    if not args.disable_kronos_confirmation:
        payload = apply_kronos_confirmation(payload, load_json(Path(args.kronos_forecast)))
    if args.use_llm_advisor:
        try:
            review = llm_review(
                payload,
                model=args.advisor_model,
                base_url=args.advisor_base_url,
                timeout=args.advisor_timeout,
            )
        except Exception as exc:
            review = {"enabled": True, "error": str(exc)}
        payload["llm_advisor"] = review
        payload = apply_llm_confirmation(payload, review)
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
