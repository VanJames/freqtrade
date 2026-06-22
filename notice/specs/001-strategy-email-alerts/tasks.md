# Tasks: Automated Strategy Email Alerts

**Input**: Design documents from `/specs/001-strategy-email-alerts/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Required by the project constitution for the liquidation page scraper, K-line analyzer, decision/email driver, external dependency retry/error handling, secret-loading configuration, and rule-based decision state machine.

**Organization**: Tasks are grouped by user story so each story can be implemented and validated as an independent increment.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the Python package, test layout, and dependency metadata required by all stories.

- [X] T001 Create package and test directory structure in `src/trading_notice/`, `tests/unit/`, `tests/contract/`, `tests/integration/`, and `tests/fixtures/`
- [X] T002 Create Python project metadata with Python 3.10+, `ccxt`, `playwright`, `pytest`, and `ruff` in `pyproject.toml`
- [X] T003 [P] Create package marker and public exports in `src/trading_notice/__init__.py`
- [X] T004 [P] Create deterministic fixture index for CoinGlass, CCXT, and SMTP test data in `tests/fixtures/README.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Define shared data structures, configuration loading, mapping fixtures, and validation contracts that all user stories depend on.

**Critical**: No user story work should begin until this phase is complete.

- [X] T005 Define dataclasses for `AnalysisConfiguration`, `ThresholdSet`, `LiquidationAccumulation`, `LiquidationSignal`, `KlineSignal`, `StrategyDecision`, `StrategyEmailReport`, `FailureNotification`, and `FailureState` in `src/trading_notice/models.py`
- [X] T006 Implement environment-variable configuration loading, recipient parsing, SMTP references, timeframe defaults, staleness defaults, and documented threshold default metadata in `src/trading_notice/config.py`
- [X] T007 Implement versioned `ScrapeMappingConfiguration`, `CoordinateCalibration`, axis-anchor parsing, monotonicity validation, and screenshot validation helpers in `src/trading_notice/scrape_mapping.py`
- [X] T008 [P] Add configuration tests for missing secrets, no secret leakage, default timeframe sets, 2x scrape staleness default, 0.50 sanity guard source basis, and pending-backtest threshold flags in `tests/unit/test_config.py`
- [X] T009 [P] Add scrape mapping tests for mapping version checks, missing selectors, blank screenshots, axis-anchor calibration, non-monotonic anchors, and coordinate-to-price conversion in `tests/unit/test_scrape_mapping.py`
- [X] T010 [P] Add deterministic CoinGlass page, SVG, screenshot, and stale-mapping fixtures in `tests/fixtures/coinglass_heatmap_valid.html`, `tests/fixtures/coinglass_heatmap_missing_svg.html`, `tests/fixtures/coinglass_heatmap_bad_axis.html`, and `tests/fixtures/blank_heatmap.png`

**Checkpoint**: Shared models, config, mapping validation, and fixtures are ready.

---

## Phase 3: User Story 1 - Automated Single/Recurring Cycle And Strategy Email (Priority: P1)

**Goal**: After configuration is present, run a single or recurring analysis cycle, gather liquidation and K-line evidence, produce a long/short decision for valid evidence, and send a structured strategy email.

**Independent Test**: Use deterministic market fixtures to run one cycle and a short recurring schedule; verify a strategy email report is produced with subject, direction, trigger condition, take-profit target, predicted liquidation side, and technical basis.

### Tests for User Story 1

- [X] T011 [P] [US1] Add liquidation scraper success-shape contract tests for upper/lower accumulations, per-side strongest candidates, mapping version, calibration evidence, and validation status in `tests/contract/test_module_contracts.py`
- [X] T012 [P] [US1] Add K-line analyzer unit tests for CCXT fixture candles, MA7/MA25/MA99 calculation, volume state, key-level state, and closed-candle handling in `tests/unit/test_kline.py`
- [X] T013 [P] [US1] Add strategy email composition tests for subject direction/target and required HTML body fields in `tests/unit/test_emailer.py`
- [X] T014 [P] [US1] Add scheduler and CLI contract tests for `run-once`, recurring non-overlap, duplicate decision suppression, dry-run email, and safe console output in `tests/contract/test_cli_contract.py`
- [X] T015 [P] [US1] Add end-to-end dry-run strategy email cycle test using fake Playwright, fake CCXT, and fake SMTP boundaries in `tests/integration/test_strategy_email_cycle.py`

