# Feature Specification: Automated Strategy Email Alerts

**Feature Branch**: `001-strategy-email-alerts`

**Created**: 2026-06-20

**Status**: Draft

**Input**: User description: "基于 .codex/xq.md 第 1、3、4 章节，生成功能规格。核心需求：自动化交易策略研判与邮件提醒系统。系统定期（可配置周期，如15分钟/1小时）分析当前市场方向（做多/做空），并通过邮件发送结构化策略报告。判断逻辑基于两类信号的组合：多周期清算密集区分布与 K 线技术形态。三个核心决策场景：破位追空、反弹诱空、右侧稳健多。邮件输出必须包含方向、触发条件、目标止盈位、清算对手盘预测、技术依据，主题需包含方向和关键目标位。成功标准：系统能在无人工干预下完成单次研判周期，并产出格式合规的邮件。"

## Clarifications

### Session 2026-06-20

- Q: How should thresholds for liquidation sufficiency, stood-above/broke-below confirmation, and volume confirmation be determined? → A: Configurable thresholds with documented defaults per symbol/interval; default values must be marked as pending backtest validation and their source basis must be identified during planning.
- Q: Should the first delivery include continuous scheduled execution or only single-cycle execution? → A: First delivery supports both recurring scheduled execution and single-cycle execution.
- Q: What notification behavior is required for no-trade and failure outcomes? → A: No-trade outcomes are recorded in logs/status only; dependency or configuration failures must send active failure notification emails so failure states are user-visible.

### Session 2026-06-21

- Q: What default watched periods should first delivery use for multi-period liquidation and K-line analysis? → A: Default liquidation periods are 15M/1H/4H/1D, and default K-line periods are 15M/1H.
- Q: How should repeated dependency or configuration failure notifications be handled in recurring mode? → A: Send the first failure immediately, then throttle same-category repeats until recovery or a configurable cooldown expires.

### Session 2026-06-21 Constitution Amendment

- Q: How must liquidation heatmap data be acquired after official CoinGlass API access became unsuitable for this project? → A: Liquidation heatmap data must be scraped from the CoinGlass free web page with Playwright using versioned selector/coordinate mappings, fail-closed layout detection, parsed-price sanity validation, and conservative scrape backoff/rate controls.

### Session 2026-06-21 Clarification

- Q: How should analysis cycles use hourly CoinGlass scrapes when strategy analysis may run more frequently? → A: Scrape at most hourly; shorter analysis cycles reuse the latest valid scrape until it exceeds the configured maximum staleness window, defaulting to 2x scrape period, after which the cycle is classified as insufficient signal due to dependency failure.
- Q: How should the system handle upper and lower liquidation candidates when both sides are present or nearly tied? → A: Preserve the strongest upper and strongest lower liquidation candidates separately, let the state machine combine them with K-line confirmation, and classify the cycle as no valid direction when side strengths are within a configurable near-tie threshold.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 自动完成单次研判并发送策略邮件 (Priority: P1)

交易员配置好交易标的、研判周期、收件人和必要凭据后，系统可以按配置周期持续运行，也可以按需执行单次研判，并在每个有效周期内向收件人发送一封结构化策略报告。

**Why this priority**: 这是系统的核心价值；没有自动研判和邮件产出，其他能力都不能形成可用闭环。

**Independent Test**: 使用一组确定的市场信号样本触发单次研判，并使用短周期配置触发连续运行，验证系统无需人工干预即可产出并发送包含全部必填字段的策略邮件。

**Acceptance Scenarios**:

1. **Given** 周期配置、交易标的、收件人和可用市场信号均已准备好，**When** 单次研判周期启动，**Then** 系统生成做多或做空结论，并发送一封主题包含方向和关键目标位的策略邮件。
2. **Given** 市场信号符合任一核心决策场景，**When** 系统完成研判，**Then** 邮件正文包含方向、具体触发价格与站稳/跌破描述、目标止盈位、清算对手盘预测和技术依据。
3. **Given** 已启用定期执行，**When** 到达每个配置周期，**Then** 系统自动启动一次研判周期，无需交易员手动触发。

---

### User Story 2 - 根据三类核心场景输出可解释方向 (Priority: P2)

交易员需要系统把清算密集区和 K 线形态组合为明确、可解释的做多或做空结论，并能看出结论对应的场景。

**Why this priority**: 决策解释直接影响交易员是否能够理解和采用提醒内容。

**Independent Test**: 分别提供场景 A、场景 B、场景 C 的确定样本，验证系统输出对应方向、触发条件和文字依据。

**Acceptance Scenarios**:

