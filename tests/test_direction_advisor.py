import sqlite3

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
    review = {"enabled": True, "action": "short", "confidence": 0.8}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "hold"
    assert "disagreed" in result["final_reason"]


def test_apply_llm_confirmation_ignores_low_confidence_hold():
    payload = {"action": "short", "reason": "stats passed"}
    review = {"enabled": True, "action": "hold", "confidence": 0.56}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "short"
    assert "high-confidence veto" in result["final_reason"]


def test_apply_llm_confirmation_blocks_high_confidence_hold():
    payload = {"action": "short", "reason": "stats passed"}
    review = {"enabled": True, "action": "hold", "confidence": 0.85}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "hold"
    assert "high confidence" in result["final_reason"]


def test_apply_llm_confirmation_cannot_override_hold():
    payload = {"action": "hold", "reason": "stats failed"}
    review = {"enabled": True, "action": "long"}

    result = direction_advisor.apply_llm_confirmation(payload, review)

    assert result["final_action"] == "hold"
    assert "not allowed" in result["final_reason"]


def test_apply_kronos_confirmation_confirms_matching_action():
    payload = {"action": "short", "final_action": "short"}
    forecast = {"summary": {"market_bias": "short", "avg_confidence": 0.7}}

    result = direction_advisor.apply_kronos_confirmation(payload, forecast)

    assert result["final_action"] == "short"
    assert result["kronos_confirmation"]["effect"] == "confirmed"


def test_apply_kronos_confirmation_keeps_low_confidence_opposition():
    payload = {"action": "short", "final_action": "short"}
    forecast = {"summary": {"market_bias": "long", "avg_confidence": 0.6}}

    result = direction_advisor.apply_kronos_confirmation(payload, forecast)

    assert result["final_action"] == "short"
    assert result["kronos_confirmation"]["effect"] == "weakened"


def test_apply_kronos_confirmation_vetoes_strong_opposition():
    payload = {"action": "short", "final_action": "short"}
    forecast = {"summary": {"market_bias": "long", "avg_confidence": 0.8}}

    result = direction_advisor.apply_kronos_confirmation(payload, forecast)

    assert result["final_action"] == "hold"
    assert result["kronos_confirmation"]["effect"] == "vetoed"


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


