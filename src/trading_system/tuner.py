from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any, Awaitable, NotRequired, TypedDict

import ccxt
import pandas as pd
from pydantic import BaseModel, Field

from trading_system.backtest import (
    BacktestConfig,
    BacktestResult,
    OKXBacktester,
    SimTrade,
)
from trading_system.config import Settings
from trading_system.email_notification import ReportEmailNotifier, SmtpConfig
from trading_system.models import Regime

PARAMETER_KNOWLEDGE_PATH = Path("knowledge/parameter_tuning_rules.md")


@dataclass(slots=True)
class RuntimeTuningCandidate:
    name: str
    description: str
    config: BacktestConfig

    @property
    def params(self) -> dict[str, float | bool]:
        return {
            "confirmation_position_sizing": self.config.confirmation_position_sizing,
            "max_signal_risk_multiplier": self.config.max_signal_risk_multiplier,
            "confirmation_max_risk_multiplier": self.config.confirmation_max_risk_multiplier,
            "same_direction_risk_limit": self.config.same_direction_risk_limit,
            "shock_leverage_limit": self.config.shock_leverage_limit,
            "trend_symbol_leverage_limit": self.config.trend_symbol_leverage_limit,
            "risk_percent": self.config.risk_percent,
        }


@dataclass(slots=True)
class RuntimeTuningResult:
    candidate: RuntimeTuningCandidate
    result: BacktestResult
    report_path: Path
    score: float


@dataclass(frozen=True, slots=True)
class BacktestQuality:
    gross_profit: float
    gross_loss: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    worst_trade: float


@dataclass(frozen=True, slots=True)
class WalkForwardStats:
    train_pnl: float
    train_win_rate: float
    train_trades: int
    validation_pnl: float
    validation_win_rate: float
    validation_trades: int
    validation_score: float


class TuningAgentState(TypedDict):
    candidates: list[RuntimeTuningCandidate]
    results: NotRequired[list[RuntimeTuningResult]]
    report_path: NotRequired[Path]


class LLMParameterCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    max_signal_risk_multiplier: float = Field(ge=1.0, le=4.0)
    confirmation_max_risk_multiplier: float = Field(ge=1.0, le=4.0)
    same_direction_risk_limit: float = Field(ge=0.01, le=0.09)
    shock_leverage_limit: float = Field(ge=1.0, le=5.0)
    trend_symbol_leverage_limit: float = Field(ge=1.0, le=8.0)
    risk_percent: float = Field(ge=0.001, le=0.015)


class LLMParameterProposal(BaseModel):
    reason: str
    candidates: list[LLMParameterCandidate]


def runtime_tuning_candidates(base: BacktestConfig) -> list[RuntimeTuningCandidate]:
    candidate_specs = [
        (
            "current",
            "当前实盘参数快照，不修改风险上限。",
            base.max_signal_risk_multiplier,
            base.confirmation_max_risk_multiplier,
            base.same_direction_risk_limit,
        ),
        ("conservative_2x", "温和动态仓位：高确认信号最高 2x，同方向风险提高到 4%。", 2.0, 2.0, 0.04),
        ("balanced_3x", "均衡动态仓位：高确认信号最高 3x，同方向风险提高到 6%。", 3.0, 3.0, 0.06),
        ("growth_4x", "增长动态仓位：高确认信号最高 4x，同方向风险提高到 8%。", 4.0, 4.0, 0.08),
        ("aggressive_5x", "进攻动态仓位：高确认信号最高 5x，同方向风险提高到 10%。", 5.0, 5.0, 0.10),
        ("wide_3x", "更宽同向风险：高确认信号最高 3x，同方向风险提高到 10%。", 3.0, 3.0, 0.10),
        ("focused_4x", "更集中单信号：高确认信号最高 4x，同方向风险保持 6%。", 4.0, 4.0, 0.06),
    ]
    candidates = [
        RuntimeTuningCandidate(
            name,
            description,
            replace(
                base,
                confirmation_position_sizing=True,
                max_signal_risk_multiplier=max(
                    base.max_signal_risk_multiplier, max_signal_risk_multiplier
                ),
                confirmation_max_risk_multiplier=max(
                    base.confirmation_max_risk_multiplier,
                    confirmation_max_risk_multiplier,
                ),
                same_direction_risk_limit=max(
                    base.same_direction_risk_limit, same_direction_risk_limit
                ),
                shock_leverage_limit=max(base.shock_leverage_limit, 3.0),
                trend_symbol_leverage_limit=max(base.trend_symbol_leverage_limit, 5.0),
            ),
        )
        for name, description, max_signal_risk_multiplier, confirmation_max_risk_multiplier, same_direction_risk_limit in candidate_specs
    ]
    unique: dict[tuple[tuple[str, float | bool], ...], RuntimeTuningCandidate] = {}
    for candidate in candidates:
        unique.setdefault(tuple(sorted(candidate.params.items())), candidate)
    return list(unique.values())


