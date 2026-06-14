from trading_system.config import Settings


def test_okx_config_skips_private_currency_fetch_for_demo_markets() -> None:
    config = Settings(okx_demo=True).okx_config()

    assert config["headers"] == {"x-simulated-trading": "1"}
    assert config["options"] == {"defaultType": "swap", "fetchCurrencies": False}
