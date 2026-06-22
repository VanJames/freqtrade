import pytest

from trading_notice.config import ConfigurationError, load_analysis_config


def base_env():
    return {
        "TRADING_SYMBOL": "ETH/USDT",
        "ANALYSIS_INTERVAL": "15m",
        "RECIPIENTS": "trader@example.test",
        "COINGLASS_HEATMAP_URL": "https://example.test/heatmap",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_FROM": "alerts@example.test",
    }


def test_load_analysis_config_defaults_are_documented_and_secret_refs_only():
    config = load_analysis_config(base_env())

    assert config.liquidation_periods == ("15M", "1H", "4H", "1D")
    assert config.kline_periods == ("15m", "1h", "4h", "1d")
    assert config.kline_exchange_id == "okx"
    assert config.kline_check_interval_seconds == 60
    assert config.scrape_min_interval_seconds == 3600
    assert config.max_scrape_staleness_seconds == 7200
    assert config.liquidation_price_move_trigger == 0.01
    assert config.liquidation_price_move_cooldown_seconds == 900
    assert config.price_sanity_bounds.max_relative_distance == 0.50
    assert config.price_sanity_bounds.source_basis == "planning_input_engineering_guardrail"
    assert config.price_sanity_bounds.pending_backtest_validation is True
    assert config.thresholds.pending_backtest_validation is True
    assert config.smtp_settings_ref["SMTP_HOST"] == "SMTP_HOST"
    assert config.coinglass_auth_ref == {}


def test_load_analysis_config_tracks_coinglass_secret_refs_only():
    env = base_env()
    env["COINGLASS_EMAIL"] = "user@example.test"
    env["COINGLASS_PASSWORD"] = "secret"

    config = load_analysis_config(env)

    assert config.coinglass_auth_ref == {
        "email": "COINGLASS_EMAIL",
        "password": "COINGLASS_PASSWORD",
    }
    assert "secret" not in repr(config)


def test_load_analysis_config_accepts_coinglass_session_path():
    env = base_env()
    env["COINGLASS_EMAIL"] = "user@example.test"
    env["COINGLASS_PASSWORD"] = "secret"
    env["COINGLASS_SESSION_PATH"] = "/tmp/coinglass-session.json"

    config = load_analysis_config(env)

    assert config.coinglass_session_path == "/tmp/coinglass-session.json"


def test_load_analysis_config_accepts_coinglass_heatmap_ranges():
    env = base_env()
    env["COINGLASS_HEATMAP_RANGES"] = "12 hour,24 hour,48 hour"

    config = load_analysis_config(env)

    assert config.coinglass_heatmap_ranges == ("12 hour", "24 hour", "48 hour")


def test_load_analysis_config_rejects_partial_coinglass_login():
    env = base_env()
    env["COINGLASS_EMAIL"] = "user@example.test"

    with pytest.raises(ConfigurationError) as exc:
        load_analysis_config(env)

    assert "COINGLASS_EMAIL and COINGLASS_PASSWORD" in str(exc.value)


def test_load_analysis_config_rejects_missing_required_values():
    env = base_env()
    del env["SMTP_HOST"]

    with pytest.raises(ConfigurationError) as exc:
        load_analysis_config(env)

    assert "SMTP_HOST" in str(exc.value)


def test_load_analysis_config_rejects_staleness_shorter_than_scrape_interval():
    env = base_env()
    env["SCRAPE_MIN_INTERVAL_SECONDS"] = "3600"
    env["MAX_SCRAPE_STALENESS_SECONDS"] = "120"

    with pytest.raises(ConfigurationError):
        load_analysis_config(env)
