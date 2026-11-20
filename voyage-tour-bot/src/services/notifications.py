"""Повторяем доставку из общей базы после сетевой ошибки и перезапуска."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def deliver_notifications(pool, bot):
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch("""
            SELECT * FROM notification_outbox WHERE sent_at IS NULL
            ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED
        """)
        for row in rows:
            try:
                await bot.send_message(chat_id=row["chat_id"], text=row["text"])
            except Exception:
                logger.warning(
                    "Не доставлено уведомление #%s; будет повторено", row["id"]
                )
                await conn.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1 WHERE id=$1",
                    row["id"],
                )
            else:
                await conn.execute(
                    "UPDATE notification_outbox SET sent_at=NOW(),attempts=attempts+1 WHERE id=$1",
                    row["id"],
                )


async def notification_worker(pool, bot):
    while True:
        try:
            await deliver_notifications(pool, bot)
        except Exception:
            logger.warning("Журнал уведомлений временно недоступен")
        await asyncio.sleep(5)
