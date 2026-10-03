"""Просмотр корзины, количество, удаление, итог."""

from telegram import Update
from telegram.ext import ContextTypes

from src.keyboards import cart_keyboard, main_menu
from src.services import cart as cart_service


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    text, _total = cart_service.cart_lines(context)
    markup = None if cart_service.is_empty(context) else cart_keyboard(context)
    await update.message.reply_text(text, reply_markup=markup)
    await update.message.reply_text("Меню «Вояж-тур».", reply_markup=main_menu())


async def edit_cart_message(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, _total = cart_service.cart_lines(context)
    markup = None if cart_service.is_empty(context) else cart_keyboard(context)
    await query.edit_message_text(text, reply_markup=markup)
