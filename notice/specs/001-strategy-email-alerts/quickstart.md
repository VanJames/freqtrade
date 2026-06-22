# Quickstart: Automated Strategy Email Alerts

This guide describes validation scenarios for the planned implementation. It
uses fake placeholder values only; never paste real secrets into docs, tests, or
logs.

## Prerequisites

- Python 3.10+
- Project installed in editable mode after implementation
- Playwright browser runtime installed after implementation, for example with
  the project-provided install command or `python -m playwright install`
- Network access to CoinGlass free liquidation heatmap pages and Binance market
  data for live validation
- SMTP test account or local SMTP test server

Environment variables:

```bash
export SMTP_HOST="smtp.example.test"
export SMTP_PORT="587"
export SMTP_USERNAME="fake-user"
export SMTP_PASSWORD="fake-password"
export SMTP_FROM="alerts@example.test"
```

## Install And Test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src pytest tests/unit tests/contract -q
```

Expected outcome:
- Unit tests pass for liquidation extraction, K-line analysis, state-machine
  decisions, email composition, selector/coordinate mapping validation,
  parsed-price sanity validation, scrape backoff, scheduler non-overlap,
  duplicate suppression, and failure throttling.
- Contract tests pass against [module contracts](./contracts/module-contracts.md)
  and [CLI contract](./contracts/cli-contract.md).

## Scenario 1: Single-Cycle Strategy Email

Run one cycle with deterministic fixtures or a dry-run email transport:

```bash
PYTHONPATH=src python -m trading_notice.cli run-once \
  --symbol ETH/USDT \
  --interval 15m \
  --recipients trader@example.test \
  --coinglass-heatmap-url https://www.coinglass.example/free-heatmap \
  --scrape-mapping-version coinglass-free-heatmap-v1 \
  --max-scrape-staleness-seconds 7200 \
  --price-sanity-max-relative-distance 0.50 \
  --dry-run-email
```

Expected outcome:
- If fixture evidence matches scenario A, B, or C, one strategy email report is
  composed.
- Subject includes direction and key target level.
- Body includes direction, trigger condition with price and stood-above/broke-
  below wording, target take-profit level, predicted liquidation side, and
  technical basis.

## Scenario 2: Recurring Scheduling

Run with a short test interval in a controlled test environment:

```bash
PYTHONPATH=src python -m trading_notice.cli run \
  --symbol ETH/USDT \
  --interval 15m \
  --recipients trader@example.test \
  --coinglass-heatmap-url https://www.coinglass.example/free-heatmap \
  --scrape-mapping-version coinglass-free-heatmap-v1 \
  --scrape-min-interval-seconds 3600 \
  --max-scrape-staleness-seconds 7200 \
  --scrape-failure-backoff-seconds 300 \
  --failure-cooldown-seconds 900 \
  --dry-run-email
```

Expected outcome:
- One cycle starts at each configured interval.
- No overlapping cycle starts if the previous cycle is still running.
- Duplicate strategy emails are not sent for the same unchanged decision.
- Analysis cycles shorter than the scrape cadence reuse the latest valid scrape
  only while it remains younger than the configured staleness cutoff.

## Scenario 3: No-Trade Outcome

Use fixtures where liquidation and K-line signals conflict or are insufficient.

Expected outcome:
- Decision status is `no_trade`.
- No strategy email is sent.
- Safe no-trade reason is recorded in logs/status.

## Scenario 4: Dependency Or Configuration Failure

Run with a stale scrape mapping fixture, a Playwright navigation failure fixture,
or a configuration error fixture.

Expected outcome:
- Decision status is `failure`.
- Failure category is `configuration` or scrape-specific, such as
  `coinglass_scrape`, `scrape_mapping`, or `scrape_sanity`.
- A failure notification email is composed/sent unless the email transport
  itself is the failing dependency.
- Failure notification contains no secret values.

## Scenario 5: Failure Notification Throttling

Run recurring mode with the same dependency failure repeated across multiple
cycles.

Expected outcome:
- First same-category failure sends a failure notification immediately.
- Repeats are suppressed until the failure recovers or cooldown expires.
- Recovery clears throttle state for that category.

## Scenario 6: Scrape Mapping And Price Sanity

Run unit/contract tests with deterministic CoinGlass page or screenshot fixtures.

Expected outcome:
- Active selector/coordinate mapping version is recorded in each successful
  liquidation signal.
- The scraper waits for chart render completion before screenshot/DOM parsing.
- Stale selectors, missing screenshot regions, missing chart/SVG DOM elements,
  blank screenshots, or failed page invariants produce fail-closed scrape
  failures.
- Price-axis tick labels are parsed as calibration anchors, and at least two
  distinct anchors are required to map SVG coordinates to price values.
- Invalid, non-monotonic, or missing calibration anchors produce a
  `scrape_calibration` failure.
- Parsed liquidation prices outside configured market-price sanity bounds,
  including the first-delivery `0.50` relative-distance guard unless overridden,
  are rejected before decision logic.
- Repeated scrape failures apply configured backoff/rate limiting.

## Scenario 7: Latest Valid Scrape Freshness

Run recurring-mode fixtures where CoinGlass scraping fails for multiple cycles
after one prior successful scrape.

Expected outcome:
- Cycles within `max_scrape_staleness_seconds` may reuse the latest valid scrape.
- Once the latest valid scrape exceeds the staleness cutoff, the decision status
  becomes `failure` or equivalent insufficient-signal dependency failure.
- A failure notification email is composed/sent unless throttled or SMTP itself
  failed.

## Scenario 8: Near-Tied Liquidation Candidates

Use fixtures where strongest upper and strongest lower liquidation candidate
strengths differ by less than the configured near-tie threshold.

Expected outcome:
- Both per-side candidates are preserved in the liquidation signal.
- Decision status is `no_trade`.
- No strategy email is sent.
- Safe no-trade reason identifies directional ambiguity.

## Scenario 9: Threshold Provenance

Inspect runtime threshold configuration and generated plan/task artifacts.

Expected outcome:
- Liquidation sufficiency, upper/lower candidate near-tie,
  stood-above/broke-below confirmation, and volume confirmation thresholds are
  configurable per symbol/interval.
- Default threshold values include a source basis.
- Default thresholds remain marked as pending backtest validation until a later
  validation feature proves them.

## Implementation Validation Notes

The current implementation uses deterministic fakes for Playwright, CCXT, and
SMTP in tests. Live validation still requires a real CoinGlass free-page URL,
Playwright browser installation, Binance market-data access through CCXT, and a
safe SMTP test account.

Fixture usage:
- CoinGlass SVG and screenshot fixtures live under `tests/fixtures/`.
- Unit tests inject fake browser pages instead of opening authenticated browser
  sessions.
- Integration tests use dry-run email mode and fake dependency boundaries.

Secret safety:
- Required SMTP values are read from environment variables.
- Tests use fake placeholder values only.
- Failure messages are expected to redact secret-like content.

Validation commands run during implementation:

```bash
PYTHONPATH=src pytest tests/unit tests/contract tests/integration -q
PYTHONPATH=src python -m py_compile src/trading_notice/*.py
```

Expected implementation result:
- Unit, contract, and integration tests pass for strategy email, no-trade,
  stale scrape, and failure notification paths.
- Compilation succeeds for all modules in `src/trading_notice/`.
