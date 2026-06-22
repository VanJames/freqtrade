<!--
Sync Impact Report
Version change: 1.0.0 -> 2.0.0
Modified principles:
- I. Mandatory Trading Technology Stack -> I. Mandatory Trading Technology Stack
- II. Explainable Rule-State Decisions -> II. Explainable Rule-State Decisions
- III. Resilient External API Boundaries -> III. Resilient External Dependency Boundaries
- IV. Secret-Free Configuration -> IV. Secret-Free Configuration
- V. Independently Testable Core Modules -> V. Independently Testable Core Modules
Added sections:
- None
Removed sections:
- None
Templates requiring updates:
- ✅ .specify/templates/plan-template.md
- ✅ .specify/templates/spec-template.md
- ✅ .specify/templates/tasks-template.md
- ✅ .specify/templates/commands/*.md (directory absent; no command templates to update)
- ✅ AGENTS.md (already points to current plan)
- ✅ specs/001-strategy-email-alerts/* active feature artifacts
Follow-up TODOs:
- None
-->
# Trading Notice Constitution

## Core Principles

### I. Mandatory Trading Technology Stack
The project MUST use Python 3.10+ for implementation. K-line market data MUST be
retrieved through CCXT. Liquidation heatmap data MUST be retrieved from the
CoinGlass free web page through a Playwright-driven headless browser scraper.
The official CoinGlass API MUST NOT be used for liquidation heatmap data unless
this constitution is amended again. Strategy emails MUST be sent with Python's
built-in `smtplib` and `email.mime` modules. Alternative K-line, liquidation, or
email mechanisms MUST NOT be introduced unless this constitution is amended
first.

Rationale: The system depends on predictable data provenance and a small,
auditable dependency surface while avoiding paid official liquidation APIs.

### II. Explainable Rule-State Decisions
Trading direction, trigger conditions, targets, and liquidation-side conclusions
MUST be produced by an explicit, rule-based state machine. Each state transition
MUST be traceable to observable CoinGlass liquidation heatmap scrape evidence,
CCXT K-line signals, or configured thresholds. Black-box predictive models,
including machine-learning forecasting, MUST NOT be added unless explicitly
requested and approved through a constitutional amendment or feature-specific
governance exception.

Rationale: The project produces trading guidance; users must be able to inspect
why a recommendation was generated before acting on it.

### III. Resilient External Dependency Boundaries
Every external dependency interaction, including CCXT exchange requests,
Playwright page navigation, CoinGlass page scraping/screenshot capture, and SMTP
delivery, MUST include explicit exception handling. Network-facing operations
MUST use bounded retry behavior with clear failure reporting. Scraping MUST use
backoff after failures, rate limiting between captures, and conservative
frequency defaults to reduce target-site load and anti-bot risk. Retry logic
MUST avoid unbounded loops and MUST preserve enough context for tests and logs to
explain the failed dependency.

Rationale: Market-data, browser, web-page, and email dependencies fail in normal
operation; silent or unbounded failures can create stale, misleading, or missing
trading notices.

### IV. Secret-Free Configuration
API keys, SMTP credentials, exchange credentials, session tokens, and other
sensitive values MUST NOT be hardcoded in source, tests, examples, logs, or
generated artifacts. Secrets MUST be read from environment variables or an
approved secret-management boundary. Tests MUST use fake values and MUST NOT
require real production credentials.

Rationale: Trading credentials and notification accounts are high-impact secrets;
source control and generated artifacts must remain safe to share.

### V. Independently Testable Core Modules
The liquidation page scraper, K-line analyzer, and decision/email driver MUST be
independently unit testable. Core logic MUST expose typed or structured
interfaces that can be exercised with deterministic fixtures without live network
access. Changes to these modules MUST include focused unit tests for success,
failure, and edge cases relevant to the changed behavior.

Rationale: Independent tests keep scrape parsing, signal interpretation, and
email composition verifiable without relying on live markets, live web pages, or
external services.

## Implementation Constraints

- Runtime code MUST keep the three core responsibilities separable: CoinGlass
  liquidation page scraping, CCXT K-line analysis, and decision/email
  orchestration.
- CoinGlass page scraping MUST use versioned selector and coordinate mapping
  configuration. The active mapping version MUST be recorded with every scrape
  result.
- Scraping MUST fail closed when required selectors, page elements, screenshot
  regions, coordinate mappings, or parseable values are missing or inconsistent.
  It MUST NOT return apparently valid liquidation data after a detectable page
  layout or mapping mismatch.
- Scraped liquidation prices MUST pass data sanity validation before entering
  decision logic. At minimum, parsed price values MUST be checked against the
  current market price and configured acceptable distance/range bounds.
- Feature specifications MUST identify which external dependencies are touched
  and how retry, timeout, backoff, rate limit, anti-bot risk, and failure states
  are represented.
- Feature specifications MUST identify all required environment variables for
  credentials or configurable endpoints without including real values.
- Decision outputs MUST include the selected direction, trigger condition, target
  level, liquidation side, and human-readable evidence.
- Any proposed dependency that overlaps with CCXT, Playwright-based CoinGlass
  page scraping, or `smtplib`/`email.mime` MUST be rejected unless an amendment
  approves it.

## Development Workflow and Quality Gates

- Plans MUST pass the Constitution Check before research/design proceeds and
  MUST be rechecked after design.
- Tasks for the liquidation page scraper, K-line analyzer, and decision/email
  driver MUST include independent unit tests.
- Tasks that add or change external dependency interactions MUST include retry,
  exception handling, backoff where relevant, and tests for transient failure
  behavior.
- Tasks that add or change CoinGlass scraping MUST include selector/coordinate
  mapping version tests, page-layout mismatch tests, and data sanity validation
  tests.
- Tasks that add or change secret-dependent configuration MUST read values from
  environment variables and include tests or validation for missing values.
- Reviews MUST verify that trading decisions remain explainable state-machine
  logic and that generated emails include enough evidence for user inspection.

## Governance

This constitution supersedes conflicting implementation guidance for this
project. Amendments MUST be documented in `.specify/memory/constitution.md`,
including the version change, affected principles, template synchronization
status, and any deferred follow-up. Changes that weaken or replace a core
principle require a MAJOR version bump. New principles, new mandatory gates, or
materially expanded guidance require a MINOR version bump. Clarifications,
wording fixes, or non-semantic template alignment require a PATCH version bump.

Every feature plan, specification, and generated task list MUST be checked
against the current constitution. Any intentional violation MUST be documented in
the plan's Complexity Tracking section with the reason, rejected simpler
alternative, and approval basis.

**Version**: 2.0.0 | **Ratified**: 2026-06-20 | **Last Amended**: 2026-06-21
