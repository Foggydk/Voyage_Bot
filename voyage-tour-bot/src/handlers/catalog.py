"""Каталог туров и карточка позиции."""

from telegram import Update
from telegram.ext import ContextTypes

from src.data.tours import TOUR_TYPE_LABELS, Tour, format_price, get_tour
from src.keyboards import catalog_keyboard, tour_card_keyboard
from src.db import get_pool
from src.services.inventory import balances

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
        CATALOG_INTRO + await availability(context),
        reply_markup=catalog_keyboard(),
    )


async def availability(context, tour_id=None):
    pool = get_pool(context)
    if pool is None:
        return "\nОстатки временно недоступны."
    rows = await balances(pool)
    return "\n\n" + "\n".join(
        f"{r['title']}: "
        + (f"доступно {r['available']} мест" if r["available"] else "Нет в наличии")
        for r in rows
        if tour_id is None or r["product_id"] == tour_id
    )


async def send_catalog_edit(query, context) -> None:
    await query.edit_message_text(
        CATALOG_INTRO + await availability(context), reply_markup=catalog_keyboard()
    )


async def send_tour_card(query, tour_id: int, context) -> None:
    tour = get_tour(tour_id)
    if tour is None:
        await query.answer("Тур не найден.", show_alert=True)
        return
    await query.edit_message_text(
        format_tour_card(tour) + await availability(context, tour_id),
        reply_markup=tour_card_keyboard(tour),
    )
