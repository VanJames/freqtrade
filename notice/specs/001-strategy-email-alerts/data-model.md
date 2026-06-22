# Data Model: Automated Strategy Email Alerts

## Entity: AnalysisConfiguration

Represents all user-controlled runtime settings for a cycle.

Fields:
- `symbol`: trading pair for analysis, e.g. `ETH/USDT` internally and provider
  normalized forms where required.
- `analysis_interval`: recurring interval identifier; must support at least
  `15m` and `1h`.
- `liquidation_periods`: watched liquidation periods; defaults to `15M`, `1H`,
  `4H`, `1D`.
- `kline_periods`: watched K-line periods; defaults to `15M`, `1H`.
- `recipients`: non-empty list of strategy/failure email recipients.
- `scrape_mapping_version`: required active CoinGlass selector/coordinate
  mapping version.
- `coinglass_heatmap_url`: configured CoinGlass free liquidation heatmap URL for
  the symbol/page variant.
- `scrape_min_interval_seconds`: minimum interval between CoinGlass page
  captures.
- `scrape_failure_backoff_seconds`: backoff after scrape failures or access
  restriction signals.
- `max_scrape_staleness_seconds`: maximum age for reusing the latest valid
  scrape; default is 2x `scrape_min_interval_seconds`.
- `price_sanity_bounds`: acceptable parsed liquidation price distance/range
  relative to current market price; initial default maximum relative distance is
  50%, sourced from the planning input as an engineering guardrail and pending
  operational/backtest validation.
- `thresholds`: per-symbol/interval threshold set for liquidation sufficiency,
  upper/lower candidate near-tie, stood-above/broke-below confirmation, and
  volume confirmation.
- `failure_cooldown_seconds`: configurable cooldown for repeated same-category
  failure notifications.
- `smtp_settings_ref`: names of required SMTP environment variables, not secret
  values.

Validation rules:
- `symbol`, `analysis_interval`, `recipients`, `scrape_mapping_version`,
  `coinglass_heatmap_url`, `price_sanity_bounds`, and environment variable
  names are required.
- Threshold defaults must carry `pending_backtest_validation=true` until later
  validated.
- `max_scrape_staleness_seconds` must be greater than or equal to
  `scrape_min_interval_seconds`.
- No field may contain actual secret values.

## Entity: ScrapeMappingConfiguration

Represents a versioned CoinGlass free-page parser definition.

Fields:
- `version`: stable mapping version identifier.
- `page_variant`: expected CoinGlass page variant or route label.
- `required_selectors`: selectors that must exist before scraping proceeds.
- `screenshot_regions`: named capture regions for heatmap extraction.
- `render_wait`: required chart-render conditions, including non-zero chart
  bounds, visible SVG, visible axis ticks, and visible heatmap bars.
- `screenshot_validation`: minimum dimensions, minimum bytes, and non-blank
  content checks for the captured chart region.
- `axis_tick_selectors`: selectors or DOM paths used to locate visible price
  tick labels and their SVG positions.
- `heatmap_bar_selectors`: selectors or DOM paths used to locate liquidation bar
  elements and their coordinate/size attributes.
- `coordinate_transforms`: mapping rules from SVG coordinates to price bands,
  calibrated from axis tick anchors.
- `expected_invariants`: required page text, dimensions, axis markers, or other
  checks that detect layout changes.
- `created_at`: mapping creation date.

Validation rules:
- `version`, `required_selectors`, `screenshot_regions`,
  `render_wait`, `screenshot_validation`, `axis_tick_selectors`,
  `heatmap_bar_selectors`, `coordinate_transforms`, and `expected_invariants`
  are required.
- A missing selector, missing screenshot region, blank screenshot, failed
  render wait, failed invariant, unsupported page variant, or failed
  calibration must produce a fail-closed scrape failure.

## Entity: CoordinateCalibration

Represents the per-scrape conversion from SVG coordinates to price values.

Fields:
- `mapping_version`: mapping definition used to create the calibration.
- `axis_anchors`: at least two parsed price-axis anchors, each containing the
  displayed price label and SVG coordinate position.
- `slope`: calibrated price-per-pixel ratio for the SVG price axis.
- `intercept`: calibrated price-axis intercept.
- `axis_direction`: whether increasing SVG y maps to higher or lower price.
- `calibrated_at`: scrape timestamp.
- `valid`: whether anchor count, monotonicity, and numeric parsing passed.
- `failure_reason`: safe explanation when calibration fails.

Validation rules:
- At least two distinct numeric price anchors are required.
- Anchor coordinates must be distinct and imply a monotonic price axis.
- Invalid calibration prevents liquidation values from entering decision logic.

