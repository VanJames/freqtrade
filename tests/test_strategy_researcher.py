import sqlite3

from scripts import strategy_researcher


def stats_for(long_profit=0.0, short_profit=0.0, long_trades=0, short_trades=0):
    return {
        "strategy": {
            "SampleStrategy": {
                "max_drawdown_account": 0.001,
                "results_per_enter_tag": [
                    {
                        "key": "trend_long",
                        "trades": long_trades,
                        "wins": long_trades,
                        "losses": 0,
                        "profit_total": long_profit / 100.0,
                        "profit_total_abs": long_profit,
                    },
                    {
                        "key": "trend_short",
                        "trades": short_trades,
                        "wins": short_trades,
                        "losses": 0,
                        "profit_total": short_profit / 100.0,
                        "profit_total_abs": short_profit,
                    },
                ],
                "results_per_pair": [
                    {
                        "key": "BTC/USDT:USDT",
                        "trades": long_trades + short_trades,
                        "wins": long_trades + short_trades,
                        "losses": 0,
                        "profit_total": max(long_profit, short_profit) / 100.0,
                        "profit_total_abs": max(long_profit, short_profit),
                    },
                    {
                        "key": "ETH/USDT:USDT",
                        "trades": 1,
                        "wins": 0,
                        "losses": 1,
                        "profit_total": -0.1 / 100.0,
                        "profit_total_abs": -0.1,
                    },
                ],
            }
        }
    }


def test_infer_factor_candidates_prefers_downtrend():
    snapshot = {
        "summary": {
            "pair_count": 3,
            "trend_up_count": 0,
            "trend_down_count": 3,
            "median_atr_pct": 0.003,
            "median_bb_width": 0.02,
        }
    }

    factors = strategy_researcher.infer_factor_candidates(snapshot)
    trend = next(item for item in factors if item.name == "trend_momentum")

    assert trend.side_bias == "short"
    assert trend.market_score > 1.0


def test_best_direction_for_window_selects_profitable_side():
    best, reason = strategy_researcher.best_direction_for_window(
        "SampleStrategy",
        stats_for(long_profit=-0.2, short_profit=0.4, long_trades=2, short_trades=2),
        min_trades=1,
        min_profit_pct=0.0,
    )

    assert reason == "passed"
    assert best.side == "short"


def test_aggregate_strategy_score_requires_passed_window():
    evaluations = [
        strategy_researcher.WindowEvaluation(
            strategy="SampleStrategy",
            window_days=1,
            timerange="20260512-20260513",
            side="short",
            trades=2,
            profit_total_pct=0.4,
            profit_total_abs=4.0,
            winrate=1.0,
            score=0.5,
            passed=True,
            reason="passed",
        ),
        strategy_researcher.WindowEvaluation(
            strategy="SampleStrategy",
            window_days=2,
            timerange="20260511-20260513",
            side="hold",
            trades=0,
            profit_total_pct=0.0,
            profit_total_abs=0.0,
            winrate=0.0,
            score=-999999.0,
            passed=False,
            reason="no side passed gates",
        ),
    ]

    result = strategy_researcher.aggregate_strategy_score(evaluations)

    assert result["strategy"] == "SampleStrategy"
    assert result["passed_windows"] == 1
    assert result["side"] == "short"


def test_aggregate_strategy_score_lightly_penalizes_no_trade_windows():
    result = strategy_researcher.aggregate_strategy_score(
        [
            strategy_researcher.WindowEvaluation(
                strategy="LowFrequency",
                window_days=1,
                timerange="20260512-20260513",
                side="hold",
                trades=0,
                profit_total_pct=0.0,
                profit_total_abs=0.0,
                winrate=0.0,
                score=-999999.0,
                passed=False,
                reason="no side passed gates",
            ),
            strategy_researcher.WindowEvaluation(
                strategy="LowFrequency",
                window_days=7,
                timerange="20260506-20260513",
                side="long",
                trades=1,
                profit_total_pct=0.5,
                profit_total_abs=5.0,
                winrate=1.0,
                score=0.5,
                passed=True,
                reason="passed",
            ),
        ]
    )

    assert result["score"] > -0.5
    assert result["passed_windows"] == 1


