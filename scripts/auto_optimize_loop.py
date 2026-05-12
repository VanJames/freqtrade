#!/usr/bin/env python3
"""
Run auto_optimize.py on a fixed interval.

This is intentionally thin: it shells out to the single-run optimizer so the
promotion rules remain in one place.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
import random
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "auto_optimize.py"
SNAPSHOT_SCRIPT = ROOT / "scripts" / "build_market_snapshot.py"
ADVISOR_SCRIPT = ROOT / "scripts" / "openai_market_advisor.py"
SNAPSHOT_PATH = ROOT / "user_data" / "autoopt" / "_market_snapshot.json"
ADVISOR_PATH = ROOT / "user_data" / "autoopt" / "_market_advisor.json"

PROFILE_PRESETS = {
    "conservative": {
        "backtest_days": 90,
        "confirm_days": 30,
        "confirm_slices": 4,
        "confirm_min_passing_slices": 3,
        "confirm_min_cumulative_score_delta": 0.02,
        "final_confirm_days": 14,
        "final_confirm_slices": 3,
        "final_confirm_min_passing_slices": 2,
        "final_confirm_min_cumulative_score_delta": 0.01,
        "epochs": 80,
        "fallback_epochs": 40,
        "min_trades": 3,
        "min_score_delta": 0.02,
        "min_profit_delta_pct": 0.02,
        "max_drawdown_worsen": 0.015,
    },
    "balanced": {
        "backtest_days": 60,
        "confirm_days": 15,
        "confirm_slices": 3,
        "confirm_min_passing_slices": 2,
        "confirm_min_cumulative_score_delta": 0.01,
        "final_confirm_days": 7,
        "final_confirm_slices": 2,
        "final_confirm_min_passing_slices": 1,
        "final_confirm_min_cumulative_score_delta": 0.0,
        "epochs": 60,
        "fallback_epochs": 20,
        "min_trades": 2,
        "min_score_delta": 0.01,
        "min_profit_delta_pct": 0.01,
        "max_drawdown_worsen": 0.02,
    },
    "aggressive": {
        "backtest_days": 30,
        "confirm_days": 7,
        "confirm_slices": 2,
        "confirm_min_passing_slices": 1,
        "confirm_min_cumulative_score_delta": 0.0,
        "final_confirm_days": 3,
        "final_confirm_slices": 2,
        "final_confirm_min_passing_slices": 1,
        "final_confirm_min_cumulative_score_delta": 0.0,
        "epochs": 40,
        "fallback_epochs": 12,
        "min_trades": 2,
        "min_score_delta": 0.005,
        "min_profit_delta_pct": 0.005,
        "max_drawdown_worsen": 0.03,
    },
}


def infer_base_url(model: str, base_url: str | None = None) -> str:
    if base_url and base_url.strip():
        return base_url.strip()
    model_name = model.lower().strip()
    if model_name.startswith("deepseek"):
        return "https://api.deepseek.com"
    return "https://api.openai.com/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repeated auto optimization runner.")
    parser.add_argument("--interval-hours", type=float, default=24.0)
    parser.add_argument("--jitter-seconds", type=int, default=300)
    parser.add_argument("--runs", type=int, default=0, help="0 means run forever.")
    parser.add_argument("--backend", choices=["compose", "local"], default="compose")
    parser.add_argument("--use-openai-advisor", action="store_true")
    parser.add_argument("--advisor-model", default=os.getenv("OPENAI_MODEL", "gpt-5.5"))
    parser.add_argument(
        "--advisor-base-url",
        default=os.getenv("OPENAI_BASE_URL", ""),
    )
    parser.add_argument("--advisor-timeout", type=int, default=60)
    parser.add_argument("--advisor-snapshot-limit", type=int, default=220)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart-bot", action="store_true")
    parser.add_argument("--strategy", default="SampleStrategy")
    parser.add_argument("--config", default=str(ROOT / "user_data" / "config.json"))
    parser.add_argument("--strategy-path", default=str(ROOT / "user_data" / "strategies"))
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
    parser.add_argument("--min-trades", type=int, default=2)
    parser.add_argument("--min-score-delta", type=float, default=0.01)
    parser.add_argument("--min-profit-delta-pct", type=float, default=0.01)
    parser.add_argument("--max-drawdown-worsen", type=float, default=0.02)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--job-workers", type=int, default=1)
    parser.add_argument("--freqaimodel", default="LightGBMRegressor")
    parser.add_argument("--fallback-hyperopt-loss", default="SharpeHyperOptLossDaily")
    parser.add_argument("--fallback-epochs", type=int, default=20)
    parser.add_argument(
        "--spaces",
        nargs="+",
        default=["buy", "sell", "roi", "stoploss", "trailing", "protection"],
    )
    return parser.parse_args()


def profile_overrides(profile: str) -> dict[str, object]:
    return PROFILE_PRESETS.get(profile, PROFILE_PRESETS["balanced"]).copy()


def build_openai_snapshot(config: str, limit: int, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(SNAPSHOT_SCRIPT),
        "--config",
        config,
        "--limit",
        str(limit),
        "--output",
        str(output),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    return output


def advise_profile(
    *,
    config: str,
    snapshot_limit: int,
    model: str,
    base_url: str,
    timeout: int,
) -> tuple[str, dict]:
    build_openai_snapshot(config, snapshot_limit, SNAPSHOT_PATH)
    cmd = [
        sys.executable,
        str(ADVISOR_SCRIPT),
        "--snapshot",
        str(SNAPSHOT_PATH),
        "--model",
        model,
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
        "--output",
        str(ADVISOR_PATH),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or "") + (proc.stderr or ""))

    payload = json.loads(ADVISOR_PATH.read_text())
    profile = str(payload.get("profile", "balanced")).lower().strip()
    if profile not in PROFILE_PRESETS:
        profile = "balanced"
    payload["applied_overrides"] = profile_overrides(profile)
    payload["snapshot_path"] = str(SNAPSHOT_PATH)
    return profile, payload


def write_advisor_record(profile: str, payload: dict | None, *, enabled: bool) -> dict:
    record = dict(payload or {})
    record.setdefault("profile", profile)
    record.setdefault("confidence", None)
    if enabled:
        record.setdefault("reason", "")
        record.setdefault("suggested_changes", [])
        record["enabled"] = True
    else:
        record["enabled"] = False
        record.setdefault("reason", "OpenAI advisor disabled")
        record.setdefault("suggested_changes", [])
    record["applied_overrides"] = profile_overrides(profile)
    record["snapshot_path"] = str(SNAPSHOT_PATH)
    ADVISOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADVISOR_PATH.write_text(json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return record


def run_once(args: argparse.Namespace) -> int:
    effective = copy.copy(args)
    profile = "balanced"
    advisor_payload = None
    if getattr(args, "use_openai_advisor", False):
        try:
            profile, advisor_payload = advise_profile(
                config=args.config,
                snapshot_limit=args.advisor_snapshot_limit,
                model=args.advisor_model,
                base_url=infer_base_url(args.advisor_model, args.advisor_base_url),
                timeout=args.advisor_timeout,
            )
            overrides = profile_overrides(profile)
            for key, value in overrides.items():
                setattr(effective, key, value)
            setattr(effective, "market_profile", profile)
            advisor_payload = write_advisor_record(profile, advisor_payload, enabled=True)
        except Exception as exc:
            print(f"[advisor] fallback to balanced: {exc}")
            profile = "balanced"
            advisor_payload = write_advisor_record(
                profile,
                {"error": str(exc)},
                enabled=True,
            )
            setattr(effective, "market_profile", profile)
    else:
        setattr(effective, "market_profile", profile)
        advisor_payload = write_advisor_record(profile, None, enabled=False)

    cmd = [
        sys.executable,
        str(SCRIPT),
        "--strategy",
        args.strategy,
        "--config",
        args.config,
        "--strategy-path",
        args.strategy_path,
        "--backend",
        effective.backend,
        "--market-profile",
        effective.market_profile,
        "--advisor-payload",
        str(ADVISOR_PATH),
        "--backtest-days",
        str(effective.backtest_days),
        "--confirm-days",
        str(effective.confirm_days),
        "--confirm-slices",
        str(effective.confirm_slices),
        "--confirm-min-passing-slices",
        str(effective.confirm_min_passing_slices),
        "--confirm-min-cumulative-score-delta",
        str(effective.confirm_min_cumulative_score_delta),
        "--final-confirm-days",
        str(effective.final_confirm_days),
        "--final-confirm-slices",
        str(effective.final_confirm_slices),
        "--final-confirm-min-passing-slices",
        str(effective.final_confirm_min_passing_slices),
        "--final-confirm-min-cumulative-score-delta",
        str(effective.final_confirm_min_cumulative_score_delta),
        "--epochs",
        str(effective.epochs),
        "--min-trades",
        str(effective.min_trades),
        "--min-score-delta",
        str(effective.min_score_delta),
        "--min-profit-delta-pct",
        str(effective.min_profit_delta_pct),
        "--max-drawdown-worsen",
        str(effective.max_drawdown_worsen),
        "--random-state",
        str(effective.random_state),
        "--job-workers",
        str(effective.job_workers),
        "--freqaimodel",
        effective.freqaimodel,
        "--fallback-hyperopt-loss",
        effective.fallback_hyperopt_loss,
        "--fallback-epochs",
        str(effective.fallback_epochs),
        "--spaces",
        *effective.spaces,
    ]
    if effective.apply:
        cmd.append("--apply")
    if effective.restart_bot:
        cmd.append("--restart-bot")

    if advisor_payload is not None:
        print(f"[advisor] profile={profile}")
        print(json.dumps(advisor_payload, ensure_ascii=True, indent=2, sort_keys=True))

    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main() -> int:
    args = parse_args()
    if not args.use_openai_advisor and os.getenv("OPENAI_API_KEY", "").strip():
        args.use_openai_advisor = True
    interval = max(float(args.interval_hours), 0.1) * 3600.0
    jitter = max(int(args.jitter_seconds), 0)
    runs_left = int(args.runs)

    while True:
        ts = datetime.now().isoformat(timespec="seconds")
        print(f"[{ts}] starting auto-optimize run")
        code = run_once(args)
        print(f"[{ts}] auto-optimize finished with exit code {code}")

        if runs_left > 0:
            runs_left -= 1
            if runs_left == 0:
                return 0

        sleep_for = interval
        if jitter > 0:
            sleep_for += random.uniform(-jitter, jitter)
        if sleep_for > 0:
            time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