## Entity: ScrapeValidationResult

Represents whether scraped heatmap values can be trusted.

Fields:
- `mapping_version`: mapping version used for the scrape.
- `layout_valid`: whether required selectors/regions/invariants matched.
- `render_valid`: whether the chart reached required rendered-state checks.
- `screenshot_valid`: whether the screenshot exists, has valid dimensions/bytes,
  and is not blank or near-blank.
- `dom_parse_valid`: whether SVG axis ticks and heatmap bar coordinate
  attributes were parsed.
- `calibration_valid`: whether SVG pixel-to-price calibration succeeded.
- `per_side_candidate_valid`: whether upper and lower candidate extraction
  succeeded for the required evidence shape.
- `price_sanity_valid`: whether parsed prices are within configured bounds
  around current market price.
- `current_market_price`: reference price used for validation.
- `latest_valid_scrape_at`: timestamp for the latest reusable valid scrape.
- `staleness_seconds`: age of the latest valid scrape at the analysis cycle.
- `failure_reason`: safe explanation when validation fails.

Validation rules:
- Any false value among `layout_valid`, `render_valid`, `screenshot_valid`,
  `dom_parse_valid`, `calibration_valid`, `per_side_candidate_valid`, or
  `price_sanity_valid` prevents liquidation values from entering decision logic.
- If `staleness_seconds` exceeds `max_scrape_staleness_seconds`, the scrape
  evidence is stale and the cycle must be classified as dependency failure /
  insufficient signal.
- `failure_reason` must not include raw page content that could contain private
  session values.

## Entity: ThresholdSet

Represents configurable decision thresholds.

Fields:
- `liquidation_sufficiency`: minimum strength or rank required for a
  liquidation accumulation to count as scenario fuel.
- `upper_lower_near_tie`: maximum allowed strength difference or relative
  difference for treating opposing upper/lower liquidation candidates as
  directional ambiguity.
- `stood_above_confirmation`: rule parameters for confirming price stood above a
  key level.
- `broke_below_confirmation`: rule parameters for confirming price broke below a
  key level.
- `volume_confirmation`: rule parameters for low-volume rebound or
  high-volume breakout/breakdown.
- `source_basis`: text label identifying where a default came from, such as
  historical backtest, documented operator choice, or common market heuristic.
- `pending_backtest_validation`: boolean flag.

Validation rules:
- Default values are invalid unless `source_basis` is present.
- Defaults remain provisional when `pending_backtest_validation=true`.

## Entity: LiquidationAccumulation

Represents one liquidation concentration zone.

Fields:
- `side`: `upper_short` or `lower_long`.
- `period`: source liquidation period, e.g. `15M`, `1H`, `4H`, `1D`.
- `price_low`: lower bound of the zone.
- `price_high`: upper bound of the zone.
- `strength`: comparable numeric or ordinal concentration score.
- `svg_coordinates`: parsed SVG coordinate and dimension attributes used to
  identify the zone.
- `calibration`: `CoordinateCalibration` summary or reference.
- `source`: provider label, expected `coinglass_web`.
- `mapping_version`: CoinGlass scrape mapping version used to produce the zone.
- `validation`: `ScrapeValidationResult` summary or reference.
- `evidence`: human-readable basis for later email explanation.

Validation rules:
- `price_low` must be less than or equal to `price_high`.
- `side` determines whether the zone contributes bullish fuel or bearish fuel.
- Mapping and price sanity validation must pass before this entity is accepted
  as decision evidence.

## Entity: LiquidationSignal

Represents normalized extractor output.

Fields:
- `symbol`: analyzed symbol.
- `upper_accumulations`: list of `LiquidationAccumulation` where upper short
  liquidations can fuel upside movement.
- `lower_accumulations`: list of `LiquidationAccumulation` where lower long
  liquidations can fuel downside movement.
- `strongest_upper_candidate`: strongest valid `upper_short` accumulation for
  decision evaluation.
- `strongest_lower_candidate`: strongest valid `lower_long` accumulation for
  decision evaluation.
- `near_tie`: whether the strongest upper/lower candidate strengths are within
  the configured near-tie threshold.
- `fetched_at`: cycle timestamp.
- `latest_valid_scrape_at`: timestamp of the scrape data used by this cycle.
- `mapping_version`: scrape mapping version used.
- `validation`: scrape validation result.
- `failure`: optional `FailureState`.

Validation rules:
- Successful signals include both lists, even if one list is empty.
- Failure signals must include a failure category and must not be treated as
  valid market evidence.
- A failed scrape validation must produce a failure signal instead of successful
  empty lists.
