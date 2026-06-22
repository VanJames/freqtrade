# Module Contracts: Automated Strategy Email Alerts

These contracts define module boundaries for implementation and tests. Concrete
Python types may be dataclasses, typed dictionaries, or equivalent structured
models, but raw provider dictionaries must not cross core module boundaries.

## Liquidation Page Scraper

Module: `src/trading_notice/liquidation_scraper.py`

Responsibility:
- Navigate to and capture CoinGlass free-page liquidation heatmap data through
  Playwright.
- Apply versioned selector/coordinate mapping configuration.
- Wait for chart rendering to complete before capture and parsing.
- Save or expose chart-region screenshot evidence.
- Parse DOM/SVG axis ticks and heatmap bar coordinate attributes.
- Calibrate SVG pixel coordinates to price coordinates from visible price-axis
  tick anchors.
- Fail closed on page layout, selector, screenshot region, blank screenshot,
  render wait, DOM/SVG parse, calibration, or coordinate mapping mismatch.
- Validate parsed prices against current market price sanity bounds.
- Normalize output into upper and lower accumulation lists while preserving the
  strongest candidate on each side.
- Return a failure state instead of raising provider-specific exceptions into
  decision logic.

Required callable:

```text
scrape_liquidation_signal(config: AnalysisConfiguration) -> LiquidationSignal
```

Successful return shape:

```json
{
  "symbol": "ETH/USDT",
  "upper_accumulations": [
    {
      "side": "upper_short",
      "period": "1H",
      "price_low": 1746.0,
      "price_high": 1780.0,
      "strength": 0.82,
      "svg_coordinates": {"x": 412.0, "y": 118.0, "width": 9.0, "height": 42.0},
      "source": "coinglass_web",
      "mapping_version": "coinglass-free-heatmap-v1",
      "calibration": {
        "axis_anchors": [
          {"price": 1700.0, "svg_y": 212.0},
          {"price": 1800.0, "svg_y": 88.0}
        ],
        "valid": true
      },
      "validation": {
        "layout_valid": true,
        "render_valid": true,
        "screenshot_valid": true,
        "dom_parse_valid": true,
        "calibration_valid": true,
        "per_side_candidate_valid": true,
        "price_sanity_valid": true
      },
      "evidence": "Upper short liquidation concentration near 1746-1780"
    }
  ],
  "lower_accumulations": [
    {
      "side": "lower_long",
      "period": "4H",
      "price_low": 1650.0,
      "price_high": 1711.0,
      "strength": 0.76,
      "svg_coordinates": {"x": 220.0, "y": 184.0, "width": 10.0, "height": 39.0},
      "source": "coinglass_web",
      "mapping_version": "coinglass-free-heatmap-v1",
      "calibration": {
        "axis_anchors": [
          {"price": 1600.0, "svg_y": 236.0},
          {"price": 1800.0, "svg_y": 64.0}
        ],
        "valid": true
      },
      "validation": {
        "layout_valid": true,
        "render_valid": true,
        "screenshot_valid": true,
        "dom_parse_valid": true,
        "calibration_valid": true,
        "per_side_candidate_valid": true,
        "price_sanity_valid": true
      },
      "evidence": "Lower long liquidation concentration near 1650-1711"
    }
  ],
  "strongest_upper_candidate": {"side": "upper_short", "price_low": 1746.0, "price_high": 1780.0, "strength": 0.82},
  "strongest_lower_candidate": {"side": "lower_long", "price_low": 1650.0, "price_high": 1711.0, "strength": 0.76},
  "near_tie": false,
  "latest_valid_scrape_at": "2026-06-21T10:00:00Z",
  "failure": null
}
```

Failure return requirements:
- `failure.category` is one of `coinglass_scrape`, `scrape_mapping`,
  `scrape_render`, `scrape_screenshot`, `scrape_calibration`,
  `scrape_sanity`, `stale_scrape`, `configuration`, or `parsing`.
- `failure.attempts` reflects bounded retries.
- No secret values appear in `failure.safe_message`.
- Required selectors, screenshot regions, coordinate transforms, and page
  invariants fail closed rather than returning partial liquidation data.
- Empty, zero-byte, invalid-dimension, or near-blank chart screenshots fail
  closed as `scrape_screenshot`.
- Missing chart container/SVG/axis tick/heatmap bar DOM elements fail closed as
  `scrape_mapping` or `scrape_render`.
- Calibration requires at least two parseable price-axis tick anchors with
  distinct SVG positions; calibration failure returns `scrape_calibration`.
- Parsed liquidation prices outside configured market-price sanity bounds fail
  closed before decision logic. The first-delivery default sanity guard rejects
  candidate prices more than 50% away from current market price unless
  configuration overrides the bound with documented source basis.
- Reusing a latest valid scrape older than `max_scrape_staleness_seconds`
  returns `stale_scrape` and does not enter scenario evaluation.

Contract tests:
- Upper/lower lists are always present on success.
- Playwright navigation/capture errors produce a failure state.
- Chart-render timeout before SVG/bar visibility produces a render failure.
- Blank screenshot fixtures produce a screenshot failure.
- Missing or stale selector/coordinate mapping produces a scrape mapping failure
  state.
