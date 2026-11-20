"""Учебный платёж: блокировка заказа → платежа, атомарный журнал и outbox."""

from dataclasses import dataclass

from src.config import get_manager_ids
from src.services.money import format_kopecks, to_kopecks
from src.services import inventory

CURRENCY = "RUB"
PROVIDER = "simulator"
DEMO_LABEL = "Демонстрация без платёжного провайдера"
rubles_to_kopecks = to_kopecks


class PaymentError(ValueError):
    def __init__(self, message: str, *, alert: bool = True):
        super().__init__(message)
        self.message = message
        self.alert = alert


@dataclass(frozen=True)
class PaymentView:
    id: int
    order_id: int
    public_number: str
    amount_kopecks: int
    currency: str
    status: str
    order_status: str
    already_receipt: bool = False


def _view(payment, order, repeated=False):
    return PaymentView(
        payment["id"],
        order["id"],
        order["public_number"],
        payment["amount_kopecks"],
        payment["currency"],
        payment["status"],
        order["status"],
        repeated,
    )


async def _lock_order(conn, number):
    row = await conn.fetchrow(
        """
        SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id
        WHERE o.public_number=$1 FOR UPDATE OF o
    """,
        number,
    )
    if row is None:
        raise PaymentError("Заявка не найдена.")
    return dict(row)


async def _lock_payment(conn, payment_id):
    number = await conn.fetchval(
        """
        SELECT o.public_number FROM payments p JOIN orders o ON o.id=p.order_id
        WHERE p.id=$1
    """,
        payment_id,
    )
    if number is None:
        raise PaymentError("Платёж не найден.")
    order = await _lock_order(conn, number)
    payment = dict(
        await conn.fetchrow("SELECT * FROM payments WHERE id=$1 FOR UPDATE", payment_id)
    )
    return order, payment


def _owner(order, telegram_id):
    if order["telegram_id"] != telegram_id:
        raise PaymentError("Заявка не найдена или недоступна.")


def _validate(payment, order, amount, currency):
    expected = to_kopecks(order["total_amount"])
    if (
        type(amount) is not int
        or amount <= 0
        or amount != expected
        or payment["amount_kopecks"] != expected
    ):
        raise PaymentError("Сумма платежа не совпадает с заявкой.")
    if currency != CURRENCY or payment["currency"] != CURRENCY:
        raise PaymentError("Валюта платежа должна быть RUB.")
    if payment["provider"] != PROVIDER:
        raise PaymentError("Этот платёж нельзя подтвердить имитатором.")


async def start_payment(pool, telegram_id, public_number) -> PaymentView:
    async with pool.acquire() as conn, conn.transaction():
        order = await _lock_order(conn, public_number)
        _owner(order, telegram_id)
        if order["status"] != "awaiting_payment":
            raise PaymentError("Оплатить можно только заявку, ожидающую оплаты.")
        if await inventory.lock_reservation(conn, order["id"]) is None:
            # Неоплаченная заявка прежней версии: резерв создаётся при запуске оплаты.
            rows = await conn.fetch(
                "SELECT product_id,quantity FROM order_items WHERE order_id=$1",
                order["id"],
            )
            if not rows or any(r["product_id"] is None for r in rows):
                raise PaymentError("Невозможно зарезервировать позиции старой заявки.")
            items = {}
            for r in rows:
                items[r["product_id"]] = items.get(r["product_id"], 0) + r["quantity"]
            await inventory.check_available(conn, items)
            await inventory.reserve(conn, order["id"], items, telegram_id)
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE order_id=$1 AND status='pending'", order["id"]
        )
        if payment is None:
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM payments WHERE order_id=$1)", order["id"]
            ):
                raise PaymentError(
                    "Попытка оплаты уже завершена. Для новой покупки оформите новый заказ."
                )
            amount = to_kopecks(order["total_amount"])
            if amount <= 0:
                raise PaymentError("Сумма заказа должна быть положительной.")
            payment = await conn.fetchrow(
                """
                INSERT INTO payments (order_id, amount_kopecks, currency, status, provider)
                VALUES ($1,$2,'RUB','pending','simulator') RETURNING *
            """,
                order["id"],
                amount,
            )
        _validate(payment, order, payment["amount_kopecks"], payment["currency"])
        return _view(payment, order)


