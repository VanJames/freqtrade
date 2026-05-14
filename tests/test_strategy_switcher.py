import argparse
import sqlite3
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


def test_should_not_switch_when_target_required_window_is_negative():
    registry = registry_for()
    registry["window_evaluations"] = [
        {
            "strategy": "SampleStrategyPullbackShort",
            "window_days": 30,
            "trades": 20,
            "passed": False,
            "profit_total_pct": -1.2,
        }
    ]

    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry,
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        required_positive_windows={30},
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "target failed required positive window gate"


def test_effective_allow_strategies_prefers_formal_pool():
    resolved = strategy_switcher.effective_allow_strategies(
        ["SampleStrategy", "SampleStrategyPullbackShort"],
        {"allowed_strategies": ["AIGeneratedWinner"]},
    )

    assert resolved == {"AIGeneratedWinner"}


def test_formal_pool_priority_bonus_reads_bonus_from_priority_scores():
    bonus = strategy_switcher.formal_pool_priority_bonus(
        {
            "priority_scores": {"AIGeneratedWinner": 1.15},
            "strategies": [{"strategy": "AIGeneratedWinner", "score": 1.0}],
        },
        "AIGeneratedWinner",
    )

    assert bonus == 0.15


def test_should_switch_to_ai_generated_strategy_after_gates_pass():
    registry = registry_for(target="AIGeneratedLongTrendContinuation", target_score=0.5)
    registry["recommended"]["side"] = "long"
    registry["strategy_scores"][0]["profit_total_pct"] = 0.9
    registry["window_evaluations"] = [
        {
            "strategy": "AIGeneratedLongTrendContinuation",
            "window_days": 30,
            "trades": 8,
            "passed": True,
            "profit_total_pct": 0.71,
        }
    ]

    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry,
        {},
        allow_strategies={"SampleStrategy"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        required_positive_windows={30},
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is True
    assert decision.target_strategy == "AIGeneratedLongTrendContinuation"


def test_should_switch_when_formal_pool_priority_bonus_pushes_margin_over_threshold():
    registry = registry_for(target="AIGeneratedWinner", target_score=0.3, current_score=0.2)
    registry["recommended"]["side"] = "long"
    registry["strategy_scores"][0]["profit_total_pct"] = 0.9
    registry["window_evaluations"] = [
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 30,
            "trades": 8,
            "passed": True,
            "profit_total_pct": 0.71,
        }
    ]

    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry,
        {},
        formal_pool={
            "priority_scores": {"AIGeneratedWinner": 0.5},
            "strategies": [{"strategy": "AIGeneratedWinner", "score": 0.3}],
        },
        allow_strategies={"SampleStrategy", "AIGeneratedWinner"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        required_positive_windows={30},
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is True
    assert decision.target_strategy == "AIGeneratedWinner"
    assert decision.details["target_priority_bonus"] == 0.2


def test_should_not_switch_to_ai_generated_strategy_when_disabled():
    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry_for(target="AIGeneratedLongTrendContinuation", target_score=0.5),
        {},
        allow_strategies={"SampleStrategy"},
        allow_ai_generated=False,
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "target strategy not allowed"


def test_should_not_switch_when_pool_required_window_has_no_positive_strategy():
    registry = registry_for()
    registry["window_evaluations"] = [
        {
            "strategy": "SampleStrategy",
            "window_days": 30,
            "trades": 20,
            "passed": False,
            "profit_total_pct": -0.5,
        },
        {
            "strategy": "SampleStrategyPullbackShort",
            "window_days": 30,
            "trades": 20,
            "passed": False,
            "profit_total_pct": -1.2,
        },
    ]

    decision = strategy_switcher.should_switch(
        {"strategy": "SampleStrategy"},
        registry,
        {},
        allow_strategies={"SampleStrategy", "SampleStrategyPullbackShort"},
        open_trades=0,
        open_orders=0,
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        pool_positive_windows={30},
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "strategy pool failed required positive window gate"


def test_required_positive_window_fails_when_window_is_missing():
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
        required_positive_windows={30},
        max_registry_age_minutes=180,
        cooldown_minutes=360,
    )

    assert decision.should_switch is False
    assert decision.reason == "target failed required positive window gate"
    assert decision.details["target_required_positive_windows"]["missing_days"] == [30]


def test_open_trade_order_counts_handles_missing_database(tmp_path):
    assert strategy_switcher.open_trade_order_counts(tmp_path / "missing.sqlite") == (0, 0)


def test_valid_strategies_include_range_and_breakout():
    assert "SampleStrategyRangeMeanReversion" in strategy_switcher.VALID_STRATEGIES
    assert "SampleStrategyBreakoutMomentum" in strategy_switcher.VALID_STRATEGIES


def test_desired_pair_whitelist_prefers_strategy_specific_allowed_pairs():
    config = {"exchange": {"pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"]}}
    pair_selection = {
        "recommended_strategy": "AIGeneratedLongTrendContinuationRunner2xSelective",
        "recommended_allowed_pairs": ["BTC/USDT:USDT"],
        "strategies": {
            "AIGeneratedLongTrendContinuationRunner2xSelective": {
                "allowed_pairs": ["BTC/USDT:USDT", "SOL/USDT:USDT"]
            }
        },
    }

    result = strategy_switcher.desired_pair_whitelist(
        config,
        pair_selection,
        "AIGeneratedLongTrendContinuationRunner2xSelective",
    )

    assert result == ["BTC/USDT:USDT", "SOL/USDT:USDT"]


