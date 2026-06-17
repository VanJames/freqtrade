#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.expectancy_optimizer import ExpectancyOptimizer
from trading_system.ml_factor_selector import parse_backtest_markdown


def diagnose(backtest_file: str, output: str | None = None) -> str:
    path = Path(backtest_file)
    if path.suffix.lower() == ".md":
        trades = parse_backtest_markdown(path)
    elif path.suffix.lower() == ".csv":
        trades = pd.read_csv(path)
    else:
        raise ValueError("diagnose_strategy currently supports .md and .csv reports")
    metrics = ExpectancyOptimizer.calculate_metrics(trades)
    profit_factor = metrics["profit_factor"]
    profit_factor_text = "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    lines = [
        "# 策略诊断报告",
        "",
        "## 基础指标",
        f"- 交易次数: `{int(metrics['total_trades'])}`",
        f"- 胜率: `{metrics['win_rate']:.2%}` (`{int(metrics['wins'])}W/{int(metrics['losses'])}L`)",
        f"- 总盈利: `{trades['pnl'].sum():.2f}`" if "pnl" in trades.columns else "- 总盈利: `N/A`",
        f"- 单笔期望值: `{metrics['expectancy']:.4f}`",
        f"- 利润因子: `{profit_factor_text}`",
        "",
        "## 诊断建议",
    ]
    if metrics["total_trades"] < 20:
        lines.append("- 交易样本偏少，优先扩大周期或谨慎评估新增信号。")
    if metrics["win_rate"] < 0.45:
        lines.append("- 胜率低于 45%，应加强信号过滤，不建议提高仓位。")
    elif metrics["win_rate"] < 0.55:
        lines.append("- 胜率一般，重点检查亏损归因和交易频率。")
    else:
        lines.append("- 胜率良好，下一步优先优化盈亏比和回撤。")
    if metrics["expectancy"] <= 0:
        lines.append("- 单笔期望值为负，应关闭对应新增策略或收紧触发条件。")
    if "signal_reason" in trades.columns and not trades.empty:
        grouped = trades.groupby("signal_reason")["pnl"].agg(["count", "sum", "mean"]).sort_values("sum")
        lines.extend(["", "## 信号归因", "", "| signal | count | pnl | avg |", "|---|---:|---:|---:|"])
        for signal, row in grouped.iterrows():
            lines.append(f"| {signal} | {int(row['count'])} | {row['sum']:.2f} | {row['mean']:.2f} |")
    report = "\n".join(lines) + "\n"
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose an OKX backtest markdown report.")
    parser.add_argument("backtest_file")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    print(diagnose(args.backtest_file, args.output or None))


if __name__ == "__main__":
    main()
