from datetime import UTC, datetime, timedelta

from scripts import strategy_switcher


def registry_for(target="SampleStrategyPullbackShort", target_score=1.0, current_score=0.2):
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "recommended": {
            "strategy": target,
            "side": "short",
            "score": target_score,
        },
        "strategy_scores": [
            {
                "strategy": target,
                "score": target_score,
                "passed_windows": 2,
                "trades": 12,
            },
            {
                "strategy": "SampleStrategy",
                "score": current_score,
                "passed_windows": 1,
                "trades": 3,
            },
        ],
    }


def inactive_registry_for():
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "recommended": {
            "strategy": "SampleStrategy",
            "side": "short",
            "score": -0.6214,
        },
        "strategy_scores": [
            {
                "strategy": "SampleStrategy",
                "score": -0.6214,
                "passed_windows": 2,
                "trades": 21,
                "profit_total_pct": 0.8659,
            },
            {
                "strategy": "SampleStrategyPullbackShort",
                "score": -0.9194,
                "passed_windows": 2,
                "trades": 56,
                "profit_total_pct": 0.2253,
            },
        ],
        "window_evaluations": [
            {
                "strategy": "SampleStrategy",
                "window_days": 1,
                "trades": 0,
                "passed": False,
                "profit_total_pct": 0.0,
            },
            {
                "strategy": "SampleStrategyPullbackShort",
                "window_days": 2,
                "trades": 16,
                "passed": True,
                "profit_total_pct": 0.1212,
            },
            {
                "strategy": "SampleStrategyPullbackShort",
                "window_days": 7,
                "trades": 40,
                "passed": True,
                "profit_total_pct": 0.1041,
            },
        ],
    }


def test_should_switch_when_recommended_strategy_passes_gates():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry_for(),
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is True
    assert decision.target_strategy == "SampleStrategyPullbackShort"


def test_should_not_switch_with_open_trade():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry_for(),
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=1,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "open trades or orders exist"


def test_should_not_switch_when_score_margin_is_too_small():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry_for(target_score=0.3, current_score=0.2),
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "score margin below threshold"


def test_should_not_switch_during_cooldown():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry_for(),
        {"last_switch_at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat()},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "switch cooldown active"


def test_should_switch_to_active_fallback_when_current_strategy_is_inactive():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        inactive_registry_for(),
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is True
    assert decision.reason == "current strategy inactive; active fallback passed gates"
    assert decision.target_strategy == "SampleStrategyPullbackShort"
    assert decision.details["switch_mode"] == "inactivity_fallback"


def test_should_not_use_active_fallback_when_disabled():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        inactive_registry_for(),
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        inactivity_fallback_enabled=False,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "already using recommended strategy"


def test_open_trade_order_counts_handles_missing_database(tmp_path):
    assert strategy_switcher.open_trade_order_counts(tmp_path / "missing.sqlite") == (0, 0)


def test_valid_strategies_include_range_and_breakout():
    assert "SampleStrategyRangeMeanReversion" in strategy_switcher.VALID_STRATEGIES
    assert "SampleStrategyBreakoutMomentum" in strategy_switcher.VALID_STRATEGIES
