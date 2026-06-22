---

description: "Task list template for feature implementation"
---

# Tasks: [FEATURE NAME]

**Input**: Design documents from `/specs/[###-feature-name]/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Tests are REQUIRED for production-sensitive, exchange adapter, engine,
execution, risk, storage, dashboard/runtime config, tuner/report/email, and LLM behavior
changes. Documentation-only changes may state that no runtime test is required. Use the
smallest focused command from `AGENTS.md`.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Application package**: `src/trading_system/`
- **Tests**: `tests/test_*.py`
- **Runtime guidance**: `AGENTS.md`, `README.md`, `knowledge/*.md`
- **Generated reports**: `reports/` when tuner/backtest output is involved
- Use concrete repository paths from plan.md; do not invent `backend/`, `frontend/`,
  `ios/`, or generic service directories unless the plan adds them.

<!--
  ============================================================================
  IMPORTANT: The tasks below are SAMPLE TASKS for illustration purposes only.

  The /speckit-tasks command MUST replace these with actual tasks based on:
  - User stories from spec.md (with their priorities P1, P2, P3...)
  - Feature requirements from plan.md
  - Entities from data-model.md
  - Endpoints from contracts/

  Tasks MUST be organized by user story so each story can be:
  - Implemented independently
  - Tested independently
  - Delivered as an MVP increment

  DO NOT keep these sample tasks in the generated tasks.md file.
  ============================================================================
-->

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Confirm touched surfaces from plan.md: production safety, exchange adapter,
      engine/execution/risk, storage/cache/dashboard, tuner/report/email, LLM, or docs
- [ ] T002 Identify focused verification command(s) from `AGENTS.md`
- [ ] T003 [P] Confirm no live trading, destructive database, Docker volume, or remote
      restart action is required unless explicitly specified

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

Examples of foundational tasks (adjust based on your project):

- [ ] T004 Define or update typed contracts in `src/trading_system/models.py`,
      `src/trading_system/config.py`, or feature-specific parser helpers
- [ ] T005 [P] Define exchange-specific field mapping in `src/trading_system/exchange.py`
      or `src/trading_system/hotcoin.py` if adapter behavior changes
- [ ] T006 [P] Define PostgreSQL/Redis/dashboard state impact in
      `src/trading_system/store.py`, `src/trading_system/cache.py`, or
      `src/trading_system/dashboard.py` if durable or cached state changes
- [ ] T007 Add or update focused tests before implementation for any non-doc behavior
      change
- [ ] T008 Configure error handling and logging without exposing secrets
- [ ] T009 Update environment/runtime setting handling in `src/trading_system/config.py`
      or `src/trading_system/runtime_config.py` when required

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - [Title] (Priority: P1) 🎯 MVP

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 1 (REQUIRED for non-doc behavior changes) ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T010 [P] [US1] Focused pytest coverage for [changed behavior] in
      tests/test_[surface].py
- [ ] T011 [P] [US1] Adapter/state/dashboard regression test for [boundary] in
      tests/test_[surface].py, if this story touches that boundary

### Implementation for User Story 1

- [ ] T012 [P] [US1] Update typed domain model or settings in
      src/trading_system/[file].py
- [ ] T013 [P] [US1] Update exchange/store/cache/dashboard parser or schema in
      src/trading_system/[file].py, if required
- [ ] T014 [US1] Implement engine/strategy/risk/execution/dashboard behavior in
      src/trading_system/[file].py (depends on T012, T013)
- [ ] T015 [US1] Integrate feature through existing CLI, dashboard API, or engine path
- [ ] T016 [US1] Add validation and error handling
- [ ] T017 [US1] Add non-secret diagnostic logging for user story 1 operations

**Checkpoint**: At this point, User Story 1 should be fully functional and testable independently

---

## Phase 4: User Story 2 - [Title] (Priority: P2)

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 2 (REQUIRED for non-doc behavior changes) ⚠️

- [ ] T018 [P] [US2] Focused pytest coverage for [changed behavior] in
      tests/test_[surface].py
- [ ] T019 [P] [US2] Adapter/state/dashboard regression test for [boundary] in
      tests/test_[surface].py, if this story touches that boundary

### Implementation for User Story 2

- [ ] T020 [P] [US2] Update typed domain model, settings, parser, or schema in
      src/trading_system/[file].py
- [ ] T021 [US2] Implement behavior in the existing engine/adapter/store/dashboard path
- [ ] T022 [US2] Integrate through existing CLI, dashboard API, runtime config, or engine
      path
- [ ] T023 [US2] Integrate with User Story 1 components (if needed)

**Checkpoint**: At this point, User Stories 1 AND 2 should both work independently

---

## Phase 5: User Story 3 - [Title] (Priority: P3)

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 3 (REQUIRED for non-doc behavior changes) ⚠️

- [ ] T024 [P] [US3] Focused pytest coverage for [changed behavior] in
      tests/test_[surface].py
- [ ] T025 [P] [US3] Adapter/state/dashboard regression test for [boundary] in
      tests/test_[surface].py, if this story touches that boundary

### Implementation for User Story 3

- [ ] T026 [P] [US3] Update typed domain model, settings, parser, or schema in
      src/trading_system/[file].py
- [ ] T027 [US3] Implement behavior in the existing engine/adapter/store/dashboard path
- [ ] T028 [US3] Integrate through existing CLI, dashboard API, runtime config, or engine
      path

**Checkpoint**: All user stories should now be independently functional

---

[Add more user story phases as needed, following the same pattern]

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [ ] TXXX [P] Documentation updates in docs/
- [ ] TXXX Code cleanup and refactoring
- [ ] TXXX Performance optimization across all stories
- [ ] TXXX [P] Additional focused tests in `tests/test_[surface].py`
- [ ] TXXX Security hardening
- [ ] TXXX Run quickstart.md validation
- [ ] TXXX Run focused verification command(s) named in plan.md and record results

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational phase completion
  - User stories can then proceed in parallel (if staffed)
  - Or sequentially in priority order (P1 → P2 → P3)
- **Polish (Final Phase)**: Depends on all desired user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2) - No dependencies on other stories
- **User Story 2 (P2)**: Can start after Foundational (Phase 2) - May integrate with US1 but should be independently testable
- **User Story 3 (P3)**: Can start after Foundational (Phase 2) - May integrate with US1/US2 but should be independently testable

### Within Each User Story

- Required tests MUST be written and FAIL before implementation for non-doc behavior
  changes
- Typed contracts/settings before engine/adapter/store/dashboard implementation
- Parser/schema changes before call-site changes
- Core implementation before integration
- Story complete before moving to next priority

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Foundational tasks marked [P] can run in parallel (within Phase 2)
- Once Foundational phase completes, all user stories can start in parallel (if team capacity allows)
- All tests for a user story marked [P] can run in parallel
- Models within a story marked [P] can run in parallel
- Different user stories can be worked on in parallel by different team members

---

## Parallel Example: User Story 1

```bash
# Launch all tests for User Story 1 together:
Task: "Focused pytest coverage for [changed behavior] in tests/test_[surface].py"
Task: "Adapter/state/dashboard regression test for [boundary] in tests/test_[surface].py"

# Launch independent implementation tasks for User Story 1:
Task: "Update typed settings/parser in src/trading_system/[file].py"
Task: "Update dashboard/API display in src/trading_system/dashboard.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL - blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: Test User Story 1 independently
5. Deploy/demo if ready

### Incremental Delivery

1. Complete Setup + Foundational → Foundation ready
2. Add User Story 1 → Test independently → Deploy/Demo (MVP!)
3. Add User Story 2 → Test independently → Deploy/Demo
4. Add User Story 3 → Test independently → Deploy/Demo
5. Each story adds value without breaking previous stories

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: User Story 1
   - Developer B: User Story 2
   - Developer C: User Story 3
3. Stories complete and integrate independently

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Verify tests fail before implementing
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- Avoid: vague tasks, same file conflicts, cross-story dependencies that break independence
- Avoid: live trading, destructive database/volume operations, or remote restarts unless
  explicitly required by the feature and captured in plan.md
