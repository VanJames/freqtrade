from __future__ import annotations

import asyncio
import logging
from typing import Optional

import typer

from trading_system.config import Settings
from trading_system.engine import OKXQuantEngine
from trading_system.logging import configure_logging
from trading_system.cache import StateCache
from trading_system.backtest import BacktestConfig, OKXBacktester
from trading_system.runtime_config import apply_runtime_config
from trading_system.store import StateStore
from trading_system.tuner import (
    RuntimeTuningCandidate,
    build_backtest_config_from_settings,
    run_runtime_tuning,
    run_runtime_tuning_loop,
    runtime_tuning_candidates,
)

app = typer.Typer(no_args_is_help=True)


async def settings_with_runtime_overrides(settings: Settings) -> Settings:
    store = StateStore(settings.postgres_dsn, settings.exchange_id)
    try:
        runtime_values = await store.load_runtime_settings()
    finally:
        await store.close()
    if runtime_values:
        apply_runtime_config(settings, runtime_values)
        selected_exchange = runtime_values.get("selected_exchange_id")
        if selected_exchange in {"okx", "hotcoin"}:
            settings.exchange_id = selected_exchange
    return settings


@app.command()
def run(
    dry_run: bool = typer.Option(True, help="Use dry-run exchange and never send real orders."),
    once: bool = typer.Option(False, help="Run one full decision cycle and exit."),
    with_store: bool = typer.Option(False, help="Initialize PostgreSQL tables and persist orders/snapshots."),
    with_redis: bool = typer.Option(False, help="Use Redis for high-frequency positions/orders/hedge-lock cache."),
) -> None:
    configure_logging(logging.INFO)
    settings = Settings(dry_run=dry_run)
    store = StateStore(settings.postgres_dsn, settings.exchange_id) if with_store else None
    cache = StateCache(settings.redis_url) if with_redis else None

    async def main() -> None:
        if store:
            runtime_values = await store.load_runtime_settings()
            selected_exchange = runtime_values.get("selected_exchange_id")
            if selected_exchange in {"okx", "hotcoin"}:
                settings.exchange_id = selected_exchange
                store.exchange_id = selected_exchange
        engine = OKXQuantEngine(settings, store=store, cache=cache)
        await engine.initialize(init_store=with_store)
        if once:
            await engine.run_once()
            await engine.shutdown()
        else:
            await engine.run()

    asyncio.run(main())


@app.command()
def init_db() -> None:
    configure_logging(logging.INFO)
    settings = Settings()
    store = StateStore(settings.postgres_dsn)

    async def main() -> None:
        await store.initialize()
        await store.close()

    asyncio.run(main())
    typer.echo("database initialized")


