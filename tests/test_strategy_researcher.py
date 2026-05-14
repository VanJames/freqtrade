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


def test_default_strategy_candidates_include_pullback_variants(monkeypatch):
    monkeypatch.setattr("sys.argv", ["strategy_researcher.py"])
    args = strategy_researcher.parse_args()

    assert "SampleStrategyPullbackShort" in args.strategies
    assert "SampleStrategyPullbackLong" in args.strategies
    assert "SampleStrategyRangeMeanReversion" in args.strategies
    assert "SampleStrategyBreakoutMomentum" in args.strategies


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
            "freqaimodel": "LightGBMRegressor",
            "backend": "local",
            "snapshot_limit": 10,
            "output": str(output),
            "ledger": str(ledger),
            "execution_ledger": str(tmp_path / "missing_execution.jsonl"),
            "use_llm_advisor": False,
        },
    )()

    payload = strategy_researcher.run_once(args)

    assert payload["recommended"]["strategy"] == "SampleStrategy"
    assert payload["recommended"]["side"] == "short"
    assert output.exists()
    assert ledger.exists()
