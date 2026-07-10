"""
Синхронный клиент Remnawave (порт функций бота на requests).
Сайт управляет подписками точно так же, как бот.
"""
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

import config

log = logging.getLogger("remnawave")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.REMNAWAVE_TOKEN}",
        "Content-Type": "application/json",
        "Cookie": config.REMNAWAVE_COOKIE,
    }


def username(user_id: int) -> str:
    return f"truba_{user_id}"


def _squad_uuids(raw_squads) -> list[str]:
    if not raw_squads:
        return []
    out = []
    for s in raw_squads:
        if isinstance(s, dict) and s.get("uuid"):
            out.append(s["uuid"])
        elif isinstance(s, str):
            out.append(s)
    return out


def expand_squads(squad_uuid) -> list[str]:
    """Разворачивает выбранный сквад в итоговый список.

    Доступ к белым спискам всегда идёт ВМЕСТЕ с обычными серверами: если
    назначается сквад белых списков, добавляем и базовый сквад, чтобы
    работали и обычные VPN-сервера, и сервер белых списков.
    """
    if isinstance(squad_uuid, (list, tuple, set)):
        ids = [s for s in squad_uuid if s]
    elif squad_uuid:
        ids = [squad_uuid]
    else:
        ids = []
    if config.SQUAD_UUID_WHITELIST and config.SQUAD_UUID_WHITELIST in ids:
        if config.SQUAD_UUID_BASIC and config.SQUAD_UUID_BASIC not in ids:
            ids.insert(0, config.SQUAD_UUID_BASIC)
    seen: list[str] = []
    for s in ids:
        if s not in seen:
            seen.append(s)
    return seen


