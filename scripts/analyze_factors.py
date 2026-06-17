#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.ml_factor_selector import analyze_backtest_factors


DEFAULT_FACTORS = [
    "opportunity_score",
    "risk_multiplier",
    "entry_close_position_72h",
    "entry_ret_24h",
    "entry_ret_72h",
    "abs_ret_24h",
    "abs_ret_72h",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze factor importance from a backtest report.")
    parser.add_argument("--backtest-file", required=True, help="Backtest JSON/CSV/Markdown report.")
    parser.add_argument("--output", default="reports/factor_analysis.md", help="Output markdown report path.")
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS), help="Comma-separated factor columns.")
    args = parser.parse_args()
    factors = [item.strip() for item in args.factors.split(",") if item.strip()]
    output = analyze_backtest_factors(args.backtest_file, factors, args.output)
    print(f"factor analysis report: {output}")


if __name__ == "__main__":
    main()

