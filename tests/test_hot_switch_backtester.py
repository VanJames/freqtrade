from datetime import UTC, datetime

from scripts import hot_switch_backtester


def test_timerange_for_uses_prior_days():
    assert hot_switch_backtester.timerange_for(datetime(2026, 5, 13, tzinfo=UTC), 7) == (
        "20260506-20260513"
    )


def test_summarize_days_calculates_activity_and_profit():
    summary = hot_switch_backtester.summarize_days(
        [
            {"trades": 2, "profit_total_pct": 0.2, "profit_total_abs": 1.5},
            {"trades": 0, "profit_total_pct": 0.0, "profit_total_abs": 0.0},
            {"trades": 3, "profit_total_pct": -0.1, "profit_total_abs": -0.4},
        ]
    )

    assert summary["days"] == 3
    assert summary["active_days"] == 2
    assert summary["trades"] == 5
    assert summary["avg_trades_per_day"] == 1.6667
    assert summary["profit_total_pct_sum"] == 0.1
    assert summary["profit_total_abs_sum"] == 1.1


def test_parse_start_strategy_prefers_explicit_then_configured():
    assert hot_switch_backtester.parse_start_strategy(
        {"strategy": "SampleStrategy"}, "SampleStrategyScalp", ["SampleStrategy"]
    ) == "SampleStrategyScalp"
    assert hot_switch_backtester.parse_start_strategy(
        {"strategy": "SampleStrategy"}, "", ["SampleStrategyScalp"]
    ) == "SampleStrategy"
    assert hot_switch_backtester.parse_start_strategy({}, "", ["SampleStrategyScalp"]) == (
        "SampleStrategyScalp"
    )


def test_build_daily_registry_recommends_best_score(monkeypatch, tmp_path):
    def fake_eval(**kwargs):
        strategy = kwargs["strategy"]
        score = 1.0 if strategy == "Winner" else 0.1
        return hot_switch_backtester.strategy_researcher.WindowEvaluation(
            strategy=strategy,
            window_days=kwargs["window_days"],
            timerange="20260512-20260513",
            side="short",
            trades=2,
            profit_total_pct=score,
            profit_total_abs=score,
            winrate=0.5,
            score=score,
            passed=True,
            reason="passed",
        )

    monkeypatch.setattr(hot_switch_backtester, "evaluate_strategy_window", fake_eval)

    registry = hot_switch_backtester.build_daily_registry(
        strategies=["Loser", "Winner"],
        decision_windows=[1, 2],
        decision_day=datetime(2026, 5, 13, tzinfo=UTC),
        strategy_path=tmp_path,
        config=tmp_path / "config.json",
        run_dir=tmp_path,
        freqaimodel="LightGBMRegressor",
        backend="local",
        min_trades=1,
        min_profit_pct=0.0,
    )

    assert registry["recommended"]["strategy"] == "Winner"
    assert registry["recommended"]["side"] == "short"
    assert len(registry["window_evaluations"]) == 4
