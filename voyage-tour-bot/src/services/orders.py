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
    cart_items: dict[int, int],
) -> OrderView:
    if pickup_method not in PICKUP_LABELS:
        raise ValueError("Неизвестный способ получения документов.")
    if not cart_items:
        raise ValueError("Корзина пуста.")

    prepared: list[tuple[int, str, int, int]] = []
    total = 0
    for tour_id, quantity in cart_items.items():
        tour = get_tour(tour_id)
        if tour is None:
            raise ValueError("В корзине есть тур, которого нет в каталоге.")
        if quantity < 1:
            raise ValueError("Количество должно быть больше нуля.")
        prepared.append((tour.id, tour.title, tour.price, quantity))
        total += tour.price * quantity

    async with pool.acquire() as conn:
        async with conn.transaction():
            db_user_id = await _user_id(conn, telegram_user)
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
