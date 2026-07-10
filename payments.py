"""
Оплаты через YooKassa для личного кабинета на сайте.

Зовём тот же REST API YooKassa, что и SDK в боте (POST /v3/payments),
но с двумя важными отличиями, чтобы сайт НИКОГДА не вис:

1) На каждый вызов — НОВАЯ requests.Session, которая тут же закрывается.
   Официальный SDK держит ОДНО общее keep-alive соединение: когда оно
   протухает (NAT/firewall рвёт идловые сокеты), следующий Payment.create
   виснет навсегда. Свежая сессия на каждый вызов это исключает.

2) Жёсткий таймаут: connect=10, read=30 с. Если YooKassa молчит — ошибка,
   а не вечное ожидание. Повторы идут с тем же Idempotence-Key,
   поэтому двойного списания быть не может.

Заголовок User-Agent ставим как у SDK (не голый python-requests) — некоторые
сетевые фильтры/WAF придерживают (tarpit) неизвестные UA, что даёт read timeout.

Ключи берутся из окружения: SHOP_ID / YOOKASSA_KEY (те же, что у бота).
"""
import time
import uuid
import logging
import platform

import requests

import config

log = logging.getLogger("payments")

API = "https://api.yookassa.ru/v3/payments"

# (таймаут подключения, таймаут ожидания ответа). read=30 — с запасом,
# чтобы медленный-но-валидный ответ успел, но никогда не вечно.
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30
_RETRIES = 3

# User-Agent в стиле официального SDK yookassa (чтобы выглядеть как бот).
_UA = (
    f"{platform.system() or 'Linux'}/{platform.release() or ''} "
    f"Python/{platform.python_version()} "
    f"YooKassa.Python/3.5.0"
).strip()


def configured() -> bool:
    """Оплата доступна, если есть оба ключа."""
    ok = bool(config.SHOP_ID and config.YOOKASSA_KEY)
    if not ok:
        log.error("YooKassa не настроена: задайте SHOP_ID и YOOKASSA_KEY в окружении")
    return ok


def _request(method: str, url: str, *, retries: int = _RETRIES,
             read_timeout: int = _READ_TIMEOUT, **kwargs):
    """Запрос к YooKassa на свежей сессии с таймаутом и повторами.

    Новая Session на каждый вызов → никогда не переиспользуем старое
    (возможно мёртвое) соединение — именно оно раньше вешало сайт.

    retries / read_timeout можно переопределить на вызов: фоновая проверка
    статуса должна падать быстро, чтобы не тормозить кабинет.
    """
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("User-Agent", _UA)
    headers.setdefault("Accept", "application/json")
    timeout = (_CONNECT_TIMEOUT, read_timeout)
    last = None
    for attempt in range(1, retries + 1):
        sess = requests.Session()
        try:
            return sess.request(method, url, timeout=timeout, headers=headers, **kwargs)
        except requests.exceptions.RequestException as e:
            last = e
            log.warning("YooKassa %s попытка %d/%d: %s", method, attempt, retries, e)
            if attempt < retries:
                time.sleep(1.0 * attempt)
        finally:
            try:
                sess.close()
            except Exception:
                pass
    raise last


def create_payment(amount, description: str, metadata: dict, return_url: str):
    """Создаёт платёж (то же тело, что и у бота). Возвращает
    {id, status, confirmation_url} или None."""
    if not configured():
        return None
    body = {
        "amount":       {"value": f"{int(amount)}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture":      True,
        "description":  (description or "TrubaVPN")[:128],
        "metadata":     metadata,
    }
    # Один Idempotence-Key на все повторы — двойного списания не будет.
    idem = str(uuid.uuid4())
    try:
        r = _request(
            "POST", API, json=body,
            auth=(config.SHOP_ID, config.YOOKASSA_KEY),
            headers={"Idempotence-Key": idem, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "confirmation_url": (data.get("confirmation") or {}).get("confirmation_url"),
        }
    except requests.exceptions.HTTPError as e:
        # Покажем тело ошибки YooKassa (оно объясняет причину: ключи, сумма и т.п.).
        body_txt = ""
        try:
            body_txt = e.response.text[:500]
        except Exception:
            pass
        log.error("create_payment HTTP %s: %s", getattr(e.response, "status_code", "?"), body_txt)
        return None
    except Exception as e:
        log.error("create_payment: %s", e)
        return None


def get_payment(payment_id: str):
    """Проверка статуса платежа. Возвращает {id, status, metadata} или None."""
    if not payment_id or not configured():
        return None
    try:
        # Быстро-падающая проверка: 1 попытка, короткий таймаут — никогда
        # не блокирует страницу кабинета надолго.
        r = _request("GET", f"{API}/{payment_id}",
                     retries=1, read_timeout=8,
                     auth=(config.SHOP_ID, config.YOOKASSA_KEY))
        r.raise_for_status()
        data = r.json()
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "metadata": data.get("metadata") or {},
        }
    except Exception as e:
        log.error("get_payment: %s", e)
        return None
