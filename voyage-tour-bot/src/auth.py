"""Проверка исходного Telegram initData, без доверия к данным браузера."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def validate_init_data(raw, token, *, max_age=3600, now=None):
    if not isinstance(raw, str) or not raw or len(raw) > 16384:
        raise AuthError("Откройте кабинет из Telegram-бота.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        received = fields.pop("hash")
        if len(received) != 64:
            raise ValueError
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError
        timestamp = int(fields["auth_date"])
        current = time.time() if now is None else now
        if timestamp > current + 30 or current - timestamp > max_age:
            raise AuthError(
                "Сессия истекла. Закройте кабинет и откройте его заново из бота."
            )
        user = json.loads(fields["user"])
        user_id = user["id"]
        if type(user_id) is not int or user_id <= 0:
            raise ValueError
        return user_id
    except AuthError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AuthError("Не удалось проверить авторизацию Telegram.") from None
