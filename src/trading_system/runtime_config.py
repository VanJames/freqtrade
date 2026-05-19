from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_system.config import Settings


@dataclass(frozen=True, slots=True)
class RuntimeField:
    key: str
    label: str
    default: float
    min_value: float
    max_value: float
    step: float
    percent: bool = False
    boolean: bool = False


RUNTIME_FIELDS: tuple[RuntimeField, ...] = (
    RuntimeField("risk_percent", "单笔基础风险", 0.01, 0.0005, 0.05, 0.0005, True),
    RuntimeField("same_direction_risk_limit", "同方向风险上限", 0.03, 0.001, 0.20, 0.001, True),
    RuntimeField("daily_drawdown_limit", "日内熔断回撤", 0.05, 0.005, 0.20, 0.001, True),
    RuntimeField("shock_leverage_limit", "震荡最大杠杆", 3.0, 0.1, 20.0, 0.1),
    RuntimeField("trend_symbol_leverage_limit", "趋势最大杠杆", 5.0, 0.1, 20.0, 0.1),
    RuntimeField("max_signal_risk_multiplier", "信号风险倍数上限", 1.5, 0.1, 20.0, 0.1),
    RuntimeField("confirmation_position_sizing", "启用确认级别动态仓位", 0.0, 0.0, 1.0, 1.0, boolean=True),
    RuntimeField("confirmation_max_risk_multiplier", "确认级别最大风险倍数", 3.0, 0.1, 20.0, 0.1),
)


def runtime_defaults(settings: Settings) -> dict[str, float]:
    return {field.key: float(getattr(settings, field.key, field.default)) for field in RUNTIME_FIELDS}


def validate_runtime_config(values: dict[str, Any], settings: Settings | None = None) -> dict[str, float]:
    base = runtime_defaults(settings) if settings else {field.key: field.default for field in RUNTIME_FIELDS}
    result: dict[str, float] = {}
    for field in RUNTIME_FIELDS:
        raw = values.get(field.key, base[field.key])
        if field.boolean:
            value = 1.0 if str(raw).lower() in {"1", "true", "on", "yes"} else 0.0
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field.key} must be numeric") from exc
        if value < field.min_value or value > field.max_value:
            raise ValueError(f"{field.key} must be between {field.min_value} and {field.max_value}")
        result[field.key] = value
    return result


def apply_runtime_config(settings: Settings, values: dict[str, float]) -> None:
    for key, value in validate_runtime_config(values, settings).items():
        if key == "confirmation_position_sizing":
            setattr(settings, key, bool(value))
        else:
            setattr(settings, key, value)