def test_aggregate_strategy_score_applies_quality_gate_cost_buffer():
    result = strategy_researcher.aggregate_strategy_score(
        [
            strategy_researcher.WindowEvaluation(
                strategy="ThinEdge",
                window_days=1,
                timerange="20260512-20260513",
                side="long",
                trades=10,
                profit_total_pct=0.2,
                profit_total_abs=2.0,
                winrate=0.6,
                score=0.2,
                passed=True,
                reason="passed",
            ),
            strategy_researcher.WindowEvaluation(
                strategy="ThinEdge",
                window_days=2,
                timerange="20260511-20260513",
                side="long",
                trades=10,
                profit_total_pct=0.2,
                profit_total_abs=2.0,
                winrate=0.6,
                score=0.2,
                passed=True,
                reason="passed",
            ),
        ],
        slippage_buffer_pct_per_trade=0.04,
        min_net_profit_pct=0.0,
        min_pass_rate=0.5,
    )

    assert result["quality_gate"]["net_profit_pct"] < 0
    assert result["quality_gate"]["passed"] is False
    assert result["score"] < result["raw_score"]


def test_normalize_llm_config_clamps_values():
    result = strategy_researcher.normalize_llm_config(
        {
            "strategy_prior": "SampleStrategy",
            "factor_prior": "trend_momentum",
            "side_prior": "buy",
            "confidence": 2,
            "parameter_bias": {"risk": "moon", "entry": "looser", "exit": "faster"},
            "reason": "x",
        }
    )

    assert result["side_prior"] == "hold"
    assert result["confidence"] == 1.0
    assert result["parameter_bias"] == {"risk": "normal", "entry": "looser", "exit": "faster"}


def test_default_strategy_candidates_focus_on_current_live_baselines(monkeypatch):
    monkeypatch.setattr("sys.argv", ["strategy_researcher.py"])
    args = strategy_researcher.parse_args()

    assert args.strategies == [
        "AIGeneratedLongTrendContinuationRunner2xSelective",
        "AIGeneratedLongTrendContinuationRunner2x",
    ]


def test_load_ai_strategy_names_accepts_only_generated_names(tmp_path):
    path = tmp_path / "ai.json"
    path.write_text(
        '{"strategy_names": ["AIGeneratedTrend", "SampleStrategy", "../Bad", "AIGeneratedPullback1"]}'
    )

    assert strategy_researcher.load_ai_strategy_names(path) == [
        "AIGeneratedPullback1",
        "AIGeneratedTrend",
    ]


def test_execution_feedback_penalizes_losing_strategy():
    feedback = strategy_researcher.build_execution_feedback(
        [
            {"strategy": "SampleStrategy", "profit_pct": -0.4},
            {"strategy": "SampleStrategy", "profit_pct": 0.1},
        ]
    )
    adjusted = strategy_researcher.apply_execution_feedback(
        [{"strategy": "SampleStrategy", "score": 1.0, "passed_windows": 2}],
        feedback,
    )

    assert feedback["SampleStrategy"]["samples"] == 2
    assert adjusted[0]["feedback_penalty"] > 0
    assert adjusted[0]["score"] < 1.0


def test_kronos_signal_uses_weak_return_when_bias_is_hold():
    signal = strategy_researcher.kronos_signal(
        {"summary": {"market_bias": "hold", "avg_confidence": 0.08, "avg_pred_return_pct": 0.34}},
        0.12,
    )

    assert signal["side"] == "long"
    assert signal["strength"] > 0


def test_apply_kronos_scoring_rewards_matching_side():
    scores = [
        {"strategy": "Long", "side": "long", "score": 1.0},
        {"strategy": "Short", "side": "short", "score": 1.0},
    ]

    adjusted = strategy_researcher.apply_kronos_scoring(scores, {"side": "long", "strength": 0.5})

    assert adjusted[0]["score"] > 1.0
    assert adjusted[1]["score"] < 1.0


def test_extract_pair_attribution_sorts_by_profit():
    rows = strategy_researcher.extract_pair_attribution("SampleStrategy", stats_for(short_profit=0.5, short_trades=2))

    assert rows[0]["pair"] == "BTC/USDT:USDT"
    assert rows[0]["profit_total_pct"] == 0.5
    assert rows[1]["pair"] == "ETH/USDT:USDT"


