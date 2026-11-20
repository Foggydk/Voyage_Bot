"""Настоящий HTTP-процесс приёмки, без обхода Telegram-подписи."""

import argparse
import asyncio
import os

import asyncpg
from aiohttp import web

from src.services.inventory import expire_reservations, inventory_worker
from src.services.refunds import process_refunds, refund_worker
from src.web import create_app
from tests.support import TOKEN


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    pool = await asyncpg.create_pool(
        os.environ["TEST_DATABASE_URL"], min_size=1, max_size=10
    )
    async with pool.acquire() as conn:
        if not (await conn.fetchval("SELECT current_database()")).startswith(
            "voyage_test_"
        ):
            raise RuntimeError("Тестовый процесс требует временную базу.")
    await expire_reservations(pool)
    await process_refunds(pool)
    runner = web.AppRunner(create_app(pool, TOKEN), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print(f"READY {port}", flush=True)
    tasks = [
        asyncio.create_task(inventory_worker(pool)),
        asyncio.create_task(refund_worker(pool)),
    ]
    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
