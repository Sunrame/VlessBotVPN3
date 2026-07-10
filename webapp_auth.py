"""
Проверка подписи Telegram Mini App (WebApp initData).

Когда кабинет открывается как Mini App, Telegram передаёт подписанную
строку window.Telegram.WebApp.initData. Подпись проверяется тем же токеном
бота, что гарантирует подлинность Telegram ID без ввода кода.
Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qsl

import config

# Сколько секунд считаем initData свежим (защита от повторного использования).
INIT_DATA_MAX_AGE = 24 * 3600


def verify_init_data(init_data: str, max_age: int = INIT_DATA_MAX_AGE) -> dict | None:
    """Проверяет подпись initData. Возвращает dict с данными пользователя
    (включая распарсенный user) либо None, если подпись неверна/устарела."""
    if not init_data or not config.BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # Свежесть: auth_date не должна быть слишком старой.
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if max_age and auth_date and (time.time() - auth_date) > max_age:
        return None

    user = None
    if parsed.get("user"):
        try:
            user = json.loads(parsed["user"])
        except Exception:
            user = None
    parsed["user"] = user
    return parsed


def extract_user_id(init_data: str, max_age: int = INIT_DATA_MAX_AGE) -> int | None:
    """Удобная обёртка: возвращает только Telegram user_id или None."""
    data = verify_init_data(init_data, max_age)
    if not data or not isinstance(data.get("user"), dict):
        return None
    uid = data["user"].get("id")
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None
