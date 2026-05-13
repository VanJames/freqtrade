try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

from freqtrade.strategy import DecimalParameter, IntParameter


class SampleStrategyScalp(SampleStrategy):
    """
    Higher-frequency candidate for dry-run validation.

    This variant intentionally lowers entry thresholds more than
    SampleStrategyActive, but keeps the same protections, stop logic, direction
    gate, Hotcoin bridge, and Freqtrade execution path.
    """

    adx_threshold = IntParameter(10, 26, default=14, space="buy", optimize=True, load=True)
    long_rsi_trigger = IntParameter(26, 44, default=34, space="buy", optimize=True, load=True)
    short_rsi_trigger = IntParameter(52, 70, default=54, space="sell", optimize=True, load=True)
    volume_ratio_min = DecimalParameter(
        0.60, 1.80, default=0.80, decimals=2, space="buy", optimize=True, load=True
    )
    ai_edge_threshold = DecimalParameter(
        0.0010, 0.0140, default=0.0030, decimals=4, space="buy", optimize=True, load=True
    )
    risk_per_trade = DecimalParameter(
        0.0015, 0.0060, default=0.0030, decimals=4, space="buy", optimize=True, load=True
    )
