try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy


class SampleStrategyLongOnly(SampleStrategy):
    """
    Same signal engine as SampleStrategy, but disables short entries.

    This is used by the strategy selector to test whether current market
    conditions favor long-only execution without duplicating the base strategy.
    """

    can_short = False

    def populate_entry_trend(self, dataframe, metadata):
        dataframe = super().populate_entry_trend(dataframe, metadata)
        dataframe["enter_short"] = 0
        return dataframe
