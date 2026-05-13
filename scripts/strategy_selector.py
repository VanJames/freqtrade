#!/usr/bin/env python3
"""
Select the active strategy from a small, pre-reviewed strategy pool.

The selector does not generate code. It runs rolling backtests for known strategy
classes and only updates config.json when the winner passes profitability,
drawdown, and sample-out confirmation gates.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import auto_optimize


USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
SELECTION_LEDGER_PATH = AUTOOPT_DIR / "strategy_selection.jsonl"


@dataclass
class StrategyEvaluation:
    strategy: str
    train: dict
    confirm: dict | None = None
    final_confirm: dict | None = None
    score: float = 0.0
    passed: bool = False
    reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest and select the best active strategy.")
    parser.add_argument("--backend", choices=["compose", "local"], default="compose")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--strategy-path", default=str(STRATEGY_DIR))
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["SampleStrategy", "SampleStrategyLongOnly", "SampleStrategyShortOnly"],
    )
    parser.add_argument("--backtest-days", type=int, default=30)
    parser.add_argument("--confirm-days", type=int, default=7)
    parser.add_argument("--confirm-slices", type=int, default=2)
    parser.add_argument("--final-confirm-days", type=int, default=3)
    parser.add_argument("--min-trades", type=int, default=3)
    parser.add_argument("--min-profit-pct", type=float, default=0.0)
    parser.add_argument("--min-score-delta", type=float, default=0.01)
    parser.add_argument("--max-drawdown", type=float, default=0.08)
    parser.add_argument("--max-drawdown-worsen", type=float, default=0.02)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart-bot", action="store_true")
    return parser.parse_args()


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=4) + "\n")


def active_strategy(config: dict, fallback: str) -> str:
    strategy = config.get("strategy")
    return str(strategy) if strategy else fallback


def strategy_score(metrics: auto_optimize.RunMetrics) -> float:
    return round(metrics.profit_total_pct - metrics.max_drawdown_account * 100.0, 4)


def evaluate_strategy(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
) -> auto_optimize.RunMetrics:
    metrics, _ = auto_optimize.backtest(
        strategy=strategy,
        strategy_path=strategy_path,
        config=config,
        timerange=timerange,
        backtest_dir=run_dir,
        freqaimodel=freqaimodel,
        logfile=run_dir / "backtest.log",
        backend=backend,
    )
    return metrics


def evaluate_slices(
    *,
    strategy: str,
    current_strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    slices: int,
    run_dir: Path,
    freqaimodel: str,
    backend: str,
    min_score_delta: float,
    max_drawdown_worsen: float,
) -> dict:
    windows = auto_optimize.build_confirmation_slices(timerange, slices)
    results: list[dict] = []
    passing = 0
    informative = 0
    cumulative_current_score = 0.0
    cumulative_candidate_score = 0.0
    cumulative_current_profit_abs = 0.0
    cumulative_candidate_profit_abs = 0.0

    for idx, window in enumerate(windows, start=1):
        current_metrics = evaluate_strategy(
            strategy=current_strategy,
            strategy_path=strategy_path,
            config=config,
            timerange=window,
            run_dir=run_dir / f"current-{idx}",
            freqaimodel=freqaimodel,
            backend=backend,
        )
        candidate_metrics = evaluate_strategy(
            strategy=strategy,
            strategy_path=strategy_path,
            config=config,
            timerange=window,
            run_dir=run_dir / f"candidate-{idx}",
            freqaimodel=freqaimodel,
            backend=backend,
        )
        current_summary = auto_optimize.summarize(current_metrics)
        candidate_summary = auto_optimize.summarize(candidate_metrics)
        current_score = strategy_score(current_metrics)
        candidate_score = strategy_score(candidate_metrics)
        cumulative_current_score += current_score
        cumulative_candidate_score += candidate_score
        cumulative_current_profit_abs += current_metrics.profit_total_abs
        cumulative_candidate_profit_abs += candidate_metrics.profit_total_abs
        is_informative = current_metrics.trades > 0 or candidate_metrics.trades > 0
        if is_informative:
            informative += 1
        drawdown_worsen = (
            candidate_metrics.max_drawdown_account - current_metrics.max_drawdown_account
        )
        passed = (
            is_informative
            and candidate_metrics.profit_total_pct > 0
            and candidate_score - current_score >= min_score_delta
            and drawdown_worsen <= max_drawdown_worsen
        )
        if passed:
            passing += 1
        results.append(
            {
                "timerange": window,
                "current": current_summary,
                "candidate": candidate_summary,
                "informative": is_informative,
                "passed": passed,
            }
        )

    return {
        "slices": results,
        "passing_slices": passing,
        "total_slices": len(windows),
        "informative_slices": informative,
        "cumulative_current_score": round(cumulative_current_score, 4),
        "cumulative_candidate_score": round(cumulative_candidate_score, 4),
        "cumulative_current_profit_abs": round(cumulative_current_profit_abs, 8),
        "cumulative_candidate_profit_abs": round(cumulative_candidate_profit_abs, 8),
    }


def passes_train_gate(
    *,
    current_metrics: auto_optimize.RunMetrics,
    candidate_metrics: auto_optimize.RunMetrics,
    min_trades: int,
    min_profit_pct: float,
    min_score_delta: float,
    max_drawdown: float,
    max_drawdown_worsen: float,
) -> tuple[bool, str]:
    if candidate_metrics.trades < min_trades:
        return False, f"trades {candidate_metrics.trades} below minimum {min_trades}"
    if candidate_metrics.profit_total_pct <= min_profit_pct:
        return False, f"profit_total_pct {candidate_metrics.profit_total_pct:.4f} not positive"
    if candidate_metrics.max_drawdown_account > max_drawdown:
        return False, f"drawdown {candidate_metrics.max_drawdown_account:.4f} above {max_drawdown}"

    current_score = strategy_score(current_metrics)
    candidate_score = strategy_score(candidate_metrics)
    if candidate_score - current_score < min_score_delta:
        return False, f"score delta {candidate_score - current_score:.4f} below {min_score_delta}"
    drawdown_worsen = candidate_metrics.max_drawdown_account - current_metrics.max_drawdown_account
    if drawdown_worsen > max_drawdown_worsen:
        return False, f"drawdown worsened by {drawdown_worsen:.4f}"
    return True, "passed train gate"


def select_best(evaluations: Sequence[StrategyEvaluation]) -> StrategyEvaluation:
    return max(evaluations, key=lambda item: (item.passed, item.score, item.train["profit_total_pct"]))


def select_winner(
    evaluations: Sequence[StrategyEvaluation], current_strategy: str
) -> StrategyEvaluation:
    current = next((item for item in evaluations if item.strategy == current_strategy), None)
    passed = [item for item in evaluations if item.passed and item.strategy != current_strategy]
    if not passed:
        if current is not None:
            return current
        return select_best(evaluations)
    return select_best(passed)


def apply_strategy(config_path: Path, strategy: str) -> None:
    config = load_config(config_path)
    config["strategy"] = strategy
    config["strategy_path"] = "user_data/strategies/"
    write_config(config_path, config)


def append_selection_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    config = load_config(config_path)
    current_strategy = active_strategy(config, args.strategies[0])

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = AUTOOPT_DIR / "strategy-selector" / run_id
    logs_dir = run_dir / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    train_timerange, confirm_timerange = auto_optimize.build_walk_forward_timeranges(
        args.backtest_days, args.confirm_days
    )
    _, final_confirm_timerange = auto_optimize.build_walk_forward_timeranges(
        args.backtest_days, args.final_confirm_days
    )

    current_train = evaluate_strategy(
        strategy=current_strategy,
        strategy_path=strategy_path,
        config=config_path,
        timerange=train_timerange,
        run_dir=run_dir / "current-train",
        freqaimodel=args.freqaimodel,
        backend=args.backend,
    )

    evaluations: list[StrategyEvaluation] = []
    for strategy in dict.fromkeys([current_strategy, *args.strategies]):
        strategy_file = strategy_path / f"{strategy}.py"
        if not strategy_file.is_file():
            evaluations.append(
                StrategyEvaluation(
                    strategy=strategy,
                    train={},
                    score=-999999.0,
                    passed=False,
                    reason=f"strategy file not found: {strategy_file}",
                )
            )
            continue

        train_metrics = (
            current_train
            if strategy == current_strategy
            else evaluate_strategy(
                strategy=strategy,
                strategy_path=strategy_path,
                config=config_path,
                timerange=train_timerange,
                run_dir=run_dir / f"{strategy}-train",
                freqaimodel=args.freqaimodel,
                backend=args.backend,
            )
        )
        train_summary = auto_optimize.summarize(train_metrics)
        passed, reason = passes_train_gate(
            current_metrics=current_train,
            candidate_metrics=train_metrics,
            min_trades=args.min_trades,
            min_profit_pct=args.min_profit_pct,
            min_score_delta=0.0 if strategy == current_strategy else args.min_score_delta,
            max_drawdown=args.max_drawdown,
            max_drawdown_worsen=args.max_drawdown_worsen,
        )

        confirmation = None
        final_confirmation = None
        if passed and strategy != current_strategy and args.confirm_days > 0:
            confirmation = evaluate_slices(
                strategy=strategy,
                current_strategy=current_strategy,
                strategy_path=strategy_path,
                config=config_path,
                timerange=confirm_timerange,
                slices=args.confirm_slices,
                run_dir=run_dir / f"{strategy}-confirm",
                freqaimodel=args.freqaimodel,
                backend=args.backend,
                min_score_delta=args.min_score_delta,
                max_drawdown_worsen=args.max_drawdown_worsen,
            )
            required = max(1, min(args.confirm_slices, confirmation["total_slices"]))
            cumulative_score_delta = (
                confirmation["cumulative_candidate_score"]
                - confirmation["cumulative_current_score"]
            )
            cumulative_profit_delta = (
                confirmation["cumulative_candidate_profit_abs"]
                - confirmation["cumulative_current_profit_abs"]
            )
            if (
                confirmation["informative_slices"] == 0
                or confirmation["passing_slices"] < required
                or cumulative_score_delta < args.min_score_delta
                or cumulative_profit_delta <= 0
            ):
                passed = False
                reason = "failed confirm gate"

        if passed and strategy != current_strategy and args.final_confirm_days > 0:
            final_confirmation = evaluate_slices(
                strategy=strategy,
                current_strategy=current_strategy,
                strategy_path=strategy_path,
                config=config_path,
                timerange=final_confirm_timerange,
                slices=1,
                run_dir=run_dir / f"{strategy}-final-confirm",
                freqaimodel=args.freqaimodel,
                backend=args.backend,
                min_score_delta=0.0,
                max_drawdown_worsen=args.max_drawdown_worsen,
            )
            final_profit_delta = (
                final_confirmation["cumulative_candidate_profit_abs"]
                - final_confirmation["cumulative_current_profit_abs"]
            )
            if (
                final_confirmation["informative_slices"] == 0
                or final_confirmation["passing_slices"] < 1
                or final_profit_delta <= 0
            ):
                passed = False
                reason = "failed final confirm gate"

        evaluations.append(
            StrategyEvaluation(
                strategy=strategy,
                train=train_summary,
                confirm=confirmation,
                final_confirm=final_confirmation,
                score=strategy_score(train_metrics),
                passed=passed,
                reason=reason,
            )
        )

    winner = select_winner(evaluations, current_strategy)
    switched = winner.strategy != current_strategy and winner.passed
    if switched and args.apply:
        apply_strategy(config_path, winner.strategy)
        if args.restart_bot:
            auto_optimize.restart_target(args.backend, logs_dir / "restart.log")

    summary = {
        "record_type": "strategy_selection_run",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "current_strategy": current_strategy,
        "selected_strategy": winner.strategy,
        "switched": switched,
        "applied": bool(switched and args.apply),
        "restarted": bool(switched and args.apply and args.restart_bot),
        "train_timerange": train_timerange,
        "confirm_timerange": confirm_timerange,
        "final_confirm_timerange": final_confirm_timerange,
        "evaluations": [asdict(item) for item in evaluations],
    }
    auto_optimize.write_summary(run_dir / "summary.json", summary)
    append_selection_record(SELECTION_LEDGER_PATH, summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