async def confirm_successful_payment(
    conn, payment_id, telegram_id, *, amount_kopecks=None, currency=CURRENCY, now=None
):
    """Вызывать внутри транзакции. Подтверждённое событие — источник поступления."""
    order, payment = await _lock_payment(conn, payment_id)
    _owner(order, telegram_id)
    _validate(
        payment,
        order,
        payment["amount_kopecks"] if amount_kopecks is None else amount_kopecks,
        currency,
    )
    receipt = await conn.fetchrow(
        "SELECT * FROM finance_operations WHERE order_id=$1 AND type='receipt'",
        order["id"],
    )
    if receipt is not None:
        if receipt["payment_id"] != payment_id or payment["status"] not in (
            "succeeded",
            "refund_pending",
            "refund_succeeded",
        ):
            raise PaymentError("Заказ уже оплачен другим платежом.")
        return _view(payment, order, True)
    if payment["status"] != "pending" or order["status"] not in (
        "awaiting_payment",
        "expired",
        "cancelled",
    ):
        raise PaymentError("Платёж отменён или заказ больше не ожидает оплаты.")
    accepted_sale = await inventory.accept_sale(conn, order, now=now)
    if accepted_sale is None:
        # Старый уже отправленный платёж без резерва нельзя превращать в продажу мест.
        accepted_sale = False
    items = await conn.fetch(
        "SELECT cost_snapshot, quantity FROM order_items WHERE order_id=$1", order["id"]
    )
    if not items or any(i["cost_snapshot"] is None for i in items):
        raise PaymentError(
            "У заказа отсутствует себестоимость. Обратитесь к менеджеру."
        )
    cost = (
        0
        if accepted_sale is False
        else sum(to_kopecks(i["cost_snapshot"]) * i["quantity"] for i in items)
    )
    operation_id = await conn.fetchval(
        """
        INSERT INTO finance_operations
            (type, amount_kopecks, cost_kopecks, order_id, payment_id, description, author_telegram_id, external_event_id)
        VALUES ('receipt',$1,$2,$3,$4,$5,$6,$7) RETURNING id
    """,
        payment["amount_kopecks"],
        cost,
        order["id"],
        payment_id,
        f"Оплата заявки {order['public_number']}",
        telegram_id,
        f"simulator:payment:{payment_id}:succeeded",
    )
    await conn.execute(
        "UPDATE payments SET status='succeeded', provider_payment_id=$2, confirmed_at=NOW() WHERE id=$1",
        payment_id,
        f"sim-{payment_id}",
    )
    next_status = "payment_conflict" if accepted_sale is False else "paid"
    await conn.execute(
        "UPDATE orders SET status=$2, updated_at=NOW() WHERE id=$1",
        order["id"],
        next_status,
    )
    if accepted_sale is False:
        await conn.execute(
            "UPDATE payments SET status='refund_pending' WHERE id=$1", payment_id
        )
        await conn.execute(
            "INSERT INTO refund_jobs(payment_id,operation_key) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            payment_id,
            f"refund:{payment_id}",
        )
    text = (
        f"{DEMO_LABEL}\nЗаявка {order['public_number']}: {next_status}.\n"
        f"Поступление: {format_kopecks(payment['amount_kopecks'])}.\nПлатёж #{payment_id}."
    )
    for manager_id in get_manager_ids():
        await conn.execute(
            "INSERT INTO notification_outbox (operation_id,chat_id,text) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            operation_id,
            manager_id,
            text,
        )
    payment["status"], order["status"] = (
        ("refund_pending" if accepted_sale is False else "succeeded"),
        next_status,
    )
    return _view(payment, order)


async def simulate_success(pool, telegram_id, payment_id, **event):
    async with pool.acquire() as conn, conn.transaction():
        return await confirm_successful_payment(conn, payment_id, telegram_id, **event)


async def simulate_cancel(pool, telegram_id, payment_id):
    async with pool.acquire() as conn, conn.transaction():
        order, payment = await _lock_payment(conn, payment_id)
        _owner(order, telegram_id)
        if payment["status"] not in ("pending", "canceled"):
            raise PaymentError("Завершённый платёж отменить нельзя.")
        await conn.execute(
            "UPDATE payments SET status='canceled' WHERE id=$1", payment_id
        )
        payment["status"] = "canceled"
        return _view(payment, order)


def require_manager(telegram_id):
    if telegram_id not in get_manager_ids():
        raise PermissionError("Финансовый кабинет доступен только менеджеру.")