### Implementation for User Story 1

- [X] T016 [US1] Implement Playwright-based CoinGlass navigation, render waiting, chart screenshot capture, DOM/SVG extraction, calibration use, per-side strongest candidate extraction, and success normalization in `src/trading_notice/liquidation_scraper.py`
- [X] T017 [US1] Implement CCXT Binance OHLCV fetching, bounded request attempts, MA/volume/key-level signal classification, and closed-candle filtering in `src/trading_notice/kline.py`
- [X] T018 [US1] Implement baseline rule-state decision orchestration for valid long/short outcomes, trigger text, target level, predicted liquidation side, and evidence summary in `src/trading_notice/decision.py`
- [X] T019 [US1] Implement HTML strategy email construction and SMTP send path using only `smtplib` and `email.mime` in `src/trading_notice/emailer.py`
- [X] T020 [US1] Implement `run_once`, recurring `run_forever`, non-overlap guard, latest valid scrape reuse within freshness window, duplicate strategy email suppression, and cycle logging in `src/trading_notice/scheduler.py`
- [X] T021 [US1] Implement `run-once` and `run` command parsing, option validation, dry-run email behavior, and exit status mapping in `src/trading_notice/cli.py`

**Checkpoint**: User Story 1 is independently functional with dry-run strategy emails and recurring scheduling.

---

## Phase 4: User Story 2 - Explainable Scenario A/B/C Direction Output (Priority: P2)

**Goal**: Convert liquidation and K-line evidence into explicit scenario A, B, or C decisions with deterministic, human-readable reasoning.

**Independent Test**: Provide fixed scenario A, B, and C fixtures and verify the expected direction, matched scenario, trigger condition, and explanation text.

### Tests for User Story 2

- [X] T022 [P] [US2] Add decision state-machine tests for scenario A breakdown short, scenario B rebound-trap short, and scenario C confirmed long in `tests/unit/test_decision.py`
- [X] T023 [P] [US2] Add threshold behavior tests for liquidation sufficiency, stood-above/broke-below confirmation, volume confirmation, documented default source basis, and pending-backtest validation flags in `tests/unit/test_decision.py`
- [X] T024 [P] [US2] Add email evidence tests that verify scenario labels, trigger price wording, target take-profit, liquidation counterparty side, and technical basis are preserved in `tests/unit/test_emailer.py`
- [X] T025 [P] [US2] Add integration tests for scenario A/B/C fixture cycles and expected strategy email reports in `tests/integration/test_strategy_email_cycle.py`

### Implementation for User Story 2

- [X] T026 [US2] Complete scenario A/B/C state-machine rules, threshold comparison, deterministic tie-free ordering, and matched scenario assignment in `src/trading_notice/decision.py`
- [X] T027 [US2] Implement trigger condition and take-profit derivation from liquidation/K-line evidence for each scenario in `src/trading_notice/decision.py`
- [X] T028 [US2] Implement human-readable technical basis formatting that cites liquidation side, price range, K-line state, volume state, and mapping version in `src/trading_notice/decision.py`
- [X] T029 [US2] Update strategy email rendering to include scenario label, trigger wording, take-profit target, predicted counterparty side, and technical basis without leaking raw provider payloads in `src/trading_notice/emailer.py`

**Checkpoint**: User Story 2 is independently testable with deterministic A/B/C scenario fixtures.

---

## Phase 5: User Story 3 - No-Trade And Dependency Failure Safety (Priority: P3)

**Goal**: Avoid misleading strategy emails when signals are missing, malformed, stale, conflicting, near-tied, or blocked by dependency/configuration failures; make dependency/configuration failures user-visible through throttled failure notifications.

