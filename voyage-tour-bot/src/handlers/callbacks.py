"""Обработка нажатий inline-кнопок. См. examples.inlinekeyboard в документации PTB."""

from telegram import Update
from telegram.ext import ContextTypes

from src.data.tours import format_price
from src.handlers.cart import edit_cart_message
from src.handlers.catalog import send_catalog_edit, send_tour_card
from src.services import cart as cart_service


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    data = query.data
    parts = data.split(":")

    if data == "tour:list":
        await query.answer()
        await send_catalog_edit(query)
        return

    if len(parts) == 2 and parts[0] == "tour" and parts[1].isdigit():
        await query.answer()
        await send_tour_card(query, int(parts[1]))
        return

    if data == "cart:view":
        await query.answer()
        await edit_cart_message(query, context)
        return

    if len(parts) == 3 and parts[0] == "cart" and parts[2].isdigit():
        action, tour_id = parts[1], int(parts[2])
        await _handle_cart_action(query, context, action, tour_id)
        return

    await query.answer("Неизвестная кнопка.", show_alert=True)


async def _handle_cart_action(query, context, action: str, tour_id: int) -> None:
    if action == "add":
        tour = cart_service.add_item(context, tour_id)
        if tour is None:
            await query.answer("Не удалось добавить тур.", show_alert=True)
            return
        await query.answer(f"Добавлено: {tour.title} ({format_price(tour.price)})")
        return

    if action == "inc":
        error = cart_service.change_quantity(context, tour_id, 1)
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer("Количество увеличено")
        await edit_cart_message(query, context)
        return

    if action == "dec":
        error = cart_service.change_quantity(context, tour_id, -1)
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer("Количество уменьшено")
        await edit_cart_message(query, context)
        return

    if action == "del":
        removed = cart_service.remove_item(context, tour_id)
        if not removed:
            await query.answer("Позиции уже нет в корзине.", show_alert=True)
            return
        await query.answer("Позиция удалена")
        await edit_cart_message(query, context)
        return

    await query.answer("Неизвестное действие.", show_alert=True)
