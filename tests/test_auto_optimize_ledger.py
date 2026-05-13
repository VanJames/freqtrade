import json
from datetime import UTC, datetime
from unittest.mock import patch

from scripts import auto_optimize


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 5, 13, 9, tzinfo=tz or UTC)


def test_build_research_ledger_record_contains_decision_fields():
    summary = {
        "run_id": "20260513-010203",
        "strategy": "SampleStrategy",
        "market_profile": "balanced",
        "advisor_profile": "balanced",
        "advisor_confidence": 0.7,
        "baseline": {"trades": 3},
        "candidate": {"trades": 4},
        "would_promote": True,
        "promoted": False,
        "reason": "dry run",
    }

    record = auto_optimize.build_research_ledger_record(summary)

    assert record["record_type"] == "autoopt_run"
    assert record["run_id"] == "20260513-010203"
    assert record["baseline"] == {"trades": 3}
    assert record["would_promote"] is True
    assert record["promoted"] is False


def test_append_jsonl_appends_one_json_record(tmp_path):
    path = tmp_path / "research_ledger.jsonl"

    auto_optimize.append_jsonl(path, {"run_id": "r1", "promoted": False})

    assert json.loads(path.read_text().strip()) == {"promoted": False, "run_id": "r1"}


def test_build_walk_forward_timerange_uses_latest_complete_utc_day():
    with patch.object(auto_optimize, "datetime", FrozenDateTime):
        train, confirm = auto_optimize.build_walk_forward_timeranges(1, 0)

    assert train == "20260512-20260513"
    assert confirm == "20260512-20260513"


def test_build_walk_forward_timerange_has_no_gap_before_confirmation():
    with patch.object(auto_optimize, "datetime", FrozenDateTime):
        train, confirm = auto_optimize.build_walk_forward_timeranges(7, 2)

    assert train == "20260504-20260511"
    assert confirm == "20260511-20260513"