def test_trade_db_direction_feedback_groups_long_and_short(tmp_path):
    db_path = tmp_path / "trades.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table trades (
                id integer primary key,
                is_open boolean not null,
                is_short boolean,
                close_profit float,
                close_date datetime
            )
            """
        )
        conn.executemany(
            """
            insert into trades (is_open, is_short, close_profit, close_date)
            values (?, ?, ?, ?)
            """,
            [
                (0, 0, 0.01, "2026-05-14 10:00:00"),
                (0, 1, 0.02, "2026-05-14 09:00:00"),
                (0, 1, -0.01, "2026-05-14 08:00:00"),
            ],
        )

    summary = direction_advisor.trade_db_direction_feedback(db_path, limit=10)

    assert summary["sample_size"] == 3
    assert summary["by_action"]["long"]["count"] == 1
    assert summary["by_action"]["short"]["count"] == 2
    assert summary["by_action"]["short"]["avg_profit_total_pct"] == 0.5


def test_apply_real_trade_direction_feedback_can_flip_best_side():
    metrics = [
        direction_advisor.DirectionMetrics(
            strategy="A",
            side="long",
            trades=4,
            wins=3,
            losses=1,
            winrate=0.75,
            profit_total_pct=0.5,
            profit_total_abs=0.5,
            score=0.5,
        ),
        direction_advisor.DirectionMetrics(
            strategy="B",
            side="short",
            trades=4,
            wins=2,
            losses=2,
            winrate=0.5,
            profit_total_pct=0.4,
            profit_total_abs=0.4,
            score=0.4,
        ),
    ]

    adjusted = direction_advisor.apply_real_trade_direction_feedback(
        metrics,
        {
            "by_action": {
                "long": {"count": 3, "avg_profit_total_pct": -0.4, "positive_rate": 0.33},
                "short": {"count": 4, "avg_profit_total_pct": 0.6, "positive_rate": 0.75},
            }
        },
        min_samples=3,
        bonus_step=0.04,
        max_bonus=0.15,
    )

    action, best, _ = direction_advisor.choose_recommendation(adjusted, min_trades=3, min_profit_pct=0.0)

    assert action == "short"
    assert best.strategy == "B"


def test_apply_registry_safety_gate_holds_when_pool_window_is_negative():
    payload = {
        "action": "short",
        "final_action": "short",
        "recommended_strategy": "SampleStrategy",
    }
    registry = {
        "window_evaluations": [
            {
                "strategy": "SampleStrategy",
                "window_days": 30,
                "trades": 10,
                "passed": False,
                "profit_total_pct": -1.0,
            }
        ]
    }

    result = direction_advisor.apply_registry_safety_gate(
        payload,
        registry,
        pool_positive_days={30},
        target_positive_days=set(),
        min_profit_pct=0.0,
    )

    assert result["final_action"] == "hold"
    assert "pool failed" in result["final_reason"]


def test_apply_registry_safety_gate_holds_when_target_window_is_negative():
    payload = {
        "action": "short",
        "final_action": "short",
        "recommended_strategy": "SampleStrategy",
    }
    registry = {
        "window_evaluations": [
            {
                "strategy": "SampleStrategy",
                "window_days": 30,
                "trades": 10,
                "passed": False,
                "profit_total_pct": -1.0,
            },
            {
                "strategy": "OtherStrategy",
                "window_days": 30,
                "trades": 10,
                "passed": True,
                "profit_total_pct": 1.0,
            },
        ]
    }

    result = direction_advisor.apply_registry_safety_gate(
        payload,
        registry,
        pool_positive_days={30},
        target_positive_days={30},
        min_profit_pct=0.0,
    )

    assert result["final_action"] == "hold"
    assert "Recommended strategy failed" in result["final_reason"]


def test_run_once_holds_when_all_strategy_backtests_fail(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    (strategy_path / "SampleStrategy.py").write_text("class SampleStrategy: pass\n")
    output = tmp_path / "direction.json"
    ledger = tmp_path / "direction.jsonl"
    config = tmp_path / "config.json"
    config.write_text("{}")

    def fail_backtest(**kwargs):
        raise RuntimeError("backtest failed")

    monkeypatch.setattr(direction_advisor, "run_strategy_backtest", fail_backtest)
    monkeypatch.setattr(direction_advisor, "latest_prices", lambda config, limit: {"summary": {}})
    monkeypatch.setattr(direction_advisor.auto_optimize, "build_walk_forward_timeranges", lambda days, confirm: ("20260512-20260513", ""))
    monkeypatch.setattr(direction_advisor, "AUTOOPT_DIR", tmp_path / "autoopt")

    args = type(
        "Args",
        (),
        {
            "config": str(config),
            "strategy_path": str(strategy_path),
            "backtest_days": 1,
            "strategies": ["SampleStrategy"],
            "freqaimodel": "LightGBMRegressor",
            "backend": "local",
            "min_side_trades": 1,
            "min_side_profit_pct": 0.0,
            "snapshot_limit": 10,
            "ledger": str(ledger),
            "output": str(output),
            "candidate_registry": str(tmp_path / "missing_registry.json"),
            "kronos_forecast": str(tmp_path / "missing_kronos.json"),
            "disable_kronos_confirmation": False,
            "registry_pool_positive_window_days": [],
            "registry_target_positive_window_days": [],
            "min_registry_window_profit_pct": 0.0,
            "use_llm_advisor": False,
        },
    )()

    payload = direction_advisor.run_once(args)

    assert payload["final_action"] == "hold"
    assert payload["recommended_side"] == "hold"
    assert payload["errors"][0]["strategy"] == "SampleStrategy"
    assert "All strategy backtests failed" in payload["reason"]


def test_load_registry_strategies_extends_fallback(tmp_path):
    registry = tmp_path / "strategy_registry.json"
    registry.write_text(
        '{"strategy_candidates": ["SampleStrategyShortOnly", "SampleStrategy"]}'
    )

    assert direction_advisor.load_registry_strategies(registry, ["SampleStrategy"]) == [
        "SampleStrategy",
        "SampleStrategyShortOnly",
    ]


def test_load_registry_strategies_uses_fallback_on_bad_file(tmp_path):
    registry = tmp_path / "strategy_registry.json"
    registry.write_text("{bad")

    assert direction_advisor.load_registry_strategies(registry, ["SampleStrategy"]) == [
        "SampleStrategy"
    ]


def test_default_strategy_candidates_include_pullback_variants(monkeypatch):
    monkeypatch.setattr("sys.argv", ["direction_advisor.py"])
    args = direction_advisor.parse_args()

    assert "SampleStrategyPullbackShort" in args.strategies
    assert "SampleStrategyPullbackLong" in args.strategies
    assert "SampleStrategyRangeMeanReversion" in args.strategies
    assert "SampleStrategyBreakoutMomentum" in args.strategies
