"""Заказы в PostgreSQL. Корзина пока остаётся в памяти сессии."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import asyncpg
from telegram import User

from src.data.tours import get_tour

PICKUP_LABELS = {
    "email_voucher": "Ваучер на e-mail",
    "office": "Документы в офисе",
    "airport_meet": "Встреча в аэропорту",
}

STATUS_LABELS = {
    "new": "новая",
    "confirmed": "подтверждена",
    "in_progress": "в работе",
    "completed": "выполнена",
    "cancelled": "отменена",
}

PAYMENT_NOTE = "pay_on_receipt"
PAYMENT_LABEL = "оплата при получении"


class EmptyCartError(Exception):
    """Корзина пуста: нечего оформлять (или заявка уже оформлена)."""


@dataclass(frozen=True)
class OrderItemView:
    title: str
    quantity: int
    price: int


@dataclass(frozen=True)
class OrderView:
    public_number: str
    status: str
    recipient_name: str
    pickup_method: str
    total_amount: int
    created_at: datetime
    items: tuple[OrderItemView, ...]


def pickup_label(code: str) -> str:
    return PICKUP_LABELS.get(code, code)


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def _money(value: Decimal | int | float | str) -> int:
    return int(Decimal(str(value)))


async def _user_id(conn: asyncpg.Connection, telegram_user: User) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id, username, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username,
            display_name = EXCLUDED.display_name
        RETURNING id
        """,
        telegram_user.id,
        telegram_user.username,
        telegram_user.full_name,
    )
    return int(row["id"])


async def create_order(
    pool: asyncpg.Pool,
    telegram_user: User,
    recipient_name: str,
    pickup_method: str,
) -> OrderView:
    """Создаёт заявку из корзины пользователя, которая лежит в БД.

    Корзина забирается в той же транзакции (DELETE ... RETURNING), поэтому
    повторное подтверждение не создаст вторую заявку: корзина уже пуста.
    """
    if pickup_method not in PICKUP_LABELS:
        raise ValueError("Неизвестный способ получения документов.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            db_user_id = await _user_id(conn, telegram_user)
            cart_rows = await conn.fetch(
                "DELETE FROM cart_items WHERE user_id = $1 RETURNING product_id, quantity",
                db_user_id,
            )
            if not cart_rows:
                raise EmptyCartError("Корзина пуста.")

            prepared: list[tuple[int, str, int, int]] = []
            total = 0
            for row in sorted(cart_rows, key=lambda r: r["product_id"]):
                tour = get_tour(int(row["product_id"]))
                quantity = int(row["quantity"])
                if tour is None:
                    raise ValueError("В корзине есть тур, которого нет в каталоге.")
                if quantity < 1:
                    raise ValueError("Количество должно быть больше нуля.")
                prepared.append((tour.id, tour.title, tour.price, quantity))
                total += tour.price * quantity

            temp_number = f"tmp-{telegram_user.id}-{int(datetime.now().timestamp() * 1000)}"
            order = await conn.fetchrow(
                """
                INSERT INTO orders (
                    public_number, user_id, status, recipient_name,
                    pickup_method, payment_note, total_amount
                )
                VALUES ($1, $2, 'new', $3, $4, $5, $6)
                RETURNING id, created_at
                """,
                temp_number,
                db_user_id,
                recipient_name,
                pickup_method,
                PAYMENT_NOTE,
                total,
            )
            public_number = f"VT-{int(order['id']):06d}"
            await conn.execute(
                "UPDATE orders SET public_number = $1 WHERE id = $2",
                public_number,
                order["id"],
            )
            item_views: list[OrderItemView] = []
            for product_id, title, price, quantity in prepared:
                await conn.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_id, title_snapshot, price_snapshot, quantity
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    order["id"],
                    product_id,
                    title,
                    price,
                    quantity,
                )
                item_views.append(
                    OrderItemView(title=title, quantity=quantity, price=price)
                )

    return OrderView(
        public_number=public_number,
        status="new",
        recipient_name=recipient_name,
        pickup_method=pickup_method,
        total_amount=total,
        created_at=order["created_at"],
        items=tuple(item_views),
    )


