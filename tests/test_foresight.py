from __future__ import annotations

from trading_system.foresight import ForesightPoint, ForesightProvider


def test_foresight_detects_liquidation_percentile_and_oi_growth() -> None:
    provider = ForesightProvider()
    for idx in range(480):
        provider.update(
            "BTC/USDT:USDT",
            ForesightPoint(
                timestamp_ms=idx,
                short_liquidation=1 if idx < 464 else 100,
                long_liquidation=1,
                open_interest=100 if idx < 464 else 100 + (idx - 464) * 2,
                price=100 + idx,
            ),
        )

    data = provider.get("BTC/USDT:USDT")

    assert data.short_liq_p95_hit
    assert not data.long_liq_p95_hit
    assert data.oi_change_4h > 0.15

