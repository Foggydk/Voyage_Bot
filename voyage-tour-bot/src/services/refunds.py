"""Долговечные возвраты и учебный провайдер с ошибками и потерей ответа."""

import asyncio
import logging

from src.services.payments import record_confirmed_refund

logger = logging.getLogger(__name__)


async def simulator_refund(pool, payment_id, operation_key):
    error = None
    result = None
    async with pool.acquire() as conn, conn.transaction():
        # Провайдер — отдельная транзакция, независимая от локального учёта.
        await conn.fetchrow(
            "SELECT id FROM payments WHERE id=$1 FOR UPDATE", payment_id
        )
        existing = await conn.fetchrow(
            "SELECT * FROM simulator_refunds WHERE operation_key=$1", operation_key
        )
        if existing:
            if existing["payment_id"] != payment_id:
                raise ValueError("Ключ провайдера относится к другому платежу.")
            return dict(existing)
        payment = await conn.fetchrow(
            "SELECT status FROM payments WHERE id=$1", payment_id
        )
        if payment is None or payment["status"] != "refund_pending":
            raise ValueError("Платёж не ожидает возврата.")
        fault = await conn.fetchrow(
            "SELECT * FROM simulator_faults WHERE payment_id=$1 FOR UPDATE", payment_id
        )
        if fault and fault["failures_left"] > 0:
            await conn.execute(
                "UPDATE simulator_faults SET failures_left=failures_left-1 WHERE payment_id=$1",
                payment_id,
            )
            error = "Имитатор: временная ошибка сети"
        else:
            result = dict(
                await conn.fetchrow(
                    """
                INSERT INTO simulator_refunds(operation_key,payment_id,provider_refund_id)
                VALUES ($1,$2,$3) RETURNING *
            """,
                    operation_key,
                    payment_id,
                    f"sim-refund-{payment_id}",
                )
            )
            if fault and fault["lost_responses_left"] > 0:
                await conn.execute(
                    "UPDATE simulator_faults SET lost_responses_left=lost_responses_left-1 WHERE payment_id=$1",
                    payment_id,
                )
                error = "Имитатор: ответ потерян после успешного возврата; будет сверка по постоянному ключу"
    if error:
        raise OSError(error)
    return result


async def process_refunds(pool, *, limit=30, force=False):
    done = 0
    for _ in range(limit):
        async with pool.acquire() as conn, conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT * FROM refund_jobs WHERE
                    (status='pending' AND ($1 OR next_attempt_at<=clock_timestamp()))
                    OR (status='processing' AND lease_until<=clock_timestamp())
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """,
                force,
            )
            if job is None:
                break
            await conn.execute(
                """
                UPDATE refund_jobs SET status='processing',attempts=attempts+1,
                    lease_until=clock_timestamp()+INTERVAL '10 seconds' WHERE id=$1
            """,
                job["id"],
            )
        try:
            await simulator_refund(pool, job["payment_id"], job["operation_key"])
            async with pool.acquire() as conn, conn.transaction():
                await record_confirmed_refund(conn, job["payment_id"], None)
            done += 1
        except (OSError, ValueError) as exc:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE refund_jobs SET status='pending',last_error=$2,
                        next_attempt_at=clock_timestamp()+INTERVAL '5 seconds',lease_until=NULL
                    WHERE id=$1 AND status!='succeeded'
                """,
                    job["id"],
                    str(exc),
                )
            # Force в тестах — один проход, не мгновенный бесконечный повтор ошибки.
            if force:
                break
    return done


async def refund_worker(pool):
    while True:
        try:
            await process_refunds(pool)
        except Exception:
            logger.warning("Возвраты временно недоступны; задания сохранены")
        await asyncio.sleep(5)
