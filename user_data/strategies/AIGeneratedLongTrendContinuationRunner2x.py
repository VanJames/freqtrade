try:
    from user_data.strategies.AIGeneratedLongTrendContinuationRunner import (
        AIGeneratedLongTrendContinuationRunner,
    )
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuationRunner import AIGeneratedLongTrendContinuationRunner


class AIGeneratedLongTrendContinuationRunner2x(AIGeneratedLongTrendContinuationRunner):
    """Runner with capped 2x leverage. This scales both profit and loss."""

    def leverage(
        self,
        pair,
        current_time,
        current_rate,
        proposed_leverage,
        max_leverage,
        entry_tag,
        side,
        **kwargs,
    ) -> float:
        return min(2.0, float(max_leverage or 1.0))
