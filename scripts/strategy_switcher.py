#!/usr/bin/env python3
"""
Safely switch the active Freqtrade strategy from the strategy registry.

The switcher is intentionally conservative:
- It normally switches only to the registry's recommended strategy.
- It can switch to an active fallback if the current strategy produced no
  recent trades while another reviewed strategy is positive and active.
- It requires enough passing windows, enough trades, and a score improvement
  unless the active fallback gates are met.
- It refuses to switch while there are open trades or open orders.
- It backs up config.json and writes an audit ledger before restarting Freqtrade.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_CONFIG = USER_DATA / "config.json"
DEFAULT_REGISTRY = AUTOOPT_DIR / "strategy_registry.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "strategy_switcher.jsonl"
DEFAULT_STATE = AUTOOPT_DIR / "strategy_switcher_state.json"
DEFAULT_DB = USER_DATA / "tradesv3.sqlite"
VALID_STRATEGIES = {
    "SampleStrategy",
    "SampleStrategyActive",
    "SampleStrategyScalp",
    "SampleStrategyPullbackShort",
    "SampleStrategyPullbackLong",
    "SampleStrategyLongOnly",
    "SampleStrategyShortOnly",
}


@dataclass
class SwitchDecision:
    should_switch: bool
    reason: str
    current_strategy: str
    target_strategy: str
    details: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Switch active Freqtrade strategy safely.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backend", choices=["docker", "compose"], default="docker")
    parser.add_argument("--container", default="freqtrade")
    parser.add_argument("--min-passed-windows", type=int, default=2)
    parser.add_argument("--min-trades", type=int, default=5)
    parser.add_argument("--min-score-margin", type=float, default=0.15)
    parser.add_argument("--disable-inactivity-fallback", action="store_true")
    parser.add_argument("--inactive-window-days", type=int, default=1)
    parser.add_argument("--inactive-min-trades", type=int, default=10)
    parser.add_argument("--inactive-min-profit-pct", type=float, default=0.05)
    parser.add_argument("--inactive-min-score", type=float, default=-1.25)
    parser.add_argument("--inactive-confirm-windows", nargs="+", type=int, default=[2, 7])
    parser.add_argument("--max-registry-age-minutes", type=float, default=180.0)
    parser.add_argument("--cooldown-minutes", type=float, default=360.0)
    parser.add_argument("--allow-strategies", nargs="+", default=sorted(VALID_STRATEGIES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=30.0)
    return parser.parse_args()


def load_json(path: Path, default: Any = None) -> Any:
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


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def age_minutes(value: Any, now: datetime | None = None) -> float | None:
    created = parse_dt(value)
    if created is None:
        return None
    now = now or datetime.now(UTC)
    return max((now - created).total_seconds() / 60.0, 0.0)


def find_strategy_score(registry: dict[str, Any], strategy: str) -> dict[str, Any]:
    for item in registry.get("strategy_scores", []):
        if isinstance(item, dict) and item.get("strategy") == strategy:
            return item
    return {}


def strategy_window_evaluations(registry: dict[str, Any], strategy: str) -> list[dict[str, Any]]:
    return [
        item
        for item in registry.get("window_evaluations", [])
        if isinstance(item, dict) and item.get("strategy") == strategy
    ]


def current_strategy_is_inactive(
    registry: dict[str, Any],
    current: str,
    inactive_window_days: int,
) -> bool:
    for item in strategy_window_evaluations(registry, current):
        if int(item.get("window_days", 0)) == inactive_window_days:
            return int(item.get("trades", 0)) == 0
    return False


def select_inactivity_fallback(
    registry: dict[str, Any],
    current: str,
    *,
    allow_strategies: set[str],
    min_passed_windows: int,
    inactive_window_days: int,
    inactive_min_trades: int,
    inactive_min_profit_pct: float,
    inactive_min_score: float,
    inactive_confirm_windows: set[int],
) -> tuple[str, dict[str, Any]]:
    if not current_strategy_is_inactive(registry, current, inactive_window_days):
        return "", {"inactive": False}

    candidates: list[tuple[float, float, int, str, dict[str, Any]]] = []
    for item in registry.get("strategy_scores", []):
        if not isinstance(item, dict):
            continue
        strategy = str(item.get("strategy") or "")
        if not strategy or strategy == current or strategy not in allow_strategies:
            continue
        if int(item.get("passed_windows", 0)) < min_passed_windows:
            continue
        if int(item.get("trades", 0)) < inactive_min_trades:
            continue
        if float(item.get("profit_total_pct", 0.0)) < inactive_min_profit_pct:
            continue
        if float(item.get("score", -999999.0)) < inactive_min_score:
            continue

        confirming = [
            row
            for row in strategy_window_evaluations(registry, strategy)
            if int(row.get("window_days", 0)) in inactive_confirm_windows
            and bool(row.get("passed"))
            and int(row.get("trades", 0)) > 0
            and float(row.get("profit_total_pct", 0.0)) > 0
        ]
        if not confirming:
            continue
        candidates.append(
            (
                float(item.get("profit_total_pct", 0.0)),
                float(item.get("score", -999999.0)),
                int(item.get("trades", 0)),
                strategy,
                {"strategy_score": item, "confirming_windows": confirming},
            )
        )

    if not candidates:
        return "", {"inactive": True, "fallback_candidates": 0}
    candidates.sort(reverse=True)
    _, _, _, strategy, details = candidates[0]
    details["inactive"] = True
    return strategy, details


def open_trade_order_counts(db_path: Path) -> tuple[int, int]:
    if not db_path.exists():
        return 0, 0
    with sqlite3.connect(str(db_path)) as conn:
        trade_count = 0
        order_count = 0
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
        }
        if "trades" in tables:
            cols = {row[1] for row in conn.execute("pragma table_info(trades)").fetchall()}
            if "is_open" in cols:
                trade_count = int(
                    conn.execute("select count(*) from trades where is_open = 1").fetchone()[0]
                )
        if "orders" in tables:
            cols = {row[1] for row in conn.execute("pragma table_info(orders)").fetchall()}
            if "ft_is_open" in cols:
                order_count = int(
                    conn.execute("select count(*) from orders where ft_is_open = 1").fetchone()[0]
                )
            elif "status" in cols:
                order_count = int(
                    conn.execute(
                        "select count(*) from orders where lower(status) in ('open', 'new')"
                    ).fetchone()[0]
                )
    return trade_count, order_count


def should_switch(
    config: dict[str, Any],
    registry: dict[str, Any],
    state: dict[str, Any],
    *,
    allow_strategies: set[str],
    open_trades: int,
    open_orders: int,
    min_passed_windows: int,
    min_trades: int,
    min_score_margin: float,
    inactivity_fallback_enabled: bool = True,
    inactive_window_days: int = 1,
    inactive_min_trades: int = 10,
    inactive_min_profit_pct: float = 0.05,
    inactive_min_score: float = -1.25,
    inactive_confirm_windows: set[int] | None = None,
    max_registry_age_minutes: float,
    cooldown_minutes: float,
    now: datetime | None = None,
) -> SwitchDecision:
    now = now or datetime.now(UTC)
    current = str(config.get("strategy") or "")
    recommended = registry.get("recommended", {}) if isinstance(registry, dict) else {}
    target = str(recommended.get("strategy") or "")
    details: dict[str, Any] = {
        "open_trades": open_trades,
        "open_orders": open_orders,
        "recommended": recommended,
    }

    if not current:
        return SwitchDecision(False, "missing current strategy", current, target, details)

    registry_age = age_minutes(registry.get("created_at"), now)
    details["registry_age_minutes"] = registry_age
    if registry_age is None or registry_age > max_registry_age_minutes:
        return SwitchDecision(False, "strategy registry is stale", current, target, details)

    if open_trades or open_orders:
        return SwitchDecision(False, "open trades or orders exist", current, target, details)

    last_switch_age = age_minutes(state.get("last_switch_at"), now)
    details["last_switch_age_minutes"] = last_switch_age
    if last_switch_age is not None and last_switch_age < cooldown_minutes:
        return SwitchDecision(False, "switch cooldown active", current, target, details)

    switch_mode = "recommended"
    if (
        inactivity_fallback_enabled
        and (not target or target == current or str(recommended.get("side", "hold")) == "hold")
    ):
        fallback_target, fallback_details = select_inactivity_fallback(
            registry,
            current,
            allow_strategies=allow_strategies,
            min_passed_windows=min_passed_windows,
            inactive_window_days=inactive_window_days,
            inactive_min_trades=inactive_min_trades,
            inactive_min_profit_pct=inactive_min_profit_pct,
            inactive_min_score=inactive_min_score,
            inactive_confirm_windows=inactive_confirm_windows or {2, 7},
        )
        details["inactivity_fallback"] = fallback_details
        if fallback_target:
            target = fallback_target
            switch_mode = "inactivity_fallback"

    if not target:
        return SwitchDecision(False, "missing recommended strategy", current, target, details)
    if target == current:
        return SwitchDecision(False, "already using recommended strategy", current, target, details)
    if target not in allow_strategies:
        return SwitchDecision(False, "target strategy not allowed", current, target, details)
    if switch_mode == "recommended" and str(recommended.get("side", "hold")) == "hold":
        return SwitchDecision(False, "recommended side is hold", current, target, details)

    target_score = find_strategy_score(registry, target)
    current_score = find_strategy_score(registry, current)
    details["target_score"] = target_score
    details["current_score"] = current_score
    details["switch_mode"] = switch_mode
    if int(target_score.get("passed_windows", 0)) < min_passed_windows:
        return SwitchDecision(False, "target passed windows below threshold", current, target, details)
    if int(target_score.get("trades", 0)) < min_trades:
        return SwitchDecision(False, "target trades below threshold", current, target, details)

    target_value = float(target_score.get("score", recommended.get("score", -999999.0)))
    current_value = float(current_score.get("score", -999999.0)) if current_score else -999999.0
    details["score_margin"] = round(target_value - current_value, 6)
    if switch_mode == "recommended" and current_score and target_value - current_value < min_score_margin:
        return SwitchDecision(False, "score margin below threshold", current, target, details)

    if switch_mode == "inactivity_fallback":
        return SwitchDecision(True, "current strategy inactive; active fallback passed gates", current, target, details)
    return SwitchDecision(True, "passed switch gates", current, target, details)


def backup_and_write_config(config_path: Path, config: dict[str, Any], target: str) -> Path:
    backup_dir = AUTOOPT_DIR / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"config.{stamp}.{config.get('strategy', 'unknown')}.json"
    shutil.copy2(config_path, backup_path)
    config["strategy"] = target
    write_json(config_path, config)
    return backup_path


def restart_freqtrade(backend: str, container: str) -> None:
    if backend == "compose":
        cmd = ["docker", "compose", "restart", "freqtrade"]
    else:
        cmd = ["docker", "restart", container]
    subprocess.run(cmd, cwd=ROOT, check=True)


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config)
    registry_path = Path(args.registry)
    ledger_path = Path(args.ledger)
    state_path = Path(args.state)

    config = load_json(config_path, {})
    registry = load_json(registry_path, {})
    state = load_json(state_path, {})
    open_trades, open_orders = open_trade_order_counts(Path(args.db))
    decision = should_switch(
        config,
        registry,
        state,
        allow_strategies=set(args.allow_strategies),
        open_trades=open_trades,
        open_orders=open_orders,
        min_passed_windows=args.min_passed_windows,
        min_trades=args.min_trades,
        min_score_margin=args.min_score_margin,
        inactivity_fallback_enabled=not args.disable_inactivity_fallback,
        inactive_window_days=args.inactive_window_days,
        inactive_min_trades=args.inactive_min_trades,
        inactive_min_profit_pct=args.inactive_min_profit_pct,
        inactive_min_score=args.inactive_min_score,
        inactive_confirm_windows=set(args.inactive_confirm_windows),
        max_registry_age_minutes=args.max_registry_age_minutes,
        cooldown_minutes=args.cooldown_minutes,
    )
    payload = {
        "record_type": "strategy_switcher",
        "created_at": datetime.now(UTC).isoformat(),
        "dry_run": bool(args.dry_run),
        "should_switch": decision.should_switch,
        "reason": decision.reason,
        "current_strategy": decision.current_strategy,
        "target_strategy": decision.target_strategy,
        "details": decision.details,
    }

    if decision.should_switch and not args.dry_run:
        backup_path = backup_and_write_config(config_path, config, decision.target_strategy)
        restart_freqtrade(args.backend, args.container)
        state_payload = {
            "last_switch_at": datetime.now(UTC).isoformat(),
            "from_strategy": decision.current_strategy,
            "to_strategy": decision.target_strategy,
            "backup_path": str(backup_path),
        }
        write_json(state_path, state_payload)
        payload["switched"] = True
        payload["backup_path"] = str(backup_path)
    else:
        payload["switched"] = False

    append_jsonl(ledger_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return payload


def main() -> int:
    args = parse_args()
    while True:
        try:
            run_once(args)
        except Exception as exc:
            payload = {
                "record_type": "strategy_switcher_error",
                "created_at": datetime.now(UTC).isoformat(),
                "error": str(exc),
            }
            append_jsonl(Path(args.ledger), payload)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(args.interval_minutes, 1.0) * 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