1. **Given** 下方多头清算堆积充足且价格跌破强支撑位，**When** 系统研判方向，**Then** 输出场景 A“破位追空”的做空结论。
2. **Given** 上方空头清算密集、短期抛压未尽且价格无量反弹至阻力位滞涨，**When** 系统研判方向，**Then** 输出场景 B“反弹诱空”的做空结论。
3. **Given** 上方空头清算堆积充足且价格放量站稳关键位，**When** 系统研判方向，**Then** 输出场景 C“右侧稳健多”的做多结论。

---

### User Story 3 - 在信号不足或依赖失败时避免误导提醒 (Priority: P3)

交易员需要系统在数据不足、依赖失败或信号冲突时避免发送看似确定但依据不足的交易建议。

**Why this priority**: 交易提醒具有高影响性，错误或过度确定的提醒会造成实际交易风险。

**Independent Test**: 提供缺失数据、超时、格式异常和互相冲突的信号样本，验证系统不发送合规策略邮件，并记录或返回明确失败原因。

**Acceptance Scenarios**:

1. **Given** 任一必需市场信号缺失或无法可信解析，**When** 单次研判周期执行，**Then** 系统不生成做多/做空策略邮件，并提供可审计的失败原因。
2. **Given** 清算信号与 K 线确认信号互相冲突，**When** 系统执行决策，**Then** 系统输出“无有效方向”结果或等价的非交易状态，而不是强行选择做多或做空。
3. **Given** 外部依赖失败或配置错误导致周期无法完成，**When** 系统识别失败原因，**Then** 系统发送失败通知邮件，使用户可见该失败状态。

### Edge Cases

- 研判周期到达时，部分周期的清算密集区数据缺失或为空。
- K 线样本数量不足以确认均线位置、量能变化或关键价位站稳/跌破。
- 当前价格刚好位于关键价位附近，既未有效站稳也未有效跌破。
- 清算信号支持做多，但 K 线形态不确认；或清算信号支持做空，但 K 线形态不确认。
- 外部市场数据或邮件发送依赖超时、返回异常数据或临时不可用。
- 必需配置缺失、格式错误或收件人不可用。
- CoinGlass 免费版网页结构、选择器、截图区域或坐标映射发生变化，导致清算地图无法可信解析。
- 抓取到的清算价格超出当前市场价的配置合理范围，疑似坐标解析错位或脏数据。
- 目标网站出现反爬、访问受限或连续失败，系统必须退避并报告失败，而不是继续高频抓取。
- 连续抓取失败导致最新有效清算数据超过最大陈旧时限时，系统必须将该周期归类为依赖失败/信号不足，而不是继续使用过期清算数据强行研判。
- 上方和下方候选清算强度均有效且强度差落入配置的接近/并列阈值时，系统必须归类为无有效方向，而不是强行选择一侧。
- 无有效方向时仅记录日志或状态，不发送交易策略邮件；依赖或配置失败时必须发送失败通知邮件。
- 定期执行中同类依赖或配置失败持续发生时，系统发送首封失败通知后抑制重复通知，直到恢复或可配置冷却时间结束。
- 同一周期重复触发时，系统避免发送重复或互相矛盾的提醒。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST support both recurring scheduled analysis and single-cycle analysis without manual intervention after required configuration is present.
- **FR-002**: System MUST allow the analysis interval to be configured for recurring execution, including at least 15-minute and 1-hour style intervals.
- **FR-003**: System MUST derive lower long-liquidation accumulation from CoinGlass free-page liquidation-map screenshots captured by a headless browser on a 1-hour cadence, using SVG coordinate attributes to reverse-map pixels into price values, and classify it as bearish fuel.
- **FR-004**: System MUST derive upper short-liquidation accumulation from CoinGlass free-page liquidation-map screenshots captured by a headless browser on a 1-hour cadence, using SVG coordinate attributes to reverse-map pixels into price values, and classify it as bullish fuel.
- **FR-005**: System MUST analyze K-line technical structure, including moving-average relationships, volume changes, and whether price has stood above or broken below a key level.
- **FR-006**: System MUST implement scenario A: when lower long-liquidation accumulation is sufficient and price breaks below strong support, the decision is short.
- **FR-007**: System MUST implement scenario B: when upper short-liquidation concentration is present but selling pressure remains and price stalls on low-volume rebound near resistance, the decision is short.
- **FR-008**: System MUST implement scenario C: when upper short-liquidation accumulation is sufficient and price stands above a key level with volume confirmation, the decision is long.
- **FR-009**: System MUST produce an explicit non-trading result when required market signals are missing, malformed, insufficient, or materially conflicting without treating it as an actionable strategy email.
- **FR-010**: System MUST generate an email subject that includes the selected direction and key target level whenever a trading decision is produced.
- **FR-011**: System MUST generate an email body containing direction, trigger condition with specific price and stood-above/broke-below wording, target take-profit level, predicted liquidation counterparty side, and textual technical basis.
- **FR-012**: System MUST ensure every strategy email has enough human-readable evidence for a trading user to understand why the direction was selected.
- **FR-013**: System MUST record clear no-trade reasons when a cycle cannot produce a valid strategy email due to insufficient or conflicting market signals.
- **FR-014**: System MUST prevent duplicate strategy emails for the same analysis cycle and unchanged decision.
- **FR-015**: System MUST keep analysis output deterministic for the same input signal set and configuration.
- **FR-016**: System MUST treat liquidation sufficiency, upper/lower candidate near-tie, stood-above/broke-below confirmation, and volume confirmation thresholds as configurable per symbol and interval.
- **FR-017**: System MUST label any default threshold values as pending backtest validation until validated, and planning MUST document each default value's source basis before implementation.
- **FR-018**: System MUST send a failure notification email when dependency failures or configuration errors prevent a cycle from completing.
- **FR-019**: System MUST use 15M, 1H, 4H, and 1D as default liquidation analysis periods, and 15M and 1H as default K-line confirmation periods.
- **FR-020**: System MUST send the first notification for a dependency or configuration failure immediately, then throttle same-category repeat notifications until the failure recovers or a configurable cooldown expires.
- **FR-021**: System MUST scrape CoinGlass free-page liquidation heatmap data with a versioned selector/coordinate mapping configuration and retain the mapping version with extracted liquidation evidence.
- **FR-022**: System MUST fail closed when CoinGlass page selectors, screenshot regions, coordinate mappings, or required parsed values are missing or inconsistent.
- **FR-023**: System MUST validate scraped liquidation prices against current market price and configured sanity bounds before using them in decisions.
- **FR-024**: System MUST apply conservative scrape frequency limits and failure backoff when CoinGlass page scraping fails or anti-bot/rate-limit risk is detected.
- **FR-025**: System MUST detect and report scrape failures when the liquidation-map screenshot cannot be captured, SVG coordinate attributes cannot be parsed, pixel-to-price mapping cannot be applied, or per-side highest liquidation candidates cannot be identified.
- **FR-026**: System MUST reject extracted liquidation candidate prices that fall outside configured market-price sanity bounds, and such rejected values MUST NOT enter scenario A/B/C decision logic.
- **FR-027**: System MUST allow analysis cycles shorter than the CoinGlass scrape cadence to reuse the latest valid scrape only while it is within the configured maximum staleness window.
- **FR-028**: System MUST classify a cycle as insufficient signal due to dependency failure when the latest valid liquidation scrape exceeds the maximum staleness window; the default maximum staleness window is 2x the scrape period.
- **FR-029**: System MUST preserve the strongest upper short-liquidation candidate and strongest lower long-liquidation candidate separately before decision evaluation.
- **FR-030**: System MUST classify a cycle as no valid direction when upper and lower liquidation candidate strengths are within a configurable near-tie threshold, and that threshold MUST follow the documented-default and pending-backtest-validation rules in FR-016 and FR-017.