def test_desired_pair_whitelist_falls_back_to_current_config():
    config = {"exchange": {"pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"]}}

    result = strategy_switcher.desired_pair_whitelist(
        config,
        {},
        "SampleStrategy",
    )

    assert result == ["BTC/USDT:USDT", "ETH/USDT:USDT"]


def test_backup_and_write_config_updates_pair_whitelist(tmp_path):
    config_path = tmp_path / "config.json"
    config = {
        "strategy": "SampleStrategy",
        "exchange": {"pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
    }
    strategy_switcher.write_json(config_path, config)

    backup_path = strategy_switcher.backup_and_write_config(
        config_path,
        config,
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        pair_whitelist=["BTC/USDT:USDT", "SOL/USDT:USDT"],
    )
    written = strategy_switcher.load_json(config_path, {})

    assert backup_path.exists()
    assert written["strategy"] == "AIGeneratedLongTrendContinuationRunner2xSelective"
    assert written["exchange"]["pair_whitelist"] == ["BTC/USDT:USDT", "SOL/USDT:USDT"]


def _create_trade_db(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table trades (
                id integer primary key,
                strategy varchar(100),
                is_open boolean not null,
                close_profit float,
                close_profit_abs float,
                close_date datetime,
                exit_reason varchar(255),
                pair varchar(25)
            )
            """
        )
        conn.executemany(
            """
            insert into trades (strategy, is_open, close_profit, close_profit_abs, close_date, exit_reason, pair)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_recent_closed_trade_stats_counts_losses(tmp_path):
    db_path = tmp_path / "trades.sqlite"
    _create_trade_db(
        db_path,
        [
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.02, -2.0, "2026-05-14 10:00:00", "stoploss", "BTC/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, 0.01, 1.0, "2026-05-14 09:00:00", "roi", "SOL/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.01, -1.0, "2026-05-14 08:00:00", "stoploss", "LINK/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.03, -3.0, "2026-05-14 07:00:00", "stoploss", "BTC/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, 0.02, 2.0, "2026-05-14 06:00:00", "roi", "SOL/USDT:USDT"),
            ("OtherStrategy", 0, -0.04, -4.0, "2026-05-14 11:00:00", "stoploss", "ETH/USDT:USDT"),
        ],
    )

    stats = strategy_switcher.recent_closed_trade_stats(
        db_path,
        "AIGeneratedLongTrendContinuationRunner2x",
        5,
    )

    assert stats["count"] == 5
    assert stats["losses"] == 3
    assert stats["wins"] == 2


def test_recent_trade_stats_from_rows_counts_recent_wins_and_losses():
    rows = [
        {"strategy": "AIGeneratedLongTrendContinuationRunner2x", "close_date": "2026-05-14 10:00:00", "close_profit_abs": -2.0, "is_loss": True},
        {"strategy": "AIGeneratedLongTrendContinuationRunner2x", "close_date": "2026-05-14 09:00:00", "close_profit_abs": 1.0, "is_loss": False},
        {"strategy": "AIGeneratedLongTrendContinuationRunner2x", "close_date": "2026-05-14 08:00:00", "close_profit_abs": -1.0, "is_loss": True},
        {"strategy": "AIGeneratedLongTrendContinuationRunner2x", "close_date": "2026-05-14 07:00:00", "close_profit_abs": -3.0, "is_loss": True},
        {"strategy": "AIGeneratedLongTrendContinuationRunner2x", "close_date": "2026-05-14 06:00:00", "close_profit_abs": 2.0, "is_loss": False},
    ]

    stats = strategy_switcher.recent_trade_stats_from_rows(
        rows,
        "AIGeneratedLongTrendContinuationRunner2x",
        5,
    )

    assert stats["count"] == 5
    assert stats["losses"] == 3
    assert stats["wins"] == 2


def test_maybe_apply_recent_trade_override_triggers_recovery():
    decision = strategy_switcher.SwitchDecision(
        False,
        "already using recommended strategy",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        {
            "last_switch_age_minutes": 6000,
            "current_score": {"score": 0.2},
            "target_score": {"score": 0.8},
        },
    )
    recovery_stats = {
        "count": 5,
        "wins": 3,
        "losses": 2,
        "rows": [],
    }

    updated = strategy_switcher.maybe_apply_recent_trade_override(
        decision,
        current_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        allow_strategies={
            "AIGeneratedLongTrendContinuationRunner2x",
            "AIGeneratedLongTrendContinuationRunner2xSelective",
        },
        allow_ai_generated=True,
        cooldown_minutes=360,
        loss_fallback_from_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_fallback_to_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_fallback_lookback_trades=5,
        loss_fallback_loss_threshold=3,
        loss_fallback_stats={"count": 0, "wins": 0, "losses": 0, "rows": []},
        loss_recovery_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_recovery_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_recovery_lookback_trades=5,
        loss_recovery_win_threshold=3,
        loss_recovery_min_hold_days=3,
        loss_recovery_min_score_margin=0.4,
        trend_activation_from_strategy="",
        trend_activation_to_strategy="",
        trend_activation_required_side="",
        trend_activation_min_recommended_score=0.0,
        trend_activation_min_score_margin=0.0,
        loss_recovery_stats=recovery_stats,
    )

    assert updated.should_switch is True
    assert updated.target_strategy == "AIGeneratedLongTrendContinuationRunner2x"
    assert updated.reason == "recent closed-trade win threshold triggered recovery"


def test_maybe_apply_recent_trade_override_blocks_recovery_until_min_hold_and_score_margin():
    decision = strategy_switcher.SwitchDecision(
        False,
        "already using recommended strategy",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        {
            "last_switch_age_minutes": 1200,
            "current_score": {"score": 0.5},
            "target_score": {"score": 0.7},
        },
    )
    recovery_stats = {
        "count": 5,
        "wins": 4,
        "losses": 1,
        "rows": [],
    }

    updated = strategy_switcher.maybe_apply_recent_trade_override(
        decision,
        current_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        allow_strategies={
            "AIGeneratedLongTrendContinuationRunner2x",
            "AIGeneratedLongTrendContinuationRunner2xSelective",
        },
        allow_ai_generated=True,
        cooldown_minutes=360,
        loss_fallback_from_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_fallback_to_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_fallback_lookback_trades=5,
        loss_fallback_loss_threshold=3,
        loss_fallback_stats={"count": 0, "wins": 0, "losses": 0, "rows": []},
        loss_recovery_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_recovery_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_recovery_lookback_trades=5,
        loss_recovery_win_threshold=3,
        loss_recovery_min_hold_days=3,
        loss_recovery_min_score_margin=0.4,
        trend_activation_from_strategy="",
        trend_activation_to_strategy="",
        trend_activation_required_side="",
        trend_activation_min_recommended_score=0.0,
        trend_activation_min_score_margin=0.0,
        loss_recovery_stats=recovery_stats,
    )

    assert updated is decision


def test_maybe_apply_recent_trade_override_blocks_recommended_switch_back_until_recovery_ready():
    decision = strategy_switcher.SwitchDecision(
        True,
        "passed switch gates",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        "AIGeneratedLongTrendContinuationRunner2x",
        {
            "last_switch_age_minutes": 1200,
            "current_score": {"score": 0.5},
            "target_score": {"score": 0.7},
            "switch_mode": "recommended",
        },
    )
    recovery_stats = {
        "count": 5,
        "wins": 4,
        "losses": 1,
        "rows": [],
    }

    updated = strategy_switcher.maybe_apply_recent_trade_override(
        decision,
        current_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        allow_strategies={
            "AIGeneratedLongTrendContinuationRunner2x",
            "AIGeneratedLongTrendContinuationRunner2xSelective",
        },
        allow_ai_generated=True,
        cooldown_minutes=360,
        loss_fallback_from_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_fallback_to_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_fallback_lookback_trades=5,
        loss_fallback_loss_threshold=3,
        loss_fallback_stats={"count": 0, "wins": 0, "losses": 0, "rows": []},
        loss_recovery_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_recovery_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_recovery_lookback_trades=5,
        loss_recovery_win_threshold=3,
        loss_recovery_min_hold_days=3,
        loss_recovery_min_score_margin=0.4,
        trend_activation_from_strategy="",
        trend_activation_to_strategy="",
        trend_activation_required_side="",
        trend_activation_min_recommended_score=0.0,
        trend_activation_min_score_margin=0.0,
        loss_recovery_stats=recovery_stats,
    )

    assert updated.should_switch is False
    assert updated.reason == "recovery min hold not reached"
    assert updated.details["switch_mode"] == "recovery_lock"


def test_maybe_apply_recent_trade_override_allows_trend_activation_override():
    decision = strategy_switcher.SwitchDecision(
        True,
        "passed switch gates",
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        "AIGeneratedLongTrendContinuationRunner2x",
        {
            "last_switch_age_minutes": 1200,
            "current_score": {"score": 0.2},
            "target_score": {"score": 1.0},
            "recommended": {
                "strategy": "AIGeneratedLongTrendContinuationRunner2x",
                "side": "long",
                "score": 0.62,
            },
            "switch_mode": "recommended",
        },
    )
    recovery_stats = {
        "count": 5,
        "wins": 1,
        "losses": 4,
        "rows": [],
    }

    updated = strategy_switcher.maybe_apply_recent_trade_override(
        decision,
        current_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        allow_strategies={
            "AIGeneratedLongTrendContinuationRunner2x",
            "AIGeneratedLongTrendContinuationRunner2xSelective",
        },
        allow_ai_generated=True,
        cooldown_minutes=360,
        loss_fallback_from_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_fallback_to_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_fallback_lookback_trades=5,
        loss_fallback_loss_threshold=3,
        loss_fallback_stats={"count": 0, "wins": 0, "losses": 0, "rows": []},
        loss_recovery_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_recovery_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_recovery_lookback_trades=5,
        loss_recovery_win_threshold=3,
        loss_recovery_min_hold_days=3,
        loss_recovery_min_score_margin=0.4,
        trend_activation_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        trend_activation_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        trend_activation_required_side="long",
        trend_activation_min_recommended_score=0.55,
        trend_activation_min_score_margin=0.5,
        loss_recovery_stats=recovery_stats,
    )

    assert updated.should_switch is True
    assert updated.reason == "trend activation override"
    assert updated.details["switch_mode"] == "trend_activation"


def test_run_once_switches_to_selective_after_three_losses_in_last_five(tmp_path):
    config_path = tmp_path / "config.json"
    registry_path = tmp_path / "registry.json"
    pair_selection_path = tmp_path / "pair_selection.json"
    formal_pool_path = tmp_path / "formal_strategy_pool.json"
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    db_path = tmp_path / "trades.sqlite"

    strategy_switcher.write_json(
        config_path,
        {
            "strategy": "AIGeneratedLongTrendContinuationRunner2x",
            "exchange": {"pair_whitelist": ["BTC/USDT:USDT", "SOL/USDT:USDT", "LINK/USDT:USDT"]},
        },
    )
    strategy_switcher.write_json(registry_path, registry_for(target="AIGeneratedLongTrendContinuationRunner2x"))
    strategy_switcher.write_json(
        pair_selection_path,
        {
            "strategies": {
                "AIGeneratedLongTrendContinuationRunner2xSelective": {
                    "allowed_pairs": ["BTC/USDT:USDT", "SOL/USDT:USDT"]
                }
            }
        },
    )
    strategy_switcher.write_json(
        formal_pool_path,
        {
            "allowed_strategies": [
                "AIGeneratedLongTrendContinuationRunner2x",
                "AIGeneratedLongTrendContinuationRunner2xSelective",
            ]
        },
    )
    _create_trade_db(
        db_path,
        [
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.02, -2.0, "2026-05-14 10:00:00", "stoploss", "BTC/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, 0.01, 1.0, "2026-05-14 09:00:00", "roi", "SOL/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.01, -1.0, "2026-05-14 08:00:00", "stoploss", "LINK/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, -0.03, -3.0, "2026-05-14 07:00:00", "stoploss", "BTC/USDT:USDT"),
            ("AIGeneratedLongTrendContinuationRunner2x", 0, 0.02, 2.0, "2026-05-14 06:00:00", "roi", "SOL/USDT:USDT"),
        ],
    )

    args = argparse.Namespace(
        config=str(config_path),
        registry=str(registry_path),
        pair_selection=str(pair_selection_path),
        formal_pool=str(formal_pool_path),
        ledger=str(ledger_path),
        state=str(state_path),
        db=str(db_path),
        backend="docker",
        container="freqtrade",
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        disable_inactivity_fallback=False,
        inactive_window_days=1,
        inactive_min_trades=10,
        inactive_min_profit_pct=0.05,
        inactive_min_score=-1.25,
        inactive_confirm_windows=[2, 7],
        require_positive_window_days=[],
        pool_positive_window_days=[],
        min_required_window_profit_pct=0.0,
        max_registry_age_minutes=180.0,
        cooldown_minutes=360.0,
        allow_strategies=[
            "AIGeneratedLongTrendContinuationRunner2x",
            "AIGeneratedLongTrendContinuationRunner2xSelective",
        ],
        disable_ai_generated_switches=False,
        dry_run=True,
        loss_fallback_from_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_fallback_to_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_fallback_lookback_trades=5,
        loss_fallback_loss_threshold=3,
        loss_recovery_from_strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        loss_recovery_to_strategy="AIGeneratedLongTrendContinuationRunner2x",
        loss_recovery_lookback_trades=5,
        loss_recovery_win_threshold=3,
        loss_recovery_min_hold_days=3,
        loss_recovery_min_score_margin=0.4,
        trend_activation_from_strategy="",
        trend_activation_to_strategy="",
        trend_activation_required_side="",
        trend_activation_min_recommended_score=0.0,
        trend_activation_min_score_margin=0.0,
    )

    payload = strategy_switcher.run_once(args)

    assert payload["should_switch"] is True
    assert payload["target_strategy"] == "AIGeneratedLongTrendContinuationRunner2xSelective"
    assert payload["reason"] == "recent closed-trade loss threshold triggered fallback"
    assert payload["details"]["switch_mode"] == "loss_fallback"


def test_run_once_blocks_switch_when_target_not_in_formal_pool(tmp_path):
    config_path = tmp_path / "config.json"
    registry_path = tmp_path / "registry.json"
    pair_selection_path = tmp_path / "pair_selection.json"
    formal_pool_path = tmp_path / "formal_strategy_pool.json"
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    db_path = tmp_path / "trades.sqlite"

    strategy_switcher.write_json(
        config_path,
        {
            "strategy": "SampleStrategy",
            "exchange": {"pair_whitelist": ["BTC/USDT:USDT"]},
        },
    )
    strategy_switcher.write_json(registry_path, registry_for(target="SampleStrategyPullbackShort"))
    strategy_switcher.write_json(pair_selection_path, {})
    strategy_switcher.write_json(formal_pool_path, {"allowed_strategies": ["SampleStrategy"]})
    _create_trade_db(db_path, [])

    args = argparse.Namespace(
        config=str(config_path),
        registry=str(registry_path),
        pair_selection=str(pair_selection_path),
        formal_pool=str(formal_pool_path),
        ledger=str(ledger_path),
        state=str(state_path),
        db=str(db_path),
        backend="docker",
        container="freqtrade",
        min_passed_windows=2,
        min_trades=5,
        min_score_margin=0.15,
        disable_inactivity_fallback=False,
        inactive_window_days=1,
        inactive_min_trades=10,
        inactive_min_profit_pct=0.05,
        inactive_min_score=-1.25,
        inactive_confirm_windows=[2, 7],
        require_positive_window_days=[],
        pool_positive_window_days=[],
        min_required_window_profit_pct=0.0,
        max_registry_age_minutes=180.0,
        cooldown_minutes=360.0,
        allow_strategies=["SampleStrategy", "SampleStrategyPullbackShort"],
        disable_ai_generated_switches=False,
        dry_run=True,
        loss_fallback_from_strategy="",
        loss_fallback_to_strategy="",
        loss_fallback_lookback_trades=0,
        loss_fallback_loss_threshold=0,
        loss_recovery_from_strategy="",
        loss_recovery_to_strategy="",
        loss_recovery_lookback_trades=0,
        loss_recovery_win_threshold=0,
        loss_recovery_min_hold_days=0.0,
        loss_recovery_min_score_margin=0.0,
        trend_activation_from_strategy="",
        trend_activation_to_strategy="",
        trend_activation_required_side="",
        trend_activation_min_recommended_score=0.0,
        trend_activation_min_score_margin=0.0,
    )

    payload = strategy_switcher.run_once(args)

    assert payload["should_switch"] is False
    assert payload["reason"] == "target strategy not allowed"
    assert payload["effective_allow_strategies"] == ["SampleStrategy"]
