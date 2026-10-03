"""Список заявок текущего пользователя из PostgreSQL."""

from telegram import Update
from telegram.ext import ContextTypes

from src.data.tours import format_price
from src.db import get_pool
from src.handlers.checkout import DB_REQUIRED
from src.keyboards import main_menu
from src.services import orders as orders_service


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
    chunks = [format_order(order) for order in orders]
    await update.message.reply_text(
        "Ваши заявки:\n\n" + "\n\n".join(chunks),
        reply_markup=main_menu(),
    )
