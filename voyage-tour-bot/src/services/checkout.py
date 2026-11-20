"""Повторяемое оформление: сохранённая заявка запроса и атомарный резерв всего состава."""

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

import asyncpg

from src.services import inventory


def _key(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise ValueError("Некорректный ключ оформления.") from None


def _payload(name, pickup, items):
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 80:
        raise ValueError("Имя должно содержать от 2 до 80 символов.")
    if pickup not in ("email_voucher", "office", "airport_meet"):
        raise ValueError("Неизвестный способ получения.")
    try:
        cart = {int(k): v for k, v in items.items()}
        if not cart or any(
            k <= 0 or type(v) is not int or not 1 <= v <= 10000 for k, v in cart.items()
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Корзина пуста или содержит некорректное количество."
        ) from None
    payload = {
        "recipient_name": name.strip(),
        "pickup_method": pickup,
        "items": {str(k): v for k, v in sorted(cart.items())},
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return payload, fingerprint


async def prepare_checkout(
    pool, user, name, pickup, *, request_id=None, expected_cart=None
):
    from src.services.orders import _user_id, EmptyCartError

    key = _key(request_id or uuid4())
    async with pool.acquire() as conn, conn.transaction():
        uid = await _user_id(conn, user)
        existing = await conn.fetchrow(
            "SELECT * FROM checkout_requests WHERE telegram_id=$1 AND request_key=$2 FOR UPDATE",
            user.id,
            key,
        )
        rows = await conn.fetch(
            "SELECT product_id,quantity FROM cart_items WHERE user_id=$1 ORDER BY product_id",
            uid,
        )
        current = {r["product_id"]: r["quantity"] for r in rows}
        if expected_cart is None:
            if not current and existing:
                expected_cart = json.loads(existing["payload"])["items"]
            else:
                expected_cart = current
        if not expected_cart:
            raise EmptyCartError("Корзина пуста.")
        payload, fingerprint = _payload(name, pickup, expected_cart)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError(
                    "Этот ключ оформления уже использован для другого состава или получателя."
                )
        else:
            if {int(k): v for k, v in payload["items"].items()} != current:
                raise ValueError("Корзина изменилась. Начните новое оформление.")
            await conn.execute(
                """
                INSERT INTO checkout_requests(telegram_id,request_key,fingerprint,payload) VALUES ($1,$2,$3,$4::jsonb)
            """,
                user.id,
                key,
                fingerprint,
                json.dumps(payload, ensure_ascii=False),
            )
    return str(key)


async def _complete(pool, user, request_id):
    from src.services.orders import _row_to_view, _ORDER_SELECT

    error = None
    async with pool.acquire() as conn, conn.transaction():
        # Сначала пользователь, затем запрос: порядок одинаков у prepare и complete.
        uid = await conn.fetchval(
            "SELECT id FROM users WHERE telegram_id=$1 FOR UPDATE", user.id
        )
        request = await conn.fetchrow(
            "SELECT * FROM checkout_requests WHERE telegram_id=$1 AND request_key=$2 FOR UPDATE",
            user.id,
            _key(request_id),
        )
        if request is None:
            raise ValueError("Оформление не найдено. Откройте корзину.")
        if request["error"]:
            raise inventory.StockError(request["error"])
        order_id = request["order_id"]
        if order_id is None:
            payload = json.loads(request["payload"])
            items = {int(k): v for k, v in payload["items"].items()}
            rows = await conn.fetch(
                "SELECT product_id,quantity FROM cart_items WHERE user_id=$1 ORDER BY product_id FOR UPDATE",
                uid,
            )
            if {r["product_id"]: r["quantity"] for r in rows} != items:
                raise ValueError(
                    "Корзина изменилась. Начните оформление заново с новым ключом."
                )
            try:
                await inventory.check_available(conn, items)
            except inventory.StockError as exc:
                error = str(exc)
                await conn.execute(
                    "UPDATE checkout_requests SET error=$3 WHERE telegram_id=$1 AND request_key=$2",
                    user.id,
                    _key(request_id),
                    error,
                )
            if error is None:
                products = await conn.fetch(
                    "SELECT * FROM products WHERE id=ANY($1::bigint[]) AND is_active ORDER BY id FOR SHARE",
                    sorted(items),
                )
                if len(products) != len(items) or any(
                    p["cost_price"] is None for p in products
                ):
                    raise ValueError("Каталог изменился или отсутствует себестоимость.")
                total = sum(p["price"] * items[p["id"]] for p in products)
                row = await conn.fetchrow(
                    """
                    INSERT INTO orders(public_number,user_id,status,recipient_name,pickup_method,payment_note,total_amount)
                    VALUES ($1,$2,'awaiting_payment',$3,$4,'awaiting_demo_payment',$5) RETURNING id
                """,
                    "tmp-" + str(uuid4()),
                    uid,
                    payload["recipient_name"],
                    payload["pickup_method"],
                    total,
                )
                order_id = row["id"]
                await conn.execute(
                    "UPDATE orders SET public_number=$2 WHERE id=$1",
                    order_id,
                    f"VT-{order_id:06d}",
                )
                for p in products:
                    await conn.execute(
                        """
                        INSERT INTO order_items(order_id,product_id,title_snapshot,price_snapshot,cost_snapshot,quantity)
                        VALUES ($1,$2,$3,$4,$5,$6)
                    """,
                        order_id,
                        p["id"],
                        p["title"],
                        p["price"],
                        p["cost_price"],
                        items[p["id"]],
                    )
                await inventory.reserve(conn, order_id, items, user.id)
                from src.services.demo_faults import checkpoint

                await checkpoint(conn, "checkout_before_commit")
                await conn.execute("DELETE FROM cart_items WHERE user_id=$1", uid)
                await conn.execute(
                    "UPDATE checkout_requests SET order_id=$3 WHERE telegram_id=$1 AND request_key=$2",
                    user.id,
                    _key(request_id),
                    order_id,
                )
        if error is None:
            row = await conn.fetchrow(
                _ORDER_SELECT.format(where="WHERE o.id=$1", direction="ASC"), order_id
            )
    if error:
        raise inventory.StockError(error)
    return _row_to_view(row)


async def complete_checkout(pool, user, request_id):
    for attempt in range(3):
        try:
            return await _complete(pool, user, request_id)
        except (asyncpg.DeadlockDetectedError, asyncpg.SerializationError):
            if attempt == 2:
                raise ValueError(
                    "Сервер занят. Повторите оформление с тем же ключом."
                ) from None
            await asyncio.sleep(0.05 * (attempt + 1))


async def create_order(
    pool, user, name, pickup, *, request_id=None, expected_cart=None
):
    key = await prepare_checkout(
        pool, user, name, pickup, request_id=request_id, expected_cart=expected_cart
    )
    return await complete_checkout(pool, user, key)
