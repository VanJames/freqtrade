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
DEFAULT_PAIR_SELECTION = AUTOOPT_DIR / "pair_selection.json"
DEFAULT_FORMAL_POOL = AUTOOPT_DIR / "formal_strategy_pool.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "strategy_switcher.jsonl"
DEFAULT_STATE = AUTOOPT_DIR / "strategy_switcher_state.json"
DEFAULT_DB = USER_DATA / "tradesv3.sqlite"
VALID_STRATEGIES = {
    "SampleStrategy",
    "SampleStrategyActive",
    "SampleStrategyScalp",
    "SampleStrategyPullbackShort",
    "SampleStrategyPullbackLong",
    "SampleStrategyRangeMeanReversion",
    "SampleStrategyBreakoutMomentum",
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
    parser.add_argument("--pair-selection", default=str(DEFAULT_PAIR_SELECTION))
    parser.add_argument("--formal-pool", default=str(DEFAULT_FORMAL_POOL))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backend", choices=["docker", "compose"], default="docker")
    parser.add_argument("--container", default="freqtrade")
    parser.add_argument("--loss-fallback-from-strategy", default="")
    parser.add_argument("--loss-fallback-to-strategy", default="")
    parser.add_argument("--loss-fallback-lookback-trades", type=int, default=0)
    parser.add_argument("--loss-fallback-loss-threshold", type=int, default=0)
    parser.add_argument("--loss-recovery-from-strategy", default="")
    parser.add_argument("--loss-recovery-to-strategy", default="")
    parser.add_argument("--loss-recovery-lookback-trades", type=int, default=0)
    parser.add_argument("--loss-recovery-win-threshold", type=int, default=0)
    parser.add_argument("--loss-recovery-min-hold-days", type=float, default=0.0)
    parser.add_argument("--loss-recovery-min-score-margin", type=float, default=0.0)
    parser.add_argument("--trend-activation-from-strategy", default="")
    parser.add_argument("--trend-activation-to-strategy", default="")
    parser.add_argument("--trend-activation-required-side", default="")
    parser.add_argument("--trend-activation-min-recommended-score", type=float, default=0.0)
    parser.add_argument("--trend-activation-min-score-margin", type=float, default=0.0)
    parser.add_argument("--min-passed-windows", type=int, default=2)
    parser.add_argument("--min-trades", type=int, default=5)
    parser.add_argument("--min-score-margin", type=float, default=0.15)
    parser.add_argument("--disable-inactivity-fallback", action="store_true")
    parser.add_argument("--inactive-window-days", type=int, default=1)
    parser.add_argument("--inactive-min-trades", type=int, default=10)
    parser.add_argument("--inactive-min-profit-pct", type=float, default=0.05)
    parser.add_argument("--inactive-min-score", type=float, default=-1.25)
    parser.add_argument("--inactive-confirm-windows", nargs="+", type=int, default=[2, 7])
    parser.add_argument("--require-positive-window-days", nargs="+", type=int, default=[])
    parser.add_argument("--pool-positive-window-days", nargs="+", type=int, default=[])
    parser.add_argument("--min-required-window-profit-pct", type=float, default=0.0)
    parser.add_argument("--max-registry-age-minutes", type=float, default=180.0)
    parser.add_argument("--cooldown-minutes", type=float, default=360.0)
    parser.add_argument("--allow-strategies", nargs="+", default=sorted(VALID_STRATEGIES))
    parser.add_argument("--disable-ai-generated-switches", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=30.0)
    return parser.parse_args()


def strategy_allowed(strategy: str, allow_strategies: set[str], *, allow_ai_generated: bool) -> bool:
    if strategy in allow_strategies:
        return True
    # AI strategy-lab renders generated strategies from a restricted local
    # template.  The LLM only proposes JSON DSL fields, so allowing this prefix
    # is acceptable after the normal backtest gates pass.
    return allow_ai_generated and strategy.startswith("AIGenerated")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def effective_allow_strategies(
    cli_allow_strategies: list[str],
    formal_pool: dict[str, Any],
) -> set[str]:
    allowed = formal_pool.get("allowed_strategies", []) if isinstance(formal_pool, dict) else []
    formal = {str(item).strip() for item in allowed if str(item).strip()}
    if formal:
        return formal
    return {str(item).strip() for item in cli_allow_strategies if str(item).strip()}


def formal_pool_priority_bonus(formal_pool: dict[str, Any], strategy: str) -> float:
    if not strategy or not isinstance(formal_pool, dict):
        return 0.0
    scores = formal_pool.get("priority_scores", {})
    if isinstance(scores, dict) and strategy in scores:
        try:
            raw = float(scores.get(strategy, 0.0))
        except (TypeError, ValueError):
            raw = 0.0
        for item in formal_pool.get("strategies", []):
            if not isinstance(item, dict) or item.get("strategy") != strategy:
                continue
            try:
                base = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                base = 0.0
            return round(raw - base, 6)
    for item in formal_pool.get("strategies", []):
        if isinstance(item, dict) and item.get("strategy") == strategy:
            try:
                return round(float(item.get("priority_bonus", 0.0) or 0.0), 6)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def current_pair_whitelist(config: dict[str, Any]) -> list[str]:
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        return []
    pairs = exchange.get("pair_whitelist")
    if not isinstance(pairs, list):
        return []
    return [str(item) for item in pairs if str(item)]


def desired_pair_whitelist(
    config: dict[str, Any],
    pair_selection: dict[str, Any],
    strategy: str,
) -> list[str]:
    strategies = pair_selection.get("strategies")
    if isinstance(strategies, dict):
        row = strategies.get(strategy)
        if isinstance(row, dict):
            allowed = row.get("allowed_pairs")
            if isinstance(allowed, list):
                pairs = [str(item) for item in allowed if str(item)]
                if pairs:
                    return pairs
    recommended_strategy = str(pair_selection.get("recommended_strategy") or "")
    if recommended_strategy == strategy:
        allowed = pair_selection.get("recommended_allowed_pairs")
        if isinstance(allowed, list):
            pairs = [str(item) for item in allowed if str(item)]
            if pairs:
                return pairs
    return current_pair_whitelist(config)


def pair_whitelist_changed(config: dict[str, Any], desired_pairs: list[str]) -> bool:
    current = current_pair_whitelist(config)
    return current != list(desired_pairs)


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


def positive_window_gate(
    registry: dict[str, Any],
    strategy: str,
    required_days: set[int],
    min_profit_pct: float,
) -> tuple[bool, dict[str, Any]]:
    if not required_days:
        return True, {"required_days": []}
    rows = [
        item
        for item in strategy_window_evaluations(registry, strategy)
        if int(item.get("window_days", 0)) in required_days
    ]
    seen_days = {int(item.get("window_days", 0)) for item in rows}
    missing_days = sorted(required_days - seen_days)
    failed = [
        item
        for item in rows
        if not bool(item.get("passed"))
        or int(item.get("trades", 0)) <= 0
        or float(item.get("profit_total_pct", 0.0)) <= min_profit_pct
    ]
    return not failed and not missing_days, {
        "required_days": sorted(required_days),
        "missing_days": missing_days,
        "checked": rows,
        "failed": failed,
        "min_profit_pct": min_profit_pct,
        "missing_policy": "fail",
    }


def pool_positive_window_gate(
    registry: dict[str, Any],
    required_days: set[int],
    min_profit_pct: float,
) -> tuple[bool, dict[str, Any]]:
    if not required_days:
        return True, {"required_days": []}
    rows = [
        item
        for item in registry.get("window_evaluations", [])
        if isinstance(item, dict) and int(item.get("window_days", 0)) in required_days
    ]
    seen_days = {int(item.get("window_days", 0)) for item in rows}
    missing_days = sorted(required_days - seen_days)
    positive = [
        item
        for item in rows
        if bool(item.get("passed"))
        and int(item.get("trades", 0)) > 0
        and float(item.get("profit_total_pct", 0.0)) > min_profit_pct
    ]
    return bool(positive) and not missing_days, {
        "required_days": sorted(required_days),
        "missing_days": missing_days,
        "positive_count": len(positive),
        "checked_count": len(rows),
        "min_profit_pct": min_profit_pct,
        "missing_policy": "fail",
    }


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
    allow_ai_generated: bool,
    min_passed_windows: int,
    inactive_window_days: int,
    inactive_min_trades: int,
    inactive_min_profit_pct: float,
    inactive_min_score: float,
    inactive_confirm_windows: set[int],
    required_positive_windows: set[int],
    min_required_window_profit_pct: float,
) -> tuple[str, dict[str, Any]]:
    if not current_strategy_is_inactive(registry, current, inactive_window_days):
        return "", {"inactive": False}

    candidates: list[tuple[float, float, int, str, dict[str, Any]]] = []
    for item in registry.get("strategy_scores", []):
        if not isinstance(item, dict):
            continue
        strategy = str(item.get("strategy") or "")
        if not strategy or strategy == current:
            continue
        if not strategy_allowed(strategy, allow_strategies, allow_ai_generated=allow_ai_generated):
            continue
        if int(item.get("passed_windows", 0)) < min_passed_windows:
            continue
        if int(item.get("trades", 0)) < inactive_min_trades:
            continue
        if float(item.get("profit_total_pct", 0.0)) < inactive_min_profit_pct:
            continue
        if float(item.get("score", -999999.0)) < inactive_min_score:
            continue
        required_ok, required_details = positive_window_gate(
            registry,
            strategy,
            required_positive_windows,
            min_required_window_profit_pct,
        )
        if not required_ok:
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
                {
                    "strategy_score": item,
                    "confirming_windows": confirming,
                    "required_positive_windows": required_details,
                },
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


