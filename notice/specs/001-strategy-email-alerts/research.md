# Research: Automated Strategy Email Alerts

## Decision: Use Playwright to scrape CoinGlass free-page liquidation heatmaps

Use Playwright-driven headless browser navigation and screenshot/DOM extraction
for CoinGlass free-page liquidation heatmap data. The official CoinGlass API is
not used for liquidation heatmaps under constitution v2.0.0.

Rationale: The amended constitution requires free-page scraping because official
CoinGlass API access is unsuitable for this project. Playwright gives controlled
browser execution, deterministic screenshot capture, reliable handling of modern
SPA rendering, and testable page fixtures.

Alternatives considered:
- Official CoinGlass API: rejected by constitution v2.0.0.
- Raw HTTP page fetch without browser rendering: rejected because heatmap pages
  may require browser rendering and coordinate/screenshot capture.
- Selenium: acceptable as a general browser automation alternative, but not
  selected because Playwright has stronger built-in waiting primitives,
  deterministic browser-context control, and lower friction for modern SPA
  rendering tests.
- Third-party scraping services: rejected because they add a replacement
  liquidation mechanism and reduce local testability.

## Decision: Use a render-complete scrape flow before parsing

The liquidation scraper flow is:

1. Scheduled task requests a scrape only when cadence/backoff/staleness rules
   allow it.
2. Playwright launches a headless browser context and opens the configured
   CoinGlass URL.
3. The scraper waits for required chart selectors, non-zero chart bounds, SVG
   axis labels, and heatmap bars to be present and stable.
4. The chart region is captured to a configured screenshot path or in-memory
   bytes for audit/test evidence.
5. The scraper reads the DOM/SVG coordinate attributes for axis ticks and
   heatmap bars.
6. The coordinate calibration maps SVG pixels to price values.
7. The strongest upper short-liquidation candidate and strongest lower
   long-liquidation candidate are extracted and validated.

Rationale: Waiting for chart-render completion before screenshot and DOM parse
reduces blank or partially-rendered captures and makes failure modes explicit.

Alternatives considered:
- Parse immediately after navigation: rejected because SPA chart rendering can
  lag behind page load.
- Screenshot-only image recognition: rejected for first delivery because SVG
  DOM coordinates are more deterministic and easier to fixture-test.
- DOM-only without screenshot: rejected because screenshots provide evidence
  for manual debugging and blank-capture detection.

## Decision: Version selector and coordinate mapping configuration

Represent each CoinGlass page parser as a versioned mapping containing expected
page invariants, selectors, screenshot region definitions, coordinate-to-price
transforms, and supported page variant labels. Every scrape result records the
mapping version used.

Rationale: CoinGlass page layout can change without notice. Versioned mappings
make parser assumptions explicit, testable, and reviewable.

Alternatives considered:
- Inline selectors in scraper code: rejected because changes would be hard to
  audit and test separately.
- Best-effort parse with missing selectors ignored: rejected because it can
  silently return wrong liquidation levels.

## Decision: Calibrate SVG pixel-to-price mapping from visible axis tick anchors

Coordinate mapping is calibrated per scrape from at least two visible price-axis
tick labels and their SVG positions. The mapping computes a monotonic
pixel-to-price transform, for example `price = slope * svg_y + intercept` for a
linear vertical price axis. The scraper verifies that tick labels parse as
prices, anchor positions are distinct, the inferred axis direction is
consistent, and transformed candidate prices align with the current market price
sanity bounds.

Rationale: Using axis tick anchors avoids hardcoding a single pixel scale and
lets the scraper detect layout or axis-range changes that would otherwise shift
price extraction silently.

Alternatives considered:
- Fixed pixel-to-price ratios per mapping version: rejected because chart zoom,
  symbol, viewport, or page layout changes can shift the axis range.
- Current price plus bar offset approximation: rejected because it does not
  reliably convert chart coordinates to explicit liquidation prices.
- OCR of screenshot tick labels: deferred; DOM/SVG tick labels are preferred
  when available because they are more stable in tests.

## Decision: Fail closed on layout or mapping mismatch

The scraper returns a structured failure state when required selectors,
screenshot regions, rendered SVG elements, coordinate mappings, axis-anchor
calibration, expected page invariants, or parsed values are missing or
inconsistent. It must not emit apparently valid liquidation zones after a
detectable mismatch.

Rationale: A stale selector or shifted coordinate map can create incorrect
trading evidence. Failing closed protects decision logic from corrupted scrape
data.

