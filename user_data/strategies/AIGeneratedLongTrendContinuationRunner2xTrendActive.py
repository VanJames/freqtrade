try:
    from user_data.strategies.AIGeneratedLongTrendContinuationRunner2x import (
        AIGeneratedLongTrendContinuationRunner2x,
    )
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuationRunner2x import AIGeneratedLongTrendContinuationRunner2x


class AIGeneratedLongTrendContinuationRunner2xTrendActive(
    AIGeneratedLongTrendContinuationRunner2x
):
    """
    Slightly more active trend continuation runner.

    It keeps the original signal structure but tunes risk/exit a bit tighter
    so short-lived momentum bursts can still contribute without turning into a
    medium-horizon drag.
    """

    ai_candidate = {
        **AIGeneratedLongTrendContinuationRunner2x.ai_candidate,
        "class_name": "AIGeneratedLongTrendContinuationRunner2xTrendActive",
        "reason": "Active continuation variant with tighter exits and slightly stronger confirmation.",
        "adx_min": 22.0,
        "volume_min": 0.9,
        "rsi_long_max": 47,
    }

    minimal_roi = {"0": 0.026, "30": 0.01, "120": 0.0}
    stoploss = -0.038
    trailing_stop_positive = 0.009
    trailing_stop_positive_offset = 0.02