def recent_closed_trade_stats(db_path: Path, strategy: str, limit: int) -> dict[str, Any]:
    if not db_path.exists() or not strategy or limit <= 0:
        return {"strategy": strategy, "count": 0, "losses": 0, "wins": 0, "rows": []}
    with sqlite3.connect(str(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
        }
        if "trades" not in tables:
            return {"strategy": strategy, "count": 0, "losses": 0, "wins": 0, "rows": []}
        rows = conn.execute(
            """
            select close_date, close_profit, close_profit_abs, exit_reason, pair
            from trades
            where is_open = 0
              and strategy = ?
              and close_date is not null
            order by close_date desc, id desc
            limit ?
            """,
            (strategy, limit),
        ).fetchall()

    normalized: list[dict[str, Any]] = []
    losses = 0
    wins = 0
    for close_date, close_profit, close_profit_abs, exit_reason, pair in rows:
        profit_abs = float(close_profit_abs or 0.0)
        profit_ratio = float(close_profit or 0.0)
        is_loss = profit_abs < 0 or profit_ratio < 0
        is_win = profit_abs > 0 or profit_ratio > 0
        if is_loss:
            losses += 1
        elif is_win:
            wins += 1
        normalized.append(
            {
                "close_date": close_date,
                "close_profit": profit_ratio,
                "close_profit_abs": profit_abs,
                "exit_reason": exit_reason,
                "pair": pair,
                "is_loss": is_loss,
            }
        )
    return {
        "strategy": strategy,
        "count": len(normalized),
        "losses": losses,
        "wins": wins,
        "rows": normalized,
    }


def recent_trade_stats_from_rows(rows: list[dict[str, Any]], strategy: str, limit: int) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if str(row.get("strategy") or strategy) == strategy
    ]
    selected.sort(key=lambda item: str(item.get("close_date") or ""), reverse=True)
    selected = selected[: max(limit, 0)]
    losses = sum(1 for row in selected if bool(row.get("is_loss")))
    wins = sum(1 for row in selected if not bool(row.get("is_loss")) and float(row.get("close_profit_abs") or 0.0) > 0)
    return {
        "strategy": strategy,
        "count": len(selected),
        "losses": losses,
        "wins": wins,
        "rows": selected,
    }


