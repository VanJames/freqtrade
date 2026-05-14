try:
    from user_data.strategies.AIGeneratedLongTrendContinuationRunner2x import (
        AIGeneratedLongTrendContinuationRunner2x,
    )
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from AIGeneratedLongTrendContinuationRunner2x import AIGeneratedLongTrendContinuationRunner2x

import json
from pathlib import Path


class AIGeneratedLongTrendContinuationRunner2xSelective(
    AIGeneratedLongTrendContinuationRunner2x
):
    """
    Restrict the runner to pairs with positive recent full-period attribution.

    This is a pragmatic bridge before implementing a fully dynamic pair
    selection pipeline.  It keeps the proven entry/exit logic and removes the
    pairs that dragged the latest 30-day portfolio negative.
    """

    fallback_pairs = {
        "BTC/USDT:USDT",
        "SOL/USDT:USDT",
        "XLM/USDT:USDT",
        "LINK/USDT:USDT",
    }
    pair_selection_path = Path(__file__).resolve().parents[1] / "autoopt" / "pair_selection.json"

    def _load_allowed_pairs(self) -> set[str]:
        try:
            payload = json.loads(self.pair_selection_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set(self.fallback_pairs)

        strategies = payload.get("strategies", {}) if isinstance(payload, dict) else {}
        current = strategies.get(self.__class__.__name__, {}) if isinstance(strategies, dict) else {}
        allowed = current.get("allowed_pairs", []) if isinstance(current, dict) else []
        cleaned = {str(item).strip() for item in allowed if str(item).strip()}
        return cleaned or set(self.fallback_pairs)

    def populate_entry_trend(self, dataframe, metadata):
        dataframe = super().populate_entry_trend(dataframe, metadata)
        if metadata.get("pair") not in self._load_allowed_pairs():
            dataframe["enter_long"] = 0
            dataframe["enter_short"] = 0
        return dataframe
