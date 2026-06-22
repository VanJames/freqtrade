"""Command line interface for trading notice workflows."""

from __future__ import annotations

import argparse
import os
import sys

from trading_notice.config import ConfigurationError, load_analysis_config
from trading_notice.scheduler import run_forever, run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-notice")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run-once", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--symbol")
        cmd.add_argument("--interval")
        cmd.add_argument("--recipients")
        cmd.add_argument("--coinglass-heatmap-url")
        cmd.add_argument("--coinglass-session-path")
        cmd.add_argument("--coinglass-heatmap-ranges")
        cmd.add_argument("--scrape-mapping-version")
        cmd.add_argument("--kline-check-interval-seconds")
        cmd.add_argument("--scrape-min-interval-seconds")
        cmd.add_argument("--max-scrape-staleness-seconds")
        cmd.add_argument("--scrape-failure-backoff-seconds")
        cmd.add_argument("--liquidation-price-move-trigger")
        cmd.add_argument("--liquidation-price-move-cooldown-seconds")
        cmd.add_argument("--price-sanity-max-relative-distance")
        cmd.add_argument("--failure-cooldown-seconds")
        cmd.add_argument("--dry-run-email", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_available()
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    _set_if_present(env, "TRADING_SYMBOL", args.symbol)
    _set_if_present(env, "ANALYSIS_INTERVAL", args.interval)
    _set_if_present(env, "RECIPIENTS", args.recipients)
    _set_if_present(env, "COINGLASS_HEATMAP_URL", args.coinglass_heatmap_url)
    _set_if_present(env, "SCRAPE_MAPPING_VERSION", args.scrape_mapping_version)
    _set_if_present(env, "KLINE_CHECK_INTERVAL_SECONDS", args.kline_check_interval_seconds)
    _set_if_present(env, "SCRAPE_MIN_INTERVAL_SECONDS", args.scrape_min_interval_seconds)
    _set_if_present(env, "MAX_SCRAPE_STALENESS_SECONDS", args.max_scrape_staleness_seconds)
    _set_if_present(env, "SCRAPE_FAILURE_BACKOFF_SECONDS", args.scrape_failure_backoff_seconds)
    _set_if_present(env, "LIQUIDATION_PRICE_MOVE_TRIGGER", args.liquidation_price_move_trigger)
    _set_if_present(
        env,
        "LIQUIDATION_PRICE_MOVE_COOLDOWN_SECONDS",
        args.liquidation_price_move_cooldown_seconds,
    )
    _set_if_present(
        env,
        "PRICE_SANITY_MAX_RELATIVE_DISTANCE",
        args.price_sanity_max_relative_distance,
    )
    _set_if_present(env, "FAILURE_COOLDOWN_SECONDS", args.failure_cooldown_seconds)
    _set_if_present(env, "COINGLASS_SESSION_PATH", args.coinglass_session_path)
    _set_if_present(env, "COINGLASS_HEATMAP_RANGES", args.coinglass_heatmap_ranges)
    try:
        config = load_analysis_config(env)
    except ConfigurationError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 1
    _print_config_summary(args.command, config, args.dry_run_email)
    if args.command == "run":
        run_forever(config, dry_run_email=args.dry_run_email)
        return 0
    print("notice_cycle_start")
    decision = run_once(config, dry_run_email=args.dry_run_email)
    _print_decision(decision)
    return 2 if decision.status == "failure" else 0


def _set_if_present(env: dict[str, str], key: str, value: object | None) -> None:
    if value not in (None, ""):
        env[key] = str(value)


def _load_dotenv_if_available() -> None:
    if os.environ.get("TRADING_NOTICE_SKIP_DOTENV") == "1":
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(".env", override=False)


def _print_config_summary(command: str, config, dry_run_email: bool) -> None:
    print(
        "notice_start "
        f"command={command} symbol={config.symbol} interval={config.analysis_interval} "
        f"ranges={','.join(config.coinglass_heatmap_ranges)} "
        f"scrape_min_seconds={config.scrape_min_interval_seconds} "
        f"max_staleness_seconds={config.max_scrape_staleness_seconds} "
        f"dry_run_email={str(dry_run_email).lower()}",
        flush=True,
    )


def _print_decision(decision) -> None:
    parts = [
        f"cycle_status={decision.status}",
        f"symbol={decision.symbol}",
        f"scenario={decision.matched_scenario}",
    ]
    if decision.target_take_profit is not None:
        parts.append(f"target_take_profit={decision.target_take_profit:.8f}")
    if decision.suggested_entry_price is not None:
        parts.append(f"suggested_entry_price={decision.suggested_entry_price:.8f}")
    if decision.stop_loss is not None:
        parts.append(f"stop_loss={decision.stop_loss:.8f}")
    if decision.no_trade_reason:
        parts.append(f"no_trade_reason={decision.no_trade_reason}")
    if decision.failure:
        parts.append(f"failure_category={decision.failure.category}")
        parts.append(f"failure_message={decision.failure.safe_message}")
    if decision.basis:
        parts.append(f"basis={decision.basis[:240]}")
    print(" ".join(parts), flush=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
