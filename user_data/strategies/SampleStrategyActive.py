try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

from freqtrade.strategy import DecimalParameter, IntParameter


class SampleStrategyActive(SampleStrategy):
    """
    More active variant of SampleStrategy for short-term validation.

    This keeps the same signal structure and risk controls, but lowers the
    optimized entry gates so the researcher can compare whether a higher signal
    frequency improves recent 1d/2d/7d performance.
    """

    adx_threshold = IntParameter(14, 30, default=18, space="buy", optimize=True, load=True)
    long_rsi_trigger = IntParameter(28, 44, default=36, space="buy", optimize=True, load=True)
    short_rsi_trigger = IntParameter(54, 72, default=56, space="sell", optimize=True, load=True)
    volume_ratio_min = DecimalParameter(
        0.80, 2.20, default=1.10, decimals=2, space="buy", optimize=True, load=True
    )
    ai_edge_threshold = DecimalParameter(
        0.0030, 0.0200, default=0.0060, decimals=4, space="buy", optimize=True, load=True
    )
