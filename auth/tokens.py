"""Подписанные сессионные токены для mini app.

Mini App может быть открыт двумя способами:
1. Как WebApp внутри Telegram — тогда браузер присылает initData, подписанный Telegram.
2. Как обычная ссылка (GitHub Pages, локальный сервер, Telegram Desktop) — initData нет.

Второй случай закрывается токеном вида ``<user_id>.<expires>.<signature>``: его подписывает
бот тем же BOT_TOKEN, что и API, поэтому обе стороны доверяют друг другу без общей БД сессий.
"""
import hashlib
import hmac
import os
import time

# Импорт загружает .env в os.environ (побочный эффект database.db), чтобы токен
# подписывался одинаково в боте, API и тестах.
import database.db  # noqa: F401

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class SessionError(ValueError):
    pass


def _secret() -> bytes:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SessionError("BOT_TOKEN is not configured")
    return hashlib.sha256(("sfu-session:" + token).encode()).digest()


def sign_session(user_id: int, ttl: int = SESSION_TTL_SECONDS) -> str:
    expires = int(time.time()) + int(ttl)
    payload = f"{int(user_id)}.{expires}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def verify_session(token: str) -> int:
    token = (token or "").strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise SessionError("Неверный формат токена")
    raw_id, raw_exp, signature = parts
    try:
        user_id = int(raw_id)
        expires = int(raw_exp)
    except ValueError as exc:
        raise SessionError("Неверный формат токена") from exc
    payload = f"{user_id}.{expires}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        raise SessionError("Подпись токена не совпала")
    if expires < int(time.time()):
        raise SessionError("Сессия истекла, открой Mini App из бота заново")
    return user_id