def candidate_from_llm_proposal(
    base: BacktestConfig, proposal: LLMParameterCandidate
) -> RuntimeTuningCandidate:
    return RuntimeTuningCandidate(
        name=f"llm_{proposal.name}",
        description="LLM 根据参数调优知识库提出，已通过本地边界校验。",
        config=replace(
            base,
            confirmation_position_sizing=True,
            max_signal_risk_multiplier=proposal.max_signal_risk_multiplier,
            confirmation_max_risk_multiplier=proposal.confirmation_max_risk_multiplier,
            same_direction_risk_limit=proposal.same_direction_risk_limit,
            shock_leverage_limit=proposal.shock_leverage_limit,
            trend_symbol_leverage_limit=proposal.trend_symbol_leverage_limit,
            risk_percent=proposal.risk_percent,
        ),
    )


def score_runtime_result(result: BacktestResult, initial_equity: float) -> float:
    trade_count = len(result.trades)
    quality = backtest_quality(result, initial_equity)
    score = result.total_pnl
    score -= max(0, 20 - trade_count) * 40.0
    score -= max(0.0, 0.58 - result.win_rate) * initial_equity * 0.25
    if result.avg_win:
        score -= max(0.0, abs(result.avg_loss) - result.avg_win * 1.25) * 1.5
    for pnl in pnl_by_symbol(result.trades).values():
        if pnl < 0:
            score += pnl * 1.5
    for pnl in pnl_by_regime(result.trades).values():
        if pnl < 0:
            score += pnl * 0.75
    score -= max(0.0, abs(quality.worst_trade) - initial_equity * 0.015) * 2.0
    score -= max(0.0, quality.max_drawdown_pct - 0.08) * initial_equity * 2.0
    score -= max(0.0, 1.35 - quality.profit_factor) * initial_equity * 0.15
    walk = walk_forward_stats(result)
    if walk.validation_trades == 0:
        score -= 500.0
    if walk.validation_pnl < 0:
        score += walk.validation_pnl * 1.2
    score -= max(0.0, 0.55 - walk.validation_win_rate) * initial_equity * 0.12
    return score


