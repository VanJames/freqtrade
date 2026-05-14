#!/usr/bin/env python3
"""
One-shot health check for the trading stack.

This script is intentionally read-only. It inspects local files and optional
docker container status so operators can quickly answer:

- Is the bot process alive?
- Which strategy is configured right now?
- Are advisor / formal pool / pair selection artifacts fresh?
- Are signals being blocked by obvious conditions?
- Are strategy recommendations aligned with current execution?
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
AUTOOPT_DIR = USER_DATA / "autoopt"

DEFAULT_CONTAINERS = [
    "freqtrade",
    "freqtrade-trade-settings",
    "freqtrade-ai-strategy-lab",
    "freqtrade-strategy-researcher",
    "freqtrade-direction-advisor",
    "freqtrade-kronos-advisor",
    "freqtrade-strategy-switcher",
    "freqtrade-optimizer",
]


@dataclass
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a one-shot trading stack health check.")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--trade-settings", default=str(USER_DATA / "trade_execution.json"))
    parser.add_argument("--formal-pool", default=str(AUTOOPT_DIR / "formal_strategy_pool.json"))
    parser.add_argument("--pair-selection", default=str(AUTOOPT_DIR / "pair_selection.json"))
    parser.add_argument("--direction-advisor", default=str(AUTOOPT_DIR / "direction_advisor.json"))
    parser.add_argument("--signal-diagnostics", default=str(USER_DATA / "signals" / "signal_diagnostics.jsonl"))
    parser.add_argument("--trade-db", default=str(USER_DATA / "tradesv3.sqlite"))
    parser.add_argument("--max-age-minutes", type=float, default=180.0)
    parser.add_argument("--signal-limit", type=int, default=100)
    parser.add_argument("--log-tail", type=int, default=200)
    parser.add_argument("--signal-stale-minutes", type=float, default=90.0)
    parser.add_argument("--containers", nargs="*", default=DEFAULT_CONTAINERS)
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def minutes_old(path: Path) -> float | None:
    try:
        return round((utc_now().timestamp() - path.stat().st_mtime) / 60.0, 2)
    except FileNotFoundError:
        return None


def read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except (FileNotFoundError, OSError):
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def top_counts(values: list[str], limit: int = 5) -> list[tuple[str, int]]:
    return Counter(values).most_common(limit)


def check_config(path: Path) -> CheckResult:
    payload = read_json(path)
    if not isinstance(payload, dict):
        return CheckResult("config", "fail", "config.json missing or invalid", {"path": str(path)})
    exchange = payload.get("exchange", {}) if isinstance(payload.get("exchange"), dict) else {}
    pair_whitelist = exchange.get("pair_whitelist") if isinstance(exchange.get("pair_whitelist"), list) else []
    details = {
        "path": str(path),
        "strategy": payload.get("strategy"),
        "dry_run": payload.get("dry_run"),
        "exchange": exchange.get("name"),
        "pair_count": len(pair_whitelist),
        "pair_whitelist": pair_whitelist,
    }
    return CheckResult(
        "config",
        "ok",
        f"strategy={details['strategy']} exchange={details['exchange']} pairs={details['pair_count']}",
        details,
    )


def check_trade_settings(path: Path) -> CheckResult:
    payload = read_json(path)
    if not isinstance(payload, dict):
        return CheckResult("trade_settings", "warn", "trade_execution.json missing", {"path": str(path)})
    gate_enabled = bool(payload.get("direction_advisor_gate_enabled", False))
    bridge_enabled = bool(payload.get("hotcoin_signal_bridge_enabled", False))
    details = {
        "path": str(path),
        "direction_advisor_gate_enabled": gate_enabled,
        "live_trading_disabled": bool(payload.get("live_trading_disabled", False)),
        "hotcoin_signal_bridge_enabled": bridge_enabled,
        "hotcoin_signal_execute": bool(payload.get("hotcoin_signal_execute", False)),
    }
    status = "ok"
    summary = f"direction_gate={gate_enabled} hotcoin_bridge={bridge_enabled}"
    return CheckResult("trade_settings", status, summary, details)


def check_direction_advisor(path: Path, max_age_minutes: float) -> CheckResult:
    payload = read_json(path)
    age = minutes_old(path)
    if not isinstance(payload, dict):
        return CheckResult("direction_advisor", "warn", "direction_advisor.json missing", {"path": str(path)})
    status = "ok"
    if age is None or age > max_age_minutes:
        status = "warn"
    details = {
        "path": str(path),
        "age_minutes": age,
        "final_action": payload.get("final_action"),
        "final_reason": payload.get("final_reason"),
        "recommended_strategy": payload.get("recommended_strategy"),
        "recommended_side": payload.get("recommended_side"),
        "real_trade_feedback": payload.get("real_trade_feedback"),
    }
    summary = (
        f"action={details['final_action']} strategy={details['recommended_strategy']} age={age}m"
    )
    return CheckResult("direction_advisor", status, summary, details)


def check_formal_pool(path: Path, max_age_minutes: float) -> CheckResult:
    payload = read_json(path)
    age = minutes_old(path)
    if not isinstance(payload, dict):
        return CheckResult("formal_pool", "warn", "formal_strategy_pool.json missing", {"path": str(path)})
    strategies = payload.get("allowed_strategies") if isinstance(payload.get("allowed_strategies"), list) else []
    details = {
        "path": str(path),
        "age_minutes": age,
        "allowed_strategies": strategies,
        "priority_scores": payload.get("priority_scores", {}),
    }
    status = "ok"
    if age is None or age > max_age_minutes:
        status = "warn"
    if not strategies:
        status = "warn"
    summary = f"allowed={len(strategies)} age={age}m"
    return CheckResult("formal_pool", status, summary, details)


def check_pair_selection(path: Path, max_age_minutes: float) -> CheckResult:
    payload = read_json(path)
    age = minutes_old(path)
    if not isinstance(payload, dict):
        return CheckResult("pair_selection", "warn", "pair_selection.json missing", {"path": str(path)})
    recommended_strategy = payload.get("recommended_strategy")
    allowed_pairs = payload.get("recommended_allowed_pairs")
    if not isinstance(allowed_pairs, list):
        allowed_pairs = []
    details = {
        "path": str(path),
        "age_minutes": age,
        "recommended_strategy": recommended_strategy,
        "recommended_allowed_pairs": allowed_pairs,
    }
    status = "ok"
    if age is None or age > max_age_minutes:
        status = "warn"
    summary = f"strategy={recommended_strategy} pairs={len(allowed_pairs)} age={age}m"
    return CheckResult("pair_selection", status, summary, details)


def analyze_signal_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "count": 0,
            "enter_long_true": 0,
            "enter_short_true": 0,
            "both_blocked": 0,
            "top_long_blockers": [],
            "top_short_blockers": [],
        }
    long_blockers: list[str] = []
    short_blockers: list[str] = []
    enter_long_true = 0
    enter_short_true = 0
    both_blocked = 0
    for record in records:
        if record.get("enter_long") is True:
            enter_long_true += 1
        if record.get("enter_short") is True:
            enter_short_true += 1
        if record.get("enter_long") is False and record.get("enter_short") is False:
            both_blocked += 1
        long_blockers.extend(str(item) for item in record.get("long_blockers", []) if item)
        short_blockers.extend(str(item) for item in record.get("short_blockers", []) if item)
    return {
        "count": len(records),
        "enter_long_true": enter_long_true,
        "enter_short_true": enter_short_true,
        "both_blocked": both_blocked,
        "top_long_blockers": top_counts(long_blockers),
        "top_short_blockers": top_counts(short_blockers),
        "latest_pair": records[0].get("pair"),
        "latest_time": records[0].get("time"),
    }


def check_signal_diagnostics(path: Path, limit: int) -> CheckResult:
    records = read_jsonl_tail(path, limit)
    analysis = analyze_signal_diagnostics(records)
    if not records:
        return CheckResult(
            "signal_diagnostics",
            "warn",
            "signal_diagnostics.jsonl missing or empty",
            {"path": str(path), **analysis},
        )
    status = "ok"
    if analysis["enter_long_true"] == 0 and analysis["enter_short_true"] == 0:
        status = "warn"
    summary = (
        f"records={analysis['count']} long_true={analysis['enter_long_true']} "
        f"short_true={analysis['enter_short_true']} blocked={analysis['both_blocked']}"
    )
    details = {"path": str(path), **analysis}
    return CheckResult("signal_diagnostics", status, summary, details)


def check_signal_diagnostics_freshness(path: Path, limit: int, stale_minutes: float) -> CheckResult:
    records = read_jsonl_tail(path, limit)
    if not records:
        return CheckResult(
            "signal_diagnostics_freshness",
            "warn",
            "no diagnostics available",
            {"path": str(path), "latest_time": None, "age_minutes": None},
        )
    latest_time = iso_to_dt(str(records[-1].get("created_at") or records[-1].get("time") or ""))
    age = None
    status = "ok"
    if latest_time is not None:
        age = round((utc_now() - latest_time).total_seconds() / 60.0, 2)
        if age > stale_minutes:
            status = "warn"
    else:
        status = "warn"
    return CheckResult(
        "signal_diagnostics_freshness",
        status,
        f"latest diagnostics age={age}m",
        {"path": str(path), "latest_time": latest_time.isoformat() if latest_time else None, "age_minutes": age},
    )


def check_trade_db(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult("trade_db", "warn", "tradesv3.sqlite missing", {"path": str(path)})
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        totals = conn.execute(
            """
            select
              sum(case when is_open = 1 then 1 else 0 end) as open_trades,
              sum(case when is_open = 0 then 1 else 0 end) as closed_trades
            from trades
            """
        ).fetchone()
        latest = conn.execute(
            """
            select pair, strategy, is_short, close_profit, close_profit_abs, close_date
            from trades
            where is_open = 0
            order by close_date desc
            limit 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        return CheckResult("trade_db", "fail", f"sqlite error: {exc}", {"path": str(path)})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    details = {
        "path": str(path),
        "open_trades": int(totals["open_trades"] or 0),
        "closed_trades": int(totals["closed_trades"] or 0),
        "latest_closed_trade": dict(latest) if latest else None,
    }
    return CheckResult(
        "trade_db",
        "ok",
        f"open={details['open_trades']} closed={details['closed_trades']}",
        details,
    )


