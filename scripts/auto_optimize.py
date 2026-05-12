#!/usr/bin/env python3
"""
Rolling backtest + hyperopt promotion helper for Freqtrade.

This script keeps the trading engine unchanged. It works on a scratch copy of the
strategy, validates the current parameters on a recent timerange, runs hyperopt to
search for better settings, then optionally promotes the candidate parameter file
back to the active strategy only when it beats the baseline by a conservative margin.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import smtplib
import ssl
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for site_packages in Path("/home/ftuser/.local/lib").glob("python*/site-packages"):
    site_str = str(site_packages)
    if site_str not in sys.path:
        sys.path.insert(0, site_str)

from freqtrade.data.btanalysis.bt_fileutils import load_backtest_stats


ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
FTUSER_SITE_PACKAGES = next(Path("/home/ftuser/.local/lib").glob("python*/site-packages"), None)


@dataclass
class RunMetrics:
    trades: int
    profit_total_pct: float
    profit_total_abs: float
    max_drawdown_account: float
    profit_factor: float
    total_volume: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling backtest + auto hyperopt promoter.")
    parser.add_argument("--backend", choices=["compose", "local"], default="compose")
    parser.add_argument("--market-profile", default="balanced")
    parser.add_argument("--advisor-payload", default="")
    parser.add_argument("--strategy", default="SampleStrategy")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--strategy-path", default=str(STRATEGY_DIR))
    parser.add_argument("--backtest-days", type=int, default=60)
    parser.add_argument("--confirm-days", type=int, default=15)
    parser.add_argument("--confirm-slices", type=int, default=3)
    parser.add_argument("--confirm-min-passing-slices", type=int, default=2)
    parser.add_argument("--confirm-min-cumulative-score-delta", type=float, default=0.01)
    parser.add_argument("--final-confirm-days", type=int, default=7)
    parser.add_argument("--final-confirm-slices", type=int, default=2)
    parser.add_argument("--final-confirm-min-passing-slices", type=int, default=1)
    parser.add_argument("--final-confirm-min-cumulative-score-delta", type=float, default=0.0)
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument(
        "--spaces",
        nargs="+",
        default=["buy", "sell", "roi", "stoploss", "trailing", "protection"],
    )
    parser.add_argument("--min-trades", type=int, default=2)
    parser.add_argument("--min-score-delta", type=float, default=0.01)
    parser.add_argument("--min-profit-delta-pct", type=float, default=0.01)
    parser.add_argument("--max-drawdown-worsen", type=float, default=0.02)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--job-workers", type=int, default=1)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--hyperopt-loss", default="ProfitDrawDownHyperOptLoss")
    parser.add_argument("--fallback-hyperopt-loss", default="SharpeHyperOptLossDaily")
    parser.add_argument("--fallback-epochs", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart-bot", action="store_true")
    return parser.parse_args()


def load_json_file(path: str) -> dict:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        return json.loads(file_path.read_text())
    except json.JSONDecodeError:
        return {}


def backend_path(path: Path, backend: str) -> str:
    if backend == "compose":
        return "/freqtrade/" + path.relative_to(ROOT).as_posix()
    return str(path)


def run_command(cmd: Sequence[str], log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    pythonpath_parts = [str(ROOT)]
    if FTUSER_SITE_PACKAGES is not None:
        pythonpath_parts.append(str(FTUSER_SITE_PACKAGES))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = ":".join(pythonpath_parts)
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env)
    log_file.write_text(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-40:])
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{tail}")


def build_command(subcommand: str, extra: Sequence[str], backend: str) -> list[str]:
    if backend == "compose":
        return [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "freqtrade",
            subcommand,
            *extra,
        ]
    return [sys.executable, "-m", "freqtrade", subcommand, *extra]


def build_timerange(start: datetime, end: datetime) -> str:
    return f"{start.date():%Y%m%d}-{end.date():%Y%m%d}"


def build_walk_forward_timeranges(backtest_days: int, confirm_days: int) -> tuple[str, str]:
    today = datetime.now(UTC).date()
    end = datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    confirm_days = max(confirm_days, 0)
    if confirm_days == 0:
        start = end - timedelta(days=backtest_days)
        timerange = build_timerange(start, end)
        return timerange, timerange

    confirm_start = end - timedelta(days=confirm_days)
    train_end = confirm_start - timedelta(days=1)
    train_start = train_end - timedelta(days=backtest_days)
    return build_timerange(train_start, train_end), build_timerange(confirm_start, end)


def build_confirmation_slices(confirm_timerange: str, slices: int) -> list[str]:
    if slices <= 1:
        return [confirm_timerange]

    start_s, end_s = confirm_timerange.split("-")
    start = datetime.strptime(start_s, "%Y%m%d").date()
    end = datetime.strptime(end_s, "%Y%m%d").date()
    total_days = (end - start).days + 1
    slice_count = max(1, min(slices, total_days))
    base, remainder = divmod(total_days, slice_count)

    windows: list[str] = []
    cursor = start
    for idx in range(slice_count):
        duration = base + (1 if idx < remainder else 0)
        window_end = cursor + timedelta(days=duration - 1)
        windows.append(f"{cursor:%Y%m%d}-{window_end:%Y%m%d}")
        cursor = window_end + timedelta(days=1)
    return windows


def evaluate_confirmation_pass(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    confirm_timerange: str,
    confirm_slices: int,
    max_drawdown_worsen: float,
    backend: str,
    freqaimodel: str,
    scratch_strategy_dir: Path,
    run_dir: Path,
    logs_dir: Path,
) -> tuple[dict, int, int]:
    slices = build_confirmation_slices(confirm_timerange, confirm_slices)
    slice_results: list[dict] = []
    passing_slices = 0
    informative_slices = 0
    cumulative_baseline_score = 0.0
    cumulative_candidate_score = 0.0
    cumulative_baseline_profit_abs = 0.0
    cumulative_candidate_profit_abs = 0.0

    for idx, slice_timerange in enumerate(slices, start=1):
        confirm_baseline_metrics, _ = backtest(
            strategy=strategy,
            strategy_path=scratch_strategy_dir,
            config=config,
            timerange=slice_timerange,
            backtest_dir=run_dir / f"confirm-baseline-{idx}",
            freqaimodel=freqaimodel,
            logfile=logs_dir / f"confirm-baseline-{idx}.log",
            backend=backend,
        )
        confirm_candidate_metrics, _ = backtest(
            strategy=strategy,
            strategy_path=scratch_strategy_dir,
            config=config,
            timerange=slice_timerange,
            backtest_dir=run_dir / f"confirm-candidate-{idx}",
            freqaimodel=freqaimodel,
            logfile=logs_dir / f"confirm-candidate-{idx}.log",
            backend=backend,
        )
        baseline_summary = summarize(confirm_baseline_metrics)
        candidate_summary = summarize(confirm_candidate_metrics)
        cumulative_baseline_score += float(baseline_summary["score"])
        cumulative_candidate_score += float(candidate_summary["score"])
        cumulative_baseline_profit_abs += float(baseline_summary["profit_total_abs"])
        cumulative_candidate_profit_abs += float(candidate_summary["profit_total_abs"])
        is_informative = (
            confirm_baseline_metrics.trades > 0 or confirm_candidate_metrics.trades > 0
        )
        if is_informative:
            informative_slices += 1
        passed = False
        if is_informative:
            passed = better_than(
                confirm_baseline_metrics,
                confirm_candidate_metrics,
                min_score_delta=0.0,
                min_profit_delta_pct=0.0,
                max_drawdown_worsen=max_drawdown_worsen,
                min_trades=1,
            )
            if passed:
                passing_slices += 1
        slice_results.append(
            {
                "timerange": slice_timerange,
                "baseline": baseline_summary,
                "candidate": candidate_summary,
                "informative": is_informative,
                "passed": passed,
            }
        )

    return (
        {
            "slices": slice_results,
            "passing_slices": passing_slices,
            "total_slices": len(slices),
            "informative_slices": informative_slices,
            "cumulative_baseline_score": round(cumulative_baseline_score, 4),
            "cumulative_candidate_score": round(cumulative_candidate_score, 4),
            "cumulative_baseline_profit_abs": round(cumulative_baseline_profit_abs, 8),
            "cumulative_candidate_profit_abs": round(cumulative_candidate_profit_abs, 8),
        },
        passing_slices,
        informative_slices,
    )


def extract_metrics(stats: dict, strategy: str) -> RunMetrics:
    comparison = stats["strategy_comparison"]
    if not comparison:
        raise RuntimeError("Backtest did not return strategy comparison data.")

    row = comparison[0]
    return RunMetrics(
        trades=int(row.get("trades", 0)),
        profit_total_pct=float(row.get("profit_total_pct", 0.0)),
        profit_total_abs=float(row.get("profit_total_abs", 0.0)),
        max_drawdown_account=float(row.get("max_drawdown_account", 0.0)),
        profit_factor=float(row.get("profit_factor", 0.0)),
        total_volume=float(stats["strategy"][strategy].get("total_volume", 0.0)),
    )


def summarize(metrics: RunMetrics) -> dict:
    data = asdict(metrics)
    data["score"] = round(metrics.profit_total_pct - metrics.max_drawdown_account * 100.0, 4)
    return data


def backtest(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    backtest_dir: Path,
    freqaimodel: str,
    logfile: Path,
    backend: str,
) -> tuple[RunMetrics, Path]:
    backtest_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(
        "backtesting",
        [
            "-c",
            backend_path(config, backend),
            "--userdir",
            backend_path(USER_DATA, backend),
            "--strategy",
            strategy,
            "--strategy-path",
            backend_path(strategy_path, backend),
            "--freqaimodel",
            freqaimodel,
            "--timerange",
            timerange,
            "--enable-protections",
            "--cache",
            "none",
            "--backtest-directory",
            backend_path(backtest_dir, backend),
            "--logfile",
            backend_path(logfile, backend),
            "--no-color",
        ],
        backend,
    )
    run_command(cmd, logfile)
    stats = load_backtest_stats(backtest_dir)
    return extract_metrics(stats, strategy), backtest_dir


def hyperopt(
    *,
    strategy: str,
    strategy_path: Path,
    config: Path,
    timerange: str,
    epochs: int,
    spaces: Sequence[str],
    freqaimodel: str,
    hyperopt_loss: str,
    logfile: Path,
    random_state: int,
    job_workers: int,
    backend: str,
) -> None:
    cmd = build_command(
        "hyperopt",
        [
            "-c",
            backend_path(config, backend),
            "--userdir",
            backend_path(USER_DATA, backend),
            "--strategy",
            strategy,
            "--strategy-path",
            backend_path(strategy_path, backend),
            "--freqaimodel",
            freqaimodel,
            "--hyperopt-loss",
            hyperopt_loss,
            "--timerange",
            timerange,
            "--enable-protections",
            "--ignore-missing-spaces",
            "--spaces",
            *spaces,
            "-e",
            str(epochs),
            "-j",
            str(job_workers),
            "--random-state",
            str(random_state),
            "--logfile",
            backend_path(logfile, backend),
            "--no-color",
        ],
        backend,
    )
    run_command(cmd, logfile)


def promote_candidate(
    *,
    strategy: str,
    scratch_strategy_dir: Path,
    active_strategy_dir: Path,
) -> Path:
    candidate_json = scratch_strategy_dir / f"{strategy}.json"
    if not candidate_json.is_file():
        raise FileNotFoundError(f"Candidate parameter file not found: {candidate_json}")

    active_json = active_strategy_dir / f"{strategy}.json"
    archive_dir = AUTOOPT_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    if active_json.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = archive_dir / f"{strategy}-{stamp}.json"
        shutil.copy2(active_json, backup)

    shutil.copy2(candidate_json, active_json)
    return active_json


def write_summary(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def send_email(subject: str, body: str) -> bool:
    host = os.getenv("FT_EMAIL_HOST", "smtp.qq.com")
    port = int(os.getenv("FT_EMAIL_PORT", "465"))
    username = os.getenv("FT_EMAIL_USER", "")
    password = os.getenv("FT_EMAIL_PASSWORD", "")
    sender = os.getenv("FT_EMAIL_FROM", username)
    recipient = os.getenv("FT_EMAIL_TO", "746439274@qq.com")

    if not host or not username or not password or not sender or not recipient:
        return False

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
        server.login(username, password)
        server.send_message(message)
    return True


def build_email_summary(summary: dict) -> str:
    baseline = summary.get("baseline", {}) or {}
    candidate = summary.get("candidate", {}) or {}
    advisor = summary.get("advisor", {}) or {}
    lines = [
        f"strategy: {summary.get('strategy')}",
        f"run_id: {summary.get('run_id')}",
        f"market_profile: {summary.get('market_profile', 'balanced')}",
        f"train_timerange: {summary.get('train_timerange')}",
        f"confirm_timerange: {summary.get('confirm_timerange', '')}",
        "",
        "baseline:",
        f"  trades: {baseline.get('trades')}",
        f"  profit_total_pct: {baseline.get('profit_total_pct')}",
        f"  max_drawdown_account: {baseline.get('max_drawdown_account')}",
        "",
        "candidate:",
    ]
    if candidate:
        lines.extend(
            [
                f"  trades: {candidate.get('trades')}",
                f"  profit_total_pct: {candidate.get('profit_total_pct')}",
                f"  max_drawdown_account: {candidate.get('max_drawdown_account')}",
                f"  score: {candidate.get('score')}",
            ]
        )
    else:
        lines.append("  not generated")
    if advisor:
        suggested_changes = advisor.get("suggested_changes", []) or []
        applied_overrides = advisor.get("applied_overrides", {}) or {}
        lines.extend(
            [
                "",
                "advisor:",
                f"  profile: {advisor.get('profile', 'balanced')}",
                f"  confidence: {advisor.get('confidence', '')}",
                f"  reason: {advisor.get('reason', '')}",
            ]
        )
        if applied_overrides:
            lines.append("  applied_overrides:")
            for key in sorted(applied_overrides):
                lines.append(f"    {key}: {applied_overrides[key]}")
        if suggested_changes:
            lines.append("  suggested_changes:")
            for item in suggested_changes[:8]:
                lines.append(f"    - {item}")
    fallback_candidate = summary.get("fallback_candidate", {}) or {}
    if fallback_candidate:
        lines.extend(
            [
                "",
                "fallback_candidate:",
                f"  trades: {fallback_candidate.get('trades')}",
                f"  profit_total_pct: {fallback_candidate.get('profit_total_pct')}",
                f"  max_drawdown_account: {fallback_candidate.get('max_drawdown_account')}",
                f"  score: {fallback_candidate.get('score')}",
            ]
        )
    confirmation = summary.get("confirmation", {}) or {}
    if confirmation:
        confirm_baseline = confirmation.get("baseline", {}) or {}
        confirm_candidate = confirmation.get("candidate", {}) or {}
        lines.extend(
            [
                "",
                "confirmation:",
                "  baseline:",
                f"    trades: {confirm_baseline.get('trades')}",
                f"    profit_total_pct: {confirm_baseline.get('profit_total_pct')}",
                f"    max_drawdown_account: {confirm_baseline.get('max_drawdown_account')}",
                "  candidate:",
                f"    trades: {confirm_candidate.get('trades')}",
                f"    profit_total_pct: {confirm_candidate.get('profit_total_pct')}",
                f"    max_drawdown_account: {confirm_candidate.get('max_drawdown_account')}",
                f"    score: {confirm_candidate.get('score')}",
                f"  passing_slices: {confirmation.get('passing_slices')}",
                f"  total_slices: {confirmation.get('total_slices')}",
                f"  cumulative_baseline_score: {confirmation.get('cumulative_baseline_score')}",
                f"  cumulative_candidate_score: {confirmation.get('cumulative_candidate_score')}",
                f"  cumulative_baseline_profit_abs: {confirmation.get('cumulative_baseline_profit_abs')}",
                f"  cumulative_candidate_profit_abs: {confirmation.get('cumulative_candidate_profit_abs')}",
            ]
        )
    final_confirmation = summary.get("final_confirmation", {}) or {}
    if final_confirmation:
        final_baseline = final_confirmation.get("baseline", {}) or {}
        final_candidate = final_confirmation.get("candidate", {}) or {}
        lines.extend(
            [
                "",
                "final_confirmation:",
                "  baseline:",
                f"    trades: {final_baseline.get('trades')}",
                f"    profit_total_pct: {final_baseline.get('profit_total_pct')}",
                f"    max_drawdown_account: {final_baseline.get('max_drawdown_account')}",
                "  candidate:",
                f"    trades: {final_candidate.get('trades')}",
                f"    profit_total_pct: {final_candidate.get('profit_total_pct')}",
                f"    max_drawdown_account: {final_candidate.get('max_drawdown_account')}",
                f"    score: {final_candidate.get('score')}",
                f"  passing_slices: {final_confirmation.get('passing_slices')}",
                f"  total_slices: {final_confirmation.get('total_slices')}",
                f"  cumulative_baseline_score: {final_confirmation.get('cumulative_baseline_score')}",
                f"  cumulative_candidate_score: {final_confirmation.get('cumulative_candidate_score')}",
                f"  cumulative_baseline_profit_abs: {final_confirmation.get('cumulative_baseline_profit_abs')}",
                f"  cumulative_candidate_profit_abs: {final_confirmation.get('cumulative_candidate_profit_abs')}",
            ]
        )
    lines.extend(
        [
            "",
            f"promoted: {summary.get('promoted')}",
            f"would_promote: {summary.get('would_promote', False)}",
            f"reason: {summary.get('reason', '')}",
        ]
    )
    return "\n".join(lines)


def better_than(
    baseline: RunMetrics,
    candidate: RunMetrics,
    min_score_delta: float,
    min_profit_delta_pct: float,
    max_drawdown_worsen: float,
    min_trades: int,
) -> bool:
    if candidate.trades < min_trades:
        return False
    if candidate.profit_total_pct <= 0:
        return False
    if candidate.profit_factor <= 1.0 and candidate.trades > 5:
        return False

    profit_delta = candidate.profit_total_pct - baseline.profit_total_pct
    drawdown_delta = candidate.max_drawdown_account - baseline.max_drawdown_account
    baseline_score = baseline.profit_total_pct - baseline.max_drawdown_account * 100.0
    candidate_score = candidate.profit_total_pct - candidate.max_drawdown_account * 100.0

    if drawdown_delta > max_drawdown_worsen:
        return False
    if candidate_score - baseline_score <= min_score_delta:
        return False
    if profit_delta <= min_profit_delta_pct:
        return False
    return True


def finalize_run(run_dir: Path, summary: dict, *, email_subject_prefix: str) -> None:
    write_summary(run_dir / "summary.json", summary)
    try:
        subject = f"{email_subject_prefix} {summary.get('strategy')} {summary.get('run_id')}"
        send_email(subject, build_email_summary(summary))
    except Exception:
        # Email is auxiliary only; never fail the optimization flow because of SMTP.
        pass


def restart_target(backend: str, log_file: Path) -> None:
    if backend == "compose":
        run_command(["docker", "compose", "restart", "freqtrade"], log_file)
        return
    run_command(["docker", "restart", "freqtrade"], log_file)


def main() -> int:
    args = parse_args()

    strategy = args.strategy
    active_strategy_dir = Path(args.strategy_path).resolve()
    active_strategy_py = active_strategy_dir / f"{strategy}.py"
    active_strategy_json = active_strategy_dir / f"{strategy}.json"
    if not active_strategy_py.is_file():
        raise FileNotFoundError(f"Strategy file not found: {active_strategy_py}")

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = AUTOOPT_DIR / run_id
    scratch_strategy_dir = run_dir / "strategies"
    baseline_dir = run_dir / "baseline"
    candidate_dir = run_dir / "candidate"
    logs_dir = run_dir / "logs"
    scratch_strategy_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(active_strategy_py, scratch_strategy_dir / active_strategy_py.name)
    if active_strategy_json.is_file():
        shutil.copy2(active_strategy_json, scratch_strategy_dir / active_strategy_json.name)

    train_timerange, confirm_timerange = build_walk_forward_timeranges(
        args.backtest_days, args.confirm_days
    )

    baseline_metrics, _ = backtest(
        strategy=strategy,
        strategy_path=scratch_strategy_dir,
        config=Path(args.config).resolve(),
        timerange=train_timerange,
        backtest_dir=baseline_dir,
        freqaimodel=args.freqaimodel,
        logfile=logs_dir / "baseline-backtest.log",
        backend=args.backend,
    )

    summary: dict[str, object] = {
        "run_id": run_id,
        "strategy": strategy,
        "market_profile": args.market_profile,
        "advisor_profile": args.market_profile,
        "advisor_confidence": None,
        "advisor_reason": "",
        "applied_overrides": {},
        "train_timerange": train_timerange,
        "confirm_timerange": confirm_timerange if args.confirm_days > 0 else "",
        "baseline": summarize(baseline_metrics),
        "candidate": None,
        "fallback_candidate": None,
        "confirmation": None,
        "final_confirmation": None,
        "promoted": False,
        "active_parameter_file": str(active_strategy_json),
    }
    advisor_payload = load_json_file(args.advisor_payload)
    if advisor_payload:
        summary["advisor"] = advisor_payload
        summary["advisor_profile"] = advisor_payload.get("profile", args.market_profile)
        summary["advisor_confidence"] = advisor_payload.get("confidence")
        summary["advisor_reason"] = advisor_payload.get("reason", "")
        summary["applied_overrides"] = advisor_payload.get("applied_overrides", {}) or {}

    if baseline_metrics.trades < args.min_trades:
        summary["reason"] = (
            f"baseline trades {baseline_metrics.trades} below minimum {args.min_trades}"
        )
        finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    hyperopt(
        strategy=strategy,
        strategy_path=scratch_strategy_dir,
        config=Path(args.config).resolve(),
        timerange=train_timerange,
        epochs=args.epochs,
        spaces=args.spaces,
        freqaimodel=args.freqaimodel,
        hyperopt_loss=args.hyperopt_loss,
        logfile=logs_dir / "hyperopt.log",
        random_state=args.random_state,
        job_workers=args.job_workers,
        backend=args.backend,
    )

    candidate_metrics, _ = backtest(
        strategy=strategy,
        strategy_path=scratch_strategy_dir,
        config=Path(args.config).resolve(),
        timerange=train_timerange,
        backtest_dir=candidate_dir,
        freqaimodel=args.freqaimodel,
        logfile=logs_dir / "candidate-backtest.log",
        backend=args.backend,
    )
    summary["candidate"] = summarize(candidate_metrics)

    if not better_than(
        baseline_metrics,
        candidate_metrics,
        min_score_delta=args.min_score_delta,
        min_profit_delta_pct=args.min_profit_delta_pct,
        max_drawdown_worsen=args.max_drawdown_worsen,
        min_trades=args.min_trades,
    ):
        hyperopt(
            strategy=strategy,
            strategy_path=scratch_strategy_dir,
            config=Path(args.config).resolve(),
            timerange=train_timerange,
            epochs=args.fallback_epochs,
            spaces=args.spaces,
            freqaimodel=args.freqaimodel,
            hyperopt_loss=args.fallback_hyperopt_loss,
            logfile=logs_dir / "fallback-hyperopt.log",
            random_state=args.random_state + 1,
            job_workers=args.job_workers,
            backend=args.backend,
        )

        fallback_candidate_metrics, _ = backtest(
            strategy=strategy,
            strategy_path=scratch_strategy_dir,
            config=Path(args.config).resolve(),
            timerange=train_timerange,
            backtest_dir=run_dir / "fallback-candidate",
            freqaimodel=args.freqaimodel,
            logfile=logs_dir / "fallback-candidate-backtest.log",
            backend=args.backend,
        )
        summary["fallback_candidate"] = summarize(fallback_candidate_metrics)

        if better_than(
            baseline_metrics,
            fallback_candidate_metrics,
            min_score_delta=args.min_score_delta,
            min_profit_delta_pct=args.min_profit_delta_pct,
            max_drawdown_worsen=args.max_drawdown_worsen,
            min_trades=args.min_trades,
        ):
            candidate_metrics = fallback_candidate_metrics
            summary["candidate"] = summarize(candidate_metrics)
        else:
            summary["reason"] = (
                "fallback candidate did not beat baseline under the configured gate "
                f"(score_delta>={args.min_score_delta}, "
                f"profit_delta>={args.min_profit_delta_pct}, "
                f"drawdown_worsen<={args.max_drawdown_worsen})"
            )
            finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
            print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
            return 0

    if better_than(
        baseline_metrics,
        candidate_metrics,
        min_score_delta=args.min_score_delta,
        min_profit_delta_pct=args.min_profit_delta_pct,
        max_drawdown_worsen=args.max_drawdown_worsen,
        min_trades=args.min_trades,
    ):
        if args.confirm_days > 0:
            confirm_attempts: list[int] = []
            confirm_attempts.append(max(args.confirm_days, 1))
            if args.confirm_days < 30:
                confirm_attempts.append(min(args.backtest_days, 30))

            confirmation_summary: dict | None = None
            passing_slices = 0
            informative_slices = 0
            used_confirm_days = confirm_attempts[0]

            for attempt_days in dict.fromkeys(confirm_attempts):
                _, attempt_confirm_timerange = build_walk_forward_timeranges(
                    args.backtest_days, attempt_days
                )
                used_confirm_days = attempt_days
                confirmation_summary, passing_slices, informative_slices = evaluate_confirmation_pass(
                    strategy=strategy,
                    strategy_path=scratch_strategy_dir,
                    config=Path(args.config).resolve(),
                    confirm_timerange=attempt_confirm_timerange,
                    confirm_slices=args.confirm_slices,
                    max_drawdown_worsen=args.max_drawdown_worsen,
                    backend=args.backend,
                    freqaimodel=args.freqaimodel,
                    scratch_strategy_dir=scratch_strategy_dir,
                    run_dir=run_dir,
                    logs_dir=logs_dir,
                )
                if informative_slices > 0:
                    confirm_timerange = attempt_confirm_timerange
                    break

            assert confirmation_summary is not None
            summary["train_timerange"] = train_timerange
            summary["confirm_timerange"] = confirm_timerange
            summary["confirmation"] = {
                **confirmation_summary,
                "used_confirm_days": used_confirm_days,
            }

            required = min(
                max(args.confirm_min_passing_slices, 1),
                max(confirmation_summary["total_slices"], 1),
            )
            cumulative_score_delta = (
                float(confirmation_summary["cumulative_candidate_score"])
                - float(confirmation_summary["cumulative_baseline_score"])
            )
            cumulative_profit_delta_abs = (
                float(confirmation_summary["cumulative_candidate_profit_abs"])
                - float(confirmation_summary["cumulative_baseline_profit_abs"])
            )
            if informative_slices == 0 or passing_slices < required:
                summary["reason"] = (
                    "candidate failed sample-out confirmation gate "
                    f"({passing_slices}/{confirmation_summary['total_slices']} slices passed, "
                    f"{informative_slices} informative, used_confirm_days={used_confirm_days})"
                )
                finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
                print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
                return 0
            if cumulative_score_delta < args.confirm_min_cumulative_score_delta or cumulative_profit_delta_abs <= 0:
                summary["reason"] = (
                    "candidate failed cumulative confirmation gate "
                    f"(score_delta={cumulative_score_delta:.4f}, "
                    f"profit_abs_delta={cumulative_profit_delta_abs:.8f}, "
                    f"used_confirm_days={used_confirm_days})"
                )
                finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
                print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
                return 0

        if args.final_confirm_days > 0:
            _, final_confirm_timerange = build_walk_forward_timeranges(
                args.backtest_days, args.final_confirm_days
            )
            final_confirmation_summary, final_passing_slices, final_informative_slices = (
                evaluate_confirmation_pass(
                    strategy=strategy,
                    strategy_path=scratch_strategy_dir,
                    config=Path(args.config).resolve(),
                    confirm_timerange=final_confirm_timerange,
                    confirm_slices=args.final_confirm_slices,
                    max_drawdown_worsen=args.max_drawdown_worsen,
                    backend=args.backend,
                    freqaimodel=args.freqaimodel,
                    scratch_strategy_dir=scratch_strategy_dir,
                    run_dir=run_dir,
                    logs_dir=logs_dir,
                )
            )
            summary["final_confirmation"] = {
                **final_confirmation_summary,
                "used_confirm_days": args.final_confirm_days,
            }
            final_required = min(
                max(args.final_confirm_min_passing_slices, 1),
                max(final_confirmation_summary["total_slices"], 1),
            )
            final_cumulative_score_delta = (
                float(final_confirmation_summary["cumulative_candidate_score"])
                - float(final_confirmation_summary["cumulative_baseline_score"])
            )
            final_cumulative_profit_delta_abs = (
                float(final_confirmation_summary["cumulative_candidate_profit_abs"])
                - float(final_confirmation_summary["cumulative_baseline_profit_abs"])
            )
            summary["final_confirm_timerange"] = final_confirm_timerange
            if (
                final_informative_slices == 0
                or final_passing_slices < final_required
                or final_cumulative_score_delta < args.final_confirm_min_cumulative_score_delta
                or final_cumulative_profit_delta_abs <= 0
            ):
                summary["reason"] = (
                    "candidate failed final confirmation gate "
                    f"({final_passing_slices}/{final_confirmation_summary['total_slices']} slices passed, "
                    f"{final_informative_slices} informative, "
                    f"score_delta={final_cumulative_score_delta:.4f}, "
                    f"profit_abs_delta={final_cumulative_profit_delta_abs:.8f})"
                )
                finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
                print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
                return 0

        summary["would_promote"] = True
        if args.apply:
            promoted = promote_candidate(
                strategy=strategy,
                scratch_strategy_dir=scratch_strategy_dir,
                active_strategy_dir=active_strategy_dir,
            )
            summary["promoted"] = True
            summary["promoted_file"] = str(promoted)
            if args.restart_bot:
                restart_target(args.backend, logs_dir / "restart.log")
                summary["restarted"] = True
            else:
                summary["restarted"] = False
        else:
            summary["promoted"] = False
            summary["restarted"] = False
    else:
        summary["reason"] = (
            "candidate did not beat baseline under the configured gate "
            f"(score_delta>={args.min_score_delta}, "
            f"profit_delta>={args.min_profit_delta_pct}, "
            f"drawdown_worsen<={args.max_drawdown_worsen})"
        )

    finalize_run(run_dir, summary, email_subject_prefix="[AutoOpt]")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