- `near_tie=true` must drive a no-trade decision rather than forcing a side.
- A latest valid scrape older than `max_scrape_staleness_seconds` must produce a
  failure/non-trading signal rather than successful evidence.

## Entity: KlineSignal

Represents normalized K-line analyzer output.

Fields:
- `symbol`: analyzed symbol.
- `period`: source K-line period, e.g. `15M` or `1H`.
- `last_closed_price`: price used for confirmed decisions.
- `ma7`, `ma25`, `ma99`: moving-average values where enough candles exist.
- `ma_position`: relationship between price and moving averages.
- `volume_state`: `low_rebound`, `high_breakout`, `high_breakdown`, `normal`,
  or `insufficient`.
- `key_level`: support/resistance level used for scenario confirmation.
- `key_level_state`: `stood_above`, `broke_below`, `stalled_at_resistance`,
  `near_level_unconfirmed`, or `unknown`.
- `evidence`: human-readable basis for later email explanation.
- `failure`: optional `FailureState`.

Validation rules:
- MA fields may be absent only when the sample size is insufficient; that case
  must drive non-trading or failure behavior rather than a valid strategy email.
- Incomplete current candles must not be treated as closed-candle confirmation.

## Entity: StrategyDecision

Represents the state-machine result for one cycle.

Fields:
- `cycle_id`: stable identifier for the analysis cycle.
- `symbol`: analyzed symbol.
- `status`: `long`, `short`, `no_trade`, or `failure`.
- `matched_scenario`: `A_breakdown_short`, `B_rebound_trap_short`,
  `C_confirmed_long`, or `none`.
- `trigger_condition`: text containing stood-above/broke-below wording and a
  specific price when `status` is `long` or `short`.
- `target_take_profit`: target level when `status` is `long` or `short`.
- `predicted_liquidation_side`: `longs`, `shorts`, or `none`.
- `basis`: combined liquidation and K-line explanation.
- `no_trade_reason`: populated for `no_trade`.
- `failure`: optional `FailureState`.

State transitions:
- `pending` -> `long`: scenario C evidence passes.
- `pending` -> `short`: scenario A or B evidence passes.
- `pending` -> `no_trade`: evidence is insufficient or conflicting.
- `pending` -> `no_trade`: upper/lower liquidation candidates are valid but
  near-tied by configured threshold.
- `pending` -> `failure`: dependency/configuration failure prevents evaluation.

Validation rules:
- `long` and `short` decisions require trigger condition, target, predicted
  liquidation side, and basis.
- `no_trade` decisions must not produce a strategy email.
- `failure` decisions must produce a failure notification unless throttled as a
  same-category repeat.

## Entity: StrategyEmailReport

Represents an actionable strategy email.

Fields:
- `cycle_id`: source cycle.
- `subject`: includes direction and key target level.
- `recipients`: target recipients.
- `direction`: `long` or `short`.
- `trigger_condition`: exact trigger description.
- `target_take_profit`: target level.
- `predicted_liquidation_side`: `longs` or `shorts`.
- `technical_basis`: human-readable evidence.
- `send_status`: `pending`, `sent`, or `failed`.

Validation rules:
- Must not be created for `no_trade` or `failure` decisions.
- Must contain every required email body field from the spec.

## Entity: FailureNotification

Represents a user-visible failure email.

Fields:
- `cycle_id`: source cycle.
- `failure_category`: `coinglass_scrape`, `scrape_mapping`, `scrape_sanity`,
  `scrape_render`, `scrape_screenshot`, `scrape_calibration`, `stale_scrape`,
  `ccxt_api`, `smtp`, `configuration`, `parsing`, or `scheduler`.
- `message`: user-visible failure summary without secrets.
- `recipients`: target recipients.
- `send_status`: `pending`, `sent`, `throttled`, or `failed`.
- `first_seen_at`: first timestamp for the active failure category.
- `last_seen_at`: latest timestamp for the active failure category.
- `recovered_at`: timestamp when recovery is observed.
- `cooldown_expires_at`: timestamp after which repeat notification is allowed.

Validation rules:
- Messages must never include API keys, SMTP credentials, tokens, or raw secret
  configuration values.
- Same-category repeats are throttled until recovery or cooldown expiry.

## Entity: FailureState

Represents a testable external dependency or configuration failure.

Fields:
- `category`: failure category.
- `retryable`: boolean.
- `attempts`: number of attempts made.
- `source`: boundary that failed.
- `safe_message`: log/email-safe message.

Validation rules:
- `safe_message` must not contain secrets.
- `attempts` must be bounded by configuration.
