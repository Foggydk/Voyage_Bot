"""Каталог туров и карточка позиции."""

from telegram import Update
from telegram.ext import ContextTypes

from src.data.tours import TOUR_TYPE_LABELS, Tour, format_price, get_tour
from src.keyboards import catalog_keyboard, tour_card_keyboard

CATALOG_INTRO = (
    "Каталог «Вояж-тур»: готовые программы по Азии.\n"
    "Есть туры для отдыха и шопинг-туры. Нажмите название, чтобы открыть карточку."
)


def format_tour_card(tour: Tour) -> str:
    kind = TOUR_TYPE_LABELS.get(tour.tour_type, tour.tour_type)
    return (
        f"{tour.title}\n\n"
        f"Направление: {tour.destination}\n"
        f"Тип: {kind}\n"
        f"Длительность: {tour.duration_days} дн.\n"
        f"Цена: {format_price(tour.price)} с человека\n\n"
        f"{tour.description}"
    )


async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        CATALOG_INTRO,
        reply_markup=catalog_keyboard(),
    )


async def send_catalog_edit(query) -> None:
    await query.edit_message_text(CATALOG_INTRO, reply_markup=catalog_keyboard())


async def send_tour_card(query, tour_id: int) -> None:
    tour = get_tour(tour_id)
    if tour is None:
        await query.answer("Тур не найден.", show_alert=True)
        return
    await query.edit_message_text(
        format_tour_card(tour),
        reply_markup=tour_card_keyboard(tour),
    )
