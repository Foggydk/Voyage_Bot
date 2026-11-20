"""Точки аварийной остановки доступны только отдельной тестовой БД через окружение."""

import os


async def checkpoint(conn, name):
    if os.getenv("TEST_MODE") != "true" or os.getenv("TEST_FAILPOINT") != name:
        return
    database = await conn.fetchval("SELECT current_database()")
    if not database.startswith("voyage_test_"):
        raise RuntimeError(
            "Точки остановки разрешены только в временной тестовой базе."
        )
    os._exit(86)
