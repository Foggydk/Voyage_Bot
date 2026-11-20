"""Кабинет менеджера: список заказов и смена статуса. Только для MANAGER_TELEGRAM_ID."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.config import get_manager_ids, get_mini_app_url
from src.services import finance
from src.services.money import format_kopecks
from src.data.tours import format_price
from src.db import get_pool
from src.handlers.checkout import DB_REQUIRED
from src.handlers.orders import format_order
from src.services import orders as orders_service

logger = logging.getLogger(__name__)

NO_ACCESS = "Эта команда доступна только менеджеру."
MENU_TEXT = "Кабинет менеджера. Выберите статус заявок:"


def _is_manager(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in get_manager_ids()


def _menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                orders_service.status_label(code).capitalize(),
                callback_data=f"mgr:list:{code}",
            )
        ]
        for code in orders_service.STATUS_LABELS
    ]
    url = get_mini_app_url()
    if url:
        rows.insert(
            0, [InlineKeyboardButton("📊 Финансы · Mini App", web_app=WebAppInfo(url))]
        )
    return InlineKeyboardMarkup(rows)


def _list_keyboard(orders) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{o.public_number} · {o.recipient_name} · {format_price(o.total_amount)}",
                callback_data=f"mgr:open:{o.public_number}",
            )
        ]
        for o in orders
    ]
    rows.append([InlineKeyboardButton("⬅️ К статусам", callback_data="mgr:menu")])
    return InlineKeyboardMarkup(rows)


def _order_keyboard(order) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"→ {orders_service.status_label(code).capitalize()}",
                callback_data=f"mgr:set:{order.public_number}:{code}",
            )
        ]
        for code in orders_service.ALLOWED_TRANSITIONS.get(order.status, ())
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ К списку", callback_data=f"mgr:list:{order.status}")]
    )
    return InlineKeyboardMarkup(rows)


async def _edit(query, text: str, markup) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def manager_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not _is_manager(update):
        await update.message.reply_text(NO_ACCESS)
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Откройте кабинет в личном чате с ботом.")
        return
    if get_pool(context) is None:
        await update.message.reply_text(DB_REQUIRED)
        return
    await update.message.reply_text(MENU_TEXT, reply_markup=_menu_keyboard())


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not _is_manager(update):
        await update.message.reply_text(NO_ACCESS)
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Финансовая сводка доступна в личном чате с ботом."
        )
        return
    pool = get_pool(context)
    if pool is None:
        await update.message.reply_text(DB_REQUIRED)
        return
    try:
        if len(context.args) not in (0, 2):
            raise ValueError("Используйте /summary или /summary ГГГГ-ММ-ДД ГГГГ-ММ-ДД.")
        start, end = context.args if context.args else (None, None)
        data = await finance.summary(pool, update.effective_user.id, start, end)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    period = f"{start} — {end}" if start else "всё время"
    await update.message.reply_text(
        f"Демонстрация без платёжного провайдера\nПериод: {period} (Москва)\n"
        f"Поступления: {format_kopecks(data['receipts'])}\nВозвраты: {format_kopecks(data['refunds'])}\n"
        f"Расходы: {format_kopecks(data['expenses'])}\n"
        f"Денежный поток: {format_kopecks(data['cash_flow'])}\n"
        "Поток = поступления − возвраты − расходы\n"
        f"Учебный результат: {format_kopecks(data['learning_result'])}\n"
        "Результат = поток − себестоимость оплаченных + себестоимость возвращённых."
    )


async def on_manager_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not _is_manager(update):
        await query.answer(NO_ACCESS, show_alert=True)
        return
    if update.effective_chat.type != "private":
        await query.answer("Откройте кабинет в личном чате с ботом.", show_alert=True)
        return
    pool = get_pool(context)
    if pool is None:
        await query.answer(DB_REQUIRED, show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        await query.answer()
        await _edit(query, MENU_TEXT, _menu_keyboard())
        return

    if (
        action == "list"
        and len(parts) == 3
        and parts[2] in orders_service.STATUS_LABELS
    ):
        await query.answer()
        status = parts[2]
        orders = await orders_service.list_by_status(pool, status)
        title = orders_service.status_label(status).capitalize()
        text = f"{title}: {len(orders)} шт." if orders else f"{title}: заявок нет."
        await _edit(query, text, _list_keyboard(orders))
        return

    if action == "open" and len(parts) == 3:
        await query.answer()
        found = await orders_service.get_order(pool, parts[2])
        if found is None:
            await _edit(query, "Заявка не найдена.", _menu_keyboard())
            return
        order, client_id = found
        await _edit(
            query,
            f"{format_order(order)}\nКлиент (Telegram ID): {client_id}",
            _order_keyboard(order),
        )
        return

    if action == "set" and len(parts) == 4:
        number, new_status = parts[2], parts[3]
        error, client_id = await orders_service.change_status(pool, number, new_status)
        if error:
            await query.answer(error, show_alert=True)
        else:
            await query.answer("Статус изменён")
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=(
                        f"Статус вашей заявки {number} изменён: "
                        f"{orders_service.status_label(new_status)}."
                    ),
                )
            except Exception:
                logger.warning("Не удалось уведомить клиента %s", client_id)
        found = await orders_service.get_order(pool, number)
        if found is not None:
            order, cid = found
            await _edit(
                query,
                f"{format_order(order)}\nКлиент (Telegram ID): {cid}",
                _order_keyboard(order),
            )
        return

    await query.answer("Неизвестное действие.", show_alert=True)
