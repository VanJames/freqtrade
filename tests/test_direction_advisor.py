from scripts import direction_advisor


def test_side_from_tag_detects_direction():
    assert direction_advisor.side_from_tag("trend_long") == "long"
    assert direction_advisor.side_from_tag(["trend_short", "short_exit"]) == "short"
    assert direction_advisor.side_from_tag("TOTAL") is None


def test_extract_direction_metrics_aggregates_enter_tags():
    stats = {
        "strategy": {
            "S": {
                "max_drawdown_account": 0.01,
                "results_per_enter_tag": [
                    {
                        "key": "trend_long",
                        "trades": 2,
                        "wins": 1,
                        "losses": 1,
                        "profit_total": 0.02,
                        "profit_total_abs": 2.0,
                    },
                    {
                        "key": "meanrev_long",
                        "trades": 1,
                        "wins": 1,
                        "losses": 0,
                        "profit_total": 0.01,
                        "profit_total_abs": 1.0,
                    },
                    {
                        "key": "trend_short",
                        "trades": 3,
                        "wins": 1,
                        "losses": 2,
                        "profit_total": -0.02,
                        "profit_total_abs": -2.0,
                    },
                ],
            }
        }
    }

    metrics = direction_advisor.extract_direction_metrics("S", stats)
    long = next(item for item in metrics if item.side == "long")
    short = next(item for item in metrics if item.side == "short")

    assert long.trades == 3
    assert long.profit_total_pct == 3.0
    assert long.profit_total_abs == 3.0
    assert short.trades == 3
    assert short.profit_total_pct == -2.0


def test_choose_recommendation_holds_without_positive_side():
    metrics = [
        direction_advisor.DirectionMetrics(
            strategy="S",
            side="long",
            trades=3,
            wins=1,
            losses=2,
            winrate=0.3333,
            profit_total_pct=-1.0,
            profit_total_abs=-1.0,
            score=-1.0,
        )
    ]

    action, best, reason = direction_advisor.choose_recommendation(
        metrics, min_trades=3, min_profit_pct=0.0
    )

    assert action == "hold"
    assert best is None
    assert "No strategy side" in reason


def test_choose_recommendation_selects_best_positive_side():
    metrics = [
        direction_advisor.DirectionMetrics(
            strategy="A",
            side="long",
            trades=3,
            wins=2,
            losses=1,
            winrate=0.6667,
            profit_total_pct=1.0,
            profit_total_abs=1.0,
            score=1.0,
        ),
        direction_advisor.DirectionMetrics(
            strategy="B",
            side="short",
            trades=4,
            wins=3,
            losses=1,
            winrate=0.75,
            profit_total_pct=0.8,
            profit_total_abs=0.8,
            score=2.0,
        ),
    ]

    action, best, _ = direction_advisor.choose_recommendation(
        metrics, min_trades=3, min_profit_pct=0.0
    )

    assert action == "short"
    assert best.strategy == "B"


def test_apply_llm_confirmation_blocks_disagreement():
    payload = {"action": "long", "reason": "stats passed"}
    review = {"enabled": True, "action": "short"}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "hold"
    assert "disagreed" in result["final_reason"]


def test_apply_llm_confirmation_cannot_override_hold():
    payload = {"action": "hold", "reason": "stats failed"}
    review = {"enabled": True, "action": "long"}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "hold"
    assert "not allowed" in result["final_reason"]


def test_normalize_llm_review_clamps_to_schema():
    result = direction_advisor.normalize_llm_review(
        {
            "action": "buy",
            "confidence": 2,
            "reason": "x",
            "parameter_bias": {"risk": "moon", "entry": "looser", "exit": "faster"},
            "warnings": "careful",
        }
    )

    assert result["enabled"] is True
    assert result["action"] == "hold"
    assert result["confidence"] == 1.0
    assert result["parameter_bias"] == {"risk": "normal", "entry": "looser", "exit": "faster"}
    assert result["warnings"] == ["careful"]


def test_build_feedback_summary_groups_historical_recommendations():
    summary = direction_advisor.build_feedback_summary(
        [
            {"run_id": "r1", "final_action": "short", "best": {"profit_total_pct": 0.4}},
            {"run_id": "r2", "final_action": "short", "best": {"profit_total_pct": -0.1}},
            {"run_id": "r3", "final_action": "long", "best": {"profit_total_pct": 0.2}},
        ]
    )

    assert summary["sample_size"] == 3
    assert summary["by_action"]["short"]["count"] == 2
    assert summary["by_action"]["short"]["positive_rate"] == 0.5
    assert summary["by_action"]["short"]["avg_profit_total_pct"] == 0.15
    assert summary["last_run_id"] == "r3"