def maybe_apply_recent_trade_override(
    decision: SwitchDecision,
    *,
    current_strategy: str,
    allow_strategies: set[str],
    allow_ai_generated: bool,
    cooldown_minutes: float,
    loss_fallback_from_strategy: str,
    loss_fallback_to_strategy: str,
    loss_fallback_lookback_trades: int,
    loss_fallback_loss_threshold: int,
    loss_fallback_stats: dict[str, Any],
    loss_recovery_from_strategy: str,
    loss_recovery_to_strategy: str,
    loss_recovery_lookback_trades: int,
    loss_recovery_win_threshold: int,
    loss_recovery_min_hold_days: float,
    loss_recovery_min_score_margin: float,
    trend_activation_from_strategy: str,
    trend_activation_to_strategy: str,
    trend_activation_required_side: str,
    trend_activation_min_recommended_score: float,
    trend_activation_min_score_margin: float,
    loss_recovery_stats: dict[str, Any],
) -> SwitchDecision:
    last_switch_age = decision.details.get("last_switch_age_minutes")
    cooldown_ready = last_switch_age is None or float(last_switch_age) >= cooldown_minutes
    recovery_hold_minutes = max(float(loss_recovery_min_hold_days or 0.0) * 1440.0, cooldown_minutes)
    recovery_hold_ready = last_switch_age is None or float(last_switch_age) >= recovery_hold_minutes
    target_score = decision.details.get("target_score") or {}
    current_score = decision.details.get("current_score") or {}
    recommended = decision.details.get("recommended") or {}
    recovery_score_margin = float(target_score.get("score", -999999.0)) - float(
        current_score.get("score", -999999.0)
    )

    if (
        loss_fallback_from_strategy
        and loss_fallback_to_strategy
        and current_strategy == loss_fallback_from_strategy
        and loss_fallback_stats.get("count", 0) >= loss_fallback_lookback_trades > 0
        and loss_fallback_stats.get("losses", 0) >= loss_fallback_loss_threshold > 0
        and strategy_allowed(
            loss_fallback_to_strategy,
            allow_strategies,
            allow_ai_generated=allow_ai_generated,
        )
    ):
        return SwitchDecision(
            True,
            "recent closed-trade loss threshold triggered fallback",
            current_strategy,
            loss_fallback_to_strategy,
            {
                **decision.details,
                "switch_mode": "loss_fallback",
                "loss_fallback": {
                    "from_strategy": loss_fallback_from_strategy,
                    "to_strategy": loss_fallback_to_strategy,
                    "lookback_trades": loss_fallback_lookback_trades,
                    "loss_threshold": loss_fallback_loss_threshold,
                    "recent_stats": loss_fallback_stats,
                },
            },
        )

    if (
        loss_recovery_from_strategy
        and loss_recovery_to_strategy
        and current_strategy == loss_recovery_from_strategy
        and decision.should_switch
        and decision.target_strategy == loss_recovery_to_strategy
    ):
        trend_activation_ready = (
            trend_activation_from_strategy
            and trend_activation_to_strategy
            and current_strategy == trend_activation_from_strategy
            and decision.target_strategy == trend_activation_to_strategy
            and (
                not trend_activation_required_side
                or str(recommended.get("side") or "").lower()
                == str(trend_activation_required_side).lower()
            )
            and float(recommended.get("score", -999999.0))
            >= float(trend_activation_min_recommended_score or 0.0)
            and recovery_score_margin >= float(trend_activation_min_score_margin or 0.0)
        )
        if trend_activation_ready:
            return SwitchDecision(
                True,
                "trend activation override",
                decision.current_strategy,
                decision.target_strategy,
                {
                    **decision.details,
                    "switch_mode": "trend_activation",
                    "trend_activation": {
                        "from_strategy": trend_activation_from_strategy,
                        "to_strategy": trend_activation_to_strategy,
                        "required_side": str(trend_activation_required_side or "").lower(),
                        "recommended_score": float(recommended.get("score", -999999.0)),
                        "min_recommended_score": float(trend_activation_min_recommended_score or 0.0),
                        "score_margin": round(recovery_score_margin, 6),
                        "min_score_margin": float(trend_activation_min_score_margin or 0.0),
                    },
                },
            )
        recovery_ready = (
            cooldown_ready
            and recovery_hold_ready
            and loss_recovery_stats.get("count", 0) >= loss_recovery_lookback_trades > 0
            and loss_recovery_stats.get("wins", 0) >= loss_recovery_win_threshold > 0
            and recovery_score_margin >= float(loss_recovery_min_score_margin or 0.0)
        )
        if not recovery_ready:
            recovery_reason = "recovery guard active"
            if not recovery_hold_ready:
                recovery_reason = "recovery min hold not reached"
            elif loss_recovery_stats.get("count", 0) < loss_recovery_lookback_trades:
                recovery_reason = "recovery trade sample below threshold"
            elif loss_recovery_stats.get("wins", 0) < loss_recovery_win_threshold:
                recovery_reason = "recovery win threshold not met"
            elif recovery_score_margin < float(loss_recovery_min_score_margin or 0.0):
                recovery_reason = "recovery score margin below threshold"
            return SwitchDecision(
                False,
                recovery_reason,
                decision.current_strategy,
                decision.target_strategy,
                {
                    **decision.details,
                    "switch_mode": "recovery_lock",
                    "loss_recovery": {
                        "from_strategy": loss_recovery_from_strategy,
                        "to_strategy": loss_recovery_to_strategy,
                        "lookback_trades": loss_recovery_lookback_trades,
                        "win_threshold": loss_recovery_win_threshold,
                        "min_hold_days": float(loss_recovery_min_hold_days or 0.0),
                        "min_score_margin": float(loss_recovery_min_score_margin or 0.0),
                        "score_margin": round(recovery_score_margin, 6),
                        "recent_stats": loss_recovery_stats,
                    },
                },
            )

    if (
        cooldown_ready
        and recovery_hold_ready
        and loss_recovery_from_strategy
        and loss_recovery_to_strategy
        and current_strategy == loss_recovery_from_strategy
        and loss_recovery_stats.get("count", 0) >= loss_recovery_lookback_trades > 0
        and loss_recovery_stats.get("wins", 0) >= loss_recovery_win_threshold > 0
        and recovery_score_margin >= float(loss_recovery_min_score_margin or 0.0)
        and strategy_allowed(
            loss_recovery_to_strategy,
            allow_strategies,
            allow_ai_generated=allow_ai_generated,
        )
    ):
        return SwitchDecision(
            True,
            "recent closed-trade win threshold triggered recovery",
            current_strategy,
            loss_recovery_to_strategy,
            {
                **decision.details,
                "switch_mode": "loss_recovery",
                "loss_recovery": {
                    "from_strategy": loss_recovery_from_strategy,
                    "to_strategy": loss_recovery_to_strategy,
                    "lookback_trades": loss_recovery_lookback_trades,
                    "win_threshold": loss_recovery_win_threshold,
                    "min_hold_days": float(loss_recovery_min_hold_days or 0.0),
                    "min_score_margin": float(loss_recovery_min_score_margin or 0.0),
                    "score_margin": round(recovery_score_margin, 6),
                    "recent_stats": loss_recovery_stats,
                },
            },
        )
    return decision