async def list_orders(pool: asyncpg.Pool, telegram_id: int) -> list[OrderView]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                o.public_number,
                o.status,
                o.recipient_name,
                o.pickup_method,
                o.total_amount,
                o.created_at,
                COALESCE(
                    json_agg(
                        json_build_object(
                            'title', oi.title_snapshot,
                            'quantity', oi.quantity,
                            'price', oi.price_snapshot
                        )
                        ORDER BY oi.id
                    ) FILTER (WHERE oi.id IS NOT NULL),
                    '[]'::json
                ) AS items
            FROM orders o
            JOIN users u ON u.id = o.user_id
            LEFT JOIN order_items oi ON oi.order_id = o.id
            WHERE u.telegram_id = $1
            GROUP BY o.id
            ORDER BY o.created_at DESC
            """,
            telegram_id,
        )

    result: list[OrderView] = []
    for row in rows:
        raw_items = row["items"]
        if raw_items is None:
            raw_items = []
        elif isinstance(raw_items, str):
            raw_items = json.loads(raw_items)
        items = tuple(
            OrderItemView(
                title=item["title"],
                quantity=int(item["quantity"]),
                price=_money(item["price"]),
            )
            for item in raw_items
        )
        result.append(
            OrderView(
                public_number=row["public_number"],
                status=row["status"],
                recipient_name=row["recipient_name"],
                pickup_method=row["pickup_method"],
                total_amount=_money(row["total_amount"]),
                created_at=row["created_at"],
                items=items,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Кабинет менеджера
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("confirmed", "cancelled"),
    "confirmed": ("in_progress", "cancelled"),
    "in_progress": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

_ORDER_SELECT = """
    SELECT
        o.public_number,
        o.status,
        o.recipient_name,
        o.pickup_method,
        o.total_amount,
        o.created_at,
        u.telegram_id,
        COALESCE(
            json_agg(
                json_build_object(
                    'title', oi.title_snapshot,
                    'quantity', oi.quantity,
                    'price', oi.price_snapshot
                )
                ORDER BY oi.id
            ) FILTER (WHERE oi.id IS NOT NULL),
            '[]'::json
        ) AS items
    FROM orders o
    JOIN users u ON u.id = o.user_id
    LEFT JOIN order_items oi ON oi.order_id = o.id
    {where}
    GROUP BY o.id, u.telegram_id
    ORDER BY o.created_at {direction}
"""


def _row_to_view(row) -> OrderView:
    raw_items = row["items"]
    if raw_items is None:
        raw_items = []
    elif isinstance(raw_items, str):
        raw_items = json.loads(raw_items)
    items = tuple(
        OrderItemView(
            title=item["title"],
            quantity=int(item["quantity"]),
            price=_money(item["price"]),
        )
        for item in raw_items
    )
    return OrderView(
        public_number=row["public_number"],
        status=row["status"],
        recipient_name=row["recipient_name"],
        pickup_method=row["pickup_method"],
        total_amount=_money(row["total_amount"]),
        created_at=row["created_at"],
        items=items,
    )


async def list_by_status(pool: asyncpg.Pool, status: str) -> list[OrderView]:
    query = _ORDER_SELECT.format(where="WHERE o.status = $1", direction="ASC")
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, status)
    return [_row_to_view(row) for row in rows]


async def get_order(
    pool: asyncpg.Pool, public_number: str
) -> tuple[OrderView, int] | None:
    """Заказ и Telegram ID клиента. Для менеджера."""
    query = _ORDER_SELECT.format(where="WHERE o.public_number = $1", direction="ASC")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, public_number)
    if row is None:
        return None
    return _row_to_view(row), int(row["telegram_id"])


async def change_status(
    pool: asyncpg.Pool, public_number: str, new_status: str
) -> tuple[str | None, int | None]:
    """Возвращает (текст ошибки или None, Telegram ID клиента)."""
    if new_status not in STATUS_LABELS:
        return "Неизвестный статус.", None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT o.id, o.status, u.telegram_id
                FROM orders o JOIN users u ON u.id = o.user_id
                WHERE o.public_number = $1
                FOR UPDATE OF o
                """,
                public_number,
            )
            if row is None:
                return "Заявка не найдена.", None
            current = row["status"]
            if new_status not in ALLOWED_TRANSITIONS.get(current, ()):
                return (
                    f"Нельзя перевести из «{status_label(current)}» "
                    f"в «{status_label(new_status)}». Откройте заявку заново.",
                    None,
                )
            await conn.execute(
                "UPDATE orders SET status = $1, updated_at = NOW() WHERE id = $2",
                new_status,
                row["id"],
            )
    return None, int(row["telegram_id"])
