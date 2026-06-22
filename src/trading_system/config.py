from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    okx_api_key: str = ""
    okx_secret: str = ""
    okx_password: str = ""
    okx_demo: bool = True
    exchange_id: str = "okx"
    hotcoin_base_url: str = "https://bi.hotcoins.cn"
    hotcoin_device_id: str = "P_qWAfKXrQ5xBBUlEAUReGmPALdmrgafYJ"
    dry_run: bool = True
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "BTC/USDT:USDT",
            "ETH/USDT:USDT",
            "SOL/USDT:USDT",
            "XAU/USDT:USDT",
        ]
    )
    postgres_dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379/0"
    live_ohlcv_timeout_seconds: float = 120.0
    exchange_request_timeout_seconds: float = 45.0
    diagnostics_log_interval_seconds: float = 60.0
    position_monitor_interval_seconds: float = 3.0
    positions_cache_ttl_seconds: float = 2.0
    live_entry_order_cooldown_seconds: float = 60.0
    live_exit_order_cooldown_seconds: float = 30.0
    risk_rejection_email_cooldown_seconds: float = 3600.0
    min_live_equity_to_order: float = 5.0

    risk_percent: float = 0.012
    same_direction_risk_limit: float = 0.08
    daily_drawdown_limit: float = 0.05
    shock_leverage_limit: float = 3.0
    trend_symbol_leverage_limit: float = 5.0
    max_signal_risk_multiplier: float = 3.0
    confirmation_position_sizing: bool = True
    confirmation_max_risk_multiplier: float = 3.0
    legacy_a_risk_sizing: bool = False
    funding_block_threshold: float = 0.001
    spike_amplitude_threshold: float = 0.035
    spike_lock_seconds: int = 7200
    min_stop_loss_pct: float = 0.002
    high_vol_min_stop_loss_pct: float = 0.004
    extreme_vol_min_stop_loss_pct: float = 0.005
    max_stop_loss_pct: float = 0.012
    shock_reward_risk: float = 1.15
    trend_reward_risk: float = 1.8
    trailing_gap_pct: float = 0.0025
    min_trailing_activate_r: float = 1.0
    enable_trend_short: bool = True
    trend_long_risk_multiplier: float = 0.35
    trend_short_risk_multiplier: float = 0.5
    defensive_risk_multiplier: float = 0.7
    shock_trend_risk_multiplier: float = 0.1
    shock_trend_down_risk_multiplier: float = 0.1
    enable_shock_trend_scout: bool = True
    shock_trend_scout_risk_multiplier: float = 1.5
    enable_liquidity_sweep_reversal: bool = False
    liquidity_sweep_risk_multiplier: float = 0.8
    liquidity_sweep_require_confirmation: bool = True
    enable_two_candle_momentum: bool = False
    enable_daily_macd_breakout: bool = True
    enable_liquidation_plugin: bool = True
    liquidation_plugin_weight: float = 0.65
    coinglass_heatmap_url: str = ""
    coinglass_session_path: str = ""
    coinglass_heatmap_ranges: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["24 hour"]
    )
    coinglass_screenshot_dir: str = ""
    liquidation_plugin_analysis_interval: str = "15m"
    liquidation_plugin_scrape_min_seconds: int = 3600
    liquidation_plugin_max_staleness_seconds: int = 7200
    liquidation_plugin_price_move_trigger: float = 0.01
    liquidation_plugin_price_move_cooldown_seconds: int = 900
    liquidation_plugin_price_sanity_max_distance: float = 0.50
    liquidation_plugin_liquidation_sufficiency: float = 0.7
    liquidation_plugin_near_tie: float = 0.05
    liquidation_plugin_stood_above: float = 0.001
    liquidation_plugin_broke_below: float = 0.001
    liquidation_plugin_volume_confirmation: float = 1.2
    enable_adaptive_strategy_switch: bool = True
    adaptive_no_trade_hours: float = 48.0
    adaptive_min_range_24h_pct: float = 0.012
    min_take_profit_pct: float = 0.004
    llm_regime_review_enabled: bool = False
    llm_regime_provider: str = "openai"
    llm_regime_model: str = "gpt-4.1-mini"
    llm_regime_base_url: str = ""
    llm_regime_api_key: str = ""
    llm_regime_api_key_env: str = ""
    llm_regime_review_cache_ttl_seconds: int = 900
    llm_regime_review_min_interval_seconds: int = 900
    email_enabled: bool = False
    email_user: str = ""
    email_pass: str = ""
    email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value  # type: ignore[return-value]

    @field_validator("coinglass_heatmap_ranges", mode="before")
    @classmethod
    def parse_coinglass_heatmap_ranges(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value  # type: ignore[return-value]

    def okx_config(self) -> dict[str, object]:
        config: dict[str, object] = {
            "apiKey": self.okx_api_key,
            "secret": self.okx_secret,
            "password": self.okx_password,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if self.okx_demo:
            config["headers"] = {"x-simulated-trading": "1"}
        return config
