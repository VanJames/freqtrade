from types import SimpleNamespace

from trading_system.liquidation_plugin import LiquidationDirectionPlugin


def plugin_settings(**overrides):
    values = {
        "enable_liquidation_plugin": True,
        "liquidation_plugin_weight": 0.65,
        "liquidation_plugin_liquidation_sufficiency": 0.7,
        "liquidation_plugin_near_tie": 0.05,
        "liquidation_plugin_stood_above": 0.001,
        "liquidation_plugin_broke_below": 0.001,
        "liquidation_plugin_volume_confirmation": 1.2,
        "liquidation_plugin_analysis_interval": "15m",
        "coinglass_heatmap_url": "https://example.test/heatmap",
        "coinglass_heatmap_ranges": ["24 hour"],
        "liquidation_plugin_scrape_min_seconds": 3600,
        "liquidation_plugin_max_staleness_seconds": 7200,
        "liquidation_plugin_price_move_trigger": 0.01,
        "liquidation_plugin_price_move_cooldown_seconds": 900,
        "liquidation_plugin_price_sanity_max_distance": 0.5,
        "coinglass_session_path": "/tmp/session.json",
        "coinglass_screenshot_dir": "/tmp/coinglass",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_liquidation_plugin_config_passes_coinglass_secret_refs(monkeypatch):
    monkeypatch.setenv("COINGLASS_EMAIL", "trader@example.test")
    monkeypatch.setenv("COINGLASS_PASSWORD", "secret-password")

    config = LiquidationDirectionPlugin(plugin_settings())._config("ETH/USDT:USDT", 1750.0)

    assert config.coinglass_auth_ref == {
        "email": "COINGLASS_EMAIL",
        "password": "COINGLASS_PASSWORD",
    }
