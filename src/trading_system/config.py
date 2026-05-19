from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    okx_api_key: str = ""
    okx_secret: str = ""
    okx_password: str = ""
    okx_demo: bool = True
    dry_run: bool = True
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    )
    postgres_dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379/0"

    risk_percent: float = 0.01
    same_direction_risk_limit: float = 0.03
    daily_drawdown_limit: float = 0.05
    shock_leverage_limit: float = 3.0
    trend_symbol_leverage_limit: float = 5.0
    max_signal_risk_multiplier: float = 1.5
    funding_block_threshold: float = 0.001
    spike_amplitude_threshold: float = 0.035
    spike_lock_seconds: int = 7200
    min_stop_loss_pct: float = 0.002
    high_vol_min_stop_loss_pct: float = 0.004
    extreme_vol_min_stop_loss_pct: float = 0.005
    max_stop_loss_pct: float = 0.012
    shock_reward_risk: float = 1.15
    trend_reward_risk: float = 1.8
    min_take_profit_pct: float = 0.004
    llm_regime_review_enabled: bool = False
    llm_regime_provider: str = "openai"
    llm_regime_model: str = "gpt-4.1-mini"
    llm_regime_base_url: str = ""
    llm_regime_api_key_env: str = ""
    llm_regime_review_cache_ttl_seconds: int = 900
    llm_regime_review_min_interval_seconds: int = 900

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> list[str]:
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
