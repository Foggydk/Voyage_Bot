# ruff: noqa: E402
# Поддерживаем как python -m src.bot, так и прежний python src/bot.py.
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
import asyncio
from contextlib import suppress
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

from src.config import get_bot_token, get_database_url, get_mini_app_url
from src.db import close_database, connect_database
from src.handlers.callbacks import on_callback
from src.handlers.cart import show_cart
from src.handlers.catalog import show_catalog
from src.handlers.checkout import build_checkout_handler
from src.handlers.manager import manager_command, on_manager_callback, summary_command
from src.handlers.orders import show_orders
from src.handlers.payments import on_payment_callback
from src.services.notifications import notification_worker
from src.web import start_web
from src.db import POOL_KEY
from src.services.inventory import inventory_worker, expire_reservations
from src.services.refunds import refund_worker, process_refunds
from src.handlers.start import help_command, menu_buttons, my_id, show_menu, start

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_startup(application: Application) -> None:
    get_mini_app_url()  # Проверка конфигурации до запуска обработчиков.
    dsn = get_database_url()
    if dsn is None:
        logger.warning(
            "DATABASE_URL не задан или содержит YOUR_PASSWORD. "
            "Каталог и корзина работают, заявки в базу не сохранятся."
        )
        return
    try:
        await connect_database(application, dsn)
        pool = application.bot_data[POOL_KEY]
        await expire_reservations(pool)
        await process_refunds(pool)
        application.bot_data["web_runner"] = await start_web(pool, get_bot_token())
        application.bot_data["notifications_task"] = asyncio.create_task(
            notification_worker(pool, application.bot)
        )
        application.bot_data["inventory_task"] = asyncio.create_task(
            inventory_worker(pool)
        )
        application.bot_data["refund_task"] = asyncio.create_task(refund_worker(pool))
    except Exception:
        await on_shutdown(application)
        logger.error(
            "Не удалось запустить базу или веб-сервер. Проверьте настройки и доступность порта."
        )
        raise


async def on_shutdown(application: Application) -> None:
    for key in ("notifications_task", "inventory_task", "refund_task"):
        task = application.bot_data.pop(key, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    runner = application.bot_data.pop("web_runner", None)
    if runner:
        await runner.cleanup()
    await close_database(application)


def main() -> None:
    token = get_bot_token()
    application = (
        Application.builder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.add_handler(build_checkout_handler())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", show_menu))
    application.add_handler(CommandHandler("catalog", show_catalog))
    application.add_handler(CommandHandler("cart", show_cart))
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(CommandHandler("manager", manager_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("orders", show_orders))
    application.add_handler(CallbackQueryHandler(on_payment_callback, pattern=r"^pay:"))
    # Обработчик менеджера должен стоять выше общего on_callback.
    application.add_handler(CallbackQueryHandler(on_manager_callback, pattern=r"^mgr:"))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_buttons)
    )

    logger.info("Бот «Вояж-тур» запущен. Остановка: Ctrl+C в терминале.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