Alternatives considered:
- Continue with partial data: rejected because partial heatmap data can invert or
  distort direction.
- Emit warnings while returning parsed values: rejected because downstream logic
  may still treat the values as valid.

## Decision: Treat empty screenshot, missing DOM, and extreme price drift as scrape failures

The scraper classifies these cases as dependency failures that flow into GR-005
failure handling:

- Screenshot capture fails, has zero bytes, has invalid dimensions, or the chart
  region is blank/near-blank.
- Required chart container, SVG, axis tick, or heatmap bar selectors are missing.
- SVG coordinate attributes or price tick labels cannot be parsed.
- Calibrated candidate prices are outside configured market-price sanity bounds.

The initial sanity-bound default for scrape failure detection is a maximum
relative distance of 50% from current market price. Source basis: user-provided
engineering guardrail in the planning input. Status: provisional and pending
operational/backtest validation; symbol-specific bounds may replace it later.

Rationale: These checks prevent blank captures and shifted coordinate parses
from entering scenario A/B/C as false market evidence.

Alternatives considered:
- Log and continue with the latest parsed number: rejected because stale or
  mis-mapped liquidation data can produce misleading trade notices.
- Hard fail the process: rejected because recurring mode should keep running and
  send user-visible failure notifications when allowed by throttle rules.

## Decision: Validate scraped liquidation prices against market-price sanity bounds

Scraped liquidation zone prices must be compared against current market price and
configured acceptable distance/range bounds before entering decision logic.
Values outside bounds produce a scraping/parsing failure or no-trade state,
depending on whether any trusted evidence remains.

Rationale: Coordinate parsing errors can produce numeric but wrong prices. Sanity
checks catch common scrape drift and dirty data before decisions.

Alternatives considered:
- Trust all parsed numeric values: rejected as unsafe.
- Hardcode fixed global bounds: rejected because symbols and volatility differ;
  bounds must be configurable.

## Decision: Reuse latest valid scrape only within the configured freshness window

CoinGlass page captures run at most on the configured scrape cadence, defaulting
to hourly for first delivery. Analysis cycles shorter than the scrape cadence can
reuse the latest valid scrape. If repeated scrape failures make the latest valid
scrape older than the configured maximum staleness window, defaulting to 2x the
scrape period, the cycle is classified as insufficient signal due to dependency
failure and must not force a directional decision.

Rationale: This balances lower scrape frequency with freshness protection. It
also makes repeated scrape failures user-visible after valid evidence becomes
too stale.

Alternatives considered:
- Scrape on every analysis cycle: rejected because it increases target-site load
  and anti-bot risk.
- Reuse latest valid scrape indefinitely: rejected because stale liquidation
  evidence can mislead decisions.

## Decision: Apply conservative scraping frequency, retries, and backoff

Recurring mode uses configured minimum intervals between CoinGlass page captures,
bounded retries, and backoff after failures or signs of access restriction. Same
failure categories feed the failure-notification throttle policy.

Rationale: This reduces target-site load, lowers anti-bot risk, and makes
failure behavior visible and testable.

Alternatives considered:
- Scrape on every internal tick: rejected as unnecessary load.
- Retry aggressively until success: rejected because it increases blocking and
  anti-bot risk.

## Decision: Keep liquidation scraper output compatible with the requested dict shape

The liquidation page scraper returns a structured object that can serialize to:

```json
{
  "upper_accumulations": [],
  "lower_accumulations": []
}
```

Each accumulation contains side, price range, strength, source period, mapping
version, calibration anchors, sanity-validation status, and evidence metadata.
The successful signal preserves the strongest upper short-liquidation candidate
and strongest lower long-liquidation candidate separately before decision
evaluation.

Rationale: The original module boundary remains useful and testable while the
data acquisition method changes from API to browser scraping.

Alternatives considered:
- Raw DOM or screenshot metadata crossing module boundaries: rejected because it
  would couple downstream decision logic to page layout details.
- A single flat list of zones: rejected because scenario logic needs explicit
  upper/short-fuel versus lower/long-fuel grouping.

## Decision: Near-tied upper/lower liquidation strengths produce no valid direction

The decision state machine receives upper and lower candidates separately. If
both sides are valid and their relative strengths fall within the configured
near-tie threshold, the cycle is classified as no valid direction instead of
forcing a long or short bias. The near-tie threshold is configurable per symbol
and interval and follows the same documented-default and pending-validation
rules as other thresholds.

Rationale: A near tie between opposing liquidation fuels is directional
ambiguity. The explicit no-trade state is safer and easier to test than
arbitrary tie-breaking.

