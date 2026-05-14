#!/usr/bin/env python3
"""
Generate safe AI/heuristic strategy candidates for Freqtrade backtesting.

The LLM is only allowed to propose a restricted JSON DSL.  Python strategy code
is rendered from this local template so arbitrary model-generated code is never
executed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_market_snapshot, openai_market_advisor


USER_DATA = ROOT / "user_data"
STRATEGY_DIR = USER_DATA / "strategies"
AUTOOPT_DIR = USER_DATA / "autoopt"
DEFAULT_OUTPUT = AUTOOPT_DIR / "ai_strategy_candidates.json"
DEFAULT_LEDGER = AUTOOPT_DIR / "ai_strategy_lab.jsonl"
DEFAULT_KRONOS_FORECAST = AUTOOPT_DIR / "kronos_forecast.json"
DEFAULT_FORMAL_POOL = AUTOOPT_DIR / "formal_strategy_pool.json"
ARCHETYPES = {"trend", "pullback", "breakout", "mean_reversion"}
SIDES = {"long", "short", "both"}
RISK_PROFILES = {"conservative", "balanced", "aggressive"}


@dataclass
class StrategyCandidate:
    name: str
    class_name: str
    archetype: str
    side: str
    risk_profile: str
    reason: str
    roi_0: float
    roi_30: float
    stoploss: float
    trailing_positive: float
    trailing_offset: float
    adx_min: float
    volume_min: float
    rsi_long_max: float
    rsi_short_min: float
    bb_tolerance: float
    generated_by: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate safe AI strategy candidates.")
    parser.add_argument("--config", default=str(USER_DATA / "config.json"))
    parser.add_argument("--strategy-path", default=str(STRATEGY_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--kronos-forecast", default=str(DEFAULT_KRONOS_FORECAST))
    parser.add_argument("--formal-pool", default=str(DEFAULT_FORMAL_POOL))
    parser.add_argument("--kronos-return-threshold-pct", type=float, default=0.12)
    parser.add_argument("--snapshot-limit", type=int, default=220)
    parser.add_argument("--max-candidates", type=int, default=4)
    parser.add_argument("--exploration-max-candidates", type=int, default=8)
    parser.add_argument("--exploration-stale-runs-threshold", type=int, default=3)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--advisor-model", default="deepseek-chat")
    parser.add_argument("--advisor-base-url", default="")
    parser.add_argument("--advisor-timeout", type=int, default=60)
    parser.add_argument("--max-preserved-strategies", type=int, default=8)
    parser.add_argument("--preserved-prune-stale-runs-threshold", type=int, default=6)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=360.0)
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def preserved_strategy_names(output_path: Path, strategy_path: Path, limit: int) -> list[str]:
    payload = read_json(output_path, {})
    names = payload.get("strategy_names", []) if isinstance(payload, dict) else []
    # Keep previously generated files as research survivors even if the latest
    # candidate JSON was replaced by a new LLM batch.
    names = [
        *names,
        *[path.stem for path in strategy_path.glob("AIGenerated*.py")],
    ]
    preserved = []
    for name in names:
        text = str(name or "").strip()
        if not re.match(r"^AIGenerated[A-Za-z0-9_]+$", text):
            continue
        if (strategy_path / f"{text}.py").is_file():
            preserved.append(text)
    return sorted(dict.fromkeys(preserved))[: max(limit, 0)]


def build_preserved_strategy_retention(
    previous_payload: dict[str, Any] | None,
    strategy_path: Path,
    *,
    current_generated_names: list[str],
    formal_pool_summary: dict[str, Any],
    limit: int,
    prune_stale_runs_threshold: int,
) -> dict[str, Any]:
    previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
    previous_retention = previous_payload.get("preserved_retention", {})
    if not isinstance(previous_retention, dict):
        previous_retention = {}
    generated_set = set(current_generated_names)
    formal_ai_allowed = set(formal_pool_summary.get("ai_allowed_strategies", []))
    previous_names = previous_payload.get("strategy_names", []) if isinstance(previous_payload, dict) else []
    local_names = [path.stem for path in strategy_path.glob("AIGenerated*.py")]
    candidates = []
    for name in [*previous_names, *local_names]:
        text = str(name or "").strip()
        if not re.match(r"^AIGenerated[A-Za-z0-9_]+$", text):
            continue
        if text in generated_set:
            continue
        if not (strategy_path / f"{text}.py").is_file():
            continue
        candidates.append(text)

    threshold = max(int(prune_stale_runs_threshold), 1)
    retained: list[str] = []
    dropped: list[dict[str, Any]] = []
    state: dict[str, Any] = {}

    for name in candidates:
        previous = previous_retention.get(name, {}) if isinstance(previous_retention, dict) else {}
        stale_runs = int(previous.get("stale_runs", 0) or 0)
        in_formal_pool = name in formal_ai_allowed
        if in_formal_pool:
            stale_runs = 0
        else:
            stale_runs += 1
        info = {
            "stale_runs": stale_runs,
            "in_formal_pool": in_formal_pool,
        }
        if in_formal_pool or stale_runs < threshold:
            retained.append(name)
            state[name] = info
        else:
            dropped.append(
                {
                    "strategy": name,
                    "stale_runs": stale_runs,
                    "reason": "preserved_candidate_stale",
                }
            )

    retained = sorted(dict.fromkeys(retained))[: max(limit, 0)]
    state = {name: state[name] for name in retained if name in state}
    return {
        "retained_names": retained,
        "retention_state": state,
        "dropped": dropped,
        "prune_stale_runs_threshold": threshold,
    }


def summarize_formal_pool(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "exists": False,
            "allowed_strategies": [],
            "ai_allowed_strategies": [],
            "candidate_count": 0,
            "required_window_days": [],
            "created_at": None,
        }
    allowed = [str(item or "").strip() for item in payload.get("allowed_strategies", [])]
    allowed = [item for item in allowed if item]
    ai_allowed = [item for item in allowed if item.startswith("AIGenerated")]
    return {
        "exists": True,
        "allowed_strategies": allowed,
        "ai_allowed_strategies": ai_allowed,
        "candidate_count": int(payload.get("candidate_count", 0) or 0),
        "required_window_days": [int(item) for item in payload.get("required_window_days", []) if int(item) > 0],
        "created_at": payload.get("created_at"),
    }


def build_exploration_state(
    previous_payload: dict[str, Any] | None,
    formal_pool_summary: dict[str, Any],
    *,
    base_max_candidates: int,
    exploration_max_candidates: int,
    stale_runs_threshold: int,
) -> dict[str, Any]:
    previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
    previous_state = previous_payload.get("exploration_state", {})
    previous_formal = previous_payload.get("formal_pool_summary", {})
    previous_ai_allowed = set(previous_formal.get("ai_allowed_strategies", [])) if isinstance(previous_formal, dict) else set()
    current_ai_allowed = set(formal_pool_summary.get("ai_allowed_strategies", []))
    new_ai_entries = sorted(current_ai_allowed - previous_ai_allowed)
    stale_runs = int(previous_state.get("stale_runs", 0) or 0)

    if not formal_pool_summary.get("exists"):
        stale_runs = 0
        reason = "formal_pool_missing"
    elif new_ai_entries:
        stale_runs = 0
        reason = "new_formal_pool_entry"
    else:
        stale_runs += 1
        reason = "no_new_formal_pool_entry"

    threshold = max(int(stale_runs_threshold), 1)
    exploration_active = bool(formal_pool_summary.get("exists")) and stale_runs >= threshold
    effective_max_candidates = (
        max(int(exploration_max_candidates), max(int(base_max_candidates), 1))
        if exploration_active
        else max(int(base_max_candidates), 1)
    )

    return {
        "stale_runs": stale_runs,
        "stale_runs_threshold": threshold,
        "exploration_active": exploration_active,
        "effective_max_candidates": effective_max_candidates,
        "base_max_candidates": max(int(base_max_candidates), 1),
        "exploration_max_candidates": max(int(exploration_max_candidates), max(int(base_max_candidates), 1)),
        "new_formal_pool_entries": new_ai_entries,
        "reason": reason,
    }


def latest_market_snapshot(config_path: Path, limit: int) -> dict[str, Any]:
    config = build_market_snapshot.load_config(str(config_path))
    exchange = config.get("exchange", {}).get("name", "")
    datadir = build_market_snapshot.default_datadir(str(config_path), exchange)
    return build_market_snapshot.build_snapshot(config, limit=limit, datadir=datadir)


def clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(low, min(parsed, high)), 4)


def normalize_token(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or default).strip().lower()
    return text if text in allowed else default


def class_name_from_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", name).title().replace(" ", "")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"Candidate{cleaned}"
    return f"AIGenerated{cleaned[:48]}"


def normalize_candidate(raw: dict[str, Any], index: int, source: str) -> StrategyCandidate:
    name = str(raw.get("name") or f"candidate_{index}").strip()[:80]
    if not name:
        name = f"candidate_{index}"
    risk = normalize_token(raw.get("risk_profile"), RISK_PROFILES, "balanced")
    risk_defaults = {
        "conservative": {"stoploss": -0.035, "roi_0": 0.018, "roi_30": 0.008},
        "balanced": {"stoploss": -0.05, "roi_0": 0.026, "roi_30": 0.012},
        "aggressive": {"stoploss": -0.07, "roi_0": 0.038, "roi_30": 0.018},
    }[risk]
    return StrategyCandidate(
        name=name,
        class_name=class_name_from_name(name),
        archetype=normalize_token(raw.get("archetype"), ARCHETYPES, "trend"),
        side=normalize_token(raw.get("side"), SIDES, "both"),
        risk_profile=risk,
        reason=str(raw.get("reason") or "")[:500],
        roi_0=clamp_float(raw.get("roi_0"), risk_defaults["roi_0"], 0.006, 0.08),
        roi_30=clamp_float(raw.get("roi_30"), risk_defaults["roi_30"], 0.0, 0.04),
        stoploss=clamp_float(raw.get("stoploss"), risk_defaults["stoploss"], -0.12, -0.015),
        trailing_positive=clamp_float(raw.get("trailing_positive"), 0.008, 0.002, 0.04),
        trailing_offset=clamp_float(raw.get("trailing_offset"), 0.016, 0.004, 0.08),
        adx_min=clamp_float(raw.get("adx_min"), 22, 8, 45),
        volume_min=clamp_float(raw.get("volume_min"), 1.0, 0.2, 3.5),
        rsi_long_max=clamp_float(raw.get("rsi_long_max"), 48, 20, 65),
        rsi_short_min=clamp_float(raw.get("rsi_short_min"), 52, 35, 80),
        bb_tolerance=clamp_float(raw.get("bb_tolerance"), 0.002, 0.0, 0.02),
        generated_by=source,
    )


def forecast_side(forecast: dict[str, Any], return_threshold_pct: float) -> str:
    summary = forecast.get("summary", {}) if isinstance(forecast, dict) else {}
    bias = str(summary.get("market_bias") or "hold").lower()
    if bias in {"long", "short"}:
        return bias
    try:
        avg_return = float(summary.get("avg_pred_return_pct") or 0.0)
    except (TypeError, ValueError):
        avg_return = 0.0
    if avg_return >= return_threshold_pct:
        return "long"
    if avg_return <= -return_threshold_pct:
        return "short"
    return "hold"


def heuristic_candidates(snapshot: dict[str, Any], forecast: dict[str, Any], return_threshold_pct: float) -> list[dict[str, Any]]:
    summary = snapshot.get("summary", {}) if isinstance(snapshot, dict) else {}
    pair_count = max(int(summary.get("pair_count") or 0), 1)
    trend_up = float(summary.get("trend_up_count") or 0) / pair_count
    trend_down = float(summary.get("trend_down_count") or 0) / pair_count
    trend_strength = max(trend_up, trend_down)
    median_atr = float(summary.get("median_atr_pct") or 0.0)
    high_vol = median_atr > 0.0025
    trend_side = "short" if trend_down > trend_up else "long" if trend_up > trend_down else "both"
    predicted_side = forecast_side(forecast, return_threshold_pct)
    side = predicted_side if predicted_side in {"long", "short"} else trend_side
    opposite_side = "short" if side == "long" else "long" if side == "short" else "both"
    candidates = [
        {
            "name": f"{side}_trend_continuation",
            "archetype": "trend",
            "side": side,
            "risk_profile": "balanced" if high_vol else "conservative",
            "reason": "Heuristic trend continuation candidate from market breadth.",
            "adx_min": 20,
            "volume_min": 0.8,
        },
        {
            "name": f"{side}_pullback_reentry",
            "archetype": "pullback",
            "side": side,
            "risk_profile": "conservative",
            "reason": "Heuristic pullback candidate for active trend re-entry.",
            "adx_min": 18,
            "volume_min": 0.5,
        },
        {
            "name": f"{opposite_side}_failed_move_fade",
            "archetype": "mean_reversion",
            "side": opposite_side,
            "risk_profile": "conservative",
            "reason": "Forecast/trend disagreement candidate for fading overstretched moves.",
            "adx_min": 20,
            "volume_min": 0.4,
            "rsi_long_max": 42,
            "rsi_short_min": 58,
        },
        {
            "name": "range_fade_balanced",
            "archetype": "mean_reversion",
            "side": "both",
            "risk_profile": "conservative",
            "reason": "Heuristic range candidate for choppy regimes.",
            "adx_min": 24,
            "volume_min": 0.35,
        },
        {
            "name": f"{side}_volatility_breakout",
            "archetype": "breakout",
            "side": side,
            "risk_profile": "aggressive" if high_vol else "balanced",
            "reason": "Heuristic breakout candidate for volatility expansion.",
            "adx_min": 22,
            "volume_min": 1.15,
        },
    ]

    if side in {"long", "short"}:
        candidates.extend(
            [
                {
                    "name": f"{side}_trend_runner_active",
                    "archetype": "trend",
                    "side": side,
                    "risk_profile": "aggressive" if high_vol else "balanced",
                    "reason": "Heuristic active runner for stronger trend continuation windows.",
                    "adx_min": 24,
                    "volume_min": 0.95,
                    "rsi_long_max": 46,
                    "rsi_short_min": 54,
                },
                {
                    "name": f"{side}_breakout_confirmation",
                    "archetype": "breakout",
                    "side": side,
                    "risk_profile": "balanced",
                    "reason": "Heuristic breakout candidate with confirmation bias and tighter participation filter.",
                    "adx_min": 24,
                    "volume_min": 1.25 if high_vol else 1.05,
                    "rsi_long_max": 58,
                    "rsi_short_min": 42,
                },
            ]
        )
    if trend_strength < 0.65:
        candidates.append(
            {
                "name": "balanced_range_reversion",
                "archetype": "mean_reversion",
                "side": "both",
                "risk_profile": "balanced",
                "reason": "Heuristic balanced range reversion candidate for weak-trend conditions.",
                "adx_min": 18,
                "volume_min": 0.45,
                "rsi_long_max": 40,
                "rsi_short_min": 60,
            }
        )
    return candidates


def candidate_diversity_key(candidate: StrategyCandidate) -> tuple[str, str, str]:
    return (candidate.archetype, candidate.side, candidate.risk_profile)


def select_diverse_candidates(
    candidates: list[StrategyCandidate],
    *,
    max_candidates: int,
) -> list[StrategyCandidate]:
    if max_candidates <= 0:
        return []
    selected: list[StrategyCandidate] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for candidate in candidates:
        key = candidate_diversity_key(candidate)
        if key in seen_keys:
            continue
        selected.append(candidate)
        seen_keys.add(key)
        if len(selected) >= max_candidates:
            return selected

    for candidate in candidates:
        if candidate in selected:
            continue
        selected.append(candidate)
        if len(selected) >= max_candidates:
            return selected
    return selected


def llm_candidates(
    snapshot: dict[str, Any],
    *,
    model: str,
    base_url: str,
    timeout: int,
    max_candidates: int,
) -> list[dict[str, Any]]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    prompt = json.dumps(
        {
            "task": "Generate short-term crypto futures strategy candidates as strict JSON only.",
            "schema": {
                "candidates": [
                    {
                        "name": "snake_case_name",
                        "archetype": "trend|pullback|breakout|mean_reversion",
                        "side": "long|short|both",
                        "risk_profile": "conservative|balanced|aggressive",
                        "reason": "why this may fit the market",
                        "roi_0": "0.006..0.08",
                        "roi_30": "0.0..0.04",
                        "stoploss": "-0.12..-0.015",
                        "trailing_positive": "0.002..0.04",
                        "trailing_offset": "0.004..0.08",
                        "adx_min": "8..45",
                        "volume_min": "0.2..3.5",
                        "rsi_long_max": "20..65",
                        "rsi_short_min": "35..80",
                        "bb_tolerance": "0.0..0.02",
                    }
                ]
            },
            "constraints": [
                "Return at most the requested number of candidates.",
                "Do not write Python code.",
                "Prefer diverse archetypes.",
                "Design for 5m futures scalping/swing trades with <=48h holding intent.",
            ],
            "max_candidates": max_candidates,
            "market_snapshot": snapshot,
        },
        ensure_ascii=False,
    )
    response = openai_market_advisor.call_openai(
        base_url=openai_market_advisor.infer_base_url(model, base_url),
        api_key=api_key,
        model=model,
        prompt=prompt,
        timeout=timeout,
    )
    payload = openai_market_advisor.normalize_payload(response)
    items = payload.get("candidates", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def render_strategy(candidate: StrategyCandidate) -> str:
    payload = json.dumps(asdict(candidate), ensure_ascii=True, sort_keys=True, indent=4)
    return f'''try:
    from user_data.strategies.SampleStrategy import SampleStrategy
except ImportError:  # pragma: no cover - Freqtrade strategy loader path
    from SampleStrategy import SampleStrategy

import pandas as pd


class {candidate.class_name}(SampleStrategy):
    """
    AI strategy-lab generated candidate.

    The candidate DSL is rendered by scripts/ai_strategy_lab.py.  The LLM never
    writes executable Python directly.
    """

    can_short = True
    minimal_roi = {{"0": {candidate.roi_0!r}, "30": {candidate.roi_30!r}, "90": 0.0}}
    stoploss = {candidate.stoploss!r}
    trailing_stop = True
    trailing_stop_positive = {candidate.trailing_positive!r}
    trailing_stop_positive_offset = {candidate.trailing_offset!r}
    trailing_only_offset_is_reached = False
    ai_candidate = {payload}

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        side = self.ai_candidate["side"]
        archetype = self.ai_candidate["archetype"]
        adx_min = float(self.ai_candidate["adx_min"])
        volume_min = float(self.ai_candidate["volume_min"])
        rsi_long_max = float(self.ai_candidate["rsi_long_max"])
        rsi_short_min = float(self.ai_candidate["rsi_short_min"])
        bb_tolerance = float(self.ai_candidate["bb_tolerance"])

        volume_ok = dataframe["volume_ratio"] > volume_min
        long_context = dataframe["trend_up_1h"] | dataframe["trend_up_15m"]
        short_context = dataframe["trend_down_1h"] | dataframe["trend_down_15m"]
        trend_long = (
            long_context
            & (dataframe["adx"] > adx_min)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["macdhist"] > 0)
            & (dataframe["rsi"] < max(rsi_long_max, 52))
            & volume_ok
        )
        trend_short = (
            short_context
            & (dataframe["adx"] > adx_min)
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["macdhist"] < 0)
            & (dataframe["rsi"] > min(rsi_short_min, 48))
            & volume_ok
        )

        pullback_long = (
            long_context
            & (dataframe["adx"] > max(adx_min - 4, 8))
            & (dataframe["low"] <= dataframe["ema20"] * (1 + bb_tolerance))
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["rsi"] < rsi_long_max)
            & volume_ok
        )
        pullback_short = (
            short_context
            & (dataframe["adx"] > max(adx_min - 4, 8))
            & (dataframe["high"] >= dataframe["ema20"] * (1 - bb_tolerance))
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["rsi"] > rsi_short_min)
            & volume_ok
        )

        recent_high = dataframe["high"].rolling(36).max().shift(1)
        recent_low = dataframe["low"].rolling(36).min().shift(1)
        volatility_expanding = (
            (dataframe["volatility_ratio"] > 1.05)
            | (dataframe["bb_width"] > dataframe["bb_width_sma"] * 1.08)
        )
        breakout_long = (
            long_context
            & volatility_expanding
            & (dataframe["close"] > recent_high)
            & (dataframe["rsi"] > 52)
            & volume_ok
        )
        breakout_short = (
            short_context
            & volatility_expanding
            & (dataframe["close"] < recent_low)
            & (dataframe["rsi"] < 48)
            & volume_ok
        )

        range_context = dataframe["range_market_1h"] | (
            (dataframe["adx"] < adx_min) & (dataframe["volatility_ratio"] < 1.35)
        )
        meanrev_long = (
            range_context
            & (dataframe["close"] <= dataframe["bb_lowerband"] * (1 + bb_tolerance))
            & (dataframe["rsi"] < rsi_long_max)
            & volume_ok
        )
        meanrev_short = (
            range_context
            & (dataframe["close"] >= dataframe["bb_upperband"] * (1 - bb_tolerance))
            & (dataframe["rsi"] > rsi_short_min)
            & volume_ok
        )

        long_map = {{
            "trend": trend_long,
            "pullback": pullback_long,
            "breakout": breakout_long,
            "mean_reversion": meanrev_long,
        }}
        short_map = {{
            "trend": trend_short,
            "pullback": pullback_short,
            "breakout": breakout_short,
            "mean_reversion": meanrev_short,
        }}
        long_signal = long_map.get(archetype, trend_long) if side in {{"long", "both"}} else pd.Series(False, index=dataframe.index)
        short_signal = short_map.get(archetype, trend_short) if side in {{"short", "both"}} else pd.Series(False, index=dataframe.index)

        if self._new_entries_disabled():
            long_signal = pd.Series(False, index=dataframe.index)
            short_signal = pd.Series(False, index=dataframe.index)

        false_series = pd.Series(False, index=dataframe.index)
        long_signal, _, short_signal, _ = self._apply_direction_advisor_gate(
            long_signal,
            false_series,
            short_signal,
            false_series,
        )

        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, f"ai_{{archetype}}_long")
        dataframe.loc[short_signal, ["enter_short", "enter_tag"]] = (1, f"ai_{{archetype}}_short")
        self._emit_signal_email(dataframe, metadata, "entry", "long")
        self._emit_signal_email(dataframe, metadata, "entry", "short")
        return dataframe
'''


def generate_candidates(args: argparse.Namespace) -> dict[str, Any]:
    config = Path(args.config).resolve()
    strategy_path = Path(args.strategy_path).resolve()
    strategy_path.mkdir(parents=True, exist_ok=True)
    previous_payload = read_json(Path(args.output), {})
    formal_pool_summary = summarize_formal_pool(read_json(Path(args.formal_pool), {}))
    exploration_state = build_exploration_state(
        previous_payload,
        formal_pool_summary,
        base_max_candidates=args.max_candidates,
        exploration_max_candidates=args.exploration_max_candidates,
        stale_runs_threshold=args.exploration_stale_runs_threshold,
    )
    effective_max_candidates = int(exploration_state["effective_max_candidates"])
    snapshot = latest_market_snapshot(config, args.snapshot_limit)
    forecast = read_json(Path(args.kronos_forecast), {})
    raw_heuristic = [
        {**item, "_generated_by": "heuristic"}
        for item in heuristic_candidates(snapshot, forecast, args.kronos_return_threshold_pct)
    ]
    raw_llm: list[dict[str, Any]] = []
    llm_error = ""
    if args.use_llm:
        try:
            raw_llm = [
                {**item, "_generated_by": "llm"}
                for item in llm_candidates(
                    snapshot,
                    model=args.advisor_model,
                    base_url=args.advisor_base_url,
                    timeout=args.advisor_timeout,
                    max_candidates=effective_max_candidates,
                )
            ]
        except Exception as exc:
            llm_error = str(exc)
    # LLM candidates are research additions, not replacements.  Keeping the
    # deterministic heuristic set prevents a newly generated batch from
    # discarding candidates that previously passed validation.
    raw_candidates = [*raw_heuristic, *raw_llm]

    normalized_candidates = []
    seen_classes: set[str] = set()
    for index, raw in enumerate(raw_candidates, start=1):
        candidate = normalize_candidate(raw, index, str(raw.get("_generated_by") or "heuristic"))
        if candidate.class_name in seen_classes:
            continue
        seen_classes.add(candidate.class_name)
        normalized_candidates.append(candidate)

    candidates = select_diverse_candidates(
        normalized_candidates,
        max_candidates=effective_max_candidates,
    )
    for candidate in candidates:
        (strategy_path / f"{candidate.class_name}.py").write_text(render_strategy(candidate), encoding="utf-8")
    generated_names = [item.class_name for item in candidates]
    preserved_retention = build_preserved_strategy_retention(
        previous_payload,
        strategy_path,
        current_generated_names=generated_names,
        formal_pool_summary=formal_pool_summary,
        limit=args.max_preserved_strategies,
        prune_stale_runs_threshold=args.preserved_prune_stale_runs_threshold,
    )
    preserved_names = preserved_retention["retained_names"]

    payload = {
        "record_type": "ai_strategy_candidates",
        "created_at": datetime.now(UTC).isoformat(),
        "strategy_path": str(strategy_path),
        "candidates": [asdict(item) for item in candidates],
        "strategy_names": [*generated_names, *preserved_names],
        "generated_strategy_names": generated_names,
        "preserved_strategy_names": preserved_names,
        "preserved_retention": preserved_retention["retention_state"],
        "dropped_preserved_strategies": preserved_retention["dropped"],
        "selection_mode": "diverse_greedy_exploration" if exploration_state["exploration_active"] else "diverse_greedy",
        "formal_pool_summary": formal_pool_summary,
        "exploration_state": exploration_state,
        "market_summary": snapshot.get("summary", {}),
        "kronos_summary": forecast.get("summary", {}) if isinstance(forecast, dict) else {},
        "llm_enabled": bool(args.use_llm),
        "llm_error": llm_error,
        "usage_note": "Generated candidates require backtesting and gating before execution.",
    }
    write_json(Path(args.output), payload)
    append_jsonl(Path(args.ledger), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return payload


def main() -> int:
    args = parse_args()
    while True:
        generate_candidates(args)
        if not args.loop:
            return 0
        time.sleep(max(args.interval_minutes, 1.0) * 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
