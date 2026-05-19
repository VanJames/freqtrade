# OKX 永续合约量化交易系统

这是按 `.codex/dev.md` 搭建的生产集成版工程骨架，默认 `DRY_RUN=true`，不会向 OKX 真实下单。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
okx-quant run --dry-run
```

## 主要模块

- `trading_system.regime`: 1H/4H 行情分类，支持爆仓与 OI 前瞻过滤。
- `trading_system.strategy`: 趋势、震荡网格、状态切换对冲信号。
- `trading_system.risk`: 单笔 1% 风险、同向 3% 总风险、资金费率、日亏损与插针熔断。
- `trading_system.execution`: OKX post-only + 15 秒后微型 TWAP/IOC 追单。
- `trading_system.store`: PostgreSQL 订单生命周期与状态快照表。
- `trading_system.cache`: Redis 高频持仓、未成交订单、锁仓状态缓存。
- `trading_system.portfolio`: 相对 BTC 强弱 Alpha Filter，同向信号只保留前 2 个。
- `trading_system.position_manager`: 趋势持仓 ATR 追踪止盈。
- `trading_system.foresight`: 15m 爆仓与 OI 5 日/4 小时前瞻因子聚合。
- `trading_system.llm_regime`: 可选 LLM 行情复核，基于 `knowledge/regime_rules.md`，默认关闭。
- `trading_system.volatility`: 基于 ATR/动量/区间波动自适应选择过滤强度，避免按单个 symbol 写死策略。
- `trading_system.exchange`: 交易所适配器，含 dry-run 适配器。

## 常用命令

```bash
cp .env.example .env
docker compose --profile tools run --rm init-db
docker compose up -d app
```

Compose 通过 Docker secret 挂载 `.env`，不要把 `docker compose config` 的输出贴到公开位置；如果你曾经把真实 key 输出到日志或聊天记录，应立刻在 OKX/DeepSeek 后台轮换。

`--with-store` 会启用 `order_tracks` 与 `account_snapshots`，启动时从最新快照恢复状态机。
`--with-redis` 会缓存当前持仓、未成交订单与锁仓状态，Redis 不可用时会降级继续运行。

回测：

```bash
BACKTEST_DAYS=90 docker compose --profile tools run --rm backtest
```

本地直接运行：

```bash
okx-quant init-db
okx-quant run --dry-run --once --with-store --with-redis
```

## LLM 行情复核

默认不开启：

```env
LLM_REGIME_REVIEW_ENABLED=false
LLM_REGIME_PROVIDER=openai
LLM_REGIME_MODEL=gpt-4.1-mini
OPENAI_API_KEY=
```

启用后，LLM 只做规则复核，不直接替代规则引擎。知识库在 `knowledge/regime_rules.md`，输出必须是固定结构化结果；低置信度不能覆盖规则结果，`TREND_SHORT` 需要更高置信度，否则禁止交易或降风险。

LLM 不会每根 K 线都调用。系统先用 `ATR_1H / price`、24h 动量、24h/72h 区间波动给出 `NORMAL/HIGH/EXTREME` 分层；只有 HIGH/EXTREME 且规则判断为 `TREND_LONG/TREND_SHORT/SHOCK_TREND_UP/SHOCK_TREND_DOWN` 时才复核。

DeepSeek 兼容模式：

```env
LLM_REGIME_REVIEW_ENABLED=true
LLM_REGIME_PROVIDER=deepseek
LLM_REGIME_MODEL=deepseek-chat
DEEPSEEK_API_KEY=你的_deepseek_key
```

如需自定义兼容地址或 key 环境变量：

```env
LLM_REGIME_BASE_URL=https://api.deepseek.com
LLM_REGIME_API_KEY_ENV=DEEPSEEK_API_KEY
```

DeepSeek 路径使用 JSON 输出并经本地 Pydantic schema 校验；解析失败会回退为保持规则结果。

## 上线注意

1. 先使用 OKX Demo Trading 与 `DRY_RUN=false` 小额验证。
2. `DRY_RUN=false` 前必须确认 `.env` API 权限、双向持仓模式、风控阈值和交易品种。
3. PostgreSQL 与 Redis 连接失败时系统仍可 dry-run，但生产运行应保证两者可用。
