""" /start, /help, /menu и кнопки главного меню."""

from telegram import Update
from telegram.ext import ContextTypes

from src.handlers.assistant import show_assistant
from src.handlers.cart import show_cart
from src.handlers.catalog import show_catalog
from src.handlers.orders import show_orders
from src.keyboards import (
    BTN_AI,
    BTN_CART,
    BTN_CATALOG,
    BTN_HELP,
    BTN_ORDERS,
    main_menu,
)

WELCOME_TEXT = (
    "Добро пожаловать в турагентство «Вояж-тур».\n\n"
    "Мы собираем поездки по Азии: отдых у моря и шопинг-туры "
    "в Японию, Таиланд, Вьетнам, Южную Корею и Индонезию.\n\n"
    "Выберите готовый тур в каталоге или позже соберите индивидуальную "
    "программу с помощником.\n\n"
    "Команды:\n"
    "/start — главное меню\n"
    "/help — справка\n"
    "/menu — показать кнопки ещё раз"
)

HELP_TEXT = (
    "Как оформить поездку:\n"
    "1. Откройте каталог: туры для отдыха и шопинг-туры.\n"
    "2. Откройте карточку и добавьте тур в корзину.\n"
    "3. В корзине можно изменить количество или убрать позицию.\n"
    "4. Оформление заказа: имя и способ получения документов.\n"
    "5. Оплата — расчёт при получении.\n"
    "6. В «Мои заказы» видны только ваши заявки.\n\n"
    "«Индивидуальный тур» — помощник под ваши даты и бюджет. "
    "Подключение ИИ сделаем отдельным шагом.\n\n"
    "Заявка сохраняется в базе. Оплата — при получении документов."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=main_menu())


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("Главное меню «Вояж-тур».", reply_markup=main_menu())


async def menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return

    text = update.message.text
    if text == BTN_HELP:
        await help_command(update, context)
        return
    if text == BTN_CATALOG:
        await show_catalog(update, context)
        return
    if text == BTN_AI:
        await show_assistant(update, context)
        return
    if text == BTN_CART:
        await show_cart(update, context)
        return
    if text == BTN_ORDERS:
        await show_orders(update, context)
        return

    await update.message.reply_text(
        "Не понял сообщение. Нажмите кнопку меню или /help.",
        reply_markup=main_menu(),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")
