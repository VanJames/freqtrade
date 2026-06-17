from __future__ import annotations

import math
from typing import Any

import pandas as pd


class ExpectancyOptimizer:
    @staticmethod
    def calculate_metrics(trades: pd.DataFrame) -> dict[str, float]:
        if trades.empty:
            return {
                "total_trades": 0.0,
                "wins": 0.0,
                "losses": 0.0,
                "win_rate": 0.0,
                "loss_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "expectancy": 0.0,
                "profit_factor": 0.0,
            }
        pnl_column = "pnl" if "pnl" in trades.columns else "profit_abs"
        pnl = pd.to_numeric(trades[pnl_column], errors="coerce").fillna(0.0)
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        total = len(pnl)
        win_rate = len(wins) / total if total else 0.0
        loss_rate = len(losses) / total if total else 0.0
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = abs(float(losses.mean())) if len(losses) else 0.0
        expectancy = win_rate * avg_win - loss_rate * avg_loss
        gross_win = float(wins.sum()) if len(wins) else 0.0
        gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf if gross_win > 0 else 0.0
        return {
            "total_trades": float(total),
            "wins": float(len(wins)),
            "losses": float(len(losses)),
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy,
            "profit_factor": profit_factor,
        }

    @staticmethod
    def optimize_position_sizing(total_balance: float, expectancy: float, win_rate: float, max_loss_per_trade: float = 0.02) -> float:
        if total_balance <= 0 or expectancy <= 0 or win_rate <= 0:
            return min(max_loss_per_trade, 0.01)
        loss_rate = max(1.0 - win_rate, 0.01)
        kelly_fraction = expectancy / total_balance / loss_rate
        return max(0.001, min(kelly_fraction * 0.25, max_loss_per_trade))

    @staticmethod
    def generate_optimization_report(backtest_results: pd.DataFrame, min_expectancy: float = 0.01) -> str:
        metrics = ExpectancyOptimizer.calculate_metrics(backtest_results)
        profit_factor = metrics["profit_factor"]
        pf_text = "inf" if math.isinf(profit_factor) else f"{profit_factor:.2f}"
        lines = [
            "# 期望值优化分析报告",
            "",
            "## 交易统计",
            f"- 总交易数: `{int(metrics['total_trades'])}`",
            f"- 盈利交易: `{int(metrics['wins'])}` (`{metrics['win_rate']:.2%}`)",
            f"- 亏损交易: `{int(metrics['losses'])}` (`{metrics['loss_rate']:.2%}`)",
            "",
            "## 期望值指标",
            f"- 平均赢: `{metrics['avg_win']:.4f}`",
            f"- 平均亏: `{metrics['avg_loss']:.4f}`",
            f"- 单笔期望值: `{metrics['expectancy']:.6f}`",
            f"- 利润因子: `{pf_text}`",
            "",
            "## 健康检查",
        ]
        if metrics["expectancy"] > min_expectancy:
            lines.append(f"- 期望值健康，大于 `{min_expectancy}`。")
        else:
            lines.append(f"- 期望值偏低，不足 `{min_expectancy}`，优先提高胜率或盈亏比。")
        if profit_factor > 2.0:
            lines.append("- 利润因子优秀。")
        elif profit_factor > 1.5:
            lines.append("- 利润因子可接受。")
        else:
            lines.append("- 利润因子不足，需要降低亏损交易密度。")
        return "\n".join(lines) + "\n"


def trades_to_frame(trades: list[Any]) -> pd.DataFrame:
    rows = []
    for trade in trades:
        rows.append(
            {
                "symbol": getattr(trade, "symbol", ""),
                "regime": str(getattr(trade, "regime", "")),
                "pnl": float(getattr(trade, "pnl", 0.0)),
                "risk_multiplier": float(getattr(trade, "risk_multiplier", 0.0)),
                "signal_reason": getattr(trade, "signal_reason", ""),
                "entry_stage": getattr(trade, "entry_stage", ""),
                "entry_close_position_72h": float(getattr(trade, "entry_close_position_72h", 0.0)),
                "entry_ret_24h": float(getattr(trade, "entry_ret_24h", 0.0)),
                "entry_ret_72h": float(getattr(trade, "entry_ret_72h", 0.0)),
                "profitable": 1 if float(getattr(trade, "pnl", 0.0)) > 0 else 0,
            }
        )
    return pd.DataFrame(rows)

