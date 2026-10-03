"""Корзина. С PostgreSQL хранится в таблице cart_items и переживает перезапуск бота.

Если база не подключена, работает запасной вариант: корзина в памяти сессии.
Все функции принимают Telegram ID пользователя (user_id).
"""

import asyncpg
from telegram.ext import ContextTypes

from src.data.tours import Tour, format_price, get_tour
from src.db import get_pool

CART_KEY = "cart"
MAX_QUANTITY = 10


def _memory(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    return context.user_data.setdefault(CART_KEY, {})


async def _db_user_id(conn: asyncpg.Connection, telegram_id: int) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id) VALUES ($1)
        ON CONFLICT (telegram_id) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
        RETURNING id
        """,
        telegram_id,
    )
    return int(row["id"])


async def items(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict[int, int]:
    """Корзина: {tour_id: количество}."""
    pool = get_pool(context)
    if pool is None:
        return dict(_memory(context))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.product_id, c.quantity
            FROM cart_items c
            JOIN users u ON u.id = c.user_id
            WHERE u.telegram_id = $1
            ORDER BY c.id
            """,
            user_id,
        )
    return {int(r["product_id"]): int(r["quantity"]) for r in rows}


async def add_item(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, tour_id: int, quantity: int = 1
) -> Tour | None:
    if quantity < 1:
        return None
    tour = get_tour(tour_id)
    if tour is None:
        return None
    pool = get_pool(context)
    if pool is None:
        cart = _memory(context)
        cart[tour_id] = min(cart.get(tour_id, 0) + quantity, MAX_QUANTITY)
        return tour
    async with pool.acquire() as conn:
        db_user = await _db_user_id(conn, user_id)
        await conn.execute(
            """
            INSERT INTO cart_items (user_id, product_id, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, product_id) DO UPDATE SET
                quantity = LEAST(cart_items.quantity + EXCLUDED.quantity, $4)
            """,
            db_user,
            tour_id,
            quantity,
            MAX_QUANTITY,
        )
    return tour


async def set_quantity(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, tour_id: int, quantity: int
) -> str | None:
    """Возвращает текст ошибки или None, если всё в порядке."""
    if get_tour(tour_id) is None:
        return "Такого тура нет в каталоге."
    if quantity < 1:
        return "Количество должно быть не меньше 1. Удалите позицию, если она не нужна."
    if quantity > MAX_QUANTITY:
        return f"Максимум {MAX_QUANTITY} шт. одного тура."
    pool = get_pool(context)
    if pool is None:
        cart = _memory(context)
        if tour_id not in cart:
            return "Этой позиции нет в корзине."
        cart[tour_id] = quantity
        return None
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE cart_items SET quantity = $1
            WHERE product_id = $2
              AND user_id = (SELECT id FROM users WHERE telegram_id = $3)
            """,
            quantity,
            tour_id,
            user_id,
        )
    if result == "UPDATE 0":
        return "Этой позиции нет в корзине."
    return None


async def change_quantity(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, tour_id: int, delta: int
) -> str | None:
    current = (await items(context, user_id)).get(tour_id)
    if current is None:
        return "Этой позиции нет в корзине."
    return await set_quantity(context, user_id, tour_id, current + delta)


async def remove_item(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, tour_id: int
) -> bool:
    pool = get_pool(context)
    if pool is None:
        return _memory(context).pop(tour_id, None) is not None
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM cart_items
            WHERE product_id = $1
              AND user_id = (SELECT id FROM users WHERE telegram_id = $2)
            """,
            tour_id,
            user_id,
        )
    return result == "DELETE 1"


async def clear(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    pool = get_pool(context)
    if pool is None:
        _memory(context).clear()
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM cart_items
            WHERE user_id = (SELECT id FROM users WHERE telegram_id = $1)
            """,
            user_id,
        )


def format_cart(cart: dict[int, int]) -> tuple[str, int]:
    """Текст корзины и итоговая сумма."""
    if not cart:
        return "Корзина пуста. Откройте каталог и добавьте тур.", 0

    lines = ["Ваша корзина:"]
    total = 0
    for tour_id, quantity in cart.items():
        tour = get_tour(tour_id)
        if tour is None:
            continue
        subtotal = tour.price * quantity
        total += subtotal
        lines.append(
            f"• {tour.title}\n"
            f"  {quantity} × {format_price(tour.price)} = {format_price(subtotal)}"
        )
    lines.append(f"\nИтого: {format_price(total)}")
    return "\n".join(lines), total