- Missing or non-monotonic axis tick anchors produce a calibration failure
  state.
- Out-of-range parsed prices produce a scrape sanity failure state.
- Stale latest-valid-scrape fixtures produce a stale scrape failure state.
- Malformed page/screenshot fixtures produce a parsing failure state.
- Per-side strongest upper/lower candidates are preserved separately on success.

## K-line Analyzer

Module: `src/trading_notice/kline.py`

Responsibility:
- Fetch Binance K-line data through CCXT.
- Compute MA7, MA25, MA99 when enough closed candles exist.
- Classify volume state and key-level confirmation.

Required callable:

```text
analyze_kline_signals(config: AnalysisConfiguration) -> list[KlineSignal]
```

Successful return shape:

```json
[
  {
    "symbol": "ETH/USDT",
    "period": "15M",
    "last_closed_price": 1732.4,
    "ma7": 1728.1,
    "ma25": 1724.0,
    "ma99": 1698.5,
    "ma_position": "above_ma25_above_ma99",
    "volume_state": "high_breakout",
    "key_level": 1730.0,
    "key_level_state": "stood_above",
    "evidence": "15M closed above 1730 with elevated volume",
    "failure": null
  }
]
```

Failure return requirements:
- CCXT/network failures produce `failure.category=ccxt_api`.
- Insufficient candle count produces `volume_state=insufficient` or an explicit
  failure/non-trading signal, depending on whether analysis can continue.
- Current incomplete candles are not used as closed-candle confirmation.

Contract tests:
- MA and volume classifications are deterministic for fixture candles.
- Insufficient candles do not produce false scenario confirmations.
- CCXT exceptions are converted to failure states.

## Decision State Machine

Module: `src/trading_notice/decision.py`

Responsibility:
- Combine `LiquidationSignal` and `KlineSignal` inputs.
- Produce exactly one `StrategyDecision` for a cycle.
- Keep logic explainable and deterministic.

Required callable:

```text
decide_strategy(
  config: AnalysisConfiguration,
  liquidation: LiquidationSignal,
  klines: list[KlineSignal],
  cycle_id: str
) -> StrategyDecision
```

State rules:
- If liquidation signal has `near_tie=true`, return `status=no_trade` with a
  no-trade reason identifying directional ambiguity.
- Scenario A: sufficient lower long-liquidation accumulation plus broke-below
  strong support -> `status=short`, `matched_scenario=A_breakdown_short`.
- Scenario B: upper short-liquidation concentration plus low-volume rebound
  stalling at resistance while selling pressure remains ->
  `status=short`, `matched_scenario=B_rebound_trap_short`.
- Scenario C: sufficient upper short-liquidation accumulation plus volume
  confirmation and stood-above key level ->
  `status=long`, `matched_scenario=C_confirmed_long`.
- Missing, insufficient, or conflicting market evidence -> `status=no_trade`.
- Dependency/configuration/scrape validation failures, including stale latest
  valid scrape beyond the configured maximum staleness window ->
  `status=failure`.

Contract tests:
- Representative scenario A/B/C fixtures map to expected decisions.
- Conflicting evidence maps to `no_trade`.
- Near-tied upper/lower liquidation strengths map to `no_trade`.
- Dependency/configuration/scrape failures map to `failure`.

## Email Driver

Module: `src/trading_notice/emailer.py`

Responsibility:
- Compose and send HTML strategy emails for valid long/short decisions.
- Compose and send failure notification emails for dependency/configuration
  failures.
- Use only `smtplib` and `email.mime`.

Required callables:

```text
build_strategy_email(decision: StrategyDecision, config: AnalysisConfiguration) -> StrategyEmailReport
build_failure_notification(decision: StrategyDecision, config: AnalysisConfiguration) -> FailureNotification
send_email(report: StrategyEmailReport | FailureNotification, config: AnalysisConfiguration) -> StrategyEmailReport | FailureNotification
```

Contract tests:
- Strategy email subject includes direction and key target level.
- Strategy email body includes direction, trigger condition, take-profit target,
  predicted liquidation side, and technical basis.
- No-trade decisions do not produce strategy emails.
- Failure notifications omit secrets and include failure category.

## Scheduler / Orchestrator

Module: `src/trading_notice/scheduler.py`

Responsibility:
- Support run-once and recurring modes.
- Prevent overlapping cycles.
- Suppress duplicate strategy emails for the same unchanged decision.
- Throttle same-category failure notifications until recovery or cooldown.
- Apply configured scrape frequency limits and scrape-failure backoff.
- Reuse latest valid liquidation scrape for faster analysis cycles only while it
  remains within `max_scrape_staleness_seconds`.

Required callables:

```text
run_once(config: AnalysisConfiguration) -> StrategyDecision
run_forever(config: AnalysisConfiguration) -> None
```

Contract tests:
- Recurring mode starts cycles at configured intervals.
- A running cycle blocks overlap.
- Same-category failure repeat notification is throttled.
- Recovery clears failure throttle state.
- Repeated CoinGlass scrape failures back off rather than scraping at the normal
  cadence.
