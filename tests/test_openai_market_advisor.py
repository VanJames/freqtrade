from scripts import openai_market_advisor


def test_build_agent_assessments_blocks_when_snapshot_has_no_data():
    agents = openai_market_advisor.build_agent_assessments(
        {"summary": {"observations": 0}, "per_pair": []}
    )

    assert agents["final_judge"]["profile"] == "conservative"
    assert agents["risk_agent"]["risk_level"] == "blocked"


def test_build_agent_assessments_allows_aggressive_when_trends_align():
    agents = openai_market_advisor.build_agent_assessments(
        {
            "summary": {
                "observations": 6,
                "median_atr_pct": 0.01,
                "median_bb_width": 0.03,
                "trend_up_count": 4,
                "trend_down_count": 0,
            },
            "per_pair": [{} for _ in range(6)],
        }
    )

    assert agents["final_judge"]["profile"] == "aggressive"
    assert agents["risk_agent"]["risk_level"] == "medium"


def test_merge_agent_assessments_keeps_valid_llm_profile():
    snapshot = {
        "summary": {
            "observations": 1,
            "median_atr_pct": 0.0,
            "median_bb_width": 0.0,
            "trend_up_count": 0,
            "trend_down_count": 0,
        },
        "per_pair": [{}],
    }

    result = openai_market_advisor.merge_agent_assessments(
        {"profile": "balanced", "confidence": 0.8, "reason": "llm"}, snapshot
    )

    assert result["profile"] == "balanced"
    assert result["agents"]["final_judge"]["profile"] == "conservative"
    assert result["risk_level"] == "low_activity"


def test_merge_agent_assessments_replaces_invalid_profile():
    result = openai_market_advisor.merge_agent_assessments(
        {"profile": "unsafe"},
        {"summary": {"observations": 0}, "per_pair": []},
    )

    assert result["profile"] == "conservative"
    assert "agents" in result
