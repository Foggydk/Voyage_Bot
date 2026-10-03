import asyncio
from urllib.parse import unquote, urlparse

import asyncpg

from src.config import get_database_url

u = urlparse(get_database_url())


async def try_db(name):
    try:
        conn = await asyncpg.connect(
            host=u.hostname,
            port=u.port,
            user=unquote(u.username),
            password=unquote(u.password),
            database=name,
            ssl=False,
        )
        print(f"[{name}] OK")
        return conn
    except Exception as e:
        print(f"[{name}] ОШИБКА: {type(e).__name__}: {e}")
        return None


async def main():
    conn = await try_db("postgres")
    if conn:
        rows = await conn.fetch("SELECT datname FROM pg_database")
        print("Базы на сервере:", [r["datname"] for r in rows])
    await try_db(u.path.lstrip("/"))


asyncio.run(main())