def should_switch(
    config: dict[str, Any],
    registry: dict[str, Any],
    state: dict[str, Any],
    *,
    formal_pool: dict[str, Any] | None = None,
    allow_strategies: set[str],
    allow_ai_generated: bool = True,
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
    required_positive_windows: set[int] | None = None,
    pool_positive_windows: set[int] | None = None,
    min_required_window_profit_pct: float = 0.0,
    max_registry_age_minutes: float = 180.0,
    cooldown_minutes: float = 360.0,
    now: datetime | None = None,
) -> SwitchDecision:
    now = now or datetime.now(UTC)
    current = str(config.get("strategy") or "")
    formal_pool = formal_pool if isinstance(formal_pool, dict) else {}
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

    pool_ok, pool_details = pool_positive_window_gate(
        registry,
        pool_positive_windows or set(),
        min_required_window_profit_pct,
    )
    details["pool_positive_windows"] = pool_details
    if not pool_ok:
        return SwitchDecision(False, "strategy pool failed required positive window gate", current, target, details)

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
            allow_ai_generated=allow_ai_generated,
            min_passed_windows=min_passed_windows,
            inactive_window_days=inactive_window_days,
            inactive_min_trades=inactive_min_trades,
            inactive_min_profit_pct=inactive_min_profit_pct,
            inactive_min_score=inactive_min_score,
            inactive_confirm_windows=inactive_confirm_windows or {2, 7},
            required_positive_windows=required_positive_windows or set(),
            min_required_window_profit_pct=min_required_window_profit_pct,
        )
        details["inactivity_fallback"] = fallback_details
        if fallback_target:
            target = fallback_target
            switch_mode = "inactivity_fallback"

    if not target:
        return SwitchDecision(False, "missing recommended strategy", current, target, details)
    if target == current:
        return SwitchDecision(False, "already using recommended strategy", current, target, details)
    if not strategy_allowed(target, allow_strategies, allow_ai_generated=allow_ai_generated):
        return SwitchDecision(False, "target strategy not allowed", current, target, details)
    if switch_mode == "recommended" and str(recommended.get("side", "hold")) == "hold":
        return SwitchDecision(False, "recommended side is hold", current, target, details)

    target_score = find_strategy_score(registry, target)
    current_score = find_strategy_score(registry, current)
    target_priority_bonus = formal_pool_priority_bonus(formal_pool, target)
    current_priority_bonus = formal_pool_priority_bonus(formal_pool, current)
    details["target_score"] = target_score
    details["current_score"] = current_score
    details["target_priority_bonus"] = round(target_priority_bonus, 6)
    details["current_priority_bonus"] = round(current_priority_bonus, 6)
    details["switch_mode"] = switch_mode
    required_ok, required_details = positive_window_gate(
        registry,
        target,
        required_positive_windows or set(),
        min_required_window_profit_pct,
    )
    details["target_required_positive_windows"] = required_details
    if not required_ok:
        return SwitchDecision(False, "target failed required positive window gate", current, target, details)
    if int(target_score.get("passed_windows", 0)) < min_passed_windows:
        return SwitchDecision(False, "target passed windows below threshold", current, target, details)
    if int(target_score.get("trades", 0)) < min_trades:
        return SwitchDecision(False, "target trades below threshold", current, target, details)

    target_value = float(target_score.get("score", recommended.get("score", -999999.0))) + target_priority_bonus
    current_value = (
        float(current_score.get("score", -999999.0)) + current_priority_bonus
        if current_score
        else -999999.0
    )
    details["target_effective_score"] = round(target_value, 6)
    details["current_effective_score"] = round(current_value, 6)
    details["score_margin"] = round(target_value - current_value, 6)
    if switch_mode == "recommended" and current_score and target_value - current_value < min_score_margin:
        return SwitchDecision(False, "score margin below threshold", current, target, details)

    if switch_mode == "inactivity_fallback":
        return SwitchDecision(True, "current strategy inactive; active fallback passed gates", current, target, details)
    return SwitchDecision(True, "passed switch gates", current, target, details)


