from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_system.config import Settings


@dataclass(frozen=True, slots=True)
class RuntimeField:
    key: str
    label: str
    default: float | str
    min_value: float
    max_value: float
    step: float
    percent: bool = False
    boolean: bool = False
    text: bool = False
    description: str = ""


RUNTIME_FIELDS: tuple[RuntimeField, ...] = (
    RuntimeField(
        "risk_percent",
        "单笔基础风险",
        0.01,
        0.0005,
        0.05,
        0.0005,
        True,
        description="每笔交易按账户权益承担的基础风险。0.01 表示止损触发时约亏账户 1%。",
    ),
    RuntimeField(
        "same_direction_risk_limit",
        "同方向风险上限",
        0.03,
        0.001,
        0.20,
        0.001,
        True,
        description="限制同一方向持仓累计风险。中等动态仓位建议 0.06，旧激进回测为 0.20。",
    ),
    RuntimeField(
        "daily_drawdown_limit",
        "日内熔断回撤",
        0.05,
        0.005,
        0.20,
        0.001,
        True,
        description="账户权益较启动时回撤超过该比例后暂停开仓并尝试平仓。0.05 表示 5%。",
    ),
    RuntimeField(
        "shock_leverage_limit",
        "震荡最大杠杆",
        3.0,
        0.1,
        20.0,
        0.1,
        description="SHOCK/震荡类交易的仓位名义价值上限。只限制策略仓位，不代表每单固定使用该杠杆。",
    ),
    RuntimeField(
        "trend_symbol_leverage_limit",
        "趋势最大杠杆",
        5.0,
        0.1,
        20.0,
        0.1,
        description="趋势和震荡单边交易的仓位名义价值上限。修改后程序会尝试同步 OKX 杠杆。",
    ),
    RuntimeField(
        "max_signal_risk_multiplier",
        "信号风险倍数上限",
        1.5,
        0.1,
        20.0,
        0.1,
        description="限制单个信号最终风险倍数。中等动态仓位建议 3；旧激进回测为 20。",
    ),
    RuntimeField(
        "confirmation_position_sizing",
        "启用确认级别动态仓位",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启后按机会评分、多周期一致性、趋势确认度动态放大或降低仓位。",
    ),
    RuntimeField(
        "confirmation_max_risk_multiplier",
        "确认级别最大风险倍数",
        3.0,
        0.1,
        20.0,
        0.1,
        description="确认级别模型内部允许放大的最高倍数。通常保持 3，不建议直接调到很高。",
    ),
    RuntimeField(
        "enable_liquidity_sweep_reversal",
        "启用插针反转策略",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="识别连续上涨/下跌后的放量插针、扫止损后快速收回信号。默认关闭，回测确认有效后再启用。",
    ),
    RuntimeField(
        "liquidity_sweep_risk_multiplier",
        "插针反转风险倍数",
        0.8,
        0.1,
        3.0,
        0.1,
        description="插针反转信号的基础仓位风险倍数。该策略止损较近，建议先保持 0.5-1.0。",
    ),
    RuntimeField(
        "liquidity_sweep_require_confirmation",
        "插针等待下一根确认",
        1.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启后插针不立即入场，等待下一根 5m 不再破针尖并继续收回后入场，降低假反转。",
    ),
    RuntimeField(
        "position_monitor_interval_seconds",
        "持仓监控间隔秒数",
        3.0,
        0.5,
        30.0,
        0.5,
        description="移动止盈/止损独立监控循环的检查间隔。越小越及时，但 OKX 请求更频繁。",
    ),
    RuntimeField(
        "positions_cache_ttl_seconds",
        "持仓缓存秒数",
        2.0,
        0.0,
        30.0,
        0.5,
        description="普通持仓查询的缓存时间。持仓止损/移动止盈监控会强制刷新，不受该缓存影响。",
    ),
    RuntimeField(
        "llm_regime_review_enabled",
        "启用 LLM 行情复核",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启后仅在波动率策略认为需要复核时调用 LLM，不会每根 K 线都调用。",
    ),
    RuntimeField(
        "llm_regime_provider",
        "LLM 服务商",
        "deepseek",
        0.0,
        0.0,
        1.0,
        text=True,
        description="支持 openai 或 deepseek。API Key 仍从服务器 .env 读取，不在网页保存。",
    ),
    RuntimeField(
        "llm_regime_model",
        "LLM 模型",
        "deepseek-v4-pro",
        0.0,
        0.0,
        1.0,
        text=True,
        description="用于行情复核的模型名，例如 deepseek-v4-pro 或 gpt-4.1-mini。",
    ),
    RuntimeField(
        "llm_regime_base_url",
        "LLM Base URL",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="可留空使用默认地址。DeepSeek 默认 https://api.deepseek.com。",
    ),
    RuntimeField(
        "llm_regime_review_cache_ttl_seconds",
        "LLM 复核缓存秒数",
        900.0,
        0.0,
        86400.0,
        60.0,
        description="相同行情特征在缓存时间内复用 LLM 结论，减少调用频率。",
    ),
    RuntimeField(
        "llm_regime_review_min_interval_seconds",
        "LLM 最小调用间隔秒数",
        900.0,
        0.0,
        86400.0,
        60.0,
        description="全局和单品种 LLM 调用最小间隔。设大一些可以避免频繁调用。",
    ),
)


def runtime_defaults(settings: Settings) -> dict[str, float | str]:
    defaults: dict[str, float | str] = {}
    for field in RUNTIME_FIELDS:
        value = getattr(settings, field.key, field.default)
        if field.text:
            defaults[field.key] = str(value or "")
        elif field.boolean:
            defaults[field.key] = 1.0 if bool(value) else 0.0
        else:
            defaults[field.key] = float(value)
    return defaults


def validate_runtime_config(values: dict[str, Any], settings: Settings | None = None) -> dict[str, float | str]:
    base = runtime_defaults(settings) if settings else {field.key: field.default for field in RUNTIME_FIELDS}
    result: dict[str, float | str] = {}
    for field in RUNTIME_FIELDS:
        raw = values.get(field.key, base[field.key])
        if field.text:
            value = str(raw or "").strip()
            if field.key == "llm_regime_provider":
                value = value.lower()
                if value not in {"openai", "deepseek"}:
                    raise ValueError("llm_regime_provider must be openai or deepseek")
            result[field.key] = value
            continue
        if field.boolean:
            if isinstance(raw, (int, float)):
                value = 1.0 if float(raw) > 0 else 0.0
            else:
                value = 1.0 if str(raw).lower() in {"1", "1.0", "true", "on", "yes"} else 0.0
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field.key} must be numeric") from exc
        if value < field.min_value or value > field.max_value:
            raise ValueError(f"{field.key} must be between {field.min_value} and {field.max_value}")
        result[field.key] = value
    return result


def apply_runtime_config(settings: Settings, values: dict[str, Any]) -> None:
    for key, value in validate_runtime_config(values, settings).items():
        if key in {
            "confirmation_position_sizing",
            "enable_liquidity_sweep_reversal",
            "liquidity_sweep_require_confirmation",
            "llm_regime_review_enabled",
        }:
            setattr(settings, key, bool(value))
        elif key in {"llm_regime_review_cache_ttl_seconds", "llm_regime_review_min_interval_seconds"}:
            setattr(settings, key, int(float(value)))
        else:
            setattr(settings, key, value)