def build_backtest_config_from_settings(
    settings: Settings,
    days: int,
    symbols: list[str],
    initial_equity: float,
    confirmation_position_sizing: bool,
    llm_review: bool,
    llm_max_calls: int,
) -> BacktestConfig:
    return BacktestConfig(
        symbols=symbols,
        days=days,
        initial_equity=initial_equity,
        risk_percent=settings.risk_percent,
        same_direction_risk_limit=settings.same_direction_risk_limit,
        daily_drawdown_limit=settings.daily_drawdown_limit,
        shock_leverage_limit=settings.shock_leverage_limit,
        trend_symbol_leverage_limit=settings.trend_symbol_leverage_limit,
        max_signal_risk_multiplier=settings.max_signal_risk_multiplier,
        confirmation_position_sizing=confirmation_position_sizing,
        confirmation_max_risk_multiplier=settings.confirmation_max_risk_multiplier,
        funding_block_threshold=settings.funding_block_threshold,
        min_stop_loss_pct=settings.min_stop_loss_pct,
        high_vol_min_stop_loss_pct=settings.high_vol_min_stop_loss_pct,
        extreme_vol_min_stop_loss_pct=settings.extreme_vol_min_stop_loss_pct,
        max_stop_loss_pct=settings.max_stop_loss_pct,
        shock_reward_risk=settings.shock_reward_risk,
        trend_reward_risk=settings.trend_reward_risk,
        trailing_gap_pct=settings.trailing_gap_pct,
        min_trailing_activate_r=settings.min_trailing_activate_r,
        enable_trend_short=settings.enable_trend_short,
        trend_long_risk_multiplier=settings.trend_long_risk_multiplier,
        trend_short_risk_multiplier=settings.trend_short_risk_multiplier,
        defensive_risk_multiplier=settings.defensive_risk_multiplier,
        shock_trend_risk_multiplier=settings.shock_trend_risk_multiplier,
        shock_trend_down_risk_multiplier=settings.shock_trend_down_risk_multiplier,
        enable_shock_trend_scout=settings.enable_shock_trend_scout,
        shock_trend_scout_risk_multiplier=settings.shock_trend_scout_risk_multiplier,
        enable_liquidity_sweep_reversal=settings.enable_liquidity_sweep_reversal,
        liquidity_sweep_risk_multiplier=settings.liquidity_sweep_risk_multiplier,
        liquidity_sweep_require_confirmation=settings.liquidity_sweep_require_confirmation,
        enable_two_candle_momentum=settings.enable_two_candle_momentum,
        enable_daily_macd_breakout=settings.enable_daily_macd_breakout,
        enable_adaptive_strategy_switch=settings.enable_adaptive_strategy_switch,
        adaptive_no_trade_hours=settings.adaptive_no_trade_hours,
        adaptive_min_range_24h_pct=settings.adaptive_min_range_24h_pct,
        min_take_profit_pct=settings.min_take_profit_pct,
        llm_regime_review_enabled=llm_review,
        llm_regime_provider=settings.llm_regime_provider,
        llm_regime_model=settings.llm_regime_model,
        llm_regime_base_url=settings.llm_regime_base_url,
        llm_regime_api_key=settings.llm_regime_api_key,
        llm_regime_api_key_env=settings.llm_regime_api_key_env,
        llm_max_calls=llm_max_calls,
    )


def run_runtime_tuning(
    candidates: Iterable[RuntimeTuningCandidate],
    progress: Callable[[str], None] | None = None,
) -> tuple[list[RuntimeTuningResult], Path]:
    candidate_list = list(candidates)
    if not candidate_list:
        raise ValueError("at least one tuning candidate is required")
    end_ms = ccxt.okx().milliseconds()
    results: list[RuntimeTuningResult] = []
    for candidate in candidate_list:
        if progress:
            progress(f"running candidate: {candidate.name}")
        tester = OKXBacktester(candidate.config)
        tester.exchange.milliseconds = lambda end_ms=end_ms: end_ms  # type: ignore[method-assign]
        result, report_path = tester.run()
        score = score_runtime_result(result, candidate.config.initial_equity)
        results.append(RuntimeTuningResult(candidate, result, report_path, score))
        if progress:
            progress(
                f"finished candidate: {candidate.name} pnl={result.total_pnl:.2f} "
                f"win_rate={result.win_rate:.2%} trades={len(result.trades)} score={score:.2f}"
            )
    results.sort(key=lambda item: item.score, reverse=True)
    report_path = write_runtime_tuning_report(results, end_ms)
    write_research_ledger(results, report_path, end_ms)
    notify_runtime_tuning_report(results, report_path, progress)
    return results, report_path


def notify_runtime_tuning_report(
    results: list[RuntimeTuningResult],
    report_path: Path,
    progress: Callable[[str], None] | None = None,
) -> bool:
    settings = Settings()
    notifier = ReportEmailNotifier(SmtpConfig.from_settings(settings))
    sent = notifier.notify_runtime_tuning_report(
        report_path, summary=format_runtime_tuning_email_summary(results)
    )
    if progress:
        progress(f"runtime tuning report email: {'sent' if sent else 'skipped'}")
    return sent