def test_build_pair_selection_payload_filters_positive_pairs():
    payload = strategy_researcher.build_pair_selection_payload(
        {
            "SampleStrategy": {
                30: [
                    {"pair": "BTC/USDT:USDT", "trades": 3, "profit_total_pct": 0.5},
                    {"pair": "ETH/USDT:USDT", "trades": 2, "profit_total_pct": -0.2},
                    {"pair": "SOL/USDT:USDT", "trades": 0, "profit_total_pct": 0.8},
                ]
            }
        },
        source_window_days=30,
        min_trades=1,
        min_profit_pct=0.0,
        max_pairs=6,
    )

    assert payload["strategies"]["SampleStrategy"]["allowed_pairs"] == ["BTC/USDT:USDT"]
    assert "ETH/USDT:USDT" in payload["strategies"]["SampleStrategy"]["blocked_pairs"]


def test_build_pair_selection_payload_retains_recent_pair_under_hold_runs():
    payload = strategy_researcher.build_pair_selection_payload(
        {
            "SampleStrategy": {
                30: [
                    {"pair": "BTC/USDT:USDT", "trades": 3, "profit_total_pct": 0.5},
                    {"pair": "ETH/USDT:USDT", "trades": 0, "profit_total_pct": 0.0},
                ]
            }
        },
        source_window_days=30,
        min_trades=1,
        min_profit_pct=0.0,
        max_pairs=6,
        previous_payload={
            "strategies": {
                "SampleStrategy": {
                    "allowed_pairs": ["ETH/USDT:USDT"],
                    "pair_state": {"ETH/USDT:USDT": {"age_runs": 1}},
                }
            }
        },
        min_hold_runs=2,
        remove_below_profit_pct=-0.15,
    )

    assert payload["strategies"]["SampleStrategy"]["allowed_pairs"] == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    ]
    assert payload["strategies"]["SampleStrategy"]["pair_state"]["ETH/USDT:USDT"]["age_runs"] == 2


def test_build_pair_selection_payload_removes_pair_when_strongly_negative():
    payload = strategy_researcher.build_pair_selection_payload(
        {
            "SampleStrategy": {
                30: [
                    {"pair": "BTC/USDT:USDT", "trades": 3, "profit_total_pct": 0.5},
                    {"pair": "ETH/USDT:USDT", "trades": 1, "profit_total_pct": -0.3},
                ]
            }
        },
        source_window_days=30,
        min_trades=1,
        min_profit_pct=0.0,
        max_pairs=6,
        previous_payload={
            "strategies": {
                "SampleStrategy": {
                    "allowed_pairs": ["ETH/USDT:USDT"],
                    "pair_state": {"ETH/USDT:USDT": {"age_runs": 1}},
                }
            }
        },
        min_hold_runs=2,
        remove_below_profit_pct=-0.15,
    )

    assert payload["strategies"]["SampleStrategy"]["allowed_pairs"] == ["BTC/USDT:USDT"]


def test_build_pair_selection_payload_uses_pair_real_trade_feedback_for_promotion():
    payload = strategy_researcher.build_pair_selection_payload(
        {
            "SampleStrategy": {
                30: [
                    {"pair": "BTC/USDT:USDT", "trades": 3, "profit_total_pct": 0.2},
                    {"pair": "ETH/USDT:USDT", "trades": 1, "profit_total_pct": -0.1},
                ]
            }
        },
        source_window_days=30,
        min_trades=1,
        min_profit_pct=0.0,
        max_pairs=6,
        pair_execution_feedback={
            "SampleStrategy": {
                "ETH/USDT:USDT": {
                    "samples": 3,
                    "avg_profit_pct": 0.4,
                    "positive_rate": 0.67,
                }
            }
        },
        pair_real_trade_min_samples=2,
    )

    assert "ETH/USDT:USDT" in payload["strategies"]["SampleStrategy"]["allowed_pairs"]


