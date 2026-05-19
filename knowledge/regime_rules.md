# 行情判断知识库

本文件用于约束 LLM 对规则引擎结果的复核。LLM 不能创造新规则，只能基于这里列出的证据判断规则结果是否需要降级、确认或在高置信度时覆盖。

## 输出原则

1. 默认信任规则引擎。
2. LLM 只能输出固定 JSON schema。
3. 置信度低于 `0.72` 时不得覆盖规则结果。
4. 缺少关键证据时必须 `action = "KEEP"` 或 `action = "DOWNGRADE_TO_SHOCK"`。
5. `TREND_SHORT` 必须比 `TREND_LONG` 更严格，因为回测中空头误判率高。
6. 不允许因为单根 K 线形态直接判断趋势。

## Regime 定义

### SHOCK

最近 42 根 4H K 线没有在最近 2 根内创出 7 天新高或新低，并且价格仍在 7 天区间内部。若趋势证据不足，应降级为 `SHOCK`。

做法：低于区间中线偏多，高于区间中线偏空，止盈至少 0.4%，止损必须有最大距离限制。

### TREND_LONG

必须同时满足：

- 最近 3 根 4H 的高点逐步抬高。
- 最近 3 根 4H 的低点逐步抬高。
- 7 天高点出现在最近 2 根 4H 内，或最近 3 根 4H 已突破 7 天高点。
- 1H EMA20 不低于 EMA60。
- 不是长上影冲高回落。

允许覆盖为 `TREND_LONG` 的最低证据：

- `three_bar_up = true`
- `near_or_break_7d_high = true`
- `ema20_1h >= ema60_1h`
- `confidence >= 0.72`

### TREND_SHORT

必须同时满足：

- 最近 3 根 4H 的高点逐步降低。
- 最近 3 根 4H 的低点逐步降低。
- 7 天低点出现在最近 2 根 4H 内，或最近 3 根 4H 已跌破 7 天低点。
- 1H EMA20 低于 EMA60。
- 最新 4H 收盘低于上一根 4H 收盘。
- 当前价格贴近 7 天低点，不能在区间中上部追空。

允许覆盖为 `TREND_SHORT` 的最低证据：

- `three_bar_down = true`
- `near_or_break_7d_low = true`
- `ema20_1h < ema60_1h`
- `last_4h_close < prev_4h_close`
- `price_position <= 0.35`
- `confidence >= 0.80`

若证据不足，必须降级为 `SHOCK_TREND_DOWN` 或 `SHOCK`。

### SHOCK_TREND_UP

用于“震荡但重心上移”，不是强趋势。必须同时满足：

- 最近 6 根 4H 有至少 3 次高点抬高。
- 最近 6 根 4H 有至少 2 次低点抬高。
- 4H 阴阳方向至少切换 3 次，说明不是单边趋势。
- 1H EMA20 高于 EMA60。
- 价格不应贴近 7 天低点。

如果高低点结构不连续，或 EMA 不支持，应降级为 `SHOCK`。

### SHOCK_TREND_DOWN

用于“震荡但重心下移”，不是强趋势。必须同时满足：

- 最近 6 根 4H 有至少 3 次低点降低。
- 最近 6 根 4H 有至少 2 次高点降低。
- 4H 阴阳方向至少切换 3 次，说明不是单边趋势。
- 1H EMA20 低于 EMA60。
- 价格不应贴近 7 天高点。

如果高低点结构不连续，或 EMA 不支持，应降级为 `SHOCK`。

## 允许动作

- `KEEP`: 保持规则引擎结果。
- `OVERRIDE`: 高置信度覆盖为另一个 regime。
- `DOWNGRADE_TO_SHOCK`: 趋势证据不足，降级为 SHOCK。
- `BLOCK_TRADE`: 允许保留 regime，但禁止本轮开仓。

## 风险偏好

- `TREND_LONG`: 正常风险。
- `TREND_SHORT`: 低风险，除非证据极强。
- `SHOCK_TREND_UP`: 中低风险。
- `SHOCK_TREND_DOWN`: 低风险。
- `SHOCK`: 防守网格风险。

## 波动率自适应过滤

规则引擎会先按数值证据给出 `volatility_tier`，LLM 只能复核这个分层是否与证据一致，不能按币种名称写死判断。

### NORMAL

- `atr_pct = ATR_1H / price` 未达到高波动阈值。
- 24h 动量与 24h/72h 区间波动没有显著放大。
- 趋势入场可以采用标准 5m 回调确认。

### HIGH

任一证据达到高波动：

- `atr_pct >= 0.45%`
- `abs(ret_24h) >= 2.5%`
- `range_24h >= 3.5%`
- `range_72h >= 7.0%`

处理原则：

- 仍可使用标准 5m 回调确认，但必须增加突破回撤过滤。
- 如果是 `TREND_LONG`，价格不能从 7 天高点回撤超过约 `0.8 * ATR_1H` 后再追多。
- 如果是 `TREND_SHORT`，价格不能从 7 天低点反抽超过约 `0.8 * ATR_1H` 后再追空。
- 止损最小距离提高到至少 0.4%。
- 趋势止损应增加 ATR buffer。
- 若证据互相矛盾，优先 `BLOCK_TRADE` 而不是强行覆盖。

### EXTREME

任一证据达到极端波动：

- `atr_pct >= 0.75%`
- `abs(ret_24h) >= 4.5%`
- `range_24h >= 5.5%`
- `range_72h >= 10.0%`

处理原则：

- 趋势追单必须更保守，风险倍率必须降低。
- 止损最小距离提高到至少 0.5%。
- 如果只有 5m 金叉/死叉，没有 15m 与 1h 共振，应 `BLOCK_TRADE`。
- 趋势追单必须要求 5m、15m、1h 同向。

LLM 只在 HIGH 或 EXTREME 且规则结果为趋势/震荡单边时介入；NORMAL 行情不得频繁调用 LLM。