@app.command()
def backtest(
    days: int = typer.Option(30, help="Backtest lookback window in calendar days."),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated OKX swap symbols. Defaults to .env SYMBOLS."),
    initial_equity: float = typer.Option(10_000.0, help="Initial equity in USDT."),
    risk_percent: Optional[float] = typer.Option(None, help="Base risk per trade, e.g. 0.01 means 1%."),
    same_direction_risk_limit: Optional[float] = typer.Option(None, help="Same-direction risk cap."),
    daily_drawdown_limit: Optional[float] = typer.Option(None, help="Daily drawdown fuse threshold."),
    shock_stop_atr: float = typer.Option(1.0, help="SHOCK stop distance in ATR."),
    shock_take_profit_atr: float = typer.Option(1.8, help="SHOCK take-profit distance in ATR."),
    classifier_mode: str = typer.Option("user_4h", help="Regime classifier mode. Only user_4h is supported."),
    shock_leverage_limit: Optional[float] = typer.Option(None, help="Maximum leverage for SHOCK grid trades."),
    trend_leverage_limit: Optional[float] = typer.Option(None, help="Maximum leverage for trend/shock-trend trades."),
    max_signal_risk_multiplier: Optional[float] = typer.Option(None, help="Maximum per-signal risk multiplier."),
    confirmation_position_sizing: Optional[bool] = typer.Option(None, help="Boost position size only for highly confirmed signals."),
    confirmation_max_risk_multiplier: Optional[float] = typer.Option(None, help="Maximum boosted risk multiplier for confirmed signals."),
    liquidity_sweep_reversal: Optional[bool] = typer.Option(
        None,
        "--liquidity-sweep-reversal/--no-liquidity-sweep-reversal",
        help="Enable liquidity-sweep pin-bar reversal entries.",
    ),
    liquidity_sweep_risk_multiplier: Optional[float] = typer.Option(
        None,
        help="Base risk multiplier for liquidity-sweep reversal entries.",
    ),
    liquidity_sweep_confirmation: Optional[bool] = typer.Option(
        None,
        "--liquidity-sweep-confirmation/--no-liquidity-sweep-confirmation",
        help="Wait one extra 5m candle to confirm liquidity-sweep reversal entries.",
    ),
    shock_trend_scout: Optional[bool] = typer.Option(
        None,
        "--shock-trend-scout/--no-shock-trend-scout",
        help="Enable small planned scout entries for SHOCK_TREND regimes before confirmation add-ons.",
    ),
    shock_trend_scout_risk_multiplier: Optional[float] = typer.Option(
        None,
        help="Risk multiplier for SHOCK_TREND scout entries.",
    ),
    funding_rate: float = typer.Option(0.0, help="Backtest funding rate assumption."),
    funding_block_threshold: Optional[float] = typer.Option(None, help="Funding rate threshold used by risk checks."),
    llm_review: Optional[bool] = typer.Option(None, help="Enable LLM regime review during backtest. Defaults to .env."),
    llm_provider: Optional[str] = typer.Option(None, help="LLM provider: openai or deepseek."),
    llm_model: Optional[str] = typer.Option(None, help="LLM model name."),
    llm_max_calls: int = typer.Option(50, help="Maximum LLM review calls during one backtest."),
) -> None:
    configure_logging(logging.INFO)
    if classifier_mode != "user_4h":
        raise typer.BadParameter("dev classifier was removed; use user_4h.")
    settings = Settings()
    selected_symbols = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else settings.symbols
    result, report_path = OKXBacktester(
        BacktestConfig(
            symbols=selected_symbols,
            days=days,
            initial_equity=initial_equity,
            risk_percent=risk_percent if risk_percent is not None else settings.risk_percent,
            same_direction_risk_limit=(
                same_direction_risk_limit if same_direction_risk_limit is not None else settings.same_direction_risk_limit
            ),
            daily_drawdown_limit=daily_drawdown_limit if daily_drawdown_limit is not None else settings.daily_drawdown_limit,
            shock_leverage_limit=shock_leverage_limit if shock_leverage_limit is not None else settings.shock_leverage_limit,
            trend_symbol_leverage_limit=trend_leverage_limit if trend_leverage_limit is not None else settings.trend_symbol_leverage_limit,
            max_signal_risk_multiplier=(
                max_signal_risk_multiplier
                if max_signal_risk_multiplier is not None
                else settings.max_signal_risk_multiplier
            ),
            confirmation_position_sizing=(
                confirmation_position_sizing
                if confirmation_position_sizing is not None
                else settings.confirmation_position_sizing
            ),
            confirmation_max_risk_multiplier=(
                confirmation_max_risk_multiplier
                if confirmation_max_risk_multiplier is not None
                else settings.confirmation_max_risk_multiplier
            ),
            enable_liquidity_sweep_reversal=(
                liquidity_sweep_reversal
                if liquidity_sweep_reversal is not None
                else settings.enable_liquidity_sweep_reversal
            ),
            liquidity_sweep_risk_multiplier=(
                liquidity_sweep_risk_multiplier
                if liquidity_sweep_risk_multiplier is not None
                else settings.liquidity_sweep_risk_multiplier
            ),
            liquidity_sweep_require_confirmation=(
                liquidity_sweep_confirmation
                if liquidity_sweep_confirmation is not None
                else settings.liquidity_sweep_require_confirmation
            ),
            enable_shock_trend_scout=(
                shock_trend_scout if shock_trend_scout is not None else settings.enable_shock_trend_scout
            ),
            shock_trend_scout_risk_multiplier=(
                shock_trend_scout_risk_multiplier
                if shock_trend_scout_risk_multiplier is not None
                else settings.shock_trend_scout_risk_multiplier
            ),
            funding_block_threshold=(
                funding_block_threshold if funding_block_threshold is not None else settings.funding_block_threshold
            ),
            backtest_funding_rate=funding_rate,
            shock_stop_atr=shock_stop_atr,
            shock_take_profit_atr=shock_take_profit_atr,
            classifier_mode=classifier_mode,
            min_stop_loss_pct=settings.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=settings.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=settings.extreme_vol_min_stop_loss_pct,
            min_take_profit_pct=settings.min_take_profit_pct,
            llm_regime_review_enabled=llm_review if llm_review is not None else settings.llm_regime_review_enabled,
            llm_regime_provider=llm_provider or settings.llm_regime_provider,
            llm_regime_model=llm_model or settings.llm_regime_model,
            llm_regime_base_url=settings.llm_regime_base_url,
            llm_regime_api_key_env=settings.llm_regime_api_key_env,
            enable_adaptive_strategy_switch=settings.enable_adaptive_strategy_switch,
            adaptive_no_trade_hours=settings.adaptive_no_trade_hours,
            adaptive_min_range_24h_pct=settings.adaptive_min_range_24h_pct,
            llm_max_calls=llm_max_calls,
        )
    ).run()
    typer.echo(f"report: {report_path}")
    typer.echo(f"trades: {len(result.trades)}")
    typer.echo(f"avg_hours_per_trade: {result.avg_hours_per_trade:.2f}")
    typer.echo(f"avg_entry_gap_hours: {result.avg_entry_gap_hours:.2f}")
    typer.echo(f"win_rate: {result.win_rate:.2%}")
    typer.echo(f"total_pnl: {result.total_pnl:.2f} USDT")
    typer.echo(f"regime_accuracy: {result.regime_accuracy:.2%}")