def test_build_pair_selection_payload_uses_pair_real_trade_feedback_for_demotion():
    payload = strategy_researcher.build_pair_selection_payload(
        {
            "SampleStrategy": {
                30: [
                    {"pair": "BTC/USDT:USDT", "trades": 3, "profit_total_pct": 0.2},
                    {"pair": "ETH/USDT:USDT", "trades": 2, "profit_total_pct": 0.05},
                ]
            }
        },
        source_window_days=30,
        min_trades=1,
        min_profit_pct=0.0,
        max_pairs=6,
        pair_execution_feedback={
            "SampleStrategy": {
                "ETH/USDT:USDT": {
                    "samples": 3,
                    "avg_profit_pct": -0.5,
                    "positive_rate": 0.33,
                }
            }
        },
        pair_real_trade_min_samples=2,
    )

    assert payload["strategies"]["SampleStrategy"]["allowed_pairs"] == ["BTC/USDT:USDT"]


def test_build_formal_strategy_pool_requires_positive_30_and_60_day_windows():
    scores = [
        {
            "strategy": "AIGeneratedWinner",
            "score": 1.2,
            "side": "long",
            "profit_total_pct": 1.6,
            "trades": 14,
            "passed_windows": 3,
            "quality_gate": {"passed": True},
        },
        {
            "strategy": "AIGeneratedLoser",
            "score": 1.8,
            "side": "long",
            "profit_total_pct": 2.0,
            "trades": 20,
            "passed_windows": 4,
            "quality_gate": {"passed": True},
        },
    ]
    windows = [
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 30,
            "passed": True,
            "trades": 8,
            "profit_total_pct": 0.8,
            "reason": "passed",
        },
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 60,
            "passed": True,
            "trades": 6,
            "profit_total_pct": 0.7,
            "reason": "passed",
        },
        {
            "strategy": "AIGeneratedLoser",
            "window_days": 30,
            "passed": True,
            "trades": 10,
            "profit_total_pct": 1.2,
            "reason": "passed",
        },
        {
            "strategy": "AIGeneratedLoser",
            "window_days": 60,
            "passed": False,
            "trades": 3,
            "profit_total_pct": -0.3,
            "reason": "failed",
        },
    ]

    payload = strategy_researcher.build_formal_strategy_pool(
        scores,
        windows,
        required_window_days=[30, 60],
        min_profit_pct=0.0,
        min_trades=5,
        max_strategies=6,
    )

    assert payload["allowed_strategies"] == ["AIGeneratedWinner"]


def test_build_formal_strategy_pool_adds_ai_priority_bonus_from_streak():
    scores = [
        {
            "strategy": "AIGeneratedWinner",
            "score": 1.0,
            "side": "long",
            "profit_total_pct": 1.1,
            "trades": 12,
            "passed_windows": 2,
            "quality_gate": {"passed": True},
        }
    ]
    windows = [
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 30,
            "passed": True,
            "trades": 6,
            "profit_total_pct": 0.6,
            "reason": "passed",
        },
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 60,
            "passed": True,
            "trades": 6,
            "profit_total_pct": 0.5,
            "reason": "passed",
        },
    ]

    payload = strategy_researcher.build_formal_strategy_pool(
        scores,
        windows,
        required_window_days=[30, 60],
        min_profit_pct=0.0,
        min_trades=5,
        max_strategies=6,
        previous_payload={
            "strategies": [
                {
                    "strategy": "AIGeneratedWinner",
                    "streak_runs": 2,
                }
            ]
        },
        priority_streak_threshold=2,
        priority_step=0.05,
        priority_max_bonus=0.2,
    )

    assert payload["allowed_strategies"] == ["AIGeneratedWinner"]
    assert payload["strategies"][0]["streak_runs"] == 3
    assert payload["strategies"][0]["priority_bonus"] == 0.1
    assert payload["priority_scores"]["AIGeneratedWinner"] == 1.1


def test_build_formal_strategy_pool_adds_real_trade_bonus():
    scores = [
        {
            "strategy": "AIGeneratedWinner",
            "score": 1.0,
            "side": "long",
            "profit_total_pct": 1.1,
            "trades": 12,
            "passed_windows": 2,
            "quality_gate": {"passed": True},
        }
    ]
    windows = [
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 30,
            "passed": True,
            "trades": 6,
            "profit_total_pct": 0.6,
            "reason": "passed",
        },
        {
            "strategy": "AIGeneratedWinner",
            "window_days": 60,
            "passed": True,
            "trades": 6,
            "profit_total_pct": 0.5,
            "reason": "passed",
        },
    ]

    payload = strategy_researcher.build_formal_strategy_pool(
        scores,
        windows,
        required_window_days=[30, 60],
        min_profit_pct=0.0,
        min_trades=5,
        max_strategies=6,
        execution_feedback={
            "AIGeneratedWinner": {
                "samples": 4,
                "avg_profit_pct": 0.8,
                "positive_rate": 0.75,
            }
        },
        real_trade_priority_min_samples=3,
        real_trade_priority_step=0.03,
        real_trade_priority_max_bonus=0.12,
    )

    assert payload["strategies"][0]["real_trade_bonus"] == 0.06
    assert payload["priority_scores"]["AIGeneratedWinner"] == 1.06


