"""Корзина в памяти на время сессии бота. Позже заменим таблицей cart_items."""

from telegram.ext import ContextTypes

from src.data.tours import Tour, format_price, get_tour

CART_KEY = "cart"


def _cart(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    stored = context.user_data.setdefault(CART_KEY, {})
    return stored


def add_item(context: ContextTypes.DEFAULT_TYPE, tour_id: int, quantity: int = 1) -> Tour | None:
    if quantity < 1:
        return None
    tour = get_tour(tour_id)
    if tour is None:
        return None
    cart = _cart(context)
    cart[tour_id] = cart.get(tour_id, 0) + quantity
    return tour


def set_quantity(context: ContextTypes.DEFAULT_TYPE, tour_id: int, quantity: int) -> str | None:
    """Возвращает текст ошибки или None, если всё в порядке."""
    if get_tour(tour_id) is None:
        return "Такого тура нет в каталоге."
    if quantity < 1:
        return "Количество должно быть не меньше 1. Удалите позицию, если она не нужна."
    cart = _cart(context)
    if tour_id not in cart:
        return "Этой позиции нет в корзине."
    cart[tour_id] = quantity
    return None


def change_quantity(context: ContextTypes.DEFAULT_TYPE, tour_id: int, delta: int) -> str | None:
    cart = _cart(context)
    current = cart.get(tour_id)
    if current is None:
        return "Этой позиции нет в корзине."
    return set_quantity(context, tour_id, current + delta)


def remove_item(context: ContextTypes.DEFAULT_TYPE, tour_id: int) -> bool:
    cart = _cart(context)
    return cart.pop(tour_id, None) is not None


def cart_lines(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, int]:
    cart = _cart(context)
    if not cart:
        return "Корзина пуста. Откройте каталог и добавьте тур.", 0

    lines = ["Ваша корзина:"]
    total = 0
    for tour_id, quantity in cart.items():
        tour = get_tour(tour_id)
        if tour is None:
            continue
        subtotal = tour.price * quantity
        total += subtotal
        lines.append(
            f"• {tour.title}\n"
            f"  {quantity} × {format_price(tour.price)} = {format_price(subtotal)}"
        )
    lines.append(f"\nИтого: {format_price(total)}")
    return "\n".join(lines), total


def is_empty(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return not _cart(context)


def items(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    return dict(_cart(context))


def clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    _cart(context).clear()