@app.command()
def optimize_params(
    symbol: str = typer.Option(..., help="Single OKX swap symbol, e.g. SOL/USDT:USDT."),
    days: int = typer.Option(30, help="Optimization lookback window in calendar days."),
    classifier_mode: str = typer.Option("user_4h", help="Regime classifier mode. Only user_4h is supported."),
    top_n: int = typer.Option(10, help="Number of top parameter sets to report."),
) -> None:
    configure_logging(logging.INFO)
    if classifier_mode != "user_4h":
        raise typer.BadParameter("dev classifier was removed; use user_4h.")
    settings = Settings()
    top, report_path = OKXBacktester(
        BacktestConfig(
            symbols=[symbol],
            days=days,
            classifier_mode=classifier_mode,
            min_stop_loss_pct=settings.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=settings.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=settings.extreme_vol_min_stop_loss_pct,
            min_take_profit_pct=settings.min_take_profit_pct,
        )
    ).optimize_symbol(
        symbol=symbol,
        min_stop_loss_pcts=[0.002, 0.004],
        min_take_profit_pcts=[0.004, 0.008],
        defensive_risk_multipliers=[0.35, 0.7],
        shock_trend_risk_multipliers=[0.0, 0.1],
        shock_trend_down_risk_multipliers=[0.0, 0.1],
        top_n=top_n,
    )
    best = top[0] if top else None
    typer.echo(f"report: {report_path}")
    if best:
        typer.echo(f"best_score: {best.score:.2f}")
        typer.echo(f"best_pnl: {best.result.total_pnl:.2f} USDT")
        typer.echo(f"best_win_rate: {best.result.win_rate:.2%}")
        typer.echo(f"best_trades: {len(best.result.trades)}")
        typer.echo(f"best_params: {best.params}")