def docker_container_state(name: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "docker",
            "inspect",
            name,
            "--format",
            "{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {"name": name, "status": "missing", "running": False, "exit_code": None}
    status, running, exit_code = (proc.stdout.strip() or "unknown|false|").split("|")
    return {
        "name": name,
        "status": status,
        "running": running == "true",
        "exit_code": int(exit_code) if exit_code.isdigit() else None,
    }


def docker_logs_tail(name: str, lines: int) -> list[str]:
    proc = subprocess.run(
        ["docker", "logs", f"--tail={lines}", name],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    output = (proc.stdout or "") + (proc.stderr or "")
    return [line for line in output.splitlines() if line.strip()]


def docker_exec_output(container: str, command: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", command],
        capture_output=True,
        text=True,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def file_line_count(path: Path) -> int | None:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (FileNotFoundError, OSError):
        return None


def check_docker(containers: list[str]) -> CheckResult:
    states = [docker_container_state(name) for name in containers]
    missing = [item["name"] for item in states if item["status"] == "missing"]
    down = [item["name"] for item in states if item["status"] not in {"running"}]
    status = "ok"
    if missing or down:
        status = "warn"
    summary = f"running={sum(1 for item in states if item['running'])}/{len(states)}"
    details = {"containers": states, "missing": missing, "down": down}
    return CheckResult("docker", status, summary, details)


def find_last_line(lines: list[str], needle: str) -> str | None:
    for line in reversed(lines):
        if needle in line:
            return line
    return None


def filter_lines(lines: list[str], needles: list[str], limit: int = 10) -> list[str]:
    hits = [line for line in lines if any(needle in line for needle in needles)]
    return hits[-limit:]


def summarize_entry_pressure(signal_analysis: dict[str, Any], direction_payload: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    if signal_analysis.get("enter_long_true", 0) == 0 and signal_analysis.get("enter_short_true", 0) == 0:
        reasons.append("recent diagnostics show no passing long/short entries")
    for blocker, _count in signal_analysis.get("top_long_blockers", [])[:2]:
        reasons.append(f"long blocker: {blocker}")
    for blocker, _count in signal_analysis.get("top_short_blockers", [])[:2]:
        reasons.append(f"short blocker: {blocker}")
    if isinstance(direction_payload, dict) and direction_payload.get("final_action") == "hold":
        reasons.append("direction advisor is currently hold")
    return reasons[:6]


def check_container_strategy_code(freqtrade_container: str) -> CheckResult:
    checks = {
        "has_hold_policy": "grep -n 'direction_advisor_hold_policy' /freqtrade/user_data/strategies/SampleStrategy.py",
        "has_hold_advisory": "grep -n 'hold_advisory' /freqtrade/user_data/strategies/SampleStrategy.py",
    }
    details: dict[str, Any] = {"container": freqtrade_container}
    missing: list[str] = []
    for key, command in checks.items():
        code, output = docker_exec_output(freqtrade_container, command)
        details[key] = {"exit_code": code, "output": output}
        if code != 0:
            missing.append(key)
    status = "ok" if not missing else "warn"
    summary = "container strategy code includes latest gate logic" if not missing else "container strategy code looks outdated"
    details["missing_markers"] = missing
    return CheckResult("container_strategy_code", status, summary, details)


def check_signal_path_sync(freqtrade_container: str, host_path: Path) -> CheckResult:
    host_age = minutes_old(host_path)
    host_lines = file_line_count(host_path)
    code, output = docker_exec_output(
        freqtrade_container,
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "p = Path('/freqtrade/user_data/signals/signal_diagnostics.jsonl')\n"
        "if not p.exists():\n"
        "    print('missing')\n"
        "else:\n"
        "    print(int(p.stat().st_mtime))\n"
        "    print(sum(1 for _ in p.open('r', encoding='utf-8')))\n"
        "PY",
    )
    container_exists = code == 0 and output and output.splitlines()[0] != "missing"
    container_age = None
    container_lines = None
    if container_exists:
        lines = output.splitlines()
        try:
            mtime = int(lines[0])
            container_age = round((utc_now().timestamp() - mtime) / 60.0, 2)
        except Exception:
            container_age = None
        try:
            container_lines = int(lines[1])
        except Exception:
            container_lines = None
    status = "ok"
    warnings: list[str] = []
    if not container_exists:
        status = "warn"
        warnings.append("container signal diagnostics file missing")
    if host_age is not None and container_age is not None and abs(host_age - container_age) > 2:
        status = "warn"
        warnings.append("host/container diagnostics file age mismatch")
    if host_lines is not None and container_lines is not None and host_lines != container_lines:
        status = "warn"
        warnings.append("host/container diagnostics line count mismatch")
    summary = "signal diagnostics path looks in sync" if not warnings else "; ".join(warnings)
    return CheckResult(
        "signal_path_sync",
        status,
        summary,
        {
            "container": freqtrade_container,
            "host_path": str(host_path),
            "host_age_minutes": host_age,
            "host_line_count": host_lines,
            "container_age_minutes": container_age,
            "container_line_count": container_lines,
            "warnings": warnings,
        },
    )


def check_core_logs(
    *,
    freqtrade_container: str,
    researcher_container: str,
    signal_analysis: dict[str, Any],
    direction_payload: dict[str, Any] | None,
    lines: int,
) -> list[CheckResult]:
    results: list[CheckResult] = []

    freqtrade_lines = docker_logs_tail(freqtrade_container, lines)
    if freqtrade_lines:
        heartbeat = find_last_line(freqtrade_lines, "Bot heartbeat")
        inference = find_last_line(freqtrade_lines, "Total time spent inferencing pairlist")
        gate = find_last_line(freqtrade_lines, "Direction advisor gate applied")
        startup = find_last_line(freqtrade_lines, "Strategy:*")
        errors = filter_lines(freqtrade_lines, [" ERROR ", " WARNING ", "Traceback", "Failed to "], limit=12)
        entry_lines = filter_lines(
            freqtrade_lines,
            [
                "confirm_trade_entry",
                "enter_long",
                "enter_short",
                "Hotcoin",
                "Signal diagnostics",
                "Direction advisor gate applied",
            ],
            limit=20,
        )
        pressure = summarize_entry_pressure(signal_analysis, direction_payload)
        status = "ok" if heartbeat else "warn"
        if not entry_lines and pressure:
            status = "warn"
        summary = "heartbeat present"
        if not entry_lines and pressure:
            summary = "bot alive but no recent entry activity"
        results.append(
            CheckResult(
                "freqtrade_logs",
                status,
                summary,
                {
                    "container": freqtrade_container,
                    "last_heartbeat": heartbeat,
                    "last_inference": inference,
                    "last_direction_gate": gate,
                    "startup_strategy_line": startup,
                    "recent_entry_related_lines": entry_lines,
                    "recent_warnings_errors": errors,
                    "entry_pressure_summary": pressure,
                },
            )
        )
        results.append(check_container_strategy_code(freqtrade_container))

    researcher_lines = docker_logs_tail(researcher_container, lines)
    if researcher_lines:
        errors = filter_lines(researcher_lines, ["Traceback", "FileNotFoundError", "RuntimeError", "Command failed"], limit=20)
        formal_pool_written = find_last_line(researcher_lines, "formal_strategy_pool")
        status = "ok" if not errors else "warn"
        summary = "researcher logs clean" if not errors else "researcher reported errors"
        results.append(
            CheckResult(
                "strategy_researcher_logs",
                status,
                summary,
                {
                    "container": researcher_container,
                    "recent_errors": errors,
                    "formal_pool_related_line": formal_pool_written,
                },
            )
        )

    return results


def check_strategy_alignment(
    config_payload: dict[str, Any] | None,
    trade_settings_payload: dict[str, Any] | None,
    pair_selection_payload: dict[str, Any] | None,
    direction_payload: dict[str, Any] | None,
    formal_pool_payload: dict[str, Any] | None,
) -> CheckResult:
    config_strategy = None
    pair_strategy = None
    direction_strategy = None
    final_action = None
    allowed_strategies: list[str] = []
    hold_policy = "normal"
    if isinstance(config_payload, dict):
        config_strategy = config_payload.get("strategy")
    if isinstance(trade_settings_payload, dict):
        hold_policy = str(trade_settings_payload.get("direction_advisor_hold_policy", "normal")).strip().lower() or "normal"
    if isinstance(pair_selection_payload, dict):
        pair_strategy = pair_selection_payload.get("recommended_strategy")
    if isinstance(direction_payload, dict):
        direction_strategy = direction_payload.get("recommended_strategy")
        final_action = direction_payload.get("final_action")
    if isinstance(formal_pool_payload, dict) and isinstance(formal_pool_payload.get("allowed_strategies"), list):
        allowed_strategies = list(formal_pool_payload.get("allowed_strategies") or [])

    warnings: list[str] = []
    if config_strategy and allowed_strategies and config_strategy not in allowed_strategies:
        warnings.append("configured strategy is outside formal pool")
    if config_strategy and pair_strategy and config_strategy != pair_strategy:
        warnings.append("configured strategy differs from pair selection recommendation")
    if config_strategy and direction_strategy and config_strategy != direction_strategy:
        warnings.append("configured strategy differs from direction advisor recommendation")
    if final_action == "hold" and hold_policy == "hold":
        warnings.append("direction advisor currently blocks new entries with hold")

    status = "ok" if not warnings else "warn"
    summary = "aligned" if not warnings else "; ".join(warnings)
    details = {
        "config_strategy": config_strategy,
        "pair_selection_strategy": pair_strategy,
        "direction_strategy": direction_strategy,
        "direction_final_action": final_action,
        "direction_hold_policy": hold_policy,
        "formal_pool_allowed_strategies": allowed_strategies,
        "warnings": warnings,
    }
    return CheckResult("strategy_alignment", status, summary, details)


def build_results(args: argparse.Namespace) -> list[CheckResult]:
    config_path = Path(args.config)
    trade_settings_path = Path(args.trade_settings)
    formal_pool_path = Path(args.formal_pool)
    pair_selection_path = Path(args.pair_selection)
    direction_path = Path(args.direction_advisor)
    signal_path = Path(args.signal_diagnostics)
    trade_db_path = Path(args.trade_db)

    config_result = check_config(config_path)
    trade_settings_result = check_trade_settings(trade_settings_path)
    formal_pool_result = check_formal_pool(formal_pool_path, args.max_age_minutes)
    pair_selection_result = check_pair_selection(pair_selection_path, args.max_age_minutes)
    direction_result = check_direction_advisor(direction_path, args.max_age_minutes)
    signal_result = check_signal_diagnostics(signal_path, args.signal_limit)
    signal_freshness_result = check_signal_diagnostics_freshness(
        signal_path, args.signal_limit, args.signal_stale_minutes
    )
    trade_db_result = check_trade_db(trade_db_path)

    config_payload = read_json(config_path)
    trade_settings_payload = read_json(trade_settings_path)
    pair_selection_payload = read_json(pair_selection_path)
    direction_payload = read_json(direction_path)
    formal_pool_payload = read_json(formal_pool_path)
    signal_records = read_jsonl_tail(signal_path, args.signal_limit)
    signal_analysis = analyze_signal_diagnostics(signal_records)

    results = [
        config_result,
        trade_settings_result,
        formal_pool_result,
        pair_selection_result,
        direction_result,
        signal_result,
        signal_freshness_result,
        trade_db_result,
        check_strategy_alignment(
            config_payload if isinstance(config_payload, dict) else None,
            trade_settings_payload if isinstance(trade_settings_payload, dict) else None,
            pair_selection_payload if isinstance(pair_selection_payload, dict) else None,
            direction_payload if isinstance(direction_payload, dict) else None,
            formal_pool_payload if isinstance(formal_pool_payload, dict) else None,
        ),
    ]
    if not args.skip_docker:
        results.insert(0, check_docker(args.containers))
        results.extend(
            check_core_logs(
                freqtrade_container="freqtrade",
                researcher_container="freqtrade-strategy-researcher",
                signal_analysis=signal_analysis,
                direction_payload=direction_payload if isinstance(direction_payload, dict) else None,
                lines=args.log_tail,
            )
        )
        results.append(check_signal_path_sync("freqtrade", signal_path))
    return results


def overall_status(results: list[CheckResult]) -> str:
    statuses = {item.status for item in results}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def print_human(results: list[CheckResult]) -> None:
    print(f"overall: {overall_status(results)}")
    for item in results:
        print(f"[{item.status.upper()}] {item.name}: {item.summary}")
        for key, value in item.details.items():
            print(f"  - {key}: {json.dumps(value, ensure_ascii=False)}")


def main() -> int:
    args = parse_args()
    results = build_results(args)
    if args.json:
        payload = {
            "generated_at": utc_now().isoformat(),
            "overall_status": overall_status(results),
            "results": [asdict(item) for item in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
