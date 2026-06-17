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
        0.06,
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
        3.0,
        0.1,
        20.0,
        0.1,
        description="限制单个信号最终风险倍数。中等动态仓位建议 3；旧激进回测为 20。",
    ),
    RuntimeField(
        "min_live_equity_to_order",
        "实盘最低下单权益",
        5.0,
        0.0,
        1000.0,
        1.0,
        description="账户权益低于该值时只发送信号/拒单邮件，不向交易所真实下单。设为 0 可关闭。",
    ),
    RuntimeField(
        "risk_rejection_email_cooldown_seconds",
        "拒单邮件冷却秒数",
        3600.0,
        0.0,
        86400.0,
        60.0,
        description="同一品种、方向、拒单原因在冷却期内只发一封未下单邮件，避免低余额或持续风控拒单时刷屏。",
    ),
    RuntimeField(
        "enable_shock_trend_scout",
        "启用 SHOCK 趋势先遣单",
        1.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启后在 SHOCK_TREND_UP/DOWN 中允许小仓位先遣单，等待确认后允许加仓。和主信号不冲突，仍保留风控门槛与确认门槛。",
    ),
    RuntimeField(
        "shock_trend_scout_risk_multiplier",
        "SHOCK 趋势先遣单风险倍数",
        1.5,
        0.1,
        5.0,
        0.1,
        description="SHOCK 先遣单单笔最大风险倍数（相对单笔基础风险）。建议 1.0-1.5。",
    ),
    RuntimeField(
        "confirmation_position_sizing",
        "启用确认级别动态仓位",
        1.0,
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
        "enable_two_candle_momentum",
        "启用两根K动量补充单",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="按优化指南补充 5m 与 15m 双周期连续放量动量信号。默认关闭，回测验证后再开启实盘。",
    ),
    RuntimeField(
        "enable_daily_macd_breakout",
        "启用日线MACD突破单",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="日线MACD近期金叉/死叉时，按单边行情小仓位跟随；默认关闭，需回测确认后再启用。",
    ),
    RuntimeField(
        "enable_adaptive_strategy_switch",
        "启用无单自适应策略",
        1.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="连续一段时间没有成交但行情仍有波动时，允许系统启用小仓位补充策略，不替换核心策略。",
    ),
    RuntimeField(
        "adaptive_no_trade_hours",
        "无单复盘小时数",
        48.0,
        6.0,
        168.0,
        1.0,
        description="距离最近一次成交超过该小时数后，系统开始复盘当前行情并允许补充策略候选信号。",
    ),
    RuntimeField(
        "adaptive_min_range_24h_pct",
        "自适应最小24h波动",
        0.012,
        0.002,
        0.08,
        0.001,
        True,
        description="只有 24h 振幅达到该阈值时才认为有足够交易机会，避免无波动行情硬下单。",
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
        description="支持 openai 或 deepseek。API Key 可在下方配置，留空则继续读取服务器 .env。",
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
        "llm_regime_api_key",
        "LLM API Key",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="可保存到运行时配置数据库；留空则使用 LLM_API_KEY_ENV 指向的 .env 变量。",
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
    RuntimeField(
        "email_enabled",
        "启用邮件下单通知",
        0.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启后，实盘入场订单提交前会发送邮件；邮件失败不阻塞下单。",
    ),
    RuntimeField(
        "email_user",
        "SMTP 发件邮箱",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="SMTP 登录用户名，通常是发件邮箱。",
    ),
    RuntimeField(
        "email_pass",
        "SMTP 授权码",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="邮箱 SMTP 授权码。会保存到本地运行时配置数据库。",
    ),
    RuntimeField(
        "email_to",
        "邮件收件人",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="接收下单通知的邮箱地址。",
    ),
    RuntimeField(
        "smtp_host",
        "SMTP Host",
        "",
        0.0,
        0.0,
        1.0,
        text=True,
        description="例如 smtp.qq.com。",
    ),
    RuntimeField(
        "smtp_port",
        "SMTP 端口",
        465.0,
        1.0,
        65535.0,
        1.0,
        description="QQ 邮箱 SSL SMTP 通常使用 465。",
    ),
    RuntimeField(
        "smtp_use_ssl",
        "SMTP 使用 SSL",
        1.0,
        0.0,
        1.0,
        1.0,
        boolean=True,
        description="开启使用 SMTP_SSL；关闭时使用 STARTTLS。",
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


def validate_runtime_config(
    values: dict[str, Any], settings: Settings | None = None
) -> dict[str, float | str]:
    base = (
        runtime_defaults(settings)
        if settings
        else {field.key: field.default for field in RUNTIME_FIELDS}
    )
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
                value = (
                    1.0
                    if str(raw).lower() in {"1", "1.0", "true", "on", "yes"}
                    else 0.0
                )
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field.key} must be numeric") from exc
        if value < field.min_value or value > field.max_value:
            raise ValueError(
                f"{field.key} must be between {field.min_value} and {field.max_value}"
            )
        result[field.key] = value
    return result


def apply_runtime_config(settings: Settings, values: dict[str, Any]) -> None:
    for key, value in validate_runtime_config(values, settings).items():
        if key in {
            "confirmation_position_sizing",
            "enable_liquidity_sweep_reversal",
            "liquidity_sweep_require_confirmation",
            "enable_shock_trend_scout",
            "enable_adaptive_strategy_switch",
            "enable_two_candle_momentum",
            "enable_daily_macd_breakout",
            "llm_regime_review_enabled",
            "email_enabled",
            "smtp_use_ssl",
        }:
            setattr(settings, key, bool(value))
        elif key in {
            "llm_regime_review_cache_ttl_seconds",
            "llm_regime_review_min_interval_seconds",
            "smtp_port",
        }:
            setattr(settings, key, int(float(value)))
        else:
            setattr(settings, key, value)
