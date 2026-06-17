from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


class MLFactorSelector:
    def __init__(self, model_path: str | Path | None = None) -> None:
        self.model_path = Path(model_path or "models/factor_importance.json")
        self.factor_importance: dict[str, float] = {}
        self.load_model()

    def prepare_backtest_data(self, backtest_file: str | Path, factor_columns: list[str]) -> tuple[pd.DataFrame, list[str]]:
        path = Path(backtest_file)
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            trades = pd.DataFrame(raw.get("trades", raw if isinstance(raw, list) else []))
        elif path.suffix.lower() == ".csv":
            trades = pd.read_csv(path)
        else:
            trades = parse_backtest_markdown(path)
        if trades.empty:
            return trades, []
        if "profitable" not in trades.columns:
            pnl_column = "pnl" if "pnl" in trades.columns else "profit_abs"
            trades["profitable"] = (pd.to_numeric(trades[pnl_column], errors="coerce").fillna(0.0) > 0).astype(int)
        valid = [column for column in factor_columns if column in trades.columns]
        return trades, valid

    def train_factor_importance(self, backtest_results: pd.DataFrame, factor_columns: list[str], output_path: str | Path | None = None) -> dict[str, float]:
        if backtest_results.empty or not factor_columns:
            self.factor_importance = {}
            return {}
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            self.factor_importance = fallback_correlation_analysis(backtest_results, factor_columns)
        else:
            data = backtest_results[factor_columns + ["profitable"]].fillna(0)
            if len(data) < 20 or data["profitable"].nunique() < 2:
                self.factor_importance = fallback_correlation_analysis(data, factor_columns)
            else:
                model = RandomForestClassifier(n_estimators=200, max_depth=12, random_state=42)
                model.fit(data[factor_columns].astype(float), data["profitable"].astype(int))
                self.factor_importance = dict(
                    sorted(zip(factor_columns, model.feature_importances_), key=lambda item: item[1], reverse=True)
                )
        if output_path:
            self._save_importance(Path(output_path))
        return self.factor_importance

    def get_top_factors(self, n: int = 5) -> dict[str, float]:
        return dict(list(sorted(self.factor_importance.items(), key=lambda item: item[1], reverse=True))[:n])

    def get_optimal_weights(self, top_n: int = 5) -> dict[str, float]:
        top = self.get_top_factors(top_n)
        total = sum(top.values())
        return {key: value / total for key, value in top.items()} if total else {}

    def generate_report(self) -> str:
        lines = [
            "# 因子重要度分析报告",
            "",
            "## 因子排名",
            "",
            "| 因子名 | 重要度 | 权重 |",
            "|---|---:|---:|",
        ]
        total = sum(self.factor_importance.values())
        for factor, importance in sorted(self.factor_importance.items(), key=lambda item: item[1], reverse=True):
            weight = importance / total if total else 0.0
            lines.append(f"| {factor} | {importance:.4f} | {weight:.2%} |")
        lines.extend(["", "## 建议", ""])
        for index, factor in enumerate(self.get_top_factors(5), start=1):
            lines.append(f"{index}. {factor}")
        return "\n".join(lines) + "\n"

    def load_model(self) -> None:
        if self.model_path.exists():
            self.factor_importance = json.loads(self.model_path.read_text(encoding="utf-8"))

    def _save_importance(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.factor_importance, indent=2, ensure_ascii=True), encoding="utf-8")


def fallback_correlation_analysis(backtest_results: pd.DataFrame, factor_columns: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    target = pd.to_numeric(backtest_results["profitable"], errors="coerce")
    for column in factor_columns:
        series = pd.to_numeric(backtest_results[column], errors="coerce")
        corr = series.corr(target)
        result[column] = 0.0 if pd.isna(corr) else abs(float(corr))
    return dict(sorted(result.items(), key=lambda item: item[1], reverse=True))


def parse_backtest_markdown(path: Path) -> pd.DataFrame:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 20"):
            continue
        columns = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(columns) < 16:
            continue
        rows.append(
            {
                "entry_time": columns[0],
                "exit_time": columns[1],
                "symbol": columns[2],
                "side": columns[3],
                "regime": columns[4],
                "signal_reason": columns[5],
                "entry_stage": columns[6],
                "opportunity_score": float(columns[7].lstrip("ABCDEF") or 0),
                "risk_multiplier": float(columns[8]),
                "pnl": float(columns[11]),
                "reason": columns[12],
                "attribution": columns[13],
                "entry_close_position_72h": float(columns[14]),
                "entry_ret_24h": float(columns[15].rstrip("%")) / 100,
                "entry_ret_72h": float(columns[16].rstrip("%")) / 100,
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["profitable"] = (frame["pnl"] > 0).astype(int)
        frame["abs_ret_24h"] = frame["entry_ret_24h"].abs()
        frame["abs_ret_72h"] = frame["entry_ret_72h"].abs()
    return frame


def analyze_backtest_factors(backtest_path: str | Path, factor_columns: list[str], output_path: str | Path = "reports/factor_analysis.md") -> Path:
    selector = MLFactorSelector()
    data, valid = selector.prepare_backtest_data(backtest_path, factor_columns)
    selector.train_factor_importance(data, valid, output_path="models/factor_importance.json")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(selector.generate_report(), encoding="utf-8")
    return output

