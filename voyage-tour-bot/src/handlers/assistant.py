"""Индивидуальный тур. Диалог с ИИ подключим отдельным этапом."""

from telegram import Update
from telegram.ext import ContextTypes

from src.ai.assistant import build_assistant
from src.keyboards import main_menu

INTRO = (
    "Индивидуальный тур: помощник спросит направление, даты, "
    "бюджет и формат — отдых или шопинг — и предложит черновик программы.\n\n"
)


async def show_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    assistant = build_assistant()
    answer = await assistant.reply("", [])
    await update.message.reply_text(INTRO + answer, reply_markup=main_menu())
