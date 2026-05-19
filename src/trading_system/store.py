from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import Any

import orjson
from sqlalchemy import Boolean, Column, DateTime, MetaData, Numeric, String, Table, Text, desc, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from trading_system.models import OrderResult, Regime

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


class StateStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.engine: AsyncEngine = create_async_engine(dsn, pool_pre_ping=True)

    async def initialize(self) -> None:
        await ensure_database_exists(self.dsn)
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def record_order(self, order: OrderResult, regime: Regime) -> None:
        async with self.engine.begin() as conn:
            stmt = insert(order_tracks).values(
                order_id=order.id,
                symbol=order.symbol,
                regime_mode=regime.value,
                initial_qty=order.amount,
                maker_filled=order.filled,
                taker_twap_filled=0,
                fee_paid=order.fee,
                created_at=datetime.now(timezone.utc),
            )
            await conn.execute(stmt)

    async def record_twap_fill(self, order_id: str, filled: float, fee: float) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(order_tracks)
                .where(order_tracks.c.order_id == order_id)
                .values(
                    taker_twap_filled=order_tracks.c.taker_twap_filled + filled,
                    fee_paid=order_tracks.c.fee_paid + fee,
                )
            )

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

    async def save_runtime_settings(self, values: dict[str, float]) -> None:
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

    async def load_runtime_settings(self) -> dict[str, float]:
        async with self.engine.begin() as conn:
            rows = await conn.execute(select(runtime_settings.c.setting_key, runtime_settings.c.setting_value))
        result: dict[str, float] = {}
        for key, value in rows:
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                logger.warning("ignored invalid runtime setting key=%s value=%s", key, value)
        return result

    async def close(self) -> None:
        await self.engine.dispose()


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
