"""Заказы в PostgreSQL. Корзина и заказы сохраняются в общей базе."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import asyncpg
from telegram import User


PICKUP_LABELS = {
    "email_voucher": "Ваучер на e-mail",
    "office": "Документы в офисе",
    "airport_meet": "Встреча в аэропорту",
}

STATUS_LABELS = {
    "new": "новая",
    "awaiting_payment": "ожидает оплаты",
    "paid": "оплачена",
    "confirmed": "подтверждена",
    "in_progress": "в работе",
    "completed": "выполнена",
    "cancelled": "отменена",
    "refunded": "возврат",
    "expired": "срок резерва истёк",
    "payment_conflict": "поздняя оплата, оформляется возврат",
}

PAYMENT_NOTE = "awaiting_demo_payment"
PAYMENT_LABEL = "Демонстрация без платёжного провайдера"


class EmptyCartError(ValueError):
    """Корзина пуста: нечего оформлять (или заявка уже оформлена)."""


@dataclass(frozen=True)
class OrderItemView:
    title: str
    quantity: int
    price: Decimal
    cost: Decimal | None = None


@dataclass(frozen=True)
class OrderView:
    id: int
    public_number: str
    status: str
    recipient_name: str
    pickup_method: str
    total_amount: Decimal
    created_at: datetime
    items: tuple[OrderItemView, ...]
    expires_at: datetime | None = None
    reservation_status: str | None = None
    payment_status: str | None = None


def pickup_label(code: str) -> str:
    return PICKUP_LABELS.get(code, code)


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value))


def _optional_money(value) -> Decimal | None:
    if value is None:
        return None
    return _money(value)


def _item_view(item: dict) -> OrderItemView:
    return OrderItemView(
        title=item["title"],
        quantity=int(item["quantity"]),
        price=_money(item["price"]),
        cost=_optional_money(item.get("cost")),
    )


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
    pool,
    telegram_user,
    recipient_name,
    pickup_method,
    *,
    request_id=None,
    expected_cart=None,
):
    from src.services.checkout import create_order as checkout

    return await checkout(
        pool,
        telegram_user,
        recipient_name,
        pickup_method,
        request_id=request_id,
        expected_cart=expected_cart,
    )


async def list_orders(pool, telegram_id):
    query = _ORDER_SELECT.format(where="WHERE u.telegram_id=$1", direction="DESC")
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, telegram_id)
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Кабинет менеджера
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("cancelled",),
    "awaiting_payment": ("cancelled",),
    "paid": ("confirmed", "in_progress", "completed"),
    "confirmed": ("in_progress", "completed"),
    "in_progress": ("completed",),
    "completed": (),
    "cancelled": (),
    "refunded": (),
}

_ORDER_SELECT = """
    SELECT
        o.id,
        o.public_number,
        o.status,
        o.recipient_name,
        o.pickup_method,
        o.total_amount,
        o.created_at,
        u.telegram_id,
        r.expires_at, r.status AS reservation_status,
        (SELECT p.status FROM payments p WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_status,
        COALESCE(
            json_agg(
                json_build_object(
                    'title', oi.title_snapshot,
                    'quantity', oi.quantity,
                    'price', oi.price_snapshot,
                    'cost', oi.cost_snapshot
                )
                ORDER BY oi.id
            ) FILTER (WHERE oi.id IS NOT NULL),
            '[]'::json
        ) AS items
    FROM orders o
    JOIN users u ON u.id = o.user_id
    LEFT JOIN reservations r ON r.order_id=o.id
    LEFT JOIN order_items oi ON oi.order_id = o.id
    {where}
    GROUP BY o.id, u.telegram_id, r.expires_at, r.status
    ORDER BY o.created_at {direction}
"""


def _row_to_view(row) -> OrderView:
    raw_items = row["items"]
    if raw_items is None:
        raw_items = []
    elif isinstance(raw_items, str):
        raw_items = json.loads(raw_items)
    items = tuple(_item_view(item) for item in raw_items)
    return OrderView(
        id=int(row["id"]),
        public_number=row["public_number"],
        status=row["status"],
        recipient_name=row["recipient_name"],
        pickup_method=row["pickup_method"],
        total_amount=_money(row["total_amount"]),
        created_at=row["created_at"],
        items=items,
        expires_at=row["expires_at"],
        reservation_status=row["reservation_status"],
        payment_status=row["payment_status"],
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
            if new_status == "cancelled" and await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM finance_operations WHERE order_id=$1 AND type='receipt')",
                row["id"],
            ):
                return (
                    "Оплаченный заказ можно только вернуть через финансовый кабинет.",
                    None,
                )
            current = row["status"]
            if new_status not in ALLOWED_TRANSITIONS.get(current, ()):
                return (
                    f"Нельзя перевести из «{status_label(current)}» "
                    f"в «{status_label(new_status)}». Откройте заявку заново.",
                    None,
                )
            if new_status == "cancelled":
                from src.services.inventory import lock_reservation, release

                reservation = await lock_reservation(conn, row["id"])
                await release(conn, reservation, "Отмена менеджером", "manager")
            await conn.execute(
                "UPDATE orders SET status = $1, updated_at = NOW() WHERE id = $2",
                new_status,
                row["id"],
            )
    return None, int(row["telegram_id"])
