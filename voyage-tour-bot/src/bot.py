"""
Точка входа бота «Вояж-тур».

Документация python-telegram-bot v22.8:
https://docs.python-telegram-bot.org/en/stable/examples.echobot.html
https://docs.python-telegram-bot.org/en/stable/examples.inlinekeyboard.html

Проверено в документации:
- Application.builder().token(...).build()
- CommandHandler, MessageHandler, CallbackQueryHandler
- InlineKeyboardButton / InlineKeyboardMarkup
- query.answer() для CallbackQuery
- Application.run_polling(allowed_updates=Update.ALL_TYPES)
"""

import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config import get_bot_token, get_database_url
from src.db import close_database, connect_database
from src.handlers.callbacks import on_callback
from src.handlers.cart import show_cart
from src.handlers.catalog import show_catalog
from src.handlers.checkout import build_checkout_handler
from src.handlers.start import help_command, menu_buttons, show_menu, start

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_startup(application: Application) -> None:
    dsn = get_database_url()
    if dsn is None:
        logger.warning(
            "DATABASE_URL не задан или содержит YOUR_PASSWORD. "
            "Каталог и корзина работают, заявки в базу не сохранятся."
        )
        return
    try:
        await connect_database(application, dsn)
    except Exception:
        logger.exception(
            "Не удалось подключить PostgreSQL. Заявки будут недоступны, "
            "пока DATABASE_URL и база voyage_tour не заработают."
        )


def main() -> None:
    token = get_bot_token()
    application = (
        Application.builder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(close_database)
        .build()
    )

    application.add_handler(build_checkout_handler())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", show_menu))
    application.add_handler(CommandHandler("catalog", show_catalog))
    application.add_handler(CommandHandler("cart", show_cart))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_buttons)
    )

    logger.info("Бот «Вояж-тур» запущен. Остановка: Ctrl+C в терминале.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