def backup_and_write_config(
    config_path: Path,
    config: dict[str, Any],
    target: str,
    *,
    pair_whitelist: list[str] | None = None,
) -> Path:
    backup_dir = AUTOOPT_DIR / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"config.{stamp}.{config.get('strategy', 'unknown')}.json"
    shutil.copy2(config_path, backup_path)
    config["strategy"] = target
    if pair_whitelist is not None:
        exchange = config.setdefault("exchange", {})
        if isinstance(exchange, dict):
            exchange["pair_whitelist"] = list(pair_whitelist)
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
    pair_selection_path = Path(args.pair_selection)
    formal_pool_path = Path(args.formal_pool)
    ledger_path = Path(args.ledger)
    state_path = Path(args.state)

    config = load_json(config_path, {})
    registry = load_json(registry_path, {})
    pair_selection = load_json(pair_selection_path, {})
    formal_pool = load_json(formal_pool_path, {})
    state = load_json(state_path, {})
    resolved_allow_strategies = effective_allow_strategies(args.allow_strategies, formal_pool)
    open_trades, open_orders = open_trade_order_counts(Path(args.db))
    decision = should_switch(
        config,
        registry,
        state,
        formal_pool=formal_pool,
        allow_strategies=resolved_allow_strategies,
        allow_ai_generated=not args.disable_ai_generated_switches,
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
        required_positive_windows=set(args.require_positive_window_days),
        pool_positive_windows=set(args.pool_positive_window_days),
        min_required_window_profit_pct=args.min_required_window_profit_pct,
        max_registry_age_minutes=args.max_registry_age_minutes,
        cooldown_minutes=args.cooldown_minutes,
    )
    loss_fallback_stats = recent_closed_trade_stats(
        Path(args.db),
        args.loss_fallback_from_strategy,
        args.loss_fallback_lookback_trades,
    )
    loss_recovery_stats = recent_closed_trade_stats(
        Path(args.db),
        args.loss_recovery_from_strategy,
        args.loss_recovery_lookback_trades,
    )
    if not open_trades and not open_orders:
        decision = maybe_apply_recent_trade_override(
            decision,
            current_strategy=str(config.get("strategy") or ""),
            allow_strategies=resolved_allow_strategies,
            allow_ai_generated=not args.disable_ai_generated_switches,
            cooldown_minutes=args.cooldown_minutes,
            loss_fallback_from_strategy=args.loss_fallback_from_strategy,
            loss_fallback_to_strategy=args.loss_fallback_to_strategy,
            loss_fallback_lookback_trades=args.loss_fallback_lookback_trades,
            loss_fallback_loss_threshold=args.loss_fallback_loss_threshold,
            loss_fallback_stats=loss_fallback_stats,
            loss_recovery_from_strategy=args.loss_recovery_from_strategy,
            loss_recovery_to_strategy=args.loss_recovery_to_strategy,
            loss_recovery_lookback_trades=args.loss_recovery_lookback_trades,
            loss_recovery_win_threshold=args.loss_recovery_win_threshold,
            loss_recovery_min_hold_days=args.loss_recovery_min_hold_days,
            loss_recovery_min_score_margin=args.loss_recovery_min_score_margin,
            trend_activation_from_strategy=args.trend_activation_from_strategy,
            trend_activation_to_strategy=args.trend_activation_to_strategy,
            trend_activation_required_side=args.trend_activation_required_side,
            trend_activation_min_recommended_score=args.trend_activation_min_recommended_score,
            trend_activation_min_score_margin=args.trend_activation_min_score_margin,
            loss_recovery_stats=loss_recovery_stats,
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
    payload["loss_fallback_stats"] = loss_fallback_stats
    payload["loss_recovery_stats"] = loss_recovery_stats
    payload["formal_pool_path"] = str(formal_pool_path)
    payload["formal_pool_priority_scores"] = formal_pool.get("priority_scores", {}) if isinstance(formal_pool, dict) else {}
    payload["effective_allow_strategies"] = sorted(resolved_allow_strategies)

    target_for_pairs = decision.target_strategy if decision.should_switch else decision.current_strategy
    desired_pairs = desired_pair_whitelist(config, pair_selection, target_for_pairs)
    pairs_changed = bool(desired_pairs) and pair_whitelist_changed(config, desired_pairs)
    payload["pair_selection_path"] = str(pair_selection_path)
    payload["desired_pair_whitelist"] = desired_pairs
    payload["pair_whitelist_changed"] = pairs_changed

    if decision.should_switch and not args.dry_run:
        backup_path = backup_and_write_config(
            config_path,
            config,
            decision.target_strategy,
            pair_whitelist=desired_pairs or None,
        )
        restart_freqtrade(args.backend, args.container)
        state_payload = {
            "last_switch_at": datetime.now(UTC).isoformat(),
            "from_strategy": decision.current_strategy,
            "to_strategy": decision.target_strategy,
            "backup_path": str(backup_path),
            "pair_whitelist": desired_pairs,
        }
        write_json(state_path, state_payload)
        payload["switched"] = True
        payload["updated_pair_whitelist"] = bool(desired_pairs)
        payload["backup_path"] = str(backup_path)
    elif pairs_changed and not args.dry_run and open_trades == 0 and open_orders == 0:
        backup_path = backup_and_write_config(
            config_path,
            config,
            decision.current_strategy,
            pair_whitelist=desired_pairs,
        )
        restart_freqtrade(args.backend, args.container)
        state_payload = {
            "last_switch_at": datetime.now(UTC).isoformat(),
            "from_strategy": decision.current_strategy,
            "to_strategy": decision.current_strategy,
            "backup_path": str(backup_path),
            "pair_whitelist": desired_pairs,
            "pair_refresh_only": True,
        }
        write_json(state_path, state_payload)
        payload["switched"] = False
        payload["pair_whitelist_refreshed"] = True
        payload["updated_pair_whitelist"] = True
        payload["backup_path"] = str(backup_path)
    else:
        payload["switched"] = False
        payload["updated_pair_whitelist"] = False

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
