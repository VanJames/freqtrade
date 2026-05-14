try:
    from user_data.strategies.AIGeneratedLongTrendContinuation import AIGeneratedLongTrendContinuation
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuation import AIGeneratedLongTrendContinuation


class AIGeneratedLongTrendContinuationRunner(AIGeneratedLongTrendContinuation):
    """Same proven entry as LongTrendContinuation with wider targets."""

    minimal_roi = {"0": 0.032, "45": 0.014, "150": 0.0}
    stoploss = -0.045
    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.030
    trailing_only_offset_is_reached = False
