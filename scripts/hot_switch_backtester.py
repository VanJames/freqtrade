#!/usr/bin/env python3
"""
Walk-forward backtest for the strategy hot-switch rules.

For each simulated day:
- Evaluate candidate strategies on prior rolling windows.
- Apply the same conservative switch decision logic used by strategy_switcher.
- Backtest the chosen active strategy on the next day.

This is intentionally offline-only. It does not modify config.json, restart
containers, or place orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freqtrade.data.btanalysis.bt_fileutils import load_backtest_stats

from scripts import auto_optimize, direction_advisor, strategy_researcher, strategy_switcher


USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_OUTPUT = AUTOOPT_DIR / "hot_switch_backtest.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "hot_switch_backtest.jsonl"
DEFAULT_STRATEGIES = [
    "SampleStrategy",
    "SampleStrategyActive",
    "SampleStrategyScalp",
    "SampleStrategyPullbackShort",
    "SampleStrategyPullbackLong",
    "SampleStrategyRangeMeanReversion",
    "SampleStrategyBreakoutMomentum",
    "SampleStrategyLongOnly",
    "SampleStrategyShortOnly",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward strategy hot-switch backtest.")
    parser.add_argument("--backend", choices=["compose", "local"], default="compose")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--strategy-path", default=str(STRATEGY_DIR))
    parser.add_argument("--strategies", nargs="+", default=DEFAULT_STRATEGIES)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--decision-windows", nargs="+", type=int, default=[1, 2, 7])
    parser.add_argument("--min-trades", type=int, default=1)
    parser.add_argument("--min-profit-pct", type=float, default=0.0)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--start-strategy", default="")
    parser.add_argument("--min-passed-windows", type=int, default=2)
    parser.add_argument("--switch-min-trades", type=int, default=5)
    parser.add_argument("--min-score-margin", type=float, default=0.15)
    parser.add_argument("--inactive-window-days", type=int, default=1)
    parser.add_argument("--inactive-min-trades", type=int, default=10)
    parser.add_argument("--inactive-min-profit-pct", type=float, default=0.05)
    parser.add_argument("--inactive-min-score", type=float, default=-1.25)
    parser.add_argument("--inactive-confirm-windows", nargs="+", type=int, default=[2, 7])
    parser.add_argument("--require-positive-window-days", nargs="+", type=int, default=[])
    parser.add_argument("--pool-positive-window-days", nargs="+", type=int, default=[])
    parser.add_argument("--min-required-window-profit-pct", type=float, default=0.0)
    parser.add_argument("--cooldown-minutes", type=float, default=360.0)
    parser.add_argument("--max-registry-age-minutes", type=float, default=180.0)
    parser.add_argument("--run-dir", default=str(AUTOOPT_DIR / "hot-switch-backtester"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument(
        "--replay-from-full-backtests",
        action="store_true",
        help="Run one full-period backtest per strategy, then replay daily switch decisions from trades.",
    )
    return parser.parse_args()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def ymd(value: datetime) -> str:
    return value.strftime("%Y%m%d")


def timerange_for(end: datetime, days: int) -> str:
    start = end - timedelta(days=days)
    return f"{ymd(start)}-{ymd(end)}"


def safe_name(value: str) -> str:
    return value.replace("/", "_").replace(":", "_").replace("-", "_")


def run_or_load_backtest(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> dict[str, Any]:
    stats_marker = run_dir / ".hot_switch_cache.json"
    result_files = sorted(run_dir.glob("backtest-result-*.zip"))
    if stats_marker.exists() and result_files:
        try:
            return load_backtest_stats(result_files[-1])
        except Exception:
            pass
    if result_files:
        try:
            return load_backtest_stats(result_files[-1])
        except Exception:
            pass
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
    stats = load_backtest_stats(run_dir)
    stats_marker.write_text(json.dumps({"timerange": timerange, "strategy": strategy}) + "\n")
    return stats


def evaluate_strategy_window(
    *,
    strategy: str,
    window_days: int,
    decision_day: datetime,
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
    min_trades: int,
    min_profit_pct: float,
) -> strategy_researcher.WindowEvaluation:
    timerange = timerange_for(decision_day, window_days)
    try:
        stats = run_or_load_backtest(
            strategy=strategy,
            strategy_path=strategy_path,
            config=config,
            timerange=timerange,
            run_dir=run_dir / safe_name(strategy) / f"decision_{ymd(decision_day)}_{window_days}d",
            freqaimodel=freqaimodel,
            backend=backend,
        )
        best, reason = strategy_researcher.best_direction_for_window(
            strategy,
            stats,
            min_trades=min_trades,
            min_profit_pct=min_profit_pct,
        )
        if best is None:
            return strategy_researcher.WindowEvaluation(
                strategy=strategy,
                window_days=window_days,
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
        return strategy_researcher.WindowEvaluation(
            strategy=strategy,
            window_days=window_days,
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
    except Exception as exc:
        return strategy_researcher.WindowEvaluation(
            strategy=strategy,
            window_days=window_days,
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


def build_daily_registry(
    *,
    strategies: list[str],
    decision_windows: list[int],
    decision_day: datetime,
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
    min_trades: int,
    min_profit_pct: float,
) -> dict[str, Any]:
    strategy_scores: list[dict[str, Any]] = []
    window_records: list[dict[str, Any]] = []
    for strategy in dict.fromkeys(strategies):
        evaluations = [
            evaluate_strategy_window(
                strategy=strategy,
                window_days=window,
                decision_day=decision_day,
                strategy_path=strategy_path,
                config=config,
                run_dir=run_dir,
                freqaimodel=freqaimodel,
                backend=backend,
                min_trades=min_trades,
                min_profit_pct=min_profit_pct,
            )
            for window in dict.fromkeys(decision_windows)
        ]
        window_records.extend(asdict(item) for item in evaluations)
        strategy_scores.append(strategy_researcher.aggregate_strategy_score(evaluations))

    viable = [item for item in strategy_scores if int(item.get("passed_windows", 0)) > 0]
    best = max(viable, key=lambda item: (item["score"], item["passed_windows"])) if viable else None
    return {
        "record_type": "hot_switch_daily_registry",
        "created_at": decision_day.replace(tzinfo=UTC).isoformat(),
        "decision_day": ymd(decision_day),
        "windows": list(dict.fromkeys(decision_windows)),
        "strategy_candidates": sorted(set(strategies)),
        "strategy_scores": sorted(strategy_scores, key=lambda item: item["score"], reverse=True),
        "window_evaluations": window_records,
        "recommended": {
            "strategy": best.get("strategy") if best else None,
            "side": best.get("side", "hold") if best else "hold",
            "score": best.get("score", -999999.0) if best else -999999.0,
        },
    }


def backtest_day(
    *,
    strategy: str,
    trade_day: datetime,
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> dict[str, Any]:
    timerange = f"{ymd(trade_day)}-{ymd(trade_day + timedelta(days=1))}"
    stats = run_or_load_backtest(
        strategy=strategy,
        strategy_path=strategy_path,
        config=config,
        timerange=timerange,
        run_dir=run_dir / safe_name(strategy) / f"trade_{timerange}",
        freqaimodel=freqaimodel,
        backend=backend,
    )
    metrics = auto_optimize.extract_metrics(stats, strategy)
    side_metrics = direction_advisor.extract_direction_metrics(strategy, stats)
    best_side = max(side_metrics, key=lambda item: (item.profit_total_pct, item.trades))
    return {
        **asdict(metrics),
        "strategy": strategy,
        "timerange": timerange,
        "side": best_side.side if best_side.trades else "hold",
        "side_profit_total_pct": best_side.profit_total_pct,
        "side_trades": best_side.trades,
        "score": round(metrics.profit_total_pct - metrics.max_drawdown_account * 100.0, 4),
    }


def summarize_days(records: list[dict[str, Any]]) -> dict[str, Any]:
    total_trades = sum(int(item.get("trades", 0)) for item in records)
    profit_total_pct = round(sum(float(item.get("profit_total_pct", 0.0)) for item in records), 4)
    profit_total_abs = round(sum(float(item.get("profit_total_abs", 0.0)) for item in records), 8)
    active_days = sum(1 for item in records if int(item.get("trades", 0)) > 0)
    return {
        "days": len(records),
        "active_days": active_days,
        "trades": total_trades,
        "avg_trades_per_day": round(total_trades / max(len(records), 1), 4),
        "profit_total_pct_sum": profit_total_pct,
        "profit_total_abs_sum": profit_total_abs,
        "avg_profit_pct_per_day": round(profit_total_pct / max(len(records), 1), 4),
    }


def parse_trade_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def side_from_trade(trade: dict[str, Any]) -> str:
    if bool(trade.get("is_short")):
        return "short"
    tag = str(trade.get("enter_tag") or "").lower()
    if "short" in tag:
        return "short"
    if "long" in tag:
        return "long"
    return "long"


def load_full_period_strategy_stats(
    *,
    strategies: list[str],
    start_day: datetime,
    end_day: datetime,
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> dict[str, dict[str, Any]]:
    timerange = f"{ymd(start_day)}-{ymd(end_day)}"
    result: dict[str, dict[str, Any]] = {}
    for strategy in dict.fromkeys(strategies):
        stats = run_or_load_backtest(
            strategy=strategy,
            strategy_path=strategy_path,
            config=config,
            timerange=timerange,
            run_dir=run_dir / "full_period" / safe_name(strategy) / timerange,
            freqaimodel=freqaimodel,
            backend=backend,
        )
        result[strategy] = stats["strategy"][strategy]
    return result


def trades_in_range(
    strategy_stats: dict[str, Any], start_day: datetime, end_day: datetime
) -> list[dict[str, Any]]:
    records = []
    for trade in strategy_stats.get("trades", []) or []:
        if not isinstance(trade, dict):
            continue
        close_dt = parse_trade_dt(trade.get("close_date"))
        if close_dt is None:
            continue
        if start_day <= close_dt < end_day:
            records.append(trade)
    return records


def direction_metrics_from_trades(
    strategy: str,
    strategy_stats: dict[str, Any],
    start_day: datetime,
    end_day: datetime,
) -> list[direction_advisor.DirectionMetrics]:
    starting_balance = float(strategy_stats.get("starting_balance") or 1000.0)
    buckets = {
        "long": {"trades": 0, "wins": 0, "losses": 0, "profit_abs": 0.0},
        "short": {"trades": 0, "wins": 0, "losses": 0, "profit_abs": 0.0},
    }
    for trade in trades_in_range(strategy_stats, start_day, end_day):
        side = side_from_trade(trade)
        bucket = buckets[side]
        profit_abs = float(trade.get("profit_abs") or 0.0)
        bucket["trades"] += 1
        bucket["profit_abs"] += profit_abs
        if profit_abs > 0:
            bucket["wins"] += 1
        elif profit_abs < 0:
            bucket["losses"] += 1

    metrics: list[direction_advisor.DirectionMetrics] = []
    for side, bucket in buckets.items():
        trades = int(bucket["trades"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        profit_abs = float(bucket["profit_abs"])
        profit_pct = round((profit_abs / max(starting_balance, 1e-9)) * 100.0, 4)
        winrate = wins / trades if trades else 0.0
        metrics.append(
            direction_advisor.DirectionMetrics(
                strategy=strategy,
                side=side,
                trades=trades,
                wins=wins,
                losses=losses,
                winrate=round(winrate, 4),
                profit_total_pct=profit_pct,
                profit_total_abs=round(profit_abs, 8),
                score=round(profit_pct + winrate * 0.25, 4),
            )
        )
    return metrics


def replay_window_evaluation(
    *,
    strategy: str,
    strategy_stats: dict[str, Any],
    window_days: int,
    decision_day: datetime,
    min_trades: int,
    min_profit_pct: float,
) -> strategy_researcher.WindowEvaluation:
    start_day = decision_day - timedelta(days=window_days)
    metrics = direction_metrics_from_trades(strategy, strategy_stats, start_day, decision_day)
    viable = [
        item
        for item in metrics
        if item.trades >= min_trades and item.profit_total_pct > min_profit_pct
    ]
    if not viable:
        return strategy_researcher.WindowEvaluation(
            strategy=strategy,
            window_days=window_days,
            timerange=f"{ymd(start_day)}-{ymd(decision_day)}",
            side="hold",
            trades=0,
            profit_total_pct=0.0,
            profit_total_abs=0.0,
            winrate=0.0,
            score=-999999.0,
            passed=False,
            reason="no side passed replay gates",
        )
    best = max(viable, key=lambda item: (item.score, item.profit_total_pct, item.trades))
    return strategy_researcher.WindowEvaluation(
        strategy=strategy,
        window_days=window_days,
        timerange=f"{ymd(start_day)}-{ymd(decision_day)}",
        side=best.side,
        trades=best.trades,
        profit_total_pct=best.profit_total_pct,
        profit_total_abs=best.profit_total_abs,
        winrate=best.winrate,
        score=best.score,
        passed=True,
        reason="passed replay gates",
    )


def build_replay_daily_registry(
    *,
    strategies: list[str],
    strategy_stats: dict[str, dict[str, Any]],
    decision_windows: list[int],
    decision_day: datetime,
    min_trades: int,
    min_profit_pct: float,
) -> dict[str, Any]:
    strategy_scores = []
    window_records = []
    for strategy in dict.fromkeys(strategies):
        evaluations = [
            replay_window_evaluation(
                strategy=strategy,
                strategy_stats=strategy_stats[strategy],
                window_days=window,
                decision_day=decision_day,
                min_trades=min_trades,
                min_profit_pct=min_profit_pct,
            )
            for window in dict.fromkeys(decision_windows)
        ]
        window_records.extend(asdict(item) for item in evaluations)
        strategy_scores.append(strategy_researcher.aggregate_strategy_score(evaluations))

    viable = [item for item in strategy_scores if int(item.get("passed_windows", 0)) > 0]
    best = max(viable, key=lambda item: (item["score"], item["passed_windows"])) if viable else None
    return {
        "record_type": "hot_switch_replay_daily_registry",
        "created_at": decision_day.replace(tzinfo=UTC).isoformat(),
        "decision_day": ymd(decision_day),
        "windows": list(dict.fromkeys(decision_windows)),
        "strategy_candidates": sorted(set(strategies)),
        "strategy_scores": sorted(strategy_scores, key=lambda item: item["score"], reverse=True),
        "window_evaluations": window_records,
        "recommended": {
            "strategy": best.get("strategy") if best else None,
            "side": best.get("side", "hold") if best else "hold",
            "score": best.get("score", -999999.0) if best else -999999.0,
        },
    }


def replay_day_result(
    strategy: str,
    strategy_stats: dict[str, Any],
    trade_day: datetime,
) -> dict[str, Any]:
    trades = trades_in_range(strategy_stats, trade_day, trade_day + timedelta(days=1))
    profit_abs = round(sum(float(trade.get("profit_abs") or 0.0) for trade in trades), 8)
    starting_balance = float(strategy_stats.get("starting_balance") or 1000.0)
    profit_pct = round((profit_abs / max(starting_balance, 1e-9)) * 100.0, 4)
    side_metrics = direction_metrics_from_trades(
        strategy, strategy_stats, trade_day, trade_day + timedelta(days=1)
    )
    best_side = max(side_metrics, key=lambda item: (item.profit_total_pct, item.trades))
    return {
        "strategy": strategy,
        "timerange": f"{ymd(trade_day)}-{ymd(trade_day + timedelta(days=1))}",
        "trades": len(trades),
        "profit_total_pct": profit_pct,
        "profit_total_abs": profit_abs,
        "max_drawdown_account": 0.0,
        "profit_factor": 0.0,
        "total_volume": round(sum(float(trade.get("stake_amount") or 0.0) for trade in trades), 8),
        "side": best_side.side if best_side.trades else "hold",
        "side_profit_total_pct": best_side.profit_total_pct,
        "side_trades": best_side.trades,
        "score": profit_pct,
    }


def run_replay(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    config = read_json(config_path, {})
    strategies = list(dict.fromkeys(args.strategies))
    start_strategy = parse_start_strategy(config, args.start_strategy, strategies)
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.run_dir).resolve() / f"{run_id}-replay"
    end_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    trade_days = [end_day - timedelta(days=days) for days in range(args.days, 0, -1)]
    full_start = trade_days[0] - timedelta(days=max(args.decision_windows or [1]))
    strategy_stats = load_full_period_strategy_stats(
        strategies=strategies,
        start_day=full_start,
        end_day=end_day,
        strategy_path=strategy_path,
        config=config_path,
        run_dir=run_dir,
        freqaimodel=args.freqaimodel,
        backend=args.backend,
    )

    current_strategy = start_strategy
    state: dict[str, Any] = {}
    switches = []
    daily_results = []
    for trade_day in trade_days:
        registry = build_replay_daily_registry(
            strategies=strategies,
            strategy_stats=strategy_stats,
            decision_windows=list(dict.fromkeys(args.decision_windows)),
            decision_day=trade_day,
            min_trades=args.min_trades,
            min_profit_pct=args.min_profit_pct,
        )
        decision = strategy_switcher.should_switch(
            {"strategy": current_strategy},
            registry,
            state,
            allow_strategies=set(strategies),
            open_trades=0,
            open_orders=0,
            min_passed_windows=args.min_passed_windows,
            min_trades=args.switch_min_trades,
            min_score_margin=args.min_score_margin,
            inactive_window_days=args.inactive_window_days,
            inactive_min_trades=args.inactive_min_trades,
            inactive_min_profit_pct=args.inactive_min_profit_pct,
            inactive_min_score=args.inactive_min_score,
            inactive_confirm_windows=set(args.inactive_confirm_windows),
            required_positive_windows=set(args.require_positive_window_days),
            pool_positive_windows=set(args.pool_positive_window_days),
            min_required_window_profit_pct=args.min_required_window_profit_pct,
            max_registry_age_minutes=args.max_registry_age_minutes,
            cooldown_minutes=args.cooldown_minutes,
            now=trade_day.replace(tzinfo=UTC),
        )
        if decision.should_switch:
            switches.append(
                {
                    "decision_day": ymd(trade_day),
                    "from_strategy": decision.current_strategy,
                    "to_strategy": decision.target_strategy,
                    "reason": decision.reason,
                    "details": decision.details,
                }
            )
            current_strategy = decision.target_strategy
            state = {
                "last_switch_at": trade_day.replace(tzinfo=UTC).isoformat(),
                "from_strategy": decision.current_strategy,
                "to_strategy": decision.target_strategy,
            }
        daily_results.append(
            {
                "trade_day": ymd(trade_day),
                "decision": {
                    "should_switch": decision.should_switch,
                    "reason": decision.reason,
                    "current_strategy": decision.current_strategy,
                    "target_strategy": decision.target_strategy,
                    "recommended": registry.get("recommended", {}),
                },
                "trade_result": replay_day_result(current_strategy, strategy_stats[current_strategy], trade_day),
            }
        )

    baselines = {
        strategy: {
            "summary": summarize_days(
                [replay_day_result(strategy, stats, trade_day) for trade_day in trade_days]
            ),
            "daily": [replay_day_result(strategy, stats, trade_day) for trade_day in trade_days],
        }
        for strategy, stats in strategy_stats.items()
    }
    payload = {
        "record_type": "hot_switch_backtest",
        "mode": "full_backtest_replay",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "days": args.days,
        "decision_windows": list(dict.fromkeys(args.decision_windows)),
        "start_strategy": start_strategy,
        "strategies": strategies,
        "hot_switch": {
            "summary": summarize_days([item["trade_result"] for item in daily_results]),
            "switches": switches,
            "daily": daily_results,
        },
        "fixed_baselines": baselines,
        "best_fixed_baseline": max(
            baselines.items(),
            key=lambda item: (
                item[1]["summary"]["profit_total_pct_sum"],
                item[1]["summary"]["trades"],
            ),
        )[0]
        if baselines
        else "",
        "usage_note": (
            "Offline full-period trade replay. Faster than exact daily retraining; "
            "use exact mode for final validation."
        ),
    }
    write_json(Path(args.output), payload)
    append_jsonl(Path(args.ledger), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return payload


def fixed_strategy_baselines(
    *,
    strategies: list[str],
    trade_days: list[datetime],
    strategy_path: Path,
    config: Path,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for strategy in dict.fromkeys(strategies):
        daily: list[dict[str, Any]] = []
        for trade_day in trade_days:
            try:
                daily.append(
                    backtest_day(
                        strategy=strategy,
                        trade_day=trade_day,
                        strategy_path=strategy_path,
                        config=config,
                        run_dir=run_dir / "fixed",
                        freqaimodel=freqaimodel,
                        backend=backend,
                    )
                )
            except Exception as exc:
                daily.append(
                    {
                        "strategy": strategy,
                        "timerange": f"{ymd(trade_day)}-{ymd(trade_day + timedelta(days=1))}",
                        "trades": 0,
                        "profit_total_pct": 0.0,
                        "profit_total_abs": 0.0,
                        "score": -999999.0,
                        "error": str(exc)[-500:],
                    }
                )
        results[strategy] = {"summary": summarize_days(daily), "daily": daily}
    return results


def parse_start_strategy(config: dict[str, Any], explicit: str, strategies: list[str]) -> str:
    if explicit:
        return explicit
    configured = str(config.get("strategy") or "")
    if configured:
        return configured
    return strategies[0]


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    if args.replay_from_full_backtests:
        return run_replay(args)
    config_path = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    config = read_json(config_path, {})
    strategies = list(dict.fromkeys(args.strategies))
    start_strategy = parse_start_strategy(config, args.start_strategy, strategies)
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.run_dir).resolve() / run_id
    end_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    trade_days = [end_day - timedelta(days=days) for days in range(args.days, 0, -1)]

    current_strategy = start_strategy
    state: dict[str, Any] = {}
    switched_records: list[dict[str, Any]] = []
    daily_results: list[dict[str, Any]] = []
    for trade_day in trade_days:
        registry = build_daily_registry(
            strategies=strategies,
            decision_windows=list(dict.fromkeys(args.decision_windows)),
            decision_day=trade_day,
            strategy_path=strategy_path,
            config=config_path,
            run_dir=run_dir / "decision",
            freqaimodel=args.freqaimodel,
            backend=args.backend,
            min_trades=args.min_trades,
            min_profit_pct=args.min_profit_pct,
        )
        decision = strategy_switcher.should_switch(
            {"strategy": current_strategy},
            registry,
            state,
            allow_strategies=set(strategies),
            open_trades=0,
            open_orders=0,
            min_passed_windows=args.min_passed_windows,
            min_trades=args.switch_min_trades,
            min_score_margin=args.min_score_margin,
            inactive_window_days=args.inactive_window_days,
            inactive_min_trades=args.inactive_min_trades,
            inactive_min_profit_pct=args.inactive_min_profit_pct,
            inactive_min_score=args.inactive_min_score,
            inactive_confirm_windows=set(args.inactive_confirm_windows),
            required_positive_windows=set(args.require_positive_window_days),
            pool_positive_windows=set(args.pool_positive_window_days),
            min_required_window_profit_pct=args.min_required_window_profit_pct,
            max_registry_age_minutes=args.max_registry_age_minutes,
            cooldown_minutes=args.cooldown_minutes,
            now=trade_day.replace(tzinfo=UTC),
        )
        if decision.should_switch:
            switched_records.append(
                {
                    "decision_day": ymd(trade_day),
                    "from_strategy": decision.current_strategy,
                    "to_strategy": decision.target_strategy,
                    "reason": decision.reason,
                    "details": decision.details,
                }
            )
            current_strategy = decision.target_strategy
            state = {
                "last_switch_at": trade_day.replace(tzinfo=UTC).isoformat(),
                "from_strategy": decision.current_strategy,
                "to_strategy": decision.target_strategy,
            }

        try:
            trade_result = backtest_day(
                strategy=current_strategy,
                trade_day=trade_day,
                strategy_path=strategy_path,
                config=config_path,
                run_dir=run_dir / "hot_switch",
                freqaimodel=args.freqaimodel,
                backend=args.backend,
            )
        except Exception as exc:
            trade_result = {
                "strategy": current_strategy,
                "timerange": f"{ymd(trade_day)}-{ymd(trade_day + timedelta(days=1))}",
                "trades": 0,
                "profit_total_pct": 0.0,
                "profit_total_abs": 0.0,
                "score": -999999.0,
                "error": str(exc)[-500:],
            }
        daily_results.append(
            {
                "trade_day": ymd(trade_day),
                "decision": {
                    "should_switch": decision.should_switch,
                    "reason": decision.reason,
                    "current_strategy": decision.current_strategy,
                    "target_strategy": decision.target_strategy,
                    "recommended": registry.get("recommended", {}),
                },
                "trade_result": trade_result,
            }
        )

    baselines = fixed_strategy_baselines(
        strategies=strategies,
        trade_days=trade_days,
        strategy_path=strategy_path,
        config=config_path,
        run_dir=run_dir,
        freqaimodel=args.freqaimodel,
        backend=args.backend,
    )
    payload = {
        "record_type": "hot_switch_backtest",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "days": args.days,
        "decision_windows": list(dict.fromkeys(args.decision_windows)),
        "start_strategy": start_strategy,
        "strategies": strategies,
        "hot_switch": {
            "summary": summarize_days([item["trade_result"] for item in daily_results]),
            "switches": switched_records,
            "daily": daily_results,
        },
        "fixed_baselines": baselines,
        "best_fixed_baseline": max(
            baselines.items(),
            key=lambda item: (
                item[1]["summary"]["profit_total_pct_sum"],
                item[1]["summary"]["trades"],
            ),
        )[0]
        if baselines
        else "",
        "usage_note": "Offline walk-forward simulation only. It does not guarantee live profitability.",
    }
    write_json(Path(args.output), payload)
    append_jsonl(Path(args.ledger), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return payload


def main() -> int:
    run_once(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