def test_trade_db_feedback_records_reads_closed_trades(tmp_path):
    db_path = tmp_path / "trades.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table trades (
                id integer primary key,
                strategy varchar(100),
                is_open boolean not null,
                close_profit float,
                close_profit_abs float,
                close_date datetime,
                pair varchar(25)
            )
            """
        )
        conn.executemany(
            """
            insert into trades (strategy, is_open, close_profit, close_profit_abs, close_date, pair)
            values (?, ?, ?, ?, ?, ?)
            """,
            [
                ("AIGeneratedWinner", 0, 0.01, 1.0, "2026-05-14 10:00:00", "BTC/USDT:USDT"),
                ("AIGeneratedWinner", 0, -0.02, -2.0, "2026-05-14 09:00:00", "ETH/USDT:USDT"),
            ],
        )

    rows = strategy_researcher.trade_db_feedback_records(db_path, limit=10)

    assert len(rows) == 2
    assert rows[0]["strategy"] == "AIGeneratedWinner"
    assert rows[0]["profit_total_pct"] == 1.0


def test_build_pair_execution_feedback_groups_by_strategy_and_pair():
    feedback = strategy_researcher.build_pair_execution_feedback(
        [
            {"strategy": "AIGeneratedWinner", "pair": "BTC/USDT:USDT", "profit_total_pct": 1.0},
            {"strategy": "AIGeneratedWinner", "pair": "BTC/USDT:USDT", "profit_total_pct": -0.5},
            {"strategy": "AIGeneratedWinner", "pair": "ETH/USDT:USDT", "profit_total_pct": 0.2},
        ]
    )

    assert feedback["AIGeneratedWinner"]["BTC/USDT:USDT"]["samples"] == 2
    assert feedback["AIGeneratedWinner"]["BTC/USDT:USDT"]["avg_profit_pct"] == 0.25
    assert feedback["AIGeneratedWinner"]["ETH/USDT:USDT"]["samples"] == 1


def test_run_once_writes_registry(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    (strategy_path / "SampleStrategy.py").write_text("class SampleStrategy: pass\n")
    output = tmp_path / "registry.json"
    ledger = tmp_path / "registry.jsonl"
    config = tmp_path / "config.json"
    config.write_text("{}")

    monkeypatch.setattr(
        strategy_researcher,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 0,
                "trend_down_count": 3,
                "median_atr_pct": 0.003,
                "median_bb_width": 0.02,
            }
        },
    )
    monkeypatch.setattr(
        strategy_researcher.auto_optimize,
        "build_walk_forward_timeranges",
        lambda days, confirm: (f"2026051{days}-20260513", ""),
    )
    monkeypatch.setattr(strategy_researcher, "AUTOOPT_DIR", tmp_path / "autoopt")
    monkeypatch.setattr(
        strategy_researcher.direction_advisor,
        "run_strategy_backtest",
        lambda **kwargs: stats_for(short_profit=0.5, short_trades=2),
    )

    args = type(
        "Args",
        (),
        {
            "config": str(config),
            "strategy_path": str(strategy_path),
            "strategies": ["SampleStrategy"],
            "windows": [1, 2],
            "min_trades": 1,
            "min_profit_pct": 0.0,
            "slippage_buffer_pct_per_trade": 0.04,
            "min_net_profit_pct": 0.0,
            "min_pass_rate": 0.5,
            "freqaimodel": "LightGBMRegressor",
            "backend": "local",
            "snapshot_limit": 10,
            "output": str(output),
            "ledger": str(ledger),
            "execution_ledger": str(tmp_path / "missing_execution.jsonl"),
            "use_llm_advisor": False,
            "include_ai_generated": False,
            "ai_candidates": str(tmp_path / "missing_ai.json"),
            "kronos_forecast": str(tmp_path / "missing_kronos.json"),
            "kronos_return_threshold_pct": 0.12,
            "disable_kronos_scoring": False,
            "pair_selection_output": str(tmp_path / "pair_selection.json"),
            "formal_pool_output": str(tmp_path / "formal_strategy_pool.json"),
            "formal_pool_required_window_days": [30, 60],
            "formal_pool_min_profit_pct": 0.0,
            "formal_pool_min_trades": 5,
            "formal_pool_max_strategies": 6,
            "pair_selection_window_days": 2,
            "pair_selection_min_trades": 1,
            "pair_selection_min_profit_pct": 0.0,
            "pair_selection_max_pairs": 6,
            "pair_selection_min_hold_runs": 2,
            "pair_selection_remove_below_profit_pct": -0.15,
        },
    )()

    payload = strategy_researcher.run_once(args)

    assert payload["recommended"]["strategy"] == "SampleStrategy"
    assert payload["recommended"]["side"] == "short"
    assert payload["pair_selection"]["strategies"]["SampleStrategy"]["allowed_pairs"] == ["BTC/USDT:USDT"]
    assert output.exists()
    assert ledger.exists()


def test_run_once_keeps_kronos_confirmed_side_despite_factor_conflict(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    (strategy_path / "SampleStrategy.py").write_text("class SampleStrategy: pass\n")
    output = tmp_path / "registry.json"
    ledger = tmp_path / "registry.jsonl"
    config = tmp_path / "config.json"
    config.write_text("{}")
    kronos = tmp_path / "kronos.json"
    kronos.write_text('{"summary": {"market_bias": "hold", "avg_pred_return_pct": 0.4}}')

    monkeypatch.setattr(
        strategy_researcher,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 0,
                "trend_down_count": 3,
                "median_atr_pct": 0.003,
                "median_bb_width": 0.02,
            }
        },
    )
    monkeypatch.setattr(
        strategy_researcher.auto_optimize,
        "build_walk_forward_timeranges",
        lambda days, confirm: (f"2026051{days}-20260513", ""),
    )
    monkeypatch.setattr(strategy_researcher, "AUTOOPT_DIR", tmp_path / "autoopt")
    monkeypatch.setattr(
        strategy_researcher.direction_advisor,
        "run_strategy_backtest",
        lambda **kwargs: stats_for(long_profit=0.5, long_trades=2),
    )

    args = type(
        "Args",
        (),
        {
            "config": str(config),
            "strategy_path": str(strategy_path),
            "strategies": ["SampleStrategy"],
            "windows": [1],
            "min_trades": 1,
            "min_profit_pct": 0.0,
            "slippage_buffer_pct_per_trade": 0.04,
            "min_net_profit_pct": 0.0,
            "min_pass_rate": 0.5,
            "freqaimodel": "LightGBMRegressor",
            "backend": "local",
            "snapshot_limit": 10,
            "output": str(output),
            "ledger": str(ledger),
            "execution_ledger": str(tmp_path / "missing_execution.jsonl"),
            "use_llm_advisor": False,
            "include_ai_generated": False,
            "ai_candidates": str(tmp_path / "missing_ai.json"),
            "kronos_forecast": str(kronos),
            "kronos_return_threshold_pct": 0.12,
            "disable_kronos_scoring": False,
            "pair_selection_output": str(tmp_path / "pair_selection.json"),
            "formal_pool_output": str(tmp_path / "formal_strategy_pool.json"),
            "formal_pool_required_window_days": [30, 60],
            "formal_pool_min_profit_pct": 0.0,
            "formal_pool_min_trades": 5,
            "formal_pool_max_strategies": 6,
            "pair_selection_window_days": 1,
            "pair_selection_min_trades": 1,
            "pair_selection_min_profit_pct": 0.0,
            "pair_selection_max_pairs": 6,
            "pair_selection_min_hold_runs": 2,
            "pair_selection_remove_below_profit_pct": -0.15,
        },
    )()

    payload = strategy_researcher.run_once(args)

    assert payload["kronos_signal"]["side"] == "long"
    assert payload["recommended"]["side"] == "long"
