# AGENTS.md

This file gives coding-agent instructions for this repository. It applies to the whole tree unless a more specific `AGENTS.md` exists in a subdirectory.

## Project Overview

This is an async perpetual futures trading system with:

- Live trading engine: `src/trading_system/engine.py`
- Exchange adapters: `src/trading_system/exchange.py` for CCXT/OKX and `src/trading_system/hotcoin.py` for Hotcoin web APIs
- Strategy, regime, risk, and sizing logic under `src/trading_system/`
- Dashboard: `src/trading_system/dashboard.py`
- Runtime tuning/backtesting: `src/trading_system/tuner.py`, `src/trading_system/backtest.py`
- PostgreSQL state/order storage: `src/trading_system/store.py`
- Redis high-frequency cache: `src/trading_system/cache.py`

Default local behavior should be safe. Do not assume real trading is acceptable unless the user explicitly asks for it or the current deployment config already does it.

## Safety Rules

- Never print, commit, or paste secrets from `.env`, cookies, QR login sessions, API keys, SMTP passwords, or database credentials.
- `docker compose config` may expose secrets through interpolated env vars. Avoid running it unless necessary, and never include its full output in responses.
- Do not reset databases, delete Docker volumes, wipe reports, or run destructive git commands unless explicitly requested.
- Treat `DRY_RUN=false`, Hotcoin, and OKX live trading paths as production-sensitive. Prefer diagnostics first, then narrow fixes.
- For Hotcoin, do not reuse CCXT/OKX assumptions for balances, positions, fees, fills, or realized PnL. Hotcoin fields must be mapped explicitly in `hotcoin.py`.
- For OKX/CCXT behavior, avoid changing Hotcoin-specific fixes into shared code unless tests prove the behavior is exchange-agnostic.

## Common Commands

Install and test locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src pytest -q
```

Run focused tests:

```bash
PYTHONPATH=src pytest tests/test_engine.py tests/test_execution.py -q
PYTHONPATH=src pytest tests/test_okx_amount_conversion.py tests/test_exchange.py -q
PYTHONPATH=src pytest tests/test_tuner.py tests/test_email_notification.py -q
```

Local app commands:

```bash
okx-quant init-db
okx-quant run --dry-run --once --with-store --with-redis
okx-quant tune-runtime --days 30 --no-llm-review
```

Docker services:

```bash
docker-compose up -d --build app dashboard runtime-tuner
docker-compose ps
docker-compose logs --tail=100 app
```

The remote production directory historically used by this project is `/opt/freqtrade`, with services `app`, `dashboard`, `runtime-tuner`, `postgres`, and `redis`.

## Testing Expectations

- Run the smallest relevant test set for every change.
- For engine, execution, risk, or exchange changes, run at least:

```bash
PYTHONPATH=src pytest tests/test_engine.py tests/test_execution.py tests/test_risk.py -q
```

- For Hotcoin/OKX adapter changes, run:

```bash
PYTHONPATH=src pytest tests/test_okx_amount_conversion.py tests/test_exchange.py tests/test_execution.py -q
```

- For dashboard/runtime config changes, run:

```bash
PYTHONPATH=src pytest tests/test_runtime_config.py -q
PYTHONPATH=src python -m py_compile src/trading_system/dashboard.py
```

- For tuner/report/email changes, run:

```bash
PYTHONPATH=src pytest tests/test_tuner.py tests/test_email_notification.py -q
```

## Code Style

- Python target is 3.10+; project uses `ruff` with line length 100.
- Use typed, explicit helpers for exchange-specific parsing instead of ad hoc string checks in call sites.
- Keep changes scoped. Avoid unrelated refactors in strategy/risk/execution code.
- Use structured parsers and existing dataclasses/models (`TradeSignal`, `Position`, `OrderResult`) instead of raw dicts crossing module boundaries.
- Comments should explain non-obvious trading/exchange behavior, not restate the code.

## Trading Engine Notes

- `ExecutionEngine.execute()` sends the order email before submitting an entry order.
- `OKXQuantEngine._execute_signals()` handles risk checks, duplicate entry/exit cooldowns, order recording, and risk reservation/release.
- Rejected entry signals may still send email. `risk_rejection_email_cooldown_seconds` throttles repeated low-equity/risk-rejection emails.
- Position protection state lives in `PositionManager.trailing` and is serialized into `account_snapshots.serialized_memory` as `trailing_states`.
- Redis only stores a subset (`positions`, `open_orders`, `hedge_locks`); do not expect trailing state in Redis.

## Exchange-Specific Rules

### OKX / CCXT

- OKX amount conversion is contract-based and handled in `CcxtOkxExchange`.
- OKX realized PnL can be merged from account bills in the dashboard.
- Do not disable CCXT safeguards or market loading behavior without tests.

### Hotcoin

- Hotcoin uses OKX public market data but Hotcoin private trading/account endpoints.
- Hotcoin QR session is stored in PostgreSQL `exchange_sessions`; expired sessions require dashboard re-scan.
- Hotcoin balance, position, fee, filled amount, and realized PnL fields differ from CCXT. Add aliases in `hotcoin.py` and test them.
- If Hotcoin order responses lack fill metrics, do not assume IOC/TWAP orders filled.
- Historical Hotcoin orders without exchange-sourced `pnl_source` should not display locally estimated fee/PnL as truth.

## Dashboard Notes

- Dashboard API is auth-protected by `okx_quant_dashboard_session`.
- `/api/status` is the main data source for the React dashboard.
- Current positions and trailing state come from the latest account snapshot memory:
  - `positions`
  - `trailing_states`
  - `recovered_positions`
- If adding fields to `order_tracks`, update both `store.py` schema and `dashboard.py` schema/selects.

## Deployment Notes

When the user asks to restart online services after syncing code or `.env`, normally run:

```bash
ssh root@199.7.140.231 "cd /opt/freqtrade && docker-compose up -d --build app dashboard runtime-tuner && docker-compose ps"
```

If only `.env` changed and images do not need rebuilding, use force recreate:

```bash
ssh root@199.7.140.231 "cd /opt/freqtrade && docker-compose up -d --force-recreate app dashboard runtime-tuner && docker-compose ps"
```

After restart, check:

```bash
ssh root@199.7.140.231 "cd /opt/freqtrade && docker-compose logs --tail=80 app && docker-compose logs --tail=40 dashboard && docker-compose logs --tail=40 runtime-tuner"
```

Report whether containers are running, whether `postgres`/`redis` are healthy, and any actionable errors.

## Git

- Preserve user changes. Do not revert files you did not intentionally edit.
- Commit only when the user asks for local submission/commit or when the workflow explicitly requires it.
- Use concise commit messages that describe the behavioral change.
