"""Просмотр корзины, количество, удаление, итог."""

from telegram import Update
from telegram.ext import ContextTypes

from src.keyboards import cart_keyboard, main_menu
from src.services import cart as cart_service


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    cart = await cart_service.items(context, update.effective_user.id)
    text, _total = cart_service.format_cart(cart)
    markup = cart_keyboard(cart) if cart else None
    await update.message.reply_text(text, reply_markup=markup)
    await update.message.reply_text("Меню «Вояж-тур».", reply_markup=main_menu())


async def edit_cart_message(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    cart = await cart_service.items(context, query.from_user.id)
    text, _total = cart_service.format_cart(cart)
    markup = cart_keyboard(cart) if cart else None
    await query.edit_message_text(text, reply_markup=markup)
