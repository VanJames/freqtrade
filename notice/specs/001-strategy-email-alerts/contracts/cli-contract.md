# CLI Contract: Automated Strategy Email Alerts

The feature exposes a minimal command-line surface for validation and operation.
Exact packaging may be decided during implementation, but commands must preserve
these inputs and outcomes.

## Command: run-once

Purpose: Execute one analysis cycle and exit.

Required inputs:
- `--symbol <pair>`: trading pair, e.g. `ETH/USDT`.
- `--interval <interval>`: analysis interval, e.g. `15m` or `1h`.
- `--recipients <emails>`: comma-separated recipient list or config reference.

Optional inputs:
- `--liquidation-periods <periods>`: defaults to `15M,1H,4H,1D`.
- `--kline-periods <periods>`: defaults to `15M,1H`.
- `--scrape-mapping-version <version>`: active CoinGlass selector/coordinate
  mapping version.
- `--coinglass-heatmap-url <url>`: CoinGlass free liquidation heatmap page URL
  or config reference for the selected symbol/page variant.
- `--scrape-min-interval-seconds <seconds>`: minimum interval between CoinGlass
  page captures.
- `--scrape-failure-backoff-seconds <seconds>`: backoff after scrape failures.
- `--max-scrape-staleness-seconds <seconds>`: maximum age for reusing the latest
  valid scrape; defaults to 2x scrape minimum interval.
- `--price-sanity-max-relative-distance <ratio>`: configured failure guard for
  parsed liquidation prices relative to current market price. First-delivery
  default is `0.50`, sourced from planning input as a provisional engineering
  guardrail.
- `--screenshot-dir <path>`: optional local directory for chart-region
  screenshots captured during scraping.
- `--dry-run-email`: compose email/report without sending SMTP.

Environment variables:
- `SMTP_HOST`: SMTP server host.
- `SMTP_PORT`: SMTP server port.
- `SMTP_USERNAME`: SMTP username, if required.
- `SMTP_PASSWORD`: SMTP password, if required.
- `SMTP_FROM`: sender address.

Outcomes:
- Valid long/short decision: sends one strategy email unless dry-run is enabled.
- No-trade decision: exits successfully after recording no-trade status.
- Dependency/configuration/scrape failure: sends a failure notification email
  unless email transport itself is unavailable.

## Command: run

Purpose: Execute recurring scheduled analysis until stopped.

Inputs:
- Same as `run-once`.
- `--failure-cooldown-seconds <seconds>`: cooldown for same-category repeated
  failure notifications.
- `--scrape-failure-backoff-seconds <seconds>`: backoff for repeated scraping
  failures or access restriction signals.
- `--max-scrape-staleness-seconds <seconds>`: stale-data cutoff for latest valid
  scrape reuse in recurring mode.

Outcomes:
- Starts one cycle per configured interval.
- Does not overlap cycles.
- Suppresses duplicate strategy emails for unchanged decisions in the same cycle
  context.
- Sends the first dependency/configuration failure notification immediately and
  throttles same-category repeats until recovery or cooldown.
- Applies scrape frequency limits and failure backoff before new CoinGlass page
  captures.
- Reuses latest valid liquidation scrape only within the configured freshness
  window; stale evidence produces a dependency failure outcome.

## Exit Status Contract

- `0`: command completed or recurring process stopped cleanly.
- `1`: configuration validation failed before cycle execution.
- `2`: cycle ended with dependency/provider failure after notification handling.
- `3`: unexpected internal error, with safe message only.

## Output Contract

Console/log output must be safe to share:
- No API keys, SMTP passwords, tokens, or secret-derived values.
- Includes cycle id, symbol, mode, decision status, matched scenario when
  applicable, and safe failure category when applicable.
