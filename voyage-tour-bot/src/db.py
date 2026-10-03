"""Пул PostgreSQL, схема и каталог туров в таблице products."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg
from telegram.ext import Application, ContextTypes

from src.data.tours import TOURS

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT_DIR / "schema.sql"
POOL_KEY = "db_pool"


def get_pool(context: ContextTypes.DEFAULT_TYPE) -> asyncpg.Pool | None:
    pool = context.application.bot_data.get(POOL_KEY)
    return pool if isinstance(pool, asyncpg.Pool) else None


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
    leftover = "\n".join(buf).strip()
    if leftover:
        statements.append(leftover)
    return statements


async def apply_schema(pool: asyncpg.Pool) -> None:
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        async with conn.transaction():
            for statement in _sql_statements(script):
                await conn.execute(statement)


async def seed_products(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for tour in TOURS:
                await conn.execute(
                    """
                    INSERT INTO products (
                        id, title, description, destination,
                        tour_type, duration_days, price, is_active
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        destination = EXCLUDED.destination,
                        tour_type = EXCLUDED.tour_type,
                        duration_days = EXCLUDED.duration_days,
                        price = EXCLUDED.price,
                        is_active = TRUE
                    """,
                    tour.id,
                    tour.title,
                    tour.description,
                    tour.destination,
                    tour.tour_type,
                    tour.duration_days,
                    tour.price,
                )
            await conn.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('products', 'id'),
                    GREATEST((SELECT MAX(id) FROM products), 1)
                )
                """
            )


async def connect_database(application: Application, dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    await apply_schema(pool)
    await seed_products(pool)
    application.bot_data[POOL_KEY] = pool
    logger.info("PostgreSQL подключена, схема и каталог готовы.")


async def close_database(application: Application) -> None:
    pool = application.bot_data.pop(POOL_KEY, None)
    if isinstance(pool, asyncpg.Pool):
        await pool.close()
        logger.info("Соединение с PostgreSQL закрыто.")
