import sys
from types import SimpleNamespace

import pytest

from trading_system.exchange import CcxtOkxExchange


@pytest.mark.asyncio
async def test_okx_exchange_disables_private_currency_fetch_before_loading_markets(monkeypatch) -> None:
    class FakeOkx:
        def __init__(self) -> None:
            self.has = {"fetchCurrencies": True}
            self.loaded_with_fetch_currencies = None

        async def load_markets(self) -> None:
            self.loaded_with_fetch_currencies = self.has["fetchCurrencies"]

        async def set_position_mode(self, hedge_mode: bool) -> None:
            assert hedge_mode is True

    fake = FakeOkx()
    monkeypatch.setitem(sys.modules, "ccxt.pro", SimpleNamespace(okx=lambda config: fake))

    exchange = CcxtOkxExchange({})
    await exchange.initialize()

    assert fake.loaded_with_fetch_currencies is False