### Governance Requirements *(mandatory for trading notice features)*

- **GR-001**: Features that use liquidation heatmap data MUST source it through Playwright-based scraping of the CoinGlass free web page, not the official CoinGlass API.
- **GR-002**: Features that use K-line data MUST source it through CCXT.
- **GR-003**: Features that send email MUST use Python's built-in `smtplib` and `email.mime`.
- **GR-004**: Trading decisions MUST be explainable rule/state-machine outcomes with observable evidence.
- **GR-005**: External dependency interactions MUST define exception handling, bounded retry behavior, failure backoff where relevant, and user-visible or test-visible failure states.
- **GR-006**: API keys, SMTP credentials, exchange credentials, and tokens MUST be read from environment variables or an approved secret boundary.
- **GR-007**: The liquidation extractor, K-line analyzer, and decision/email driver MUST remain independently unit testable when changed.
- **GR-008**: CoinGlass scraping MUST use versioned selector/coordinate mapping configuration and fail closed on page layout or mapping mismatch.
- **GR-009**: Scraped liquidation prices MUST pass sanity validation against current market price and configured range bounds before entering decisions.

### Key Entities *(include if feature involves data)*

- **Analysis Configuration**: Defines the trading symbol, analysis interval, watched liquidation periods, watched K-line periods, recipient list, scrape frequency/backoff controls, maximum scrape staleness window, selector/coordinate mapping version, sanity bounds, candidate near-tie threshold, and configurable thresholds used to evaluate signals. Default threshold values are provisional until backtest validation confirms them.
- **Liquidation Zone Signal**: Represents the strongest upper and strongest lower liquidation concentration candidates, their side, price range, relative strength, source period, and whether the two sides are within the configured near-tie threshold.
- **Scrape Mapping Configuration**: Represents the versioned selectors, screenshot regions, coordinate transforms, expected page invariants, and supported CoinGlass page variant used to parse the free-page liquidation heatmap.
- **Scrape Validation Result**: Represents screenshot capture status, SVG coordinate parsing status, pixel-to-price mapping status, per-side candidate extraction status, parsed-price sanity status, current market price reference, and fail-closed reason when scraped values cannot be trusted.
- **K-line Signal**: Represents moving-average relationship, volume condition, current/closing price, key levels, and stood-above or broke-below confirmation.
- **Strategy Decision**: Represents the chosen direction, matched scenario, trigger level, target take-profit level, predicted liquidation side, confidence/evidence summary, and non-trading reason when applicable, including no-valid-direction outcomes caused by near-tied liquidation candidate strengths.
- **Strategy Email Report**: Represents valid strategy email output, including subject, recipient set, formatted body fields, and send status.
- **Failure Notification**: Represents user-visible failure email output for dependency or configuration failures, including failure category, affected cycle, recipient set, delivery status, recovery status, and repeat-notification throttle state.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A configured single analysis cycle completes without human intervention and produces either a valid strategy email or a clearly classified non-trading result in at least 95% of valid test runs.
- **SC-007**: In recurring mode, scheduled cycles start at the configured interval without manual action in 100% of scheduling acceptance tests.
- **SC-002**: 100% of generated strategy emails include direction, trigger condition with price and stood-above/broke-below wording, target take-profit level, predicted liquidation counterparty side, and technical basis.
- **SC-003**: For representative samples of scenarios A, B, and C, the system selects the expected direction and matched scenario in 100% of acceptance tests.
- **SC-004**: For missing, malformed, insufficient, or conflicting signal samples, the system avoids sending a trading-decision email in 100% of acceptance tests.
- **SC-005**: At least 90% of reviewed strategy emails are understandable by a trading user without needing to inspect raw market data.
- **SC-006**: Duplicate sends for the same cycle and unchanged decision occur in 0% of duplicate-trigger tests.
- **SC-008**: Dependency and configuration failure samples produce user-visible failure notification emails in 100% of failure-notification acceptance tests.
- **SC-009**: Repeated same-category dependency or configuration failures produce one immediate failure notification and suppress repeats until recovery or cooldown in 100% of throttling acceptance tests.
- **SC-010**: Page layout or mapping mismatch samples are rejected before decision logic in 100% of scrape-integrity acceptance tests.
- **SC-011**: Scraped liquidation price samples outside configured market-price sanity bounds are rejected in 100% of data-validation acceptance tests.
- **SC-012**: Repeated CoinGlass scrape failures trigger configured backoff/rate limiting in 100% of scrape-failure acceptance tests.
- **SC-013**: Analysis cycles reuse a latest valid scrape within the configured staleness window and reject stale liquidation evidence after that window in 100% of freshness acceptance tests.
- **SC-014**: Samples where upper and lower liquidation candidate strengths fall within the configured near-tie threshold produce no-valid-direction outcomes in 100% of ambiguity acceptance tests.

