from __future__ import annotations

import pytest

from trading_system.config import Settings
from trading_system.runtime_config import apply_runtime_config, validate_runtime_config


def test_runtime_config_applies_llm_text_and_boolean_fields() -> None:
    settings = Settings(dry_run=True)

    values = validate_runtime_config(
        {
            "llm_regime_review_enabled": "1",
            "llm_regime_provider": "deepseek",
            "llm_regime_model": "deepseek-v4-pro",
            "llm_regime_base_url": "https://api.deepseek.com",
            "llm_regime_review_cache_ttl_seconds": "1800",
            "llm_regime_review_min_interval_seconds": "600",
        },
        settings,
    )
    apply_runtime_config(settings, values)

    assert settings.llm_regime_review_enabled is True
    assert settings.llm_regime_provider == "deepseek"
    assert settings.llm_regime_model == "deepseek-v4-pro"
    assert settings.llm_regime_base_url == "https://api.deepseek.com"
    assert settings.llm_regime_review_cache_ttl_seconds == 1800
    assert settings.llm_regime_review_min_interval_seconds == 600


def test_runtime_config_rejects_unknown_llm_provider() -> None:
    with pytest.raises(ValueError, match="llm_regime_provider"):
        validate_runtime_config({"llm_regime_provider": "invalid"}, Settings(dry_run=True))


def test_runtime_config_boolean_accepts_float_roundtrip() -> None:
    settings = Settings(dry_run=True)

    apply_runtime_config(
        settings,
        {
            "confirmation_position_sizing": 1.0,
            "enable_shock_trend_scout": 1.0,
            "llm_regime_review_enabled": 1.0,
        },
    )

    assert settings.confirmation_position_sizing is True
    assert settings.enable_shock_trend_scout is True
    assert settings.llm_regime_review_enabled is True


def test_runtime_config_applies_position_monitor_interval() -> None:
    settings = Settings(dry_run=True)

    apply_runtime_config(settings, {"position_monitor_interval_seconds": "1.5"})

    assert settings.position_monitor_interval_seconds == 1.5


def test_runtime_config_applies_positions_cache_ttl() -> None:
    settings = Settings(dry_run=True)

    apply_runtime_config(settings, {"positions_cache_ttl_seconds": "1.0"})

    assert settings.positions_cache_ttl_seconds == 1.0


def test_runtime_config_applies_email_smtp_fields() -> None:
    settings = Settings(dry_run=True)

    apply_runtime_config(
        settings,
        {
            "email_enabled": "1",
            "email_user": "sender@example.com",
            "email_pass": "secret",
            "email_to": "receiver@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "smtp_use_ssl": "1",
        },
    )

    assert settings.email_enabled is True
    assert settings.email_user == "sender@example.com"
    assert settings.email_pass == "secret"
    assert settings.email_to == "receiver@example.com"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_port == 465
    assert settings.smtp_use_ssl is True