async def start_refund(pool, telegram_id, public_number):
    require_manager(telegram_id)
    async with pool.acquire() as conn, conn.transaction():
        order = await _lock_order(conn, public_number)
        receipt = await conn.fetchrow(
            "SELECT * FROM finance_operations WHERE order_id=$1 AND type='receipt'",
            order["id"],
        )
        if receipt is None:
            raise PaymentError("Возврат доступен только после успешной оплаты.")
        payment = dict(
            await conn.fetchrow(
                "SELECT * FROM payments WHERE id=$1 FOR UPDATE", receipt["payment_id"]
            )
        )
        if payment["status"] == "refund_succeeded":
            raise PaymentError("Полный возврат уже выполнен.")
        if payment["status"] not in ("succeeded", "refund_pending"):
            raise PaymentError("Платёж не готов к возврату.")
        await conn.execute(
            "UPDATE payments SET status='refund_pending' WHERE id=$1", payment["id"]
        )
        await conn.execute(
            """
            INSERT INTO refund_jobs(payment_id,operation_key,next_attempt_at) VALUES ($1,$2,NOW()+INTERVAL '15 seconds')
            ON CONFLICT (payment_id) DO UPDATE SET status='pending',next_attempt_at=NOW()+INTERVAL '15 seconds'
        """,
            payment["id"],
            f"refund:{payment['id']}",
        )
        payment["status"] = "refund_pending"
        return _view(payment, order)


async def finish_refund(pool, telegram_id, payment_id, *, succeeded):
    require_manager(telegram_id)
    async with pool.acquire() as conn, conn.transaction():
        if not succeeded:
            order, payment = await _lock_payment(conn, payment_id)
            if payment["status"] != "refund_pending":
                raise PaymentError("Нет ожидающего возврата.")
            await conn.execute(
                "UPDATE refund_jobs SET status='pending',last_error='Учебная ошибка возврата',next_attempt_at=NOW()+INTERVAL '15 seconds' WHERE payment_id=$1",
                payment_id,
            )
            return _view(payment, order)
    # Сначала подтверждение учебного провайдера, затем локальный журнал.
    from src.services.refunds import simulator_refund

    await simulator_refund(pool, payment_id, f"refund:{payment_id}")
    async with pool.acquire() as conn, conn.transaction():
        return await record_confirmed_refund(
            conn, payment_id, telegram_id, allow_repeat=False
        )


async def record_confirmed_refund(conn, payment_id, author, *, allow_repeat=True):
    order, payment = await _lock_payment(conn, payment_id)
    if payment["status"] == "refund_succeeded" and allow_repeat:
        return _view(payment, order, True)
    if payment["status"] != "refund_pending":
        raise PaymentError(
            "Нет ожидающего подтверждения возврата. Повторный возврат запрещён."
        )
    if not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM simulator_refunds WHERE payment_id=$1 AND operation_key=$2)",
        payment_id,
        f"refund:{payment_id}",
    ):
        raise PaymentError("Провайдер ещё не подтвердил возврат.")
    receipt = await conn.fetchrow(
        "SELECT * FROM finance_operations WHERE order_id=$1 AND type='receipt' AND payment_id=$2",
        order["id"],
        payment_id,
    )
    if receipt is None:
        raise PaymentError("Поступление не найдено.")
    _validate(payment, order, receipt["amount_kopecks"], receipt["currency"])
    await inventory.return_sale(conn, order["id"], author or "refund-worker")
    await conn.execute(
        """
        INSERT INTO finance_operations(type,amount_kopecks,cost_kopecks,order_id,payment_id,description,author_telegram_id,external_event_id)
        VALUES ('refund',$1,$2,$3,$4,$5,$6,$7)
    """,
        receipt["amount_kopecks"],
        receipt["cost_kopecks"],
        order["id"],
        payment_id,
        f"Полный учебный возврат {order['public_number']}",
        author,
        f"simulator:refund:{payment_id}:succeeded",
    )
    await conn.execute(
        "UPDATE payments SET status='refund_succeeded' WHERE id=$1", payment_id
    )
    await conn.execute(
        "UPDATE orders SET status='refunded',updated_at=NOW() WHERE id=$1", order["id"]
    )
    await conn.execute(
        "UPDATE refund_jobs SET status='succeeded',last_error=NULL,lease_until=NULL WHERE payment_id=$1",
        payment_id,
    )
    payment["status"], order["status"] = "refund_succeeded", "refunded"
    return _view(payment, order)
