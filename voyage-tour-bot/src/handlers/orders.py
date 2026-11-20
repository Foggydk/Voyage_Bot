"""Список заявок текущего пользователя из PostgreSQL."""

from telegram import Update
from telegram.ext import ContextTypes

from src.data.tours import format_price
from src.db import get_pool
from src.handlers.checkout import DB_REQUIRED
from src.keyboards import main_menu
from src.services import orders as orders_service
from src.handlers.payments import payment_keyboard
from datetime import datetime, timezone


def format_order(order: orders_service.OrderView) -> str:
    lines = [
        f"Заявка {order.public_number}",
        f"Статус: {orders_service.status_label(order.status)}",
        f"Имя: {order.recipient_name}",
        f"Получение: {orders_service.pickup_label(order.pickup_method)}",
        f"Оплата: {orders_service.PAYMENT_LABEL}",
        "Состав:",
    ]
    for item in order.items:
        subtotal = item.price * item.quantity
        lines.append(
            f"• {item.title} — {item.quantity} × {format_price(item.price)} "
            f"= {format_price(subtotal)}"
        )
    lines.append(f"Итого: {format_price(order.total_amount)}")
    if order.expires_at is not None and order.reservation_status == "active":
        remaining = max(
            0, int((order.expires_at - datetime.now(timezone.utc)).total_seconds())
        )
        lines.append(
            f"Резерв: осталось {remaining} сек. Подтверждение после срока — полный учебный возврат."
        )
    if order.status == "payment_conflict":
        lines.append(
            "Резерв закончился, оформляется возврат. Места повторно не резервируются."
        )
    return "\n".join(lines)


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    pool = get_pool(context)
    if pool is None:
        await update.message.reply_text(DB_REQUIRED, reply_markup=main_menu())
        return
    orders = await orders_service.list_orders(pool, update.effective_user.id)
    if not orders:
        await update.message.reply_text(
            "Заявок пока нет. Добавьте тур в корзину и оформите заказ.",
            reply_markup=main_menu(),
        )
        return
    for order in orders:
        await update.message.reply_text(
            format_order(order),
            reply_markup=payment_keyboard(order.public_number, order.payment_status)
            if order.status == "awaiting_payment"
            else None,
        )