Alternatives considered:
- Choose the absolute strongest side: rejected because it can overstate weak
  differences between opposing fuels.
- Choose the candidate closest to current price: rejected as a secondary
  heuristic that can still force trades during ambiguous conditions.

## Decision: Use CCXT `fetch_ohlcv` against Binance for K-line data

Use CCXT's Binance exchange adapter for public OHLCV/K-line data. The K-line
analyzer converts fetched candles into deterministic signal summaries for MA
position, volume state, current/closed price, key level relationship, and
stood-above/broke-below confirmation.

Rationale: This satisfies the constitution and user input requiring CCXT for
K-line data while keeping the analyzer independent from the decision module.

Alternatives considered:
- Direct Binance REST API: rejected because K-line data must go through CCXT.
- Streaming/WebSocket K-lines: deferred; recurring polling is sufficient for the
  first delivery and simpler to test.

References:
- https://docs.ccxt.com/
- https://github.com/ccxt/ccxt/wiki/manual

## Decision: Use Python standard-library email delivery

Use `smtplib` for SMTP transport and `email.mime` for HTML email composition.
The email boundary sends two output types: valid strategy emails and failure
notification emails.

Rationale: This is mandated by the constitution and user input, and keeps email
delivery independent from strategy decision logic.

Alternatives considered:
- Provider SDKs or transactional email libraries: rejected by constitution.
- Logging-only failures: rejected because dependency/configuration failures must
  be user-visible.

## Decision: Three independently testable core modules plus small support modules

Core modules:

1. Liquidation page scraper: Playwright navigation/capture, versioned mapping,
   sanity validation, backoff/failure handling, and normalization to upper/lower
   accumulation zones.
2. K-line analyzer: CCXT OHLCV fetch, retry/failure handling, MA/volume/key-level
   signal summary.
3. Decision/email driver: state-machine decision, valid strategy email
   composition, failure notification composition, duplicate/throttle policy.

Support modules hold config, scrape mappings, shared data models, CLI,
scheduler, and email transport.

Rationale: This matches the user's architecture requirement while keeping
external dependency boundaries isolated for tests.

Alternatives considered:
- One orchestrator file: rejected because it would make independent core-module
  testing harder.
- Plugin framework: rejected as unnecessary complexity for first delivery.

## Decision: Recurring scheduler uses configurable polling with non-overlap

The first delivery supports both `run-once` and recurring `run`. Recurring mode
starts a cycle at each configured interval and must not overlap a previous cycle.
If a cycle is still running when the next tick arrives, the next tick records a
skipped-cycle status rather than launching concurrent analysis.

Rationale: The spec requires recurring and single-cycle execution. Non-overlap
prevents duplicate emails and conflicting decisions.

Alternatives considered:
- External cron only: rejected because the feature itself must include a
  scheduling mechanism.
- Async task scheduler dependency: deferred unless tasks identify an existing
  project scheduler; standard-library timing is adequate for first delivery.

## Decision: Provisional configurable thresholds require documented source basis

Liquidation sufficiency, upper/lower candidate near-tie, stood-above/broke-below
confirmation, and volume confirmation thresholds are configurable per symbol and
interval. Defaults must be marked as pending backtest validation, and plan/task
outputs must identify their source basis before implementation.

Rationale: The clarification requires no invented final parameter values.
Defaults can guide implementation but must not be represented as validated
strategy parameters.

Alternatives considered:
- Hardcoded fixed thresholds: rejected as brittle and insufficiently justified.
- Fully dynamic thresholds with no defaults: rejected because it would make
  initial acceptance tests harder to stabilize.

## Decision: Failure notification throttling is keyed by failure category

Dependency/configuration failures send the first failure notification immediately.
Recurring mode suppresses repeat notifications for the same failure category
until recovery or configurable cooldown expiry. No-trade outcomes are recorded
only in logs/status.

Rationale: This satisfies user-visible failure states without alert spam in
recurring mode.

Alternatives considered:
- Email every failed cycle: rejected as noisy.
- Failure digests only: rejected because first failure must be immediately
  visible.

## Decision: In-process runtime state for first delivery

Duplicate decision suppression and failure throttling are kept in memory for the
first delivery.

Rationale: The spec targets one configured symbol per process and does not
require restart-persistent alert suppression. Avoiding storage reduces scope and
keeps the first implementation focused.

Alternatives considered:
- SQLite or file-backed state: deferred until restart-persistence is required.
- No state: rejected because duplicate and throttled notification behavior must
  be testable.