**Independent Test**: Use fixtures for missing data, malformed SVG, blank screenshot, stale latest valid scrape, sanity failures, near-tied liquidation candidates, CCXT errors, SMTP failures, and repeated failure categories; verify no strategy email is sent and failure/no-trade handling is explicit.

### Tests for User Story 3

- [X] T030 [P] [US3] Add liquidation failure tests for Playwright navigation errors, render timeout, missing DOM/SVG selectors, blank screenshots, calibration failure, sanity-bound rejection, stale latest valid scrape, bounded attempts, and scrape backoff in `tests/unit/test_liquidation_scraper.py`
- [X] T031 [P] [US3] Add K-line and SMTP failure tests for CCXT exceptions, insufficient candles, SMTP exceptions, bounded attempts, and safe failure messages in `tests/unit/test_kline.py` and `tests/unit/test_emailer.py`
- [X] T032 [P] [US3] Add decision tests for missing evidence, conflicting liquidation/K-line signals, near-tied upper/lower candidates, stale scrape failure propagation, and no-trade reason text in `tests/unit/test_decision.py`
- [X] T033 [P] [US3] Add scheduler tests for first failure notification, same-category throttling, recovery reset, scrape backoff, skipped overlapping cycles, and stale scrape dependency failure in `tests/unit/test_scheduler.py`
- [X] T034 [P] [US3] Add integration tests for no-trade logging-only behavior and dependency/configuration failure notification behavior in `tests/integration/test_strategy_email_cycle.py`

### Implementation for User Story 3

- [X] T035 [US3] Implement fail-closed failure categories, bounded retries, render/screenshot/DOM/calibration/sanity/staleness detection, and safe failure states in `src/trading_notice/liquidation_scraper.py`
- [X] T036 [US3] Implement CCXT exception conversion, insufficient candle handling, and retry-safe failure states in `src/trading_notice/kline.py`
- [X] T037 [US3] Implement no-trade state handling for missing, malformed, insufficient, conflicting, and near-tied evidence in `src/trading_notice/decision.py`
- [X] T038 [US3] Implement failure notification email construction, SMTP failure handling, secret redaction, and no strategy email creation for no-trade/failure states in `src/trading_notice/emailer.py`
- [X] T039 [US3] Implement same-category failure notification throttling, recovery reset, scrape backoff scheduling, stale latest-valid-scrape classification, and no-trade status logging in `src/trading_notice/scheduler.py`
- [X] T040 [US3] Implement configuration validation failure reporting, safe exit statuses, and CLI output for no-trade and dependency failure outcomes in `src/trading_notice/cli.py`

**Checkpoint**: User Story 3 is independently testable and prevents actionable emails for unsafe or failed cycles.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Validate the full feature, tighten documentation, and prepare for implementation handoff.

- [X] T041 [P] Run unit and contract test suite and record validation command coverage in `specs/001-strategy-email-alerts/quickstart.md`
- [X] T042 [P] Run integration dry-run scenarios for strategy email, no-trade, stale scrape, and failure notification flows and record expected outputs in `specs/001-strategy-email-alerts/quickstart.md`
- [X] T043 [P] Add implementation notes for Playwright browser installation, fake fixture usage, and secret-safe environment variables in `specs/001-strategy-email-alerts/quickstart.md`
- [X] T044 [P] Run linting and Python compilation checks for `src/trading_notice/` and `tests/`
- [X] T045 Review task coverage against FR-001 through FR-030, GR-001 through GR-009, and SC-001 through SC-014 in `specs/001-strategy-email-alerts/tasks.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies; can start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion; blocks all user stories.
- **User Story 1 (Phase 3)**: Depends on Foundational; recommended MVP.
- **User Story 2 (Phase 4)**: Depends on Foundational and can be developed in parallel with US1 after shared models exist, but final email integration benefits from US1 email driver.
- **User Story 3 (Phase 5)**: Depends on Foundational and can be developed in parallel with US1/US2 after shared failure models exist.
- **Polish (Phase 6)**: Depends on the selected user stories being complete.

### User Story Dependencies

- **US1 (P1)**: MVP path after Foundational; no dependency on US2 or US3 for the happy-path dry-run strategy email.
- **US2 (P2)**: Uses the same models and modules as US1 but remains independently testable through decision and integration fixtures.
- **US3 (P3)**: Uses the same models and modules as US1/US2 but remains independently testable through failure/no-trade fixtures.

### Within Each User Story

- Write required tests before implementation tasks in the same story phase.
- Implement models/config before scraper, analyzer, decision, email, scheduler, and CLI code.
- Keep provider payloads behind module boundaries; downstream modules consume structured models only.
- Validate each story at its checkpoint before moving to the next priority.

---

## Parallel Opportunities

- Setup tasks T003 and T004 can run in parallel after T001/T002 are agreed.
- Foundational tests and fixtures T008, T009, and T010 can run in parallel after T005 through T007 interfaces are sketched.
- US1 tests T011 through T015 can run in parallel because they target different test files.
- US2 tests T022 through T025 can run in parallel because they target different fixture paths or non-overlapping assertions.
- US3 tests T030 through T034 can run in parallel because they cover separate modules and integration fixtures.
- Polish tasks T041 through T044 can run in parallel after story implementation is complete.

## Parallel Example: User Story 1

```bash
Task: "T011 [P] [US1] Add liquidation scraper success-shape contract tests in tests/contract/test_module_contracts.py"
Task: "T012 [P] [US1] Add K-line analyzer unit tests in tests/unit/test_kline.py"
Task: "T013 [P] [US1] Add strategy email composition tests in tests/unit/test_emailer.py"
Task: "T014 [P] [US1] Add scheduler and CLI contract tests in tests/contract/test_cli_contract.py"
Task: "T015 [P] [US1] Add end-to-end dry-run strategy email cycle test in tests/integration/test_strategy_email_cycle.py"
```

## Parallel Example: User Story 2

```bash
Task: "T022 [P] [US2] Add decision state-machine tests in tests/unit/test_decision.py"
Task: "T024 [P] [US2] Add email evidence tests in tests/unit/test_emailer.py"
Task: "T025 [P] [US2] Add integration tests for scenario A/B/C fixture cycles in tests/integration/test_strategy_email_cycle.py"
```

## Parallel Example: User Story 3

```bash
Task: "T030 [P] [US3] Add liquidation failure tests in tests/unit/test_liquidation_scraper.py"
Task: "T031 [P] [US3] Add K-line and SMTP failure tests in tests/unit/test_kline.py and tests/unit/test_emailer.py"
Task: "T033 [P] [US3] Add scheduler failure throttling tests in tests/unit/test_scheduler.py"
Task: "T034 [P] [US3] Add no-trade and failure integration tests in tests/integration/test_strategy_email_cycle.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 and Phase 2.
2. Complete Phase 3 tasks T011 through T021.
3. Validate with unit, contract, and integration dry-run tests for User Story 1.
4. Stop and review before adding broader scenario and failure behavior.

### Incremental Delivery

1. Setup + Foundational: package, models, config, mappings, and fixtures.
2. US1: single/recurring cycle and valid strategy email.
3. US2: explicit A/B/C explainable decision coverage.
4. US3: no-trade and dependency/configuration failure safety.
5. Polish: full quickstart validation and coverage review.

### Validation Commands

```bash
PYTHONPATH=src pytest tests/unit tests/contract -q
PYTHONPATH=src pytest tests/integration/test_strategy_email_cycle.py -q
PYTHONPATH=src python -m py_compile src/trading_notice/*.py
```

## Notes

- `[P]` tasks use different files or independent assertions and can run in parallel after their dependencies are available.
- `[US1]`, `[US2]`, and `[US3]` labels map directly to user stories in `spec.md`.
- Tests for core modules and external dependency failures are required by constitution, not optional.
- Default numeric thresholds remain provisional and must carry source basis plus `pending_backtest_validation=true`.
- No task may hardcode API keys, SMTP credentials, tokens, or production secrets.