@app.command("tune-runtime")
def tune_runtime(
    days: int = typer.Option(30, help="Backtest lookback window in calendar days."),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated OKX swap symbols. Defaults to .env SYMBOLS."),
    initial_equity: float = typer.Option(10_000.0, help="Initial equity in USDT."),
    llm_review: bool = typer.Option(False, "--llm-review/--no-llm-review", help="Enable LLM review while tuning."),
    llm_max_calls: int = typer.Option(20, help="Maximum LLM review calls per candidate."),
    confirmation_position_sizing: bool = typer.Option(
        True,
        "--confirmation-position-sizing/--no-confirmation-position-sizing",
        help="Use confirmation-level dynamic sizing for the current-parameter baseline.",
    ),
) -> None:
    configure_logging(logging.INFO)
    settings = asyncio.run(settings_with_runtime_overrides(Settings()))
    selected_symbols = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else settings.symbols
    base = build_backtest_config_from_settings(
        settings=settings,
        days=days,
        symbols=selected_symbols,
        initial_equity=initial_equity,
        confirmation_position_sizing=confirmation_position_sizing,
        llm_review=llm_review,
        llm_max_calls=llm_max_calls,
    )
    candidates: list[RuntimeTuningCandidate] = runtime_tuning_candidates(base)
    results, report_path = run_runtime_tuning(candidates, progress=typer.echo)
    current = next((item for item in results if item.candidate.name == "current"), results[0])
    recommended = results[0]
    typer.echo(f"report: {report_path}")
    typer.echo(
        f"current: pnl={current.result.total_pnl:.2f} USDT "
        f"win_rate={current.result.win_rate:.2%} trades={len(current.result.trades)} score={current.score:.2f}"
    )
    typer.echo(
        f"recommended: {recommended.candidate.name} pnl={recommended.result.total_pnl:.2f} USDT "
        f"win_rate={recommended.result.win_rate:.2%} trades={len(recommended.result.trades)} score={recommended.score:.2f}"
    )
    typer.echo(f"recommended_params: {recommended.candidate.params}")


@app.command("tune-runtime-loop")
def tune_runtime_loop(
    days: int = typer.Option(30, help="Backtest lookback window in calendar days."),
    interval_hours: float = typer.Option(48.0, help="Run one tuning cycle every N hours."),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated OKX swap symbols. Defaults to .env SYMBOLS."),
    initial_equity: float = typer.Option(10_000.0, help="Initial equity in USDT."),
    llm_review: bool = typer.Option(False, "--llm-review/--no-llm-review", help="Enable LLM regime review in backtests."),
    llm_propose: bool = typer.Option(False, "--llm-propose/--no-llm-propose", help="Let LLM propose bounded extra candidates."),
    max_iterations: int = typer.Option(1, help="Maximum tuning iterations in one cycle."),
    min_improvement_score: float = typer.Option(0.0, help="Minimum score improvement over current params."),
    llm_max_calls: int = typer.Option(20, help="Maximum LLM regime review calls per candidate."),
    once: bool = typer.Option(False, help="Run one loop cycle and exit."),
) -> None:
    configure_logging(logging.INFO)

    async def base_factory() -> BacktestConfig:
        settings = await settings_with_runtime_overrides(Settings())
        selected_symbols = [item.strip() for item in symbols.split(",") if item.strip()] if symbols else settings.symbols
        return build_backtest_config_from_settings(
            settings=settings,
            days=days,
            symbols=selected_symbols,
            initial_equity=initial_equity,
            confirmation_position_sizing=True,
            llm_review=llm_review,
            llm_max_calls=llm_max_calls,
        )

    asyncio.run(
        run_runtime_tuning_loop(
            base_factory,
            interval_seconds=max(1, int(interval_hours * 3600)),
            llm_propose=llm_propose,
            max_iterations=max_iterations,
            min_improvement_score=min_improvement_score,
            progress=typer.echo,
            once=once,
        )
    )


if __name__ == "__main__":
    app()
