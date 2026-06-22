# Implementation Plan: Automated Strategy Email Alerts

**Branch**: `N/A` | **Date**: 2026-06-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-strategy-email-alerts/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

Build an advisory trading-notice system that can run once or on a configured
schedule, combine CoinGlass liquidation heatmap data with CCXT Binance K-line
signals, evaluate an explainable three-scenario state machine, and send either a
valid strategy email or a user-visible failure notification. Liquidation data is
scraped from the CoinGlass free web page with Playwright using versioned
selector/coordinate mappings, SVG axis-tick calibration, screenshot/render
failure detection, sanity validation, latest-valid-scrape freshness checks, and
scrape backoff. The design keeps the liquidation page scraper, K-line analyzer,
and decision/email driver as independently testable modules that exchange
structured data.

## Technical Context

**Language/Version**: Python 3.10+

**Primary Dependencies**: `ccxt` for Binance K-line data; Playwright for
CoinGlass free-page liquidation heatmap scraping and modern SPA rendering;
Python standard-library
`smtplib` and `email.mime` for HTML email; Python standard-library
`dataclasses`, `logging`, `os`, `time`, and `json` for internal structure and
runtime support.

**Storage**: No durable database in first delivery. Runtime state for duplicate
strategy-email suppression, failure recovery, and failure-notification cooldown
is maintained in process. Persisted state can be added later if recurring mode
must survive process restarts without duplicate sends.

**Testing**: `pytest` with deterministic fixtures and monkeypatched/fake
Playwright page/screenshot, CCXT, and SMTP boundaries. Unit tests are mandatory
for the liquidation page scraper, K-line analyzer, and decision/email driver.

**Target Platform**: Local or server-side Python process with Playwright browser
runtime installed, outbound HTTPS to CoinGlass free web pages and Binance, and
SMTP access to the configured mail server.

**Project Type**: Single Python package with CLI entry points and independently
testable library modules.

**Performance Goals**: A single analysis cycle completes within one configured
interval and does not overlap the next cycle. For 15-minute recurring mode,
normal operation should complete within 60 seconds excluding provider outages.

**Constraints**: Secrets are read only from environment variables. External
dependency interactions use bounded retries and explicit failure states.
CoinGlass scraping uses versioned selector/coordinate mappings, fails closed on
layout, render, screenshot, calibration, or parse mismatch, validates parsed
liquidation prices against current market-price sanity bounds, and applies
conservative scrape frequency/backoff to reduce target-site load and anti-bot
risk. The scraper cadence is capped independently from the analysis cadence:
analysis cycles may reuse the latest valid scrape until the configured maximum
staleness window expires, defaulting to 2x scrape period. Trading decisions
remain rule-based state-machine outcomes. Threshold defaults are provisional and
must be documented with source basis before implementation; they remain pending
backtest validation.

**Scale/Scope**: First delivery targets one configured symbol per process,
default liquidation periods `15M`, `1H`, `4H`, `1D`, and default K-line periods
`15M`, `1H`. Multiple symbols, persisted runtime state, dashboards, and order
execution are out of scope.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Technology stack**: PASS. Plan uses Python 3.10+, CCXT for K-line data,
  Playwright-based CoinGlass free-page scraping for liquidation heatmap data,
  and built-in `smtplib` + `email.mime` for emails.
- **Decision logic**: PASS. Direction selection is an explicit state machine for
  scenario A, scenario B, scenario C, and non-trading/failure outcomes.
- **External dependencies**: PASS. CoinGlass page scraping through Playwright,
  CCXT, and SMTP boundaries require bounded retry, exception handling, backoff
  where relevant, and testable failure states.
- **Scrape integrity**: PASS. Design requires versioned selector/coordinate
  mappings, fail-closed layout/render/screenshot/calibration mismatch
  detection, latest-valid-scrape freshness checks, and parsed-price sanity
  validation.
- **Secrets**: PASS. API keys and SMTP credentials are environment-variable
  inputs and are never hardcoded in source, tests, docs, or examples.
- **Core module tests**: PASS. The plan and contracts require independent unit
  tests for the liquidation page scraper, K-line analyzer, and decision/email
  driver.

## Project Structure

### Documentation (this feature)

```text
specs/001-strategy-email-alerts/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── cli-contract.md
│   └── module-contracts.md
└── tasks.md              # Created by /speckit-tasks, not this command
```

### Source Code (repository root)

```text
src/
└── trading_notice/
    ├── __init__.py
    ├── cli.py
    ├── config.py
    ├── models.py
    ├── liquidation_scraper.py
    ├── scrape_mapping.py
    ├── kline.py
    ├── decision.py
    ├── emailer.py
    └── scheduler.py

tests/
├── unit/
│   ├── test_liquidation_scraper.py
│   ├── test_scrape_mapping.py
│   ├── test_kline.py
│   ├── test_decision.py
│   ├── test_emailer.py
│   └── test_scheduler.py
├── contract/
│   ├── test_module_contracts.py
│   └── test_cli_contract.py
└── integration/
    └── test_strategy_email_cycle.py
```

**Structure Decision**: Use a single package under `src/trading_notice` because
the feature is a compact CLI/library workflow, not a web service. The three core
modules are `liquidation_scraper.py`, `kline.py`, and
`decision.py`/`emailer.py` orchestration, with versioned scrape mappings in
`scrape_mapping.py`, shared structured types in `models.py`, and runtime
cadence in `scheduler.py`.

## Complexity Tracking

No constitution violations or additional complexity exceptions.

## Phase 0 Research Output

See [research.md](./research.md). All technical unknowns are resolved for
planning: Playwright scraping boundary, versioned selector/coordinate mapping,
SVG axis-anchor coordinate calibration, screenshot/render failure detection,
scrape sanity validation, latest-valid-scrape staleness handling, scrape
backoff/rate limiting, CCXT OHLCV boundary, email dependency, retry/failure
notification policy, default timeframe set, per-side liquidation candidate
handling, and threshold provenance requirements.

## Phase 1 Design Output

- [data-model.md](./data-model.md) defines the structured data exchanged between
  modules, validation rules, and state transitions.
- [contracts/module-contracts.md](./contracts/module-contracts.md) defines the
  required module interfaces and return shapes.
- [contracts/cli-contract.md](./contracts/cli-contract.md) defines the command
  surface for run-once and recurring modes.
- [quickstart.md](./quickstart.md) defines validation scenarios for single-cycle,
  recurring, no-trade, failure notification, failure throttling, scrape
  calibration/freshness, and near-tied liquidation ambiguity behavior.

## Post-Design Constitution Check

- **Technology stack**: PASS. Design artifacts keep the mandated stack and do
  not introduce replacement K-line, liquidation-data, or email mechanisms.
- **Decision logic**: PASS. Data model separates signal extraction from a
  rule-based state machine and preserves scenario evidence in the output.
- **External dependencies**: PASS. Contracts require bounded retries and
  explicit failure categories for Playwright/CoinGlass page scraping, CCXT, and
  SMTP.
- **Scrape integrity**: PASS. Contracts require mapping version checks,
  fail-closed layout/render/screenshot/calibration handling, latest-valid-scrape
  freshness checks, price sanity validation, and scrape backoff.
- **Secrets**: PASS. Quickstart uses environment variable names only and fake
  placeholder values.
- **Core module tests**: PASS. Contracts and quickstart identify independent
  unit and contract tests for each core module.
