from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trading_system.backtest import BacktestConfig, BacktestResult, SimTrade
from trading_system.models import PositionSide, Regime
from trading_system.tuner import (
    RuntimeTuningCandidate,
    RuntimeTuningResult,
    backtest_quality,
    build_backtest_config_from_settings,
    has_clear_improvement,
    run_tuning_agent_once,
    runtime_tuning_candidates,
    score_runtime_result,
    write_runtime_tuning_report,
)
from trading_system.config import Settings


def test_runtime_tuning_candidates_include_current_and_balanced() -> None:
    base = BacktestConfig(
        symbols=["BTC/USDT:USDT"],
        confirmation_position_sizing=True,
        max_signal_risk_multiplier=1.5,
    )

    candidates = runtime_tuning_candidates(base)

    assert [candidate.name for candidate in candidates] == ["current", "conservative_2x", "balanced_3x"]
    assert candidates[0].config.max_signal_risk_multiplier == 1.5
    assert candidates[-1].config.max_signal_risk_multiplier == 3.0
    assert candidates[-1].config.same_direction_risk_limit == 0.06


def test_runtime_tuning_score_penalizes_symbol_loss() -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    profitable = BacktestResult(
        started_at=started_at,
        ended_at=ended_at,
        trades=[
            SimTrade(
                symbol="BTC/USDT:USDT",
                side=PositionSide.LONG,
                regime=Regime.SHOCK_TREND_UP,
                entry_time=started_at,
                exit_time=ended_at,
                entry_price=100.0,
                exit_price=110.0,
                qty=1.0,
                pnl=100.0,
                pnl_pct_equity=0.01,
                reason="take_profit",
            )
        ]
        * 20,
    )
    uneven = BacktestResult(
        started_at=started_at,
        ended_at=ended_at,
        trades=[
            SimTrade(
                symbol="BTC/USDT:USDT",
                side=PositionSide.LONG,
                regime=Regime.SHOCK_TREND_UP,
                entry_time=started_at,
                exit_time=ended_at,
                entry_price=100.0,
                exit_price=110.0,
                qty=1.0,
                pnl=150.0,
                pnl_pct_equity=0.015,
                reason="take_profit",
            )
        ]
        * 19
        + [
            SimTrade(
                symbol="SOL/USDT:USDT",
                side=PositionSide.LONG,
                regime=Regime.SHOCK_TREND_DOWN,
                entry_time=started_at,
                exit_time=ended_at,
                entry_price=100.0,
                exit_price=80.0,
                qty=1.0,
                pnl=-1000.0,
                pnl_pct_equity=-0.1,
                reason="stop_loss",
            )
        ],
    )

    assert score_runtime_result(profitable, 10_000.0) > score_runtime_result(uneven, 10_000.0)


def test_backtest_quality_calculates_drawdown_and_profit_factor() -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    result = BacktestResult(
        started_at=started_at,
        ended_at=ended_at,
        trades=[
            SimTrade(
                symbol="BTC/USDT:USDT",
                side=PositionSide.LONG,
                regime=Regime.SHOCK_TREND_UP,
                entry_time=started_at,
                exit_time=started_at,
                entry_price=100.0,
                exit_price=110.0,
                qty=1.0,
                pnl=500.0,
                pnl_pct_equity=0.05,
                reason="take_profit",
            ),
            SimTrade(
                symbol="BTC/USDT:USDT",
                side=PositionSide.LONG,
                regime=Regime.SHOCK_TREND_UP,
                entry_time=started_at,
                exit_time=ended_at,
                entry_price=100.0,
                exit_price=90.0,
                qty=1.0,
                pnl=-300.0,
                pnl_pct_equity=-0.03,
                reason="stop_loss",
            ),
        ],
    )

    quality = backtest_quality(result, 10_000.0)

    assert quality.gross_profit == 500.0
    assert quality.gross_loss == 300.0
    assert round(quality.profit_factor, 3) == 1.667
    assert quality.max_drawdown == 300.0
    assert round(quality.max_drawdown_pct, 4) == 0.0286


def test_build_backtest_config_from_settings_preserves_runtime_values() -> None:
    settings = Settings(
        dry_run=True,
        risk_percent=0.012,
        same_direction_risk_limit=0.06,
        max_signal_risk_multiplier=3.0,
    )

    config = build_backtest_config_from_settings(
        settings=settings,
        days=30,
        symbols=["BTC/USDT:USDT"],
        initial_equity=10_000.0,
        confirmation_position_sizing=True,
        llm_review=False,
        llm_max_calls=5,
    )

    assert config.risk_percent == 0.012
    assert config.same_direction_risk_limit == 0.06
    assert config.max_signal_risk_multiplier == 3.0
    assert config.confirmation_position_sizing is True
    assert config.llm_regime_review_enabled is False
    assert config.llm_max_calls == 5


async def test_tuning_agent_once_uses_runtime_tuning(monkeypatch, tmp_path) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    candidate = RuntimeTuningCandidate(
        name="current",
        description="test",
        config=BacktestConfig(symbols=["BTC/USDT:USDT"], output_dir=tmp_path),
    )
    result = BacktestResult(started_at=started_at, ended_at=ended_at)
    tuning_result = RuntimeTuningResult(candidate, result, tmp_path / "report.md", 1.0)

    def fake_run(candidates, progress=None):
        return [tuning_result], tmp_path / "runtime.md"

    monkeypatch.setattr("trading_system.tuner.run_runtime_tuning", fake_run)

    results, report_path = await run_tuning_agent_once([candidate])

    assert results == [tuning_result]
    assert report_path == tmp_path / "runtime.md"


def test_has_clear_improvement_requires_non_current_winner() -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    current = RuntimeTuningResult(
        RuntimeTuningCandidate("current", "current", BacktestConfig(symbols=["BTC/USDT:USDT"])),
        BacktestResult(started_at=started_at, ended_at=ended_at),
        Path("current.md"),
        100.0,
    )
    recommended = RuntimeTuningResult(
        RuntimeTuningCandidate("balanced_3x", "balanced", BacktestConfig(symbols=["BTC/USDT:USDT"])),
        BacktestResult(started_at=started_at, ended_at=ended_at),
        Path("balanced.md"),
        130.0,
    )

    assert has_clear_improvement([recommended, current], 20.0)
    assert not has_clear_improvement([recommended, current], 40.0)
    assert not has_clear_improvement([current], 0.0)


def test_runtime_tuning_report_includes_live_vs_recommended_diff(tmp_path) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    current = RuntimeTuningResult(
        RuntimeTuningCandidate(
            "current",
            "current",
            BacktestConfig(symbols=["BTC/USDT:USDT"], output_dir=tmp_path, max_signal_risk_multiplier=1.5),
        ),
        BacktestResult(started_at=started_at, ended_at=ended_at),
        tmp_path / "current.md",
        100.0,
    )
    recommended = RuntimeTuningResult(
        RuntimeTuningCandidate(
            "balanced_3x",
            "balanced",
            BacktestConfig(symbols=["BTC/USDT:USDT"], output_dir=tmp_path, max_signal_risk_multiplier=3.0),
        ),
        BacktestResult(started_at=started_at, ended_at=ended_at),
        tmp_path / "balanced.md",
        130.0,
    )

    report_path = write_runtime_tuning_report([recommended, current], 1779233200000)

    text = report_path.read_text(encoding="utf-8")
    assert "## 实盘参数与推荐参数差异" in text
    assert "| max_signal_risk_multiplier | `1.5` | `3` | `+1.5` |" in text
