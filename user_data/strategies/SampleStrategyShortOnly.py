try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy


class SampleStrategyShortOnly(SampleStrategy):
    """
    Same signal engine as SampleStrategy, but disables long entries.

    This lets the selector switch to a defensive short-only profile when
    long entries are the dominant source of drawdown.
    """

    def populate_entry_trend(self, dataframe, metadata):
        dataframe = super().populate_entry_trend(dataframe, metadata)
        dataframe["enter_long"] = 0
        return dataframe
