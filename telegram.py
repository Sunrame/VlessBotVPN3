"""
Отправка сообщений через Telegram Bot API (тем же токеном, что у бота).
Используется для кодов подтверждения, ответов в тикетах и рассылки.
"""
import logging
import requests

import config

log = logging.getLogger("telegram")


def _api(method: str) -> str:
    return "https://api.telegram.org/bot" + config.BOT_TOKEN + "/" + method


_bot_username_cache: str | None = None


def get_bot_username() -> str:
    """Юзернейм бота через getMe (кэшируется). Пусто → вызывающий берёт config.BOT_USERNAME."""
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    try:
        r = requests.get(_api("getMe"), timeout=10)
        data = r.json() if r.ok else {}
        if data.get("ok"):
            _bot_username_cache = (data.get("result") or {}).get("username") or ""
            return _bot_username_cache
    except Exception as e:
        log.error("get_bot_username: %s", e)
    _bot_username_cache = ""
    return _bot_username_cache


def send_message(chat_id: int, text: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(_api("sendMessage"), json=payload, timeout=15)
        ok = r.ok and r.json().get("ok", False)
        if not ok:
            log.error("send_message failed: %s", r.text[:300])
        return ok
    except Exception as e:
        log.error("send_message: %s", e)
        return False


def send_photo(chat_id: int, photo: str, caption: str = "", parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    try:
        payload = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(_api("sendPhoto"), json=payload, timeout=20)
        ok = r.ok and r.json().get("ok", False)
        if not ok:
            log.error("send_photo failed: %s", r.text[:300])
        return ok
    except Exception as e:
        log.error("send_photo: %s", e)
        return False


def send_code(chat_id: int, code: str, ttl_min: int) -> bool:
    text = (
        "🔐 <b>Вход в админ-панель</b>\n\n"
        "Ваш код подтверждения:\n\n"
        f"<code>{code}</code>\n\n"
        f"Действует {ttl_min} мин. Если вы не запрашивали вход — проигнорируйте это сообщение."
    )
    return send_message(chat_id, text)
