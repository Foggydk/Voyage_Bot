"""Точные суммы: NUMERIC в магазине, целые копейки в финансовом журнале."""

from decimal import Decimal, InvalidOperation


def to_kopecks(value) -> int:
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("Укажите сумму с точностью до копейки.")
        return int(amount)
    except (InvalidOperation, TypeError):
        raise ValueError("Некорректная сумма.") from None


def format_kopecks(amount: int) -> str:
    sign = "−" if amount < 0 else ""
    rubles, cents = divmod(abs(amount), 100)
    whole = f"{rubles:,}".replace(",", " ")
    return f"{sign}{whole},{cents:02d} ₽"
