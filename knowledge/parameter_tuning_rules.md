# 参数调优知识库

本文件约束自动调参进程。调参进程只能提出候选参数并跑 30 天回测，不允许直接修改实盘 runtime settings。

## 目标

1. 优先提高净利润，但不能只靠无约束放大仓位。
2. 推荐参数必须同时查看胜率、交易次数、平均亏损、最大单笔亏损、分品种亏损、分行情亏损。
3. 默认使用 30 天窗口；不使用 90 天作为日常调参窗口。
4. 当前参数报告和推荐参数报告必须同时保留，方便人工对比。

## 参数边界

- `risk_percent`: 默认 0.01。自动调参不得超过 0.015。
- `same_direction_risk_limit`: 常规范围 0.03 到 0.09。超过 0.09 视为激进参考，不作为默认推荐。
- `max_signal_risk_multiplier`: 常规范围 1.5 到 4.0。超过 4.0 视为激进参考。
- `confirmation_max_risk_multiplier`: 常规范围 2.0 到 4.0。
- `shock_leverage_limit`: 常规范围 3.0 到 5.0。
- `trend_symbol_leverage_limit`: 常规范围 5.0 到 8.0。

## 推荐规则

### 可以提高仓位的情况

- 胜率不低于 60%。
- 交易次数不低于 20 笔。
- 分品种没有明显净亏损。
- `SHOCK_TREND_DOWN` 与 `SHOCK_TREND_UP` 没有持续亏损。
- 最大单笔亏损小于初始权益的 4%。

### 必须保守的情况

- 任一品种净亏损。
- 任一行情类型净亏损超过总盈利的 20%。
- 平均亏损大于平均盈利的 1.4 倍。
- 交易次数少于 20 笔。
- 胜率低于 58%。

### 推荐优先级

1. 在收益相近时选择最大单笔亏损更小的参数。
2. 在收益相近时选择 `same_direction_risk_limit` 更低的参数。
3. 不推荐只在单一 symbol 盈利、其他 symbol 亏损的参数。
4. 不推荐通过旧激进参数追求短期最高收益，除非用户明确要求。

## LLM 可做的事

LLM 只能根据本知识库和当前回测结果提出下一组候选参数，不能直接修改运行参数。

LLM 输出必须是 JSON，字段为：

- `reason`: 调整理由。
- `candidates`: 参数候选列表。

每个候选只允许包含：

- `name`
- `max_signal_risk_multiplier`
- `confirmation_max_risk_multiplier`
- `same_direction_risk_limit`
- `shock_leverage_limit`
- `trend_symbol_leverage_limit`
- `risk_percent`

本地程序必须重新校验边界；越界参数必须丢弃。
