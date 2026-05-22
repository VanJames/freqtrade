from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import Any

import orjson
from sqlalchemy import Boolean, Column, DateTime, MetaData, Numeric, String, Table, Text, desc, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from trading_system.models import OrderResult, Regime, TradeSignal

logger = getLogger(__name__)

metadata = MetaData()

order_tracks = Table(
    "order_tracks",
    metadata,
    Column("order_id", String(64), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("regime_mode", String(20), nullable=False),
    Column("initial_qty", Numeric(18, 8), nullable=False),
    Column("maker_filled", Numeric(18, 8), nullable=False, default=0),
    Column("taker_twap_filled", Numeric(18, 8), nullable=False, default=0),
    Column("fee_paid", Numeric(18, 8), nullable=False, default=0),
    Column("status", String(32), nullable=False, default=""),
    Column("filled_qty", Numeric(18, 8), nullable=False, default=0),
    Column("avg_price", Numeric(18, 8), nullable=False, default=0),
    Column("realized_pnl", Numeric(18, 8), nullable=True),
    Column("side", String(8), nullable=False, default=""),
    Column("position_side", String(8), nullable=False, default=""),
    Column("signal_reason", Text, nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("snapshot_time", DateTime(timezone=True), primary_key=True),
    Column("total_equity", Numeric(18, 4), nullable=False),
    Column("active_hedging", Boolean, nullable=False, default=False),
    Column("serialized_memory", Text, nullable=False),
)

runtime_settings = Table(
    "runtime_settings",
    metadata,
    Column("setting_key", String(64), primary_key=True),
    Column("setting_value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

exchange_sessions = Table(
    "exchange_sessions",
    metadata,
    Column("exchange_id", String(32), primary_key=True),
    Column("account_key", String(64), primary_key=True),
    Column("session_data", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class StateStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.engine: AsyncEngine = create_async_engine(dsn, pool_pre_ping=True)
        self._schema_ready = False

    async def initialize(self) -> None:
        await ensure_database_exists(self.dsn)
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await self._ensure_order_columns(conn)
        self._schema_ready = True

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        await self.initialize()

    async def record_order(
        self,
        order: OrderResult,
        regime: Regime,
        signal: TradeSignal | None = None,
    ) -> None:
        realized_pnl = order.realized_pnl
        if realized_pnl is None and signal is not None:
            realized_pnl = estimate_realized_pnl(order, signal)
        async with self.engine.begin() as conn:
            stmt = pg_insert(order_tracks).values(
                **self._order_values(order, regime, signal, realized_pnl, created_at=datetime.now(timezone.utc))
            ).on_conflict_do_update(
                index_elements=[order_tracks.c.order_id],
                set_={
                    "status": order.status,
                    "filled_qty": order.filled,
                    "avg_price": order.average or order.price,
                    "fee_paid": order.fee,
                    "realized_pnl": realized_pnl,
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "signal_reason": signal.reason if signal else order_tracks.c.signal_reason,
                },
            )
            await conn.execute(stmt)

    async def record_twap_fill(self, order_id: str, filled: float, fee: float) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(order_tracks)
                .where(order_tracks.c.order_id == order_id)
                .values(
                    taker_twap_filled=order_tracks.c.taker_twap_filled + filled,
                    filled_qty=order_tracks.c.filled_qty + filled,
                    fee_paid=order_tracks.c.fee_paid + fee,
                )
            )

    async def update_order_result(
        self,
        order: OrderResult,
        signal: TradeSignal | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        if realized_pnl is None:
            realized_pnl = order.realized_pnl
        if realized_pnl is None and signal is not None:
            realized_pnl = estimate_realized_pnl(order, signal)
        values: dict[str, Any] = {
            "status": order.status,
            "filled_qty": order.filled,
            "avg_price": order.average or order.price,
            "fee_paid": order.fee,
            "realized_pnl": realized_pnl,
            "side": order.side.value,
            "position_side": order.position_side.value,
        }
        if signal is not None:
            values["signal_reason"] = signal.reason
        async with self.engine.begin() as conn:
            await conn.execute(update(order_tracks).where(order_tracks.c.order_id == order.id).values(**values))

    async def recent_order_refs(self, limit: int = 30) -> list[tuple[str, str]]:
        async with self.engine.begin() as conn:
            rows = await conn.execute(
                select(order_tracks.c.order_id, order_tracks.c.symbol)
                .order_by(desc(order_tracks.c.created_at))
                .limit(limit)
            )
        return [(str(order_id), str(symbol)) for order_id, symbol in rows]

    async def save_snapshot(self, equity: float, active_hedging: bool, memory: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(account_snapshots).values(
                    snapshot_time=datetime.now(timezone.utc),
                    total_equity=equity,
                    active_hedging=active_hedging,
                    serialized_memory=orjson.dumps(memory).decode("utf-8"),
                )
            )

    async def load_latest_snapshot(self) -> dict[str, Any] | None:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                select(account_snapshots.c.serialized_memory)
                .order_by(desc(account_snapshots.c.snapshot_time))
                .limit(1)
            )
            row = result.first()
        if row is None:
            return None
        return orjson.loads(row.serialized_memory)

    async def save_runtime_settings(self, values: dict[str, Any]) -> None:
        await self.ensure_schema()
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as conn:
            for key, value in values.items():
                stmt = (
                    pg_insert(runtime_settings)
                    .values(setting_key=key, setting_value=str(value), updated_at=now)
                    .on_conflict_do_update(
                        index_elements=[runtime_settings.c.setting_key],
                        set_={"setting_value": str(value), "updated_at": now},
                    )
                )
                await conn.execute(stmt)

    async def load_runtime_settings(self) -> dict[str, str]:
        await self.ensure_schema()
        async with self.engine.begin() as conn:
            rows = await conn.execute(select(runtime_settings.c.setting_key, runtime_settings.c.setting_value))
        result: dict[str, str] = {}
        for key, value in rows:
            result[str(key)] = str(value)
        return result

    async def close(self) -> None:
        await self.engine.dispose()

    async def _ensure_order_columns(self, conn) -> None:
        await conn.execute(
            text(
                """
                alter table order_tracks
                add column if not exists status varchar(32) not null default '',
                add column if not exists filled_qty numeric(18, 8) not null default 0,
                add column if not exists avg_price numeric(18, 8) not null default 0,
                add column if not exists realized_pnl numeric(18, 8),
                add column if not exists side varchar(8) not null default '',
                add column if not exists position_side varchar(8) not null default '',
                add column if not exists signal_reason text not null default ''
                """
            )
        )

    def _order_values(
        self,
        order: OrderResult,
        regime: Regime,
        signal: TradeSignal | None,
        realized_pnl: float | None,
        *,
        created_at: datetime,
    ) -> dict[str, Any]:
        return {
            "order_id": order.id,
            "symbol": order.symbol,
            "regime_mode": regime.value,
            "initial_qty": order.amount,
            "maker_filled": order.filled,
            "taker_twap_filled": 0,
            "fee_paid": order.fee,
            "status": order.status,
            "filled_qty": order.filled,
            "avg_price": order.average or order.price,
            "realized_pnl": realized_pnl,
            "side": order.side.value,
            "position_side": order.position_side.value,
            "signal_reason": signal.reason if signal else "",
            "created_at": created_at,
        }


def estimate_realized_pnl(order: OrderResult, signal: TradeSignal) -> float | None:
    entry_price = _metadata_float(signal.metadata, "entry_price")
    exit_price = order.average or order.price
    filled = order.filled
    if entry_price <= 0 or exit_price <= 0 or filled <= 0:
        return None
    if signal.position_side.value == "long":
        gross = (exit_price - entry_price) * filled
    else:
        gross = (entry_price - exit_price) * filled
    return gross - order.fee


def _metadata_float(metadata: dict[str, Any], key: str) -> float:
    try:
        return float(metadata.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def ensure_database_exists(dsn: str) -> None:
    url = make_url(dsn)
    database = url.database
    if not database or database == "postgres":
        return

    admin_engine = create_async_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :database"), {"database": database})
            if exists:
                return
            preparer = conn.sync_connection.dialect.identifier_preparer
            await conn.execute(text(f"CREATE DATABASE {preparer.quote(database)}"))
            logger.info("created postgres database database=%s", database)
    finally:
        await admin_engine.dispose()
