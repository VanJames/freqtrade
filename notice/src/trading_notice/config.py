"""Configuration loading for trading notice workflows."""

from __future__ import annotations

import os
from collections.abc import Mapping

from trading_notice.models import AnalysisConfiguration, PriceSanityBounds, ThresholdSet


REQUIRED_SMTP_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")
OPTIONAL_SMTP_ENV = ("SMTP_USERNAME", "SMTP_PASSWORD")


class ConfigurationError(ValueError):
    """Raised when runtime configuration is incomplete or unsafe."""


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def load_analysis_config(env: Mapping[str, str] | None = None) -> AnalysisConfiguration:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_SMTP_ENV if not env.get(name)]
    for name in ("TRADING_SYMBOL", "ANALYSIS_INTERVAL", "RECIPIENTS", "COINGLASS_HEATMAP_URL"):
        if not env.get(name):
            missing.append(name)
    if missing:
        raise ConfigurationError(f"Missing required configuration: {', '.join(sorted(missing))}")

    scrape_min_interval = _int_env(env, "SCRAPE_MIN_INTERVAL_SECONDS", 3600)
    max_staleness = _int_env(env, "MAX_SCRAPE_STALENESS_SECONDS", scrape_min_interval * 2)
    if max_staleness < scrape_min_interval:
        raise ConfigurationError("MAX_SCRAPE_STALENESS_SECONDS must be >= scrape interval")

    smtp_refs = {name: name for name in (*REQUIRED_SMTP_ENV, *OPTIONAL_SMTP_ENV) if env.get(name)}
    coinglass_auth_ref: dict[str, str] = {}
    coinglass_email = env.get("COINGLASS_EMAIL")
    coinglass_password = env.get("COINGLASS_PASSWORD")
    if bool(coinglass_email) != bool(coinglass_password):
        raise ConfigurationError("COINGLASS_EMAIL and COINGLASS_PASSWORD must be set together")
    if coinglass_email and coinglass_password:
        coinglass_auth_ref = {
            "email": "COINGLASS_EMAIL",
            "password": "COINGLASS_PASSWORD",
        }
    current_market_price = None
    if env.get("CURRENT_MARKET_PRICE"):
        current_market_price = _float_env(env, "CURRENT_MARKET_PRICE", 0.0)

    return AnalysisConfiguration(
        symbol=env["TRADING_SYMBOL"],
        analysis_interval=env["ANALYSIS_INTERVAL"],
        recipients=_split_csv(env["RECIPIENTS"]),
        coinglass_heatmap_url=env["COINGLASS_HEATMAP_URL"],
        scrape_mapping_version=env.get("SCRAPE_MAPPING_VERSION", "coinglass-free-heatmap-v1"),
        coinglass_heatmap_ranges=_split_csv(env.get("COINGLASS_HEATMAP_RANGES", "24 hour")),
        liquidation_periods=_split_csv(env.get("LIQUIDATION_PERIODS", "15M,1H,4H,1D")),
        kline_periods=_split_csv(env.get("KLINE_PERIODS", "15m,1h,4h,1d")),
        kline_check_interval_seconds=_int_env(env, "KLINE_CHECK_INTERVAL_SECONDS", 60),
        scrape_min_interval_seconds=scrape_min_interval,
        scrape_failure_backoff_seconds=_int_env(env, "SCRAPE_FAILURE_BACKOFF_SECONDS", 300),
        max_scrape_staleness_seconds=max_staleness,
        liquidation_price_move_trigger=_float_env(env, "LIQUIDATION_PRICE_MOVE_TRIGGER", 0.01),
        liquidation_price_move_cooldown_seconds=_int_env(
            env, "LIQUIDATION_PRICE_MOVE_COOLDOWN_SECONDS", 900
        ),
        price_sanity_bounds=PriceSanityBounds(
            max_relative_distance=_float_env(env, "PRICE_SANITY_MAX_RELATIVE_DISTANCE", 0.50)
        ),
        thresholds=ThresholdSet(),
        failure_cooldown_seconds=_int_env(env, "FAILURE_COOLDOWN_SECONDS", 900),
        smtp_settings_ref=smtp_refs,
        coinglass_auth_ref=coinglass_auth_ref,
        coinglass_session_path=env.get("COINGLASS_SESSION_PATH") or None,
        current_market_price=current_market_price,
        screenshot_dir=env.get("SCREENSHOT_DIR") or None,
    )