def format_runtime_tuning_email_summary(results: list[RuntimeTuningResult]) -> str:
    if not results:
        return ""
    current = next(
        (item for item in results if item.candidate.name == "current"), results[0]
    )
    recommended = results[0]
    lines = [
        f"推荐参数: {recommended.candidate.name}",
        f"推荐收益: {recommended.result.total_pnl:.2f} USDT",
        f"推荐胜率: {recommended.result.win_rate:.2%}",
        f"推荐交易数: {len(recommended.result.trades)}",
        f"当前参数收益: {current.result.total_pnl:.2f} USDT",
        f"当前参数胜率: {current.result.win_rate:.2%}",
        f"当前参数交易数: {len(current.result.trades)}",
    ]
    if recommended.candidate.name != current.candidate.name:
        lines.append(
            f"相对当前收益变化: {recommended.result.total_pnl - current.result.total_pnl:.2f} USDT"
        )
    return "\n".join(lines)


RuntimeBaseFactory = Callable[[], BacktestConfig | Awaitable[BacktestConfig]]


async def run_runtime_tuning_loop(
    base: BacktestConfig | RuntimeBaseFactory,
    interval_seconds: int,
    llm_propose: bool = False,
    max_iterations: int = 1,
    min_improvement_score: float = 0.0,
    progress: Callable[[str], None] | None = None,
    once: bool = False,
) -> None:
    while True:
        cycle_base = await resolve_runtime_base(base)
        candidates = runtime_tuning_candidates(cycle_base)
        results, _ = await run_tuning_agent_once(candidates, progress=progress)
        iteration = 1
        while (
            llm_propose
            and iteration < max(1, max_iterations)
            and not has_clear_improvement(results, min_improvement_score)
        ):
            try:
                proposals = propose_llm_candidates(cycle_base, results)
            except Exception as exc:
                if progress:
                    progress(f"llm proposal skipped: {type(exc).__name__}")
                proposals = []
            if not proposals:
                break
            iteration += 1
            candidates = dedupe_candidates(
                [
                    *candidates,
                    *(
                        candidate_from_llm_proposal(cycle_base, item)
                        for item in proposals
                    ),
                ]
            )
            if progress:
                progress(f"llm tuning iteration: {iteration}/{max_iterations}")
            results, _ = await run_tuning_agent_once(candidates, progress=progress)
        if once:
            return
        await asyncio.sleep(interval_seconds)


async def resolve_runtime_base(
    base: BacktestConfig | RuntimeBaseFactory,
) -> BacktestConfig:
    if isinstance(base, BacktestConfig):
        return base
    value = base()
    if hasattr(value, "__await__"):
        return await value  # type: ignore[no-any-return]
    return value


async def run_tuning_agent_once(
    candidates: list[RuntimeTuningCandidate],
    progress: Callable[[str], None] | None = None,
) -> tuple[list[RuntimeTuningResult], Path]:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        return await asyncio.to_thread(run_runtime_tuning, candidates, progress)

    graph = StateGraph(TuningAgentState)

    def backtest_node(state: TuningAgentState) -> dict[str, Any]:
        results, report_path = run_runtime_tuning(state["candidates"], progress)
        return {"results": results, "report_path": report_path}

    def summarize_node(state: TuningAgentState) -> TuningAgentState:
        results = state["results"]
        recommended = results[0]
        if progress:
            progress(
                f"agent recommended: {recommended.candidate.name} "
                f"pnl={recommended.result.total_pnl:.2f} score={recommended.score:.2f}"
            )
        return state

    graph.add_node("backtest", backtest_node)
    graph.add_node("summarize", summarize_node)
    graph.set_entry_point("backtest")
    graph.add_edge("backtest", "summarize")
    graph.add_edge("summarize", END)
    app = graph.compile()
    state = await asyncio.to_thread(app.invoke, {"candidates": candidates})
    return state["results"], state["report_path"]


