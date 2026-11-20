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
                        tour_type, duration_days, price, cost_price, is_active
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        destination = EXCLUDED.destination,
                        tour_type = EXCLUDED.tour_type,
                        duration_days = EXCLUDED.duration_days,
                        price = EXCLUDED.price,
                        cost_price = EXCLUDED.cost_price,
                        is_active = TRUE
                    """,
                    tour.id,
                    tour.title,
                    tour.description,
                    tour.destination,
                    tour.tour_type,
                    tour.duration_days,
                    tour.price,
                    tour.cost,
                )
            await conn.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('products', 'id'),
                    GREATEST((SELECT MAX(id) FROM products), 1)
                )
                """
            )
            # Старые позиции заказа без снимка: берём текущую себестоимость каталога.
            # Это восстановление, не историческая цена закупки на дату заказа.
            await conn.execute(
                """
                UPDATE order_items AS oi
                SET cost_snapshot = p.cost_price, cost_estimated = TRUE
                FROM products AS p
                WHERE oi.product_id = p.id
                  AND oi.cost_snapshot IS NULL
                  AND p.cost_price IS NOT NULL
                """
            )
            await conn.execute(
                "INSERT INTO stock_balances(product_id) SELECT id FROM products ON CONFLICT DO NOTHING"
            )
            migration = await conn.fetchval("""
                INSERT INTO schema_migrations(name) VALUES ('finance_event_cost_v1')
                ON CONFLICT DO NOTHING RETURNING name
            """)
            if migration:
                await conn.execute("""
                    UPDATE finance_operations f SET cost_kopecks=x.cost
                    FROM (SELECT order_id,(SUM(cost_snapshot*quantity)*100)::bigint AS cost
                          FROM order_items GROUP BY order_id HAVING bool_and(cost_snapshot IS NOT NULL)) x
                    WHERE f.order_id=x.order_id AND f.type IN ('receipt','refund')
                """)


async def connect_database(application: Application, dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=15)
    try:
        await apply_schema(pool)
        await seed_products(pool)
    except BaseException:
        await pool.close()
        raise
    application.bot_data[POOL_KEY] = pool
    logger.info("PostgreSQL подключена, схема и каталог готовы.")


async def close_database(application: Application) -> None:
    pool = application.bot_data.pop(POOL_KEY, None)
    if isinstance(pool, asyncpg.Pool):
        await pool.close()
        logger.info("Соединение с PostgreSQL закрыто.")
