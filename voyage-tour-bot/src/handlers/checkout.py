"""Оформление заказа: имя, способ получения документов, запись в PostgreSQL."""

import logging

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.db import get_pool
from src.keyboards import (
    BTN_AI,
    BTN_CART,
    BTN_CATALOG,
    BTN_HELP,
    BTN_ORDERS,
    checkout_confirm_keyboard,
    main_menu,
    pickup_keyboard,
)
from src.services import cart as cart_service
from src.services import orders as orders_service
from src.data.tours import format_price

logger = logging.getLogger(__name__)

ASK_NAME, ASK_PICKUP, CONFIRM = range(3)
NAME_KEY = "checkout_name"
PICKUP_KEY = "checkout_pickup"

ASK_NAME_TEXT = (
    "Оформление заявки.\n\n"
    "Напишите имя, как в документах для ваучера.\n"
    "Отмена: /cancel"
)
NEED_BUTTONS = "Сейчас оформление заявки. Выберите пункт на кнопках или /cancel."
MENU_BUTTONS = {BTN_CATALOG, BTN_AI, BTN_CART, BTN_ORDERS, BTN_HELP}
CANCELLED = "Оформление отменено. Корзина сохранена."
EMPTY_CART_TEXT = "Корзина пуста. Добавьте тур из каталога."
ALREADY_DONE_TEXT = (
    "Корзина пуста — заявку сохранить нельзя. "
    "Если вы уже подтверждали заявку, она в разделе «Мои заказы»."
)
DB_REQUIRED = (
    "Чтобы сохранить заявку, укажите рабочий DATABASE_URL в .env "
    "(пароль PostgreSQL вместо YOUR_PASSWORD) и перезапустите бота."
)


def build_checkout_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_checkout, pattern=r"^checkout:start$"),
        ],
        states={
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name),
            ],
            ASK_PICKUP: [
                CallbackQueryHandler(receive_pickup, pattern=r"^checkout:pickup:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, remind_buttons),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_order, pattern=r"^checkout:ok$"),
                CallbackQueryHandler(start_checkout, pattern=r"^checkout:start$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, remind_buttons),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_checkout),
            CallbackQueryHandler(cancel_checkout, pattern=r"^checkout:cancel$"),
        ],
        allow_reentry=True,
        name="checkout",
    )


def _clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(NAME_KEY, None)
    context.user_data.pop(PICKUP_KEY, None)


async def start_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is not None:
        await query.answer()
    if update.effective_user is None:
        return ConversationHandler.END

    if get_pool(context) is None:
        if query is not None:
            await query.edit_message_text(DB_REQUIRED)
        return ConversationHandler.END

    cart = await cart_service.items(context, update.effective_user.id)
    if not cart:
        if query is not None:
            await query.edit_message_text(EMPTY_CART_TEXT)
        return ConversationHandler.END

    _clear_draft(context)
    if query is not None:
        await query.edit_message_text(ASK_NAME_TEXT)
    return ASK_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or update.message.text is None:
        return ASK_NAME
    name = " ".join(update.message.text.split())
    if name in MENU_BUTTONS:
        await update.message.reply_text(
            "Сейчас оформляем заявку. Напишите имя текстом или /cancel, "
            "чтобы вернуться в меню."
        )
        return ASK_NAME
    if len(name) < 2 or len(name) > 80:
        await update.message.reply_text(
            "Имя должно быть от 2 до 80 символов. Напишите ещё раз или /cancel."
        )
        return ASK_NAME
    context.user_data[NAME_KEY] = name
    await update.message.reply_text(
        f"Имя в заявке: {name}\n\nКак получить документы по туру?",
        reply_markup=pickup_keyboard(),
    )
    return ASK_PICKUP


async def receive_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return ASK_PICKUP
    code = query.data.rsplit(":", 1)[-1]
    if code not in orders_service.PICKUP_LABELS:
        await query.answer("Неизвестный способ получения.", show_alert=True)
        return ASK_PICKUP
    await query.answer()

    cart = await cart_service.items(context, update.effective_user.id)
    if not cart:
        _clear_draft(context)
        await query.edit_message_text(EMPTY_CART_TEXT)
        return ConversationHandler.END

    context.user_data[PICKUP_KEY] = code
    await query.edit_message_text(
        _summary_text(context, cart),
        reply_markup=checkout_confirm_keyboard(),
    )
    return CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return ConversationHandler.END
    pool = get_pool(context)
    name = context.user_data.get(NAME_KEY)
    pickup = context.user_data.get(PICKUP_KEY)
    if pool is None or not name or not pickup:
        await query.answer("Данные заявки сброшены. Начните оформление снова.", show_alert=True)
        return ConversationHandler.END

    try:
        # Корзина забирается из БД в той же транзакции, что и создание заявки.
        # Повторное нажатие найдёт пустую корзину и вторую заявку не создаст.
        order = await orders_service.create_order(
            pool,
            update.effective_user,
            name,
            pickup,
        )
    except orders_service.EmptyCartError:
        _clear_draft(context)
        await query.answer("Корзина пуста. Возможно, заявка уже оформлена.", show_alert=True)
        await query.edit_message_text(ALREADY_DONE_TEXT)
        return ConversationHandler.END
    except Exception:
        logger.exception("Ошибка записи заявки в PostgreSQL")
        await query.answer("Не удалось сохранить заявку.", show_alert=True)
        await query.edit_message_text(
            "Не получилось записать заявку в базу. Проверьте PostgreSQL и DATABASE_URL."
        )
        return CONFIRM

    _clear_draft(context)
    await query.answer("Заявка сохранена")
    await query.edit_message_text(
        "Заявка принята.\n\n"
        f"Номер: {order.public_number}\n"
        f"Статус: {orders_service.status_label(order.status)}\n"
        f"Имя: {order.recipient_name}\n"
        f"Получение: {orders_service.pickup_label(order.pickup_method)}\n"
        f"Оплата: {orders_service.PAYMENT_LABEL}\n"
        f"Сумма: {format_price(order.total_amount)}\n\n"
        "Список заявок — кнопка «Мои заказы»."
    )
    if query.message is not None:
        await query.message.reply_text("Меню «Вояж-тур».", reply_markup=main_menu())
    return ConversationHandler.END


async def remind_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    await update.message.reply_text(NEED_BUTTONS)
    current = context.user_data.get(PICKUP_KEY)
    return CONFIRM if current else ASK_PICKUP


async def cancel_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_draft(context)
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text(CANCELLED)
        return ConversationHandler.END
    if update.message is not None:
        await update.message.reply_text(CANCELLED, reply_markup=main_menu())
    return ConversationHandler.END


def _summary_text(context: ContextTypes.DEFAULT_TYPE, cart: dict[int, int]) -> str:
    name = context.user_data.get(NAME_KEY, "")
    pickup = context.user_data.get(PICKUP_KEY, "")
    cart_text, _total = cart_service.format_cart(cart)
    return (
        "Проверьте заявку:\n\n"
        f"Имя: {name}\n"
        f"Получение: {orders_service.pickup_label(pickup)}\n"
        f"Оплата: {orders_service.PAYMENT_LABEL}\n\n"
        f"{cart_text}\n\n"
        "Подтвердите отправку или отмените."
    )
