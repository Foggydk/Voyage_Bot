"""Изолированная временная PostgreSQL: рабочие таблицы не изменяются."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
from telegram import User

from src.config import get_database_url
from src.db import apply_schema, seed_products

TOKEN = "123456:unit-test-token-not-a-real-secret"
MANAGER = 900001


def auth(user_id, *, timestamp=None, token=TOKEN):
    fields = {
        "auth_date": str(int(time.time()) if timestamp is None else timestamp),
        "user": json.dumps(
            {"id": user_id, "first_name": "Test"}, separators=(",", ":")
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


class TestDatabase:
    async def open(self):
        dsn = get_database_url()
        if not dsn:
            raise RuntimeError(
                "Нужен DATABASE_URL к локальному PostgreSQL для создания отдельной тестовой базы."
            )
        parts = urlsplit(dsn)
        self.name = "voyage_test_" + uuid4().hex
        self.admin = await asyncpg.connect(dsn, timeout=5)
        await self.admin.execute(f'CREATE DATABASE "{self.name}"')
        test_dsn = urlunsplit(parts._replace(path="/" + self.name))
        self.dsn = test_dsn
        self.pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=20)
        await apply_schema(self.pool)
        await seed_products(self.pool)
        return self.pool

    async def close(self):
        if getattr(self, "pool", None):
            await self.pool.close()
        if getattr(self, "admin", None):
            # Имя создано только этим тестом, без пользовательской интерполяции.
            assert self.name.startswith("voyage_test_") and len(self.name) == 44
            await self.admin.execute(f'DROP DATABASE "{self.name}"')
            await self.admin.close()


async def cart(pool, user_id, items):
    async with pool.acquire() as conn:
        uid = await conn.fetchval(
            """
            INSERT INTO users (telegram_id,display_name) VALUES ($1,'Тестовый покупатель')
            ON CONFLICT (telegram_id) DO UPDATE SET display_name=EXCLUDED.display_name RETURNING id
        """,
            user_id,
        )
        await conn.execute("DELETE FROM cart_items WHERE user_id=$1", uid)
        for product_id, quantity in items.items():
            await conn.execute(
                "INSERT INTO cart_items(user_id,product_id,quantity) VALUES ($1,$2,$3)",
                uid,
                product_id,
                quantity,
            )
    return User(user_id, "Тестовый покупатель", False)