## Assumptions

- The first release supports configured symbols and intervals rather than user-facing symbol discovery.
- The first release includes both recurring scheduled execution and single-cycle execution.
- The default watched periods are 15M, 1H, 4H, and 1D for liquidation analysis and 15M and 1H for K-line confirmation.
- Liquidation heatmap data is sourced from the CoinGlass free web page via Playwright, not the official CoinGlass API.
- Analysis cycles may run more frequently than CoinGlass scraping; the latest valid scrape can be reused only until the configured maximum staleness window expires.
- Liquidation extraction retains separate upper and lower strongest candidates; near-tied side strengths are treated as directional ambiguity rather than a trading signal.
- The system is an advisory alerting tool and does not place trades.
- Supported decision directions are long, short, and non-trading/no-valid-direction.
- Target take-profit level and key trigger level are derived from the same signal set used for the decision.
- A valid trading email is sent only when both liquidation evidence and K-line confirmation are sufficient for one of the defined scenarios.
- No-trade outcomes are visible to maintainers through logs, status output, or an equivalent audit trail.
- Dependency and configuration failures are visible to users through active failure notification emails.
- Failure notification repeat throttling is keyed by failure category and has a configurable cooldown.
- Initial default thresholds are not final trading parameters; they require a documented source basis and later backtest validation.
- Selector/coordinate mapping defaults are not permanent guarantees; each mapping version must be validated against deterministic page or screenshot fixtures.