def propose_llm_candidates(
    base: BacktestConfig,
    previous_results: list[RuntimeTuningResult] | None = None,
    max_candidates: int = 2,
) -> list[LLMParameterCandidate]:
    provider = base.llm_regime_provider.lower()
    api_key_env = base.llm_regime_api_key_env or (
        "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
    )
    api_key = base.llm_regime_api_key or (
        api_key_env
        if api_key_env.startswith(("sk-", "sk_"))
        else os.getenv(api_key_env)
    )
    if not api_key:
        return []
    from openai import OpenAI

    knowledge = PARAMETER_KNOWLEDGE_PATH.read_text(encoding="utf-8")
    client = OpenAI(
        api_key=api_key,
        base_url=base.llm_regime_base_url
        or ("https://api.deepseek.com" if provider == "deepseek" else None),
    )
    payload = {
        "knowledge_base": knowledge,
        "current_params": {
            "max_signal_risk_multiplier": base.max_signal_risk_multiplier,
            "confirmation_max_risk_multiplier": base.confirmation_max_risk_multiplier,
            "same_direction_risk_limit": base.same_direction_risk_limit,
            "shock_leverage_limit": base.shock_leverage_limit,
            "trend_symbol_leverage_limit": base.trend_symbol_leverage_limit,
            "risk_percent": base.risk_percent,
        },
        "previous_results": [
            tuning_result_summary(item) for item in previous_results or []
        ],
        "request": f"Propose up to {max_candidates} bounded candidates for a 30-day backtest.",
    }
    response = client.chat.completions.create(
        model=base.llm_regime_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a constrained trading-parameter advisor. Return one JSON object only. "
                    "Do not suggest parameters outside the provided knowledge-base bounds."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = response.choices[0].message.content
    if not content:
        return []
    proposal = LLMParameterProposal.model_validate_json(content)
    return proposal.candidates[:max_candidates]


def tuning_result_summary(item: RuntimeTuningResult) -> dict[str, Any]:
    trades = item.result.trades
    quality = backtest_quality(item.result, item.candidate.config.initial_equity)
    walk = walk_forward_stats(item.result)
    return {
        "candidate": item.candidate.name,
        "score": round(item.score, 2),
        "pnl": round(item.result.total_pnl, 2),
        "win_rate": round(item.result.win_rate, 4),
        "trades": len(trades),
        "avg_win": round(item.result.avg_win, 2),
        "avg_loss": round(item.result.avg_loss, 2),
        "worst_trade": round(quality.worst_trade, 2),
        "profit_factor": round(quality.profit_factor, 3),
        "max_drawdown": round(quality.max_drawdown, 2),
        "max_drawdown_pct": round(quality.max_drawdown_pct, 4),
        "walk_forward": {
            "train_pnl": round(walk.train_pnl, 2),
            "train_win_rate": round(walk.train_win_rate, 4),
            "train_trades": walk.train_trades,
            "validation_pnl": round(walk.validation_pnl, 2),
            "validation_win_rate": round(walk.validation_win_rate, 4),
            "validation_trades": walk.validation_trades,
            "validation_score": round(walk.validation_score, 2),
        },
        "pnl_by_symbol": {
            symbol: round(pnl, 2) for symbol, pnl in pnl_by_symbol(trades).items()
        },
        "pnl_by_regime": {
            regime: round(pnl, 2) for regime, pnl in pnl_by_regime(trades).items()
        },
        "params": item.candidate.params,
    }


def write_runtime_tuning_report(
    results: list[RuntimeTuningResult], end_ms: int
) -> Path:
    if not results:
        raise ValueError("results cannot be empty")
    output_dir = results[0].candidate.config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = (
        output_dir / f"runtime_tuning_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%f}.md"
    )
    current = next(
        (item for item in results if item.candidate.name == "current"), results[0]
    )
    recommended = results[0]
    current_config = current.candidate.config
    lines = [
        "# 30 天实盘参数对比报告",
        "",
        f"- 固定回测结束时间: `{datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat()}`",
        f"- 回测天数: `{current_config.days}`",
        f"- 初始权益: `{current_config.initial_equity:.2f} USDT`",
        f"- 品种: `{', '.join(current_config.symbols)}`",
        f"- LLM 复核: `{current_config.llm_regime_review_enabled}`；本报告只对比参数，不会写入实盘运行时配置。",
        "- 评分: `总盈利 - 低交易数惩罚 - 低胜率惩罚 - 盈亏比惩罚 - 分品种/分行情亏损惩罚 - 单笔大亏惩罚 - 最大回撤惩罚 - profit factor 惩罚`",
        "",
        "## 参数对比",
        "",
        "| rank | candidate | score | pnl | val_pnl | val_win | win_rate | trades | max_dd | max_dd_pct | profit_factor | avg_win | avg_loss | worst | avg_risk | BTC | ETH | SOL | report |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for rank, item in enumerate(results, start=1):
        trades = item.result.trades
        by_symbol = pnl_by_symbol(trades)
        quality = backtest_quality(item.result, item.candidate.config.initial_equity)
        walk = walk_forward_stats(item.result)
        avg_risk = (
            sum(trade.risk_multiplier for trade in trades) / len(trades)
            if trades
            else 0.0
        )
        lines.append(
            f"| {rank} | {item.candidate.name} | {item.score:.2f} | {item.result.total_pnl:.2f} | "
            f"{walk.validation_pnl:.2f} | {walk.validation_win_rate:.2%} | "
            f"{item.result.win_rate:.2%} | {len(trades)} | {quality.max_drawdown:.2f} | "
            f"{quality.max_drawdown_pct:.2%} | {format_profit_factor(quality.profit_factor)} | "
            f"{item.result.avg_win:.2f} | {item.result.avg_loss:.2f} | {quality.worst_trade:.2f} | {avg_risk:.2f} | "
            f"{by_symbol.get('BTC/USDT:USDT', 0.0):.2f} | {by_symbol.get('ETH/USDT:USDT', 0.0):.2f} | "
            f"{by_symbol.get('SOL/USDT:USDT', 0.0):.2f} | {item.report_path} |"
        )
    lines.extend(
        [
            "",
            "## 当前参数",
            "",
            f"- candidate: `{current.candidate.name}`",
            f"- report: `{current.report_path}`",
            f"- pnl: `{current.result.total_pnl:.2f} USDT`",
            f"- win_rate: `{current.result.win_rate:.2%}`",
            f"- trades: `{len(current.result.trades)}`",
            f"- params: `{current.candidate.params}`",
            "",
            "## 推荐参数",
            "",
            f"- candidate: `{recommended.candidate.name}`",
            f"- report: `{recommended.report_path}`",
            f"- pnl: `{recommended.result.total_pnl:.2f} USDT`",
            f"- win_rate: `{recommended.result.win_rate:.2%}`",
            f"- trades: `{len(recommended.result.trades)}`",
            f"- params: `{recommended.candidate.params}`",
            "",
            "## 实盘参数与推荐参数差异",
            "",
            "| 参数 | 当前实盘 | 推荐 | 变化 |",
            "|---|---:|---:|---:|",
        ]
    )
    for key, current_value in current.candidate.params.items():
        recommended_value = recommended.candidate.params.get(key)
        lines.append(
            f"| {key} | {format_param_value(current_value)} | {format_param_value(recommended_value)} | "
            f"{format_param_delta(current_value, recommended_value)} |"
        )
    lines.extend(
        [
            "",
            "## 推荐原因",
            "",
        ]
    )
    if recommended.candidate.name == current.candidate.name:
        lines.append("当前参数已经是本轮候选里评分最高的一档，暂不建议自动放大仓位。")
    else:
        delta = recommended.result.total_pnl - current.result.total_pnl
        lines.append(
            f"推荐 `{recommended.candidate.name}`，相对当前参数净利润变化 `{delta:.2f} USDT`，"
            f"胜率变化 `{recommended.result.win_rate - current.result.win_rate:.2%}`。"
        )
    lines.extend(
        [
            "",
            "## 分行情表现",
            "",
            "| candidate | regime | trades | win_rate | pnl |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for item in results:
        for regime in [
            Regime.TREND_LONG,
            Regime.TREND_SHORT,
            Regime.SHOCK_TREND_UP,
            Regime.SHOCK_TREND_DOWN,
            Regime.SHOCK,
        ]:
            trades = [trade for trade in item.result.trades if trade.regime == regime]
            if not trades:
                continue
            wins = sum(1 for trade in trades if trade.pnl > 0)
            lines.append(
                f"| {item.candidate.name} | {regime.value} | {len(trades)} | "
                f"{wins / len(trades):.2%} | {sum(trade.pnl for trade in trades):.2f} |"
            )
    lines.extend(["", "## 候选说明", ""])
    for item in results:
        lines.append(f"- `{item.candidate.name}`: {item.candidate.description}")
    lines.extend(
        [
            "",
            "## Walk-forward 验证说明",
            "",
            "- 本报告把每个候选的 30 天交易按时间切成前 2/3 训练段、后 1/3 验证段。",
            "- 评分会惩罚验证段亏损、验证段无交易和验证段胜率偏低，避免只选中前半段过拟合参数。",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def write_research_ledger(
    results: list[RuntimeTuningResult], report_path: Path, end_ms: int
) -> Path:
    if not results:
        raise ValueError("results cannot be empty")
    ledger_path = results[0].candidate.config.output_dir / "runtime_tuning_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "end_time": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
        "report_path": str(report_path),
        "recommended": results[0].candidate.name,
        "candidates": [tuning_result_summary(item) for item in results],
    }
    with ledger_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return ledger_path


def has_clear_improvement(
    results: list[RuntimeTuningResult], min_improvement_score: float
) -> bool:
    if not results:
        return False
    current = next(
        (item for item in results if item.candidate.name == "current"), results[-1]
    )
    recommended = results[0]
    return (
        recommended.candidate.name != current.candidate.name
        and recommended.score - current.score >= min_improvement_score
    )


def dedupe_candidates(
    candidates: list[RuntimeTuningCandidate],
) -> list[RuntimeTuningCandidate]:
    unique: dict[tuple[tuple[str, float | bool], ...], RuntimeTuningCandidate] = {}
    for candidate in candidates:
        unique.setdefault(tuple(sorted(candidate.params.items())), candidate)
    return list(unique.values())


def format_param_value(value: float | bool | None) -> str:
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if value is None:
        return "`-`"
    return f"`{float(value):.4g}`"


def format_param_delta(
    current: float | bool | None, recommended: float | bool | None
) -> str:
    if isinstance(current, bool) or isinstance(recommended, bool):
        return "`changed`" if current != recommended else "`same`"
    if current is None or recommended is None:
        return "`-`"
    return f"`{float(recommended) - float(current):+.4g}`"


def backtest_quality(result: BacktestResult, initial_equity: float) -> BacktestQuality:
    gross_profit = sum(trade.pnl for trade in result.trades if trade.pnl > 0)
    gross_loss = abs(sum(trade.pnl for trade in result.trades if trade.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    for trade in sorted(result.trades, key=lambda item: item.exit_time):
        equity += trade.pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    max_drawdown_pct = max_drawdown / peak if peak else 0.0
    worst_trade = min((trade.pnl for trade in result.trades), default=0.0)
    return BacktestQuality(
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        worst_trade=worst_trade,
    )


def walk_forward_stats(result: BacktestResult) -> WalkForwardStats:
    split_time = result.started_at + (result.ended_at - result.started_at) * (2 / 3)
    train = [
        trade
        for trade in result.trades
        if trade_datetime(trade.entry_time) < split_time
    ]
    validation = [
        trade
        for trade in result.trades
        if trade_datetime(trade.entry_time) >= split_time
    ]
    train_pnl = sum(trade.pnl for trade in train)
    validation_pnl = sum(trade.pnl for trade in validation)
    train_win_rate = (
        sum(1 for trade in train if trade.pnl > 0) / len(train) if train else 0.0
    )
    validation_win_rate = (
        sum(1 for trade in validation if trade.pnl > 0) / len(validation)
        if validation
        else 0.0
    )
    validation_score = validation_pnl
    validation_score -= max(0, 5 - len(validation)) * 50.0
    validation_score -= max(0.0, 0.55 - validation_win_rate) * 500.0
    return WalkForwardStats(
        train_pnl=train_pnl,
        train_win_rate=train_win_rate,
        train_trades=len(train),
        validation_pnl=validation_pnl,
        validation_win_rate=validation_win_rate,
        validation_trades=len(validation),
        validation_score=validation_score,
    )


def trade_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return pd.Timestamp(value).to_pydatetime()


def format_profit_factor(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def pnl_by_symbol(trades: list[SimTrade]) -> dict[str, float]:
    values: dict[str, float] = {}
    for trade in trades:
        values[trade.symbol] = values.get(trade.symbol, 0.0) + trade.pnl
    return values


def pnl_by_regime(trades: list[SimTrade]) -> dict[str, float]:
    values: dict[str, float] = {}
    for trade in trades:
        values[trade.regime.value] = values.get(trade.regime.value, 0.0) + trade.pnl
    return values
