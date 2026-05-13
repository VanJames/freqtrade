import json

from scripts import strategy_selector
from scripts import auto_optimize_loop


def test_active_strategy_reads_config():
    assert strategy_selector.active_strategy({"strategy": "SampleStrategyShortOnly"}, "Fallback") == (
        "SampleStrategyShortOnly"
    )
    assert strategy_selector.active_strategy({}, "Fallback") == "Fallback"


def test_passes_train_gate_requires_positive_profit():
    current = strategy_selector.auto_optimize.RunMetrics(
        trades=10,
        profit_total_pct=0.1,
        profit_total_abs=1.0,
        max_drawdown_account=0.01,
        profit_factor=1.2,
        total_volume=100.0,
    )
    candidate = strategy_selector.auto_optimize.RunMetrics(
        trades=10,
        profit_total_pct=-0.01,
        profit_total_abs=-1.0,
        max_drawdown_account=0.01,
        profit_factor=0.8,
        total_volume=100.0,
    )

    passed, reason = strategy_selector.passes_train_gate(
        current_metrics=current,
        candidate_metrics=candidate,
        min_trades=3,
        min_profit_pct=0.0,
        min_score_delta=0.01,
        max_drawdown=0.08,
        max_drawdown_worsen=0.02,
    )

    assert passed is False
    assert "not positive" in reason


def test_select_best_prefers_passed_strategy():
    losing_high_score = strategy_selector.StrategyEvaluation(
        strategy="A",
        train={"profit_total_pct": 10},
        score=10,
        passed=False,
    )
    passed_lower_score = strategy_selector.StrategyEvaluation(
        strategy="B",
        train={"profit_total_pct": 1},
        score=1,
        passed=True,
    )

    assert strategy_selector.select_best([losing_high_score, passed_lower_score]).strategy == "B"


def test_select_winner_keeps_current_when_no_candidate_passed():
    current = strategy_selector.StrategyEvaluation(
        strategy="SampleStrategy",
        train={"profit_total_pct": -1},
        score=-2,
        passed=False,
    )
    higher_unpassed = strategy_selector.StrategyEvaluation(
        strategy="SampleStrategyShortOnly",
        train={"profit_total_pct": -0.1},
        score=-0.2,
        passed=False,
    )

    winner = strategy_selector.select_winner([current, higher_unpassed], "SampleStrategy")

    assert winner.strategy == "SampleStrategy"


def test_apply_strategy_updates_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"strategy": "SampleStrategy"}))

    strategy_selector.apply_strategy(config_path, "SampleStrategyShortOnly")

    config = json.loads(config_path.read_text())
    assert config["strategy"] == "SampleStrategyShortOnly"
    assert config["strategy_path"] == "user_data/strategies/"


def test_auto_optimize_loop_resolves_active_strategy(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"strategy": "SampleStrategyShortOnly"}))

    assert auto_optimize_loop.resolve_strategy("auto", str(config_path)) == (
        "SampleStrategyShortOnly"
    )
    assert auto_optimize_loop.resolve_strategy("SampleStrategy", str(config_path)) == (
        "SampleStrategy"
    )
