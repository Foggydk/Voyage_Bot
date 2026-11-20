"""Все складские переходы идут в той же транзакции, что заказ и деньги."""

import asyncio
import logging
import os
from datetime import timedelta
from uuid import UUID

from src.config import get_manager_ids

logger = logging.getLogger(__name__)


class StockError(ValueError):
    pass


def require_manager(manager_id):
    if manager_id not in get_manager_ids():
        raise PermissionError("Склад доступен только менеджеру.")


def reserve_seconds():
    value = int(os.getenv("RESERVATION_SECONDS", "120"))
    if not 1 <= value <= 86400:
        raise ValueError("RESERVATION_SECONDS: от 1 до 86400 секунд.")
    return value


async def server_now(conn):
    # Управляемые часы передаются только прямым вызовом тестового сервиса.
    return await conn.fetchval("SELECT clock_timestamp()")


async def _lock_balances(conn, ids):
    ids = sorted(set(ids))
    await conn.execute(
        "INSERT INTO stock_balances(product_id) SELECT id FROM products WHERE id=ANY($1::bigint[]) ORDER BY id ON CONFLICT DO NOTHING",
        ids,
    )
    rows = await conn.fetch(
        "SELECT * FROM stock_balances WHERE product_id=ANY($1::bigint[]) ORDER BY product_id FOR UPDATE",
        ids,
    )
    if len(rows) != len(ids):
        raise StockError("Товар не найден.")
    return {r["product_id"]: r for r in rows}


async def movement(
    conn,
    product_id,
    kind,
    on_hand,
    reserved,
    *,
    order_id=None,
    key,
    description,
    author,
):
    await conn.execute(
        """
        INSERT INTO inventory_movements(product_id,type,delta_on_hand,delta_reserved,order_id,event_key,description,author)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
    """,
        product_id,
        kind,
        on_hand,
        reserved,
        order_id,
        key,
        description,
        str(author),
    )
    await conn.execute(
        "UPDATE stock_balances SET on_hand=on_hand+$2,reserved=reserved+$3 WHERE product_id=$1",
        product_id,
        on_hand,
        reserved,
    )


