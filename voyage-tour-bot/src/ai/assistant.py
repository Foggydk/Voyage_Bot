"""
ИИ-ассистент индивидуального тура.

Сейчас это заглушка. Позже сюда подключим выбранный API
(ключ только из .env: AI_PROVIDER и AI_API_KEY).
Обработчик диалога будет вызывать TourAssistant.reply(),
поэтому смену провайдера можно сделать без переписывания меню.
"""

from typing import Protocol

from src.config import get_ai_settings


class TourAssistant(Protocol):
    async def reply(self, user_message: str, history: list[dict[str, str]]) -> str:
        ...


class PlaceholderTourAssistant:
    async def reply(self, user_message: str, history: list[dict[str, str]]) -> str:
        return (
            "Помощник по индивидуальному туру ещё не подключён к ИИ.\n\n"
            "Скоро здесь можно будет описать даты, бюджет, отдых или шопинг — "
            "и получить черновик маршрута по Азии.\n"
            "Пока выберите готовый тур в каталоге."
        )


def build_assistant() -> TourAssistant:
    provider, api_key = get_ai_settings()
    if provider and api_key:
        # Реализацию выбранного провайдера добавим отдельным этапом.
        return PlaceholderTourAssistant()
    return PlaceholderTourAssistant()
