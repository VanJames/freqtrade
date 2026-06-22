# Implementation Plan: [FEATURE]

**Branch**: `[###-feature-name]` | **Date**: [DATE] | **Spec**: [link]

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.10+ or [NEEDS CLARIFICATION]

**Primary Dependencies**: trading_system package, Typer CLI, FastAPI dashboard,
SQLAlchemy async/asyncpg, Redis, CCXT/OKX, Hotcoin web APIs, pytest/pytest-asyncio,
or [NEEDS CLARIFICATION]

**Storage**: PostgreSQL for durable order/snapshot/runtime/session state; Redis only
for high-frequency cache subset; reports under `reports/` where applicable

**Testing**: pytest with focused commands from `AGENTS.md`; include py_compile for
dashboard-only changes when relevant

**Target Platform**: local dry-run development and Linux/Docker deployment for trading
services

**Project Type**: async trading engine plus CLI, dashboard, runtime tuner, adapters,
store, and cache

**Performance Goals**: preserve timely symbol loop/position monitor behavior; avoid
blocking async trading paths; specify feature-specific latency or backtest goals here

**Constraints**: default local behavior remains dry-run; live OKX/Hotcoin,
`DRY_RUN=false`, secrets, database resets, Docker volumes, and remote restarts are
production-sensitive; do not treat Redis as durable trailing-state storage

**Scale/Scope**: [domain-specific, e.g., 10k users, 1M LOC, 50 screens or NEEDS CLARIFICATION]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Production-Safe Defaults**: Identify whether this touches `DRY_RUN=false`, live
  exchange paths, secrets, remote services, Docker volumes, or destructive database
  operations. State the safe local default and required explicit operator action.
- **Exchange Boundaries**: If OKX/CCXT or Hotcoin behavior changes, name the adapter
  files, typed models, field mappings, and tests proving behavior is exchange-specific
  or exchange-agnostic.
- **Typed Async Contracts**: Confirm new I/O remains async where current interfaces are
  async and uses existing dataclasses, `StrEnum`, Pydantic settings, or explicit parser
  helpers instead of raw dicts crossing module boundaries.
- **Risk & State Integrity**: If changing signals, sizing, execution, order storage,
  snapshot memory, trailing protection, dashboard status, or runtime settings, describe
  persistence/cache/dashboard impacts and recovery behavior.
- **Focused Verification**: List the exact focused commands expected for this change
  using `AGENTS.md` guidance; explain any command that cannot run.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
src/trading_system/
├── engine.py              # orchestration and diagnostics
├── exchange.py            # ExchangeClient, dry-run, OKX/CCXT adapter
├── hotcoin.py             # Hotcoin private trading/account/session adapter
├── execution.py           # order submission, post-only/TWAP/IOC behavior
├── risk.py                # risk gates and reservation accounting
├── store.py               # PostgreSQL schema and durable state
├── cache.py               # Redis cache subset
├── dashboard.py           # FastAPI dashboard/API
├── tuner.py               # runtime tuning/report generation
└── [feature files]

tests/
├── test_engine.py
├── test_execution.py
├── test_risk.py
├── test_exchange.py
├── test_okx_amount_conversion.py
├── test_runtime_config.py
└── [focused feature tests]
```

**Structure Decision**: [Document the selected structure and reference the real
directories captured above]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
