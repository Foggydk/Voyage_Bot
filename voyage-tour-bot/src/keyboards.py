"""Клавиатуры Telegram. ReplyKeyboardMarkup и InlineKeyboardMarkup — PTB v22."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from src.data.tours import Tour, TOURS, get_tour

BTN_CATALOG = "🌴 Каталог туров"
BTN_AI = "✨ Индивидуальный тур"
BTN_CART = "🛒 Корзина"
BTN_ORDERS = "📦 Мои заказы"
BTN_HELP = "ℹ️ Помощь"


def main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [BTN_CATALOG, BTN_AI],
        [BTN_CART, BTN_ORDERS],
        [BTN_HELP],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел меню",
    )


def catalog_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(tour.title, callback_data=f"tour:{tour.id}")]
        for tour in TOURS
    ]
    return InlineKeyboardMarkup(rows)


def tour_card_keyboard(tour: Tour) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ В корзину", callback_data=f"cart:add:{tour.id}")],
            [
                InlineKeyboardButton("⬅️ К каталогу", callback_data="tour:list"),
                InlineKeyboardButton("🛒 Корзина", callback_data="cart:view"),
            ],
        ]
    )


def cart_keyboard(cart: dict[int, int]) -> InlineKeyboardMarkup:
    """Клавиатура корзины. cart — это {tour_id: количество}."""
    rows: list[list[InlineKeyboardButton]] = []
    for tour_id, quantity in cart.items():
        tour = get_tour(tour_id)
        label = tour.title if tour else f"Тур #{tour_id}"
        rows.append(
            [InlineKeyboardButton(f"{label} × {quantity}", callback_data=f"tour:{tour_id}")]
        )
        rows.append(
            [
                InlineKeyboardButton("➖", callback_data=f"cart:dec:{tour_id}"),
                InlineKeyboardButton("➕", callback_data=f"cart:inc:{tour_id}"),
                InlineKeyboardButton("🗑", callback_data=f"cart:del:{tour_id}"),
            ]
        )
    rows.append([InlineKeyboardButton("📝 Оформить заказ", callback_data="checkout:start")])
    rows.append([InlineKeyboardButton("🌴 В каталог", callback_data="tour:list")])
    return InlineKeyboardMarkup(rows)


def pickup_keyboard() -> InlineKeyboardMarkup:
    from src.services.orders import PICKUP_LABELS

    rows = [
        [InlineKeyboardButton(label, callback_data=f"checkout:pickup:{code}")]
        for code, label in PICKUP_LABELS.items()
    ]
    rows.append([InlineKeyboardButton("Отмена", callback_data="checkout:cancel")])
    return InlineKeyboardMarkup(rows)


def checkout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить заявку", callback_data="checkout:ok")],
            [InlineKeyboardButton("✏️ Заполнить заново", callback_data="checkout:start")],
            [InlineKeyboardButton("Отмена", callback_data="checkout:cancel")],
        ]
    )