def _expire_at(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_dt(value) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def get_user(user_id: int) -> dict | None:
    try:
        r = requests.get(
            f"{config.REMNAWAVE_URL}/api/users/by-username/{username(user_id)}",
            headers=_headers(), timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("response")
    except Exception as e:
        log.error("get_user: %s", e)
        return None


def get_user_by_uuid(uuid_: str) -> dict | None:
    try:
        r = requests.get(
            f"{config.REMNAWAVE_URL}/api/users/{uuid_}",
            headers=_headers(), timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("response")
    except Exception as e:
        log.error("get_user_by_uuid: %s", e)
        return None


def create_user(user_id: int, days: int, hwid: int = 1, squad_uuid: str | None = None) -> dict | None:
    squad_uuid = squad_uuid or config.SQUAD_UUID
    payload = {
        "username": username(user_id),
        "trafficLimitBytes": 0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": _expire_at(days),
        "hwidDeviceLimit": hwid,
        "telegramId": user_id,
        "activeInternalSquads": expand_squads(squad_uuid),
    }
    try:
        r = requests.post(
            f"{config.REMNAWAVE_URL}/api/users",
            json=payload, headers=_headers(), timeout=15,
        )
        if r.status_code == 409:
            return extend_user(user_id, days, hwid, squad_uuid)
        r.raise_for_status()
        return r.json().get("response")
    except Exception as e:
        log.error("create_user: %s", e)
        return None


def extend_user(user_id: int, days: int, hwid: int | None = None,
                squad_uuid: str | None = None) -> dict | None:
    user = get_user(user_id)
    if not user:
        return create_user(user_id, days, hwid or 1, squad_uuid or config.SQUAD_UUID)

    now = datetime.now(timezone.utc)
    current = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
    base = max(current, now)
    new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Обязательно status=ACTIVE — иначе выданная/продлённая подписка
    # остаётся «неактивной», если аккаунт был просрочен или отключён.
    payload: dict = {"uuid": user["uuid"], "expireAt": new_expire, "status": "ACTIVE"}
    if squad_uuid is not None:
        squads = expand_squads(squad_uuid)
        if squads:
            payload["activeInternalSquads"] = squads
    if hwid is not None:
        payload["hwidDeviceLimit"] = hwid
    return update_user(user["uuid"], payload)


def set_expire(user_id: int, days_from_now: int) -> dict | None:
    """Установить дату окончания ровно через N дней от сейчас."""
    user = get_user(user_id)
    if not user:
        return create_user(user_id, max(0, days_from_now))
    return update_user(user["uuid"], {"expireAt": _expire_at(days_from_now)})


def update_user(uuid_: str, payload: dict) -> dict | None:
    payload = dict(payload)
    payload["uuid"] = uuid_
    try:
        r = requests.patch(
            f"{config.REMNAWAVE_URL}/api/users",
            json=payload, headers=_headers(), timeout=15,
        )
        if r.status_code >= 400:
            log.error("update_user %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json().get("response")
    except Exception as e:
        log.error("update_user: %s", e)
        return None


def disable_user(uuid_: str) -> bool:
    return update_user(uuid_, {"status": "DISABLED"}) is not None


def enable_user(uuid_: str) -> bool:
    return update_user(uuid_, {"status": "ACTIVE"}) is not None


def revoke_subscription(uuid_: str) -> dict | None:
    """Перевыпустить подписку: панель генерирует новый shortUuid/ссылку,
    старая конфигурация перестаёт работать. Срок и тариф сохраняются."""
    if not uuid_:
        return None
    endpoints = (
        f"{config.REMNAWAVE_URL}/api/users/{uuid_}/actions/revoke",
        f"{config.REMNAWAVE_URL}/api/users/{uuid_}/revoke",
    )
    for url in endpoints:
        try:
            r = requests.post(url, headers=_headers(), timeout=15)
            if r.status_code == 404:
                continue
            if r.status_code >= 400:
                log.error("revoke_subscription %s: %s", r.status_code, r.text[:500])
                continue
            resp = r.json().get("response")
            # На всякий случай убеждаемся, что подписка активна.
            return resp or get_user_by_uuid(uuid_)
        except Exception as e:
            log.error("revoke_subscription (%s): %s", url, e)
    return None


def get_all_users() -> list:
    all_users: list = []
    PAGE_SIZE = 100
    start = 0
    try:
        while True:
            r = requests.get(
                f"{config.REMNAWAVE_URL}/api/users?start={start}&size={PAGE_SIZE}",
                headers=_headers(), timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("response", {})
            users = data.get("users", [])
            total = data.get("total", 0)
            if not users:
                break
            existing = {u.get("uuid") for u in all_users}
            all_users.extend([u for u in users if u.get("uuid") not in existing])
            if len(all_users) >= total or len(users) < PAGE_SIZE:
                break
            start += PAGE_SIZE
    except Exception as e:
        log.error("get_all_users: %s", e)
    return all_users


def _extract_hwid(payload) -> list:
    data = payload.get("response", []) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("devices", []) or data.get("hwidDevices", []) or []
    return data if isinstance(data, list) else []


def get_user_hwid(uuid_: str) -> list:
    """Список HWID-устройств пользователя. Пробуем оба варианта эндпойнта Remnawave."""
    if not uuid_:
        return []
    endpoints = (
        f"{config.REMNAWAVE_URL}/api/users/{uuid_}/hwid",
        f"{config.REMNAWAVE_URL}/api/hwid/devices/{uuid_}",
    )
    for url in endpoints:
        try:
            r = requests.get(url, headers=_headers(), timeout=8)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            devices = _extract_hwid(r.json())
            if devices:
                return devices
        except Exception as e:
            log.error("get_user_hwid (%s): %s", url, e)
    return []


def get_nodes() -> list:
    """Список нод/серверов из панели Remnawave (для страницы состояния)."""
    try:
        r = requests.get(
            f"{config.REMNAWAVE_URL}/api/nodes",
            headers=_headers(), timeout=8,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
        if isinstance(data, dict):
            data = data.get("nodes", data.get("data", [])) or []
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error("get_nodes: %s", e)
        return []


def format_sub_url(user: dict) -> str:
    sub = user.get("subscriptionUrl", "")
    if not sub:
        short = user.get("shortUuid", "")
        sub = f"{config.SUB_BASE_URL}/{short}" if short else ""
    return sub


def user_snapshot(user_id: int, remna: dict | None = None) -> dict:
    """Сводка по подписке для шаблона."""
    now = int(time.time())
    if remna is None:
        remna = get_user(user_id)
    if not remna:
        return {"exists": False}
    expire = parse_dt(remna.get("expireAt"))
    traffic = remna.get("userTraffic", {}) or {}
    online_at = parse_dt(traffic.get("onlineAt"))
    squads = _squad_uuids(remna.get("activeInternalSquads"))
    return {
        "exists": True,
        "uuid": remna.get("uuid"),
        "expire": expire,
        "days_left": max(0, (expire - now) // 86400) if expire else 0,
        "used_gb": round((traffic.get("usedTrafficBytes") or 0) / 1024 ** 3, 2),
        "hwid": remna.get("hwidDeviceLimit", 1),
        "status": remna.get("status", "?"),
        "active": expire > now and remna.get("status") != "DISABLED",
        "online": online_at > (now - 180),
        "online_at": online_at,
        "sub_url": format_sub_url(remna),
        "has_whitelist": config.SQUAD_UUID_WHITELIST in squads,
        "raw": remna,
    }
