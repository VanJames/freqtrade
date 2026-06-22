<!--
Sync Impact Report
Version change: template -> 1.0.0
Modified principles:
- Template principle 1 -> I. Production-Safe Trading Defaults
- Template principle 2 -> II. Explicit Exchange Boundaries
- Template principle 3 -> III. Typed Async Domain Model
- Template principle 4 -> IV. Risk and State Integrity
- Template principle 5 -> V. Focused Verification
Added sections:
- Technology & Architecture Constraints
- Development Workflow & Quality Gates
Removed sections:
- None
Templates requiring updates:
- updated .specify/templates/plan-template.md
- updated .specify/templates/spec-template.md
- updated .specify/templates/tasks-template.md
- not present .specify/templates/commands/*.md
- reviewed README.md
- reviewed AGENTS.md
Follow-up TODOs:
- None
-->

# OKX Quant Trading System Constitution

## Core Principles

### I. Production-Safe Trading Defaults
The system MUST preserve safe local behavior by default: `dry_run` remains true unless
an explicit deployment/runtime choice disables it, and any feature touching
`DRY_RUN=false`, OKX live trading, Hotcoin trading, database resets, Docker volumes, or
remote service restarts is production-sensitive. Production-sensitive work MUST start
with diagnostics and narrow fixes, MUST NOT print or persist secrets outside the
existing secure configuration paths, and MUST NOT run destructive operations without an
explicit user request.

Rationale: the repository controls live perpetual futures execution, account sessions,
order state, and runtime tuning; accidental live orders or secret disclosure create
financial and operational risk.

### II. Explicit Exchange Boundaries
Exchange behavior MUST be isolated behind `ExchangeClient` and concrete adapters in
`exchange.py` and `hotcoin.py`. Shared engine, strategy, risk, execution, store, and
dashboard code MUST consume typed domain models such as `TradeSignal`, `Position`, and
`OrderResult` instead of exchange-specific response dictionaries. OKX/CCXT contract
conversion, safeguards, market loading, and realized PnL handling MUST remain OKX-specific
unless tests prove the behavior is exchange-agnostic. Hotcoin balance, position, fee,
filled amount, session, and realized PnL fields MUST be mapped explicitly in `hotcoin.py`;
missing Hotcoin fill or PnL fields MUST NOT be treated as confirmed fills or truth.

Rationale: the current architecture uses OKX public/CCXT semantics and Hotcoin web API
semantics side by side; mixing assumptions across adapters corrupts order, position,
fee, and PnL state.

### III. Typed Async Domain Model
Runtime I/O MUST remain asynchronous where the current interfaces are async, including
exchange, store, cache, engine, and execution paths. Cross-module contracts MUST use the
existing dataclasses, `StrEnum` values, and Pydantic settings patterns before adding raw
dict plumbing. New configuration MUST be typed in `Settings`, default to conservative
behavior, and be compatible with `.env`/runtime settings. Helpers for parsing structured
market, order, session, or LLM data MUST be explicit and locally testable.

Rationale: typed async boundaries make the engine composable, dry-run testable, and less
likely to leak exchange-specific payload shape into strategy or risk code.

### IV. Risk and State Integrity
Changes to signal generation, sizing, risk checks, execution, position recovery,
trailing protection, order lifecycle storage, or dashboard status MUST preserve the
existing state model. PostgreSQL is the durable source for order tracks, account
snapshots, runtime settings, and Hotcoin sessions; Redis is a high-frequency cache for a
subset of live state and MUST NOT be treated as the source for trailing protection.
Schema changes to `order_tracks` or snapshot memory MUST update both persistence and
dashboard reads. Entry, exit, duplicate-order cooldown, risk reservation/release, and
position recovery behavior MUST be covered by focused tests when changed.

Rationale: trading correctness depends on consistent risk accounting and recoverable
position protection across restarts, not only on immediate in-memory behavior.

### V. Focused Verification
Every code change MUST run the smallest relevant pytest or compile check before handoff,
and the chosen check MUST match the blast radius. Engine, execution, and risk changes
MUST run the engine/execution/risk tests; OKX or Hotcoin adapter changes MUST run the
adapter and execution tests; dashboard or runtime config changes MUST run runtime config
tests and compile the dashboard; tuner, report, or email changes MUST run their focused
tests. When a required check cannot run, the handoff MUST state the command attempted,
the failure, and residual risk.

Rationale: the repository already contains focused pytest coverage for strategy,
regime, risk, execution, adapters, runtime config, tuner, email, and dashboard-adjacent
behavior; matching tests to changed surfaces catches regressions without forcing a full
suite for every small edit.

## Technology & Architecture Constraints

The project is a Python 3.10+ single-package application under `src/trading_system/`
with tests under `tests/`. The CLI entry point is `okx-quant` through Typer. The engine
orchestrates market data, regime classification, strategy, alpha filtering, risk,
execution, state persistence, cache, optional LLM regime review, and dashboard-facing
diagnostics. PostgreSQL access uses SQLAlchemy async/asyncpg; Redis is optional and must
degrade safely when unavailable. The dashboard is FastAPI-based and auth-protected by the
existing session mechanism.

Source changes MUST follow the current local style: Python type hints, dataclasses with
`slots=True` for domain models where appropriate, explicit exchange-specific parsing
helpers, `orjson` for serialized state where already used, and `ruff` line length 100.
New dependencies MUST be justified by a concrete engine, adapter, dashboard, testing, or
runtime need and added through `pyproject.toml`.

LLM-related functionality MUST remain optional, schema-validated, and bounded by local
rules. It MUST NOT directly replace rule engines, risk checks, or exchange-sourced facts.

## Development Workflow & Quality Gates

Feature specs and plans MUST identify whether the change touches live trading, exchange
adapters, risk/state integrity, dashboard/runtime config, tuner/report/email behavior,
or only documentation. Plans MUST include a Constitution Check that names the relevant
principles and the focused verification commands expected for the touched surfaces.
Tasks MUST include tests for production-sensitive, exchange, engine, execution, risk,
storage, dashboard, tuner, email, or LLM behavior changes; documentation-only tasks may
record that no runtime test is required.

Implementation MUST preserve user changes in the working tree, avoid unrelated
refactors, and keep behavior scoped to the requested feature. Default local commands
MUST avoid real trading; production restarts, `.env` deployment, live exchange access,
and destructive database or volume operations require explicit user direction or an
already active deployment workflow.

Runtime guidance in `AGENTS.md` and user-facing setup guidance in `README.md` remain
the operational reference for commands, test selection, safety constraints, and
deployment notes. When these files conflict with this constitution, the more
production-safe interpretation governs until the conflict is amended.

## Governance

This constitution governs project specifications, implementation plans, task lists,
code review, and agent behavior for this repository. Amendments MUST update this file,
include a Sync Impact Report, and review dependent Spec Kit templates plus runtime
guidance for drift. Amendments that redefine or remove a principle require a MAJOR
version bump; amendments that add principles, sections, or materially expand governance
require a MINOR version bump; clarifications and wording fixes require a PATCH bump.

Every feature plan MUST pass the Constitution Check before implementation and again
after design if the scope changes. Reviews MUST verify production safety, exchange
boundary isolation, typed async contracts, risk/state integrity, and focused
verification evidence. Exceptions MUST be documented in the plan's Complexity Tracking
or handoff notes with the reason, rejected simpler alternative, and compensating test or
operational check.

**Version**: 1.0.0 | **Ratified**: 2026-06-19 | **Last Amended**: 2026-06-19
