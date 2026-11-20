"""Кнопки имитатора. Владение заказом проверяет сервис при каждом действии."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.db import get_pool
from src.services import payments
from src.services import inventory

logger = logging.getLogger(__name__)


def payment_keyboard(number, payment_status=None):
    buttons = []
    if payment_status != "canceled":
        buttons.append(
            [
                InlineKeyboardButton(
                    "🧪 Учебная оплата", callback_data=f"pay:start:{number}"
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                "Отменить заказ и освободить места",
                callback_data=f"pay:release:{number}",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


def catalog_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Выбрать новый тур", callback_data="tour:list")]]
    )


async def on_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or not query.data or update.effective_user is None:
        return
    pool = get_pool(context)
    if pool is None:
        await query.answer(
            "База временно недоступна. Попробуйте позже.", show_alert=True
        )
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    action, identifier = parts[1:]
    try:
        if action == "release":
            await inventory.cancel_order(pool, update.effective_user.id, identifier)
            await query.answer("Заказ отменён; места освобождены.")
            await query.edit_message_text(
                "Места освобождены. Для новой покупки выберите тур в каталоге.",
                reply_markup=catalog_button(),
            )
            return
        if action == "start":
            payment = await payments.start_payment(
                pool, update.effective_user.id, identifier
            )
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Имитировать успешную оплату",
                            callback_data=f"pay:success:{payment.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить учебный платёж",
                            callback_data=f"pay:cancel:{payment.id}",
                        )
                    ],
                ]
            )
            text = (
                f"{payments.DEMO_LABEL}\nЗаявка {payment.public_number}\n"
                f"Платёж #{payment.id}: pending\nСумма: {payments.format_kopecks(payment.amount_kopecks)}\n"
                "Реальные деньги не списываются. Подтверждение после срока резерва означает возврат. Выберите исход имитации."
            )
        elif action in ("success", "cancel") and identifier.isdigit():
            func = (
                payments.simulate_success
                if action == "success"
                else payments.simulate_cancel
            )
            payment = await func(pool, update.effective_user.id, int(identifier))
            text = (
                f"{payments.DEMO_LABEL}\nЗаявка {payment.public_number}\n"
                f"Платёж #{payment.id}: {payment.status}\n"
                f"Сумма: {payments.format_kopecks(payment.amount_kopecks)}\n"
                + (
                    "Резерв закончился, оформляется возврат."
                    if payment.order_status == "payment_conflict"
                    else "Повторное событие: поступление уже учтено."
                    if payment.already_receipt
                    else "Учебная оплата подтверждена."
                    if action == "success"
                    else "Платёж отменён. Чтобы оформить новую покупку, освободите места и выберите тур заново."
                )
            )
            markup = (
                payment_keyboard(payment.public_number, payment.status)
                if action == "cancel" and payment.order_status == "awaiting_payment"
                else catalog_button()
                if action == "cancel"
                else None
            )
        else:
            raise payments.PaymentError("Неизвестное действие.")
    except (payments.PaymentError, inventory.StockError) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.warning("Не удалось обработать учебный платёж")
        await query.answer(
            "Сервер недоступен. Проверьте «Мои заказы» и повторите.", show_alert=True
        )
        return
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