async def supply(pool, manager_id, product_id, quantity, request_id, description):
    require_manager(manager_id)
    if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
        raise StockError("Количество поступления: целое от 1 до 1 000 000.")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 300:
        raise StockError("Опишите поступление (1–300 символов).")
    try:
        key = "supply:" + str(UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        raise StockError("Для поступления нужен request_id UUID.") from None
    async with pool.acquire() as conn, conn.transaction():
        # Один ключ поступления сериализуется даже при подмене product_id.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", key)
        await _lock_balances(conn, [product_id])
        old = await conn.fetchrow(
            "SELECT * FROM inventory_movements WHERE event_key=$1", key
        )
        if old:
            if (
                old["product_id"],
                old["delta_on_hand"],
                old["description"],
                old["author"],
            ) != (product_id, quantity, description.strip(), str(manager_id)):
                raise StockError("Ключ поступления уже использован с другими данными.")
        else:
            await movement(
                conn,
                product_id,
                "supply",
                quantity,
                0,
                key=key,
                description=description.strip(),
                author=manager_id,
            )
        return dict(
            await conn.fetchrow(
                "SELECT *,on_hand-reserved AS available FROM stock_balances WHERE product_id=$1",
                product_id,
            )
        )


async def balances(pool):
    async with pool.acquire() as conn:
        return [
            dict(r)
            for r in await conn.fetch("""
            SELECT p.id AS product_id,p.title,COALESCE(s.on_hand,0) AS on_hand,
                COALESCE(s.reserved,0) AS reserved,COALESCE(s.on_hand-s.reserved,0) AS available
            FROM products p LEFT JOIN stock_balances s ON s.product_id=p.id WHERE p.is_active ORDER BY p.id
        """)
        ]


async def check_available(conn, items):
    rows = await _lock_balances(conn, items)
    for product_id, quantity in sorted(items.items()):
        if rows[product_id]["on_hand"] - rows[product_id]["reserved"] < quantity:
            raise StockError(
                f"Сейчас нет в наличии нужного количества мест: тур #{product_id}. Корзина сохранена."
            )


async def reserve(conn, order_id, items, author):
    now = await server_now(conn)
    reservation = await conn.fetchrow(
        """
        INSERT INTO reservations(order_id,status,expires_at) VALUES ($1,'active',$2) RETURNING *
    """,
        order_id,
        now + timedelta(seconds=reserve_seconds()),
    )
    for product_id, quantity in sorted(items.items()):
        await conn.execute(
            "INSERT INTO reservation_items VALUES ($1,$2,$3)",
            reservation["id"],
            product_id,
            quantity,
        )
        await movement(
            conn,
            product_id,
            "reserve",
            0,
            quantity,
            order_id=order_id,
            key=f"reserve:{order_id}:{product_id}",
            description="Резерв при оформлении",
            author=author,
        )
    return reservation


async def lock_reservation(conn, order_id):
    return await conn.fetchrow(
        "SELECT * FROM reservations WHERE order_id=$1 FOR UPDATE", order_id
    )


async def _items(conn, reservation):
    rows = await conn.fetch(
        "SELECT * FROM reservation_items WHERE reservation_id=$1 ORDER BY product_id",
        reservation["id"],
    )
    await _lock_balances(conn, [r["product_id"] for r in rows])
    return rows


async def release(conn, reservation, reason, author="system"):
    if reservation is None or reservation["status"] != "active":
        return False
    for item in await _items(conn, reservation):
        await movement(
            conn,
            item["product_id"],
            "release",
            0,
            -item["quantity"],
            order_id=reservation["order_id"],
            key=f"release:{reservation['order_id']}:{item['product_id']}",
            description=reason,
            author=author,
        )
    await conn.execute(
        "UPDATE reservations SET status='released',release_reason=$2 WHERE id=$1",
        reservation["id"],
        reason,
    )
    return True


async def accept_sale(conn, order, *, now=None):
    """None — прежний заказ без складского этапа; True — продажа; False — поздняя оплата."""
    reservation = await lock_reservation(conn, order["id"])
    if reservation is None:
        return None
    current = await server_now(conn) if now is None else now
    if reservation["status"] != "active" or current >= reservation["expires_at"]:
        await release(
            conn, reservation, "Срок резерва истёк перед подтверждением оплаты"
        )
        return False
    for item in await _items(conn, reservation):
        await movement(
            conn,
            item["product_id"],
            "sale",
            -item["quantity"],
            -item["quantity"],
            order_id=order["id"],
            key=f"sale:{order['id']}:{item['product_id']}",
            description="Принятая учебная оплата",
            author="payment",
        )
    await conn.execute(
        "UPDATE reservations SET status='consumed' WHERE id=$1", reservation["id"]
    )
    return True


async def return_sale(conn, order_id, author):
    reservation = await lock_reservation(conn, order_id)
    if reservation is None or reservation["status"] != "consumed":
        return
    for item in await _items(conn, reservation):
        await movement(
            conn,
            item["product_id"],
            "return",
            item["quantity"],
            0,
            order_id=order_id,
            key=f"return:{order_id}:{item['product_id']}",
            description="Полный возврат принятой продажи",
            author=author,
        )


async def cancel_order(pool, telegram_id, number):
    async with pool.acquire() as conn, conn.transaction():
        order = await conn.fetchrow(
            """
            SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id
            WHERE o.public_number=$1 FOR UPDATE OF o
        """,
            number,
        )
        if order is None or order["telegram_id"] != telegram_id:
            raise StockError("Заказ не найден или недоступен.")
        if order["status"] in ("cancelled", "expired"):
            return order["status"]
        if order["status"] != "awaiting_payment":
            raise StockError(
                "Оплаченный заказ нельзя отменить: требуется полный возврат."
            )
        reservation = await lock_reservation(conn, order["id"])
        await release(conn, reservation, "Отмена покупателем", telegram_id)
        await conn.execute(
            "UPDATE orders SET status='cancelled',updated_at=NOW() WHERE id=$1",
            order["id"],
        )
        return "cancelled"


async def expire_reservations(pool):
    async with pool.acquire() as conn:
        ids = await conn.fetch(
            "SELECT order_id FROM reservations WHERE status='active' AND expires_at<=clock_timestamp() ORDER BY order_id LIMIT 500"
        )
    count = 0
    for item in ids:
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchrow(
                "SELECT id FROM orders WHERE id=$1 FOR UPDATE", item["order_id"]
            )
            reservation = await lock_reservation(conn, item["order_id"])
            if (
                reservation
                and reservation["status"] == "active"
                and await server_now(conn) >= reservation["expires_at"]
            ):
                await release(conn, reservation, "Срок резерва истёк")
                await conn.execute(
                    "UPDATE orders SET status='expired',updated_at=NOW() WHERE id=$1",
                    item["order_id"],
                )
                count += 1
    return count


async def inventory_worker(pool):
    while True:
        try:
            await expire_reservations(pool)
        except Exception:
            logger.warning("Не удалось обработать сроки резервов; будет повторено")
        await asyncio.sleep(5)
