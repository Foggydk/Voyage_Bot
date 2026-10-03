"""Чтение настроек из .env. Секреты в код не записываем."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не найден. Скопируйте .env.example в .env "
            "и укажите токен бота от @BotFather."
        )
    return token


def get_ai_settings() -> tuple[str, str]:
    """Провайдер и ключ для ИИ-ассистента. Пока можно оставить пустыми."""
    provider = os.getenv("AI_PROVIDER", "").strip()
    api_key = os.getenv("AI_API_KEY", "").strip()
    return provider, api_key


def get_database_url() -> str | None:
    """DSN PostgreSQL. Заглушка YOUR_PASSWORD считается незаполненной."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url or "YOUR_PASSWORD" in url:
        return None
    return url


def get_manager_ids() -> set[int]:
    """ID менеджеров из MANAGER_TELEGRAM_ID (можно несколько через запятую)."""
    raw = os.getenv("MANAGER_TELEGRAM_ID", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids
