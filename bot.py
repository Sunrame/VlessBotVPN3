import os
import uuid
import logging
import time
import asyncio
import asyncpg
import httpx
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.markdown import hcode, hbold
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery



# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────
API_TOKEN    = os.environ["BOT_TOKEN"]
SHOP_ID      = os.environ["SHOP_ID"]
YOOKASSA_KEY = os.environ["YOOKASSA_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)

REMNAWAVE_URL    = os.environ["REMNAWAVE_URL"].rstrip("/")
REMNAWAVE_TOKEN  = os.environ["REMNAWAVE_TOKEN"]
REMNAWAVE_COOKIE = os.environ["REMNAWAVE_COOKIE"]
SUB_BASE_URL     = os.environ["SUB_BASE_URL"].rstrip("/")

SQUAD_UUID = "ed383cc2-c7c0-46ea-9237-19ebe8f10465"  # Default-Squad — все сервера

# Сквад с ограниченным набором нод (например, без "белых списков" сервера).
# СОЗДАЙ этот сквад в Remnawave (Внутренние сквады → Создать), включи туда
# все ноды КРОМЕ сервера "белые списки", и вставь сюда его UUID.
SQUAD_UUID_BASIC = os.environ.get("SQUAD_UUID_BASIC", SQUAD_UUID)

# Сквад С доступом к серверу "белые списки" (обычно = SQUAD_UUID, если Default-Squad
# уже включает все ноды, либо отдельный сквад, если хочешь точнее разграничить).
SQUAD_UUID_WHITELIST = os.environ.get("SQUAD_UUID_WHITELIST", SQUAD_UUID)

# UUID самой НОДЫ "белые списки" (не сквада!) — нужен для проверки трафика
# конкретного юзера именно на этом сервере. Скопируй в Remnawave:
# Ноды → [нода "белые списки"] → More actions → Copy Node UUID.
WHITELIST_NODE_UUID = os.environ.get("WHITELIST_NODE_UUID", "")

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "@Truba_VPN")

from yookassa import Configuration, Payment
Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# Московский часовой пояс UTC+3
MSK = timezone(timedelta(hours=3))

def fmt_dt(ts: int, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Unix-timestamp -> строка в московском времени."""
    if not ts:
        return "inf"
    return datetime.fromtimestamp(ts, tz=MSK).strftime(fmt) + " МСК"

def msk_now() -> datetime:
    return datetime.now(MSK)

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
TARIFFS: dict = {
    # squad — какой сквад серверов получает юзер при покупке этого тарифа.
    #   SQUAD_UUID_BASIC     — без сервера "белые списки"
    #   SQUAD_UUID_WHITELIST — с сервером "белые списки"
    # whitelist_gb — лимит трафика (в ГБ) ИМЕННО на сервере "белые списки" для этого
    #   тарифа. 0 = лимит не отслеживается (либо нет доступа к серверу, либо безлимит на нём).
    #   Работает независимо от общего трафика на остальных серверах.
    "trial":  {"name": "Пробный",      "price": 10,  "days": 1,  "desc": "⏱️ Тестовый доступ на 24 часа",                              "trial": True, "hwid": 1,  "squad": SQUAD_UUID_BASIC, "whitelist_gb": 0},
    "1_dev":  {"name": "1 устройство", "price": 99,  "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 1,  "squad": SQUAD_UUID_BASIC, "whitelist_gb": 0},
    "2_dev":  {"name": "2 устройства", "price": 179, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 2,  "squad": SQUAD_UUID_BASIC, "whitelist_gb": 0},
    "5_dev":  {"name": "5 устройств",  "price": 349, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 5,  "squad": SQUAD_UUID_BASIC, "whitelist_gb": 0},
    "family": {"name": "Семейный",     "price": 449, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость\n👨‍👩‍👧‍👦 До 10 устройств", "hwid": 10, "squad": SQUAD_UUID_BASIC, "whitelist_gb": 0},
}

MONTH_OPTIONS = {
    1:  {"label": "1 месяц",   "multiplier": 1.0},
    3:  {"label": "3 месяца",  "multiplier": 2.7},
    6:  {"label": "6 месяцев", "multiplier": 5.1},
    12: {"label": "1 год",     "multiplier": 9.6},
}

HWID_OPTIONS = {
    0: "♾️ Без лимита", 1: "📱 1 уст.", 2: "📱 2 уст.",
    3: "📱 3 уст.", 5: "🖥️ 5 уст.", 10: "💻 10 уст.",
}

# ─────────────────────────────────────────────
#  FSM STATES
# ─────────────────────────────────────────────
class PromoState(StatesGroup):
    waiting_code    = State()
    choosing_tariff = State()

class BroadcastState(StatesGroup):
    waiting_text = State()
    confirming   = State()

class AdminKeyState(StatesGroup):
    waiting_username = State()
    waiting_days     = State()
    waiting_devices  = State()

class AdminPromoState(StatesGroup):
    waiting_input = State()

class DiscountPromoState(StatesGroup):
    waiting_code    = State()
    waiting_percent = State()
    waiting_uses    = State()

class OrderPromoState(StatesGroup):
    waiting_code = State()

class SupportState(StatesGroup):
    waiting_message = State()
    admin_reply     = State()

class TemplateState(StatesGroup):
    waiting_name = State()
    waiting_text = State()

class SurveyState(StatesGroup):
    waiting_rating  = State()
    waiting_comment = State()

class CheckActionState(StatesGroup):
    waiting_days_add    = State()
    waiting_days_sub    = State()
    waiting_days_set    = State()
    waiting_hwid_set    = State()
    waiting_search      = State()
    waiting_whitelist_gb = State()

class MediaState(StatesGroup):
    waiting_username = State()
    waiting_percent  = State()

# ─────────────────────────────────────────────
#  INIT
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("trubavpn")

bot    = Bot(token=API_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()
pool: asyncpg.Pool = None

# ─────────────────────────────────────────────
#  DATABASE — заглушка или реальное подключение
# ─────────────────────────────────────────────
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                referrer_id BIGINT,
                has_paid    INTEGER DEFAULT 0,
                remna_uuid  TEXT,
                created_at  BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY, days INTEGER,
                uses INTEGER DEFAULT 1,
                promo_type TEXT DEFAULT 'days',
                tariff_key TEXT DEFAULT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY, user_id BIGINT,
                amount NUMERIC, tariff_key TEXT, days INTEGER,
                is_trial BOOLEAN DEFAULT FALSE, created_at BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT,
                status TEXT DEFAULT 'open',
                created_at BIGINT DEFAULT 0, updated_at BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY, ticket_id INTEGER, user_id BIGINT,
                is_admin BOOLEAN DEFAULT FALSE, text TEXT, sent_at BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                admin_id    BIGINT PRIMARY KEY,
                dnd         BOOLEAN DEFAULT FALSE,
                sale_notify BOOLEAN DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id SERIAL PRIMARY KEY, name TEXT, text TEXT, admin_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS survey_responses (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                username   TEXT,
                rating     INTEGER,
                comment    TEXT,
                created_at BIGINT DEFAULT 0
            )
        """)
        # Медиа-партнёры (крутые рефералы с процентом от платежей)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS media_partners (
                user_id    BIGINT PRIMARY KEY,
                username   TEXT,
                percent    INTEGER DEFAULT 10,
                created_at BIGINT DEFAULT 0
            )
        """)
        # Лимит трафика ИМЕННО на сервере "белые списки" (не общий по аккаунту)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_limits (
                user_id      BIGINT PRIMARY KEY,
                gb_limit     INTEGER DEFAULT 0,
                period_start BIGINT DEFAULT 0,
                cut_off      BOOLEAN DEFAULT FALSE
            )
        """)
        # Миграции
        for col in ["remna_uuid TEXT", "created_at BIGINT DEFAULT 0", "agreed_tos BOOLEAN DEFAULT FALSE"]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        try:
            await conn.execute("ALTER TABLE promos ADD COLUMN discount_percent INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE admin_settings ADD COLUMN sale_notify BOOLEAN DEFAULT TRUE")
        except Exception:
            pass
    log.info("PostgreSQL ready.")

# ─────────────────────────────────────────────
#  REMNAWAVE API
# ─────────────────────────────────────────────
def _remna_headers() -> dict:
    return {
        "Authorization": f"Bearer {REMNAWAVE_TOKEN}",
        "Content-Type":  "application/json",
        "Cookie":        REMNAWAVE_COOKIE,
    }

def remna_username(user_id: int) -> str:
    return f"truba_{user_id}"

def _squad_uuids(raw_squads) -> list[str]:
    """
    Remnawave в GET-ответе возвращает activeInternalSquads как список ОБЪЕКТОВ
    {"uuid": "...", "name": "..."}, а не список голых строк. При отправке PATCH
    же нужен именно список строк-UUID. Эта функция нормализует любой вариант
    (список объектов, список строк, вперемешку) в чистый список строк.
    """
    if not raw_squads:
        return []
    out = []
    for s in raw_squads:
        if isinstance(s, dict):
            u = s.get("uuid")
            if u:
                out.append(u)
        elif isinstance(s, str):
            out.append(s)
    return out

def _expire_at(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

async def remna_get_user(user_id: int) -> dict | None:
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.get(
                f"{REMNAWAVE_URL}/api/users/by-username/{remna_username(user_id)}",
                headers=_remna_headers(), timeout=15,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] get_user: %s", e)
        return None

async def remna_get_user_by_uuid(uuid_: str) -> dict | None:
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.get(
                f"{REMNAWAVE_URL}/api/users/{uuid_}",
                headers=_remna_headers(), timeout=15,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] get_user_by_uuid: %s", e)
        return None

async def remna_create_user(user_id: int, days: int, hwid: int = 1, squad_uuid: str = SQUAD_UUID) -> dict | None:
    payload = {
        "username":             remna_username(user_id),
        "trafficLimitBytes":    0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt":             _expire_at(days),
        "hwidDeviceLimit":      hwid,
        "telegramId":           user_id,
        "activeInternalSquads": [squad_uuid],
    }
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.post(
                f"{REMNAWAVE_URL}/api/users",
                json=payload, headers=_remna_headers(), timeout=15,
            )
            if r.status_code == 409:
                return await remna_extend_user(user_id, days, hwid, squad_uuid)
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] create_user: %s", e)
        return None

async def remna_extend_user(user_id: int, days: int, hwid: int | None = None,
                             squad_uuid: str | None = None) -> dict | None:
    user = await remna_get_user(user_id)
    if not user:
        return await remna_create_user(user_id, days, hwid or 1, squad_uuid or SQUAD_UUID)

    now        = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
    base       = max(current, now)
    new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload: dict = {"uuid": user["uuid"], "expireAt": new_expire}
    # Сквад меняем только если явно передан — иначе не трогаем текущий
    # (важно для команд типа /give, ca_adddays, где squad_uuid не указывается)
    if squad_uuid is not None:
        payload["activeInternalSquads"] = [squad_uuid]
    if hwid is not None:
        payload["hwidDeviceLimit"] = hwid

    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.patch(
                f"{REMNAWAVE_URL}/api/users",
                json=payload, headers=_remna_headers(), timeout=15,
            )
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] extend_user: %s", e)
        return None

async def remna_update_user(uuid_: str, payload: dict) -> dict | None:
    payload["uuid"] = uuid_
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.patch(
                f"{REMNAWAVE_URL}/api/users",
                json=payload, headers=_remna_headers(), timeout=15,
            )
            if r.status_code >= 400:
                log.error("[Remna] update_user %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] update_user: %s", e)
        return None

async def remna_update_user_verbose(uuid_: str, payload: dict) -> tuple[dict | None, str]:
    """Как remna_update_user, но дополнительно возвращает текст ответа для диагностики."""
    payload = dict(payload)
    payload["uuid"] = uuid_
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.patch(
                f"{REMNAWAVE_URL}/api/users",
                json=payload, headers=_remna_headers(), timeout=15,
            )
            body = r.text[:500]
            if r.status_code >= 400:
                log.error("[Remna] update_user %s: %s", r.status_code, body)
                return None, f"HTTP {r.status_code}: {body}"
            r.raise_for_status()
            return r.json().get("response"), "OK"
    except Exception as e:
        log.error("[Remna] update_user: %s", e)
        return None, str(e)

async def remna_disable_user(uuid_: str) -> bool:
    result = await remna_update_user(uuid_, {"status": "DISABLED"})
    return result is not None

async def remna_get_all_users() -> list:
    """Получает ВСЕХ пользователей через start+size (единственная рабочая пагинация)."""
    all_users: list = []
    PAGE_SIZE = 100
    start = 0
    try:
        async with httpx.AsyncClient(verify=True) as client:
            while True:
                r = await client.get(
                    f"{REMNAWAVE_URL}/api/users?start={start}&size={PAGE_SIZE}",
                    headers=_remna_headers(), timeout=30,
                )
                r.raise_for_status()
                data  = r.json().get("response", {})
                users = data.get("users", [])
                total = data.get("total", 0)
                if not users:
                    break
                existing = {u.get("uuid") for u in all_users}
                new_u = [u for u in users if u.get("uuid") not in existing]
                all_users.extend(new_u)
                log.info("[Remna] get_all_users start=%d got=%d new=%d total=%d collected=%d",
                         start, len(users), len(new_u), total, len(all_users))
                if len(all_users) >= total or len(users) < PAGE_SIZE:
                    break
                start += PAGE_SIZE
    except Exception as e:
        log.error("[Remna] get_all_users: %s", e)
    return all_users

async def remna_get_user_hwid(uuid_: str) -> list:
    """Список HWID-устройств пользователя: GET /api/users/{uuid}/hwid"""
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.get(
                f"{REMNAWAVE_URL}/api/users/{uuid_}/hwid",
                headers=_remna_headers(), timeout=15,
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json().get("response", [])
            if isinstance(data, dict):
                data = data.get("devices", []) or data.get("hwidDevices", []) or []
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error("[Remna] get_user_hwid: %s", e)
        return []

async def remna_get_node_bandwidth(node_uuid: str, start_dt: datetime, end_dt: datetime) -> list:
    """
    Трафик по каждому юзеру на КОНКРЕТНОЙ ноде, по дням.
    GET /api/bandwidth-stats/nodes/{uuid}/users/legacy?start=...&end=...
    Формат дат подтверждён: ISO с миллисекундами, например 2026-04-07T10:33:25.000Z
    Ответ — плоский список записей вида:
        {"userUuid": "...", "nodeUuid": "...", "username": "truba_123", "total": 12345, "date": "2026-07-05T00:00:00.000Z"}
    Один юзер встречается НЕСКОЛЬКО раз — по одной записи на каждый день,
    total — байты ЗА ЭТОТ ДЕНЬ (не накопительно), нужно суммировать самим.
    """
    if not node_uuid:
        return []
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.get(
                f"{REMNAWAVE_URL}/api/bandwidth-stats/nodes/{node_uuid}/users/legacy",
                params={"start": start_str, "end": end_str},
                headers=_remna_headers(), timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("response", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error("[Remna] get_node_bandwidth: %s", e)
        return []

async def fetch_whitelist_daily_records(days_back: int = 40) -> list[dict]:
    """
    Сырые записи по дням для ВСЕХ юзеров на ноде 'белые списки' за широкое окно
    (с запасом на самый длинный возможный тарифный период). Один вызов на всех,
    дальше каждый юзер фильтруется по своему period_start локально —
    т.к. у разных юзеров разное время начала периода.
    Возвращает список {username, ts (unix, начало дня), bytes}.
    """
    if not WHITELIST_NODE_UUID:
        return []
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)
    raw = await remna_get_node_bandwidth(WHITELIST_NODE_UUID, start_dt, end_dt)

    out = []
    for rec in raw:
        uname = rec.get("username", "")
        date_str = rec.get("date", "")
        try:
            day_ts = int(datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        out.append({
            "username": uname,
            "ts": day_ts,
            "bytes": int(rec.get("total", 0) or 0),
        })
    return out

def sum_whitelist_bytes_for_user(records: list[dict], user_id: int, since_ts: int) -> int:
    """Суммирует байты юзера из уже полученных daily-записей, начиная с since_ts."""
    uname = remna_username(user_id)  # "truba_<id>"
    return sum(r["bytes"] for r in records if r["username"] == uname and r["ts"] >= since_ts)

async def activate_subscription(user_id: int, days: int, hwid: int = 1,
                                 squad_uuid: str | None = None,
                                 whitelist_gb: int = 0) -> dict | None:
    """
    squad_uuid=None — не менять текущий сквад юзера (для /give, ca_adddays и т.п.,
    где явного тарифа нет).
    whitelist_gb>0 — тариф включает доступ к серверу "белые списки" с лимитом
    в ГБ именно на нём; заводим/обновляем запись в whitelist_limits.
    """
    user = await remna_get_user(user_id)
    if user:
        result = await remna_extend_user(user_id, days, hwid, squad_uuid)
    else:
        result = await remna_create_user(user_id, days, hwid, squad_uuid or SQUAD_UUID)

    if result:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET remna_uuid=$1 WHERE user_id=$2",
                result.get("uuid"), user_id,
            )
            if whitelist_gb > 0:
                # Новый период отсчёта лимита "белых списков" — с текущего момента
                await conn.execute(
                    "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                    "VALUES ($1,$2,$3,FALSE) "
                    "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, period_start=$3, cut_off=FALSE",
                    user_id, whitelist_gb, int(time.time()),
                )
            elif squad_uuid is not None and squad_uuid != SQUAD_UUID_WHITELIST:
                # Купили тариф БЕЗ белых списков — снимаем отслеживание, если было
                await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)
    return result

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True

async def is_admin_dnd(admin_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dnd FROM admin_settings WHERE admin_id=$1", admin_id)
        return row["dnd"] if row else False

async def is_admin_sale_notify(admin_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sale_notify FROM admin_settings WHERE admin_id=$1", admin_id)
        if row is None:
            return True
        v = row["sale_notify"]
        return v if v is not None else True

async def notify_admins_sale(u_id: int, username: str | None, tariff_name: str,
                              days: int, price: float, is_trial: bool):
    """Уведомить всех админов о новой покупке."""
    uname   = f"@{username}" if username else f"ID:{u_id}"
    kind    = "🔬 Триал" if is_trial else "💰 Оплата"
    now_str = fmt_dt(int(time.time()))
    text = (
        f"🛒 <b>Новая покупка!</b>\n\n"
        f"👤 {uname} (ID: <code>{u_id}</code>)\n"
        f"📦 Тариф: <b>{tariff_name}</b>\n"
        f"📅 Дней: <b>{days}</b>\n"
        f"💵 Сумма: <b>{price:.0f} ₽</b>\n"
        f"🏷 Тип: {kind}\n"
        f"🕐 Время: <b>{now_str}</b>"
    )
    for admin_id in ADMIN_IDS:
        if await is_admin_sale_notify(admin_id):
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass

async def is_media_partner(user_id: int) -> dict | None:
    """Вернёт запись media_partners если юзер — медиа-партнёр."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM media_partners WHERE user_id=$1", user_id)
        return dict(row) if row else None

async def notify_media_partner_sale(referrer_id: int, buyer_id: int, buyer_username: str | None,
                                     tariff_name: str, days: int, price: float):
    """Если покупатель пришёл от медиа-партнёра — уведомить партнёра с расчётом его %."""
    partner = await is_media_partner(referrer_id)
    if not partner:
        return
    percent = partner.get("percent", 10)
    earned  = round(price * percent / 100, 2)
    buyer_label = f"@{buyer_username}" if buyer_username else f"ID:{buyer_id}"
    text = (
        f"💼 <b>Ваш реферал купил подписку!</b>\n\n"
        f"👤 {buyer_label}\n"
        f"📦 Тариф: <b>{tariff_name}</b> · {days} дн.\n"
        f"💵 Сумма покупки: <b>{price:.0f} ₽</b>\n"
        f"🎯 Ваш процент ({percent}%): <b>{earned:.2f} ₽</b>"
    )
    try:
        await bot.send_message(referrer_id, text, parse_mode="HTML")
    except Exception:
        pass

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

def format_sub_url(user: dict) -> str:
    sub = user.get("subscriptionUrl", "")
    if not sub:
        short = user.get("shortUuid", "")
        sub = f"{SUB_BASE_URL}/{short}" if short else ""
    return sub

def format_key_message(user: dict) -> str:
    expire  = parse_dt(user.get("expireAt"))
    sub_url = format_sub_url(user)
    date_str = fmt_dt(expire) if expire else "inf"

    lines = [f"🗓 Подписка до: <b>{date_str}</b>", "", "━━━━━━━━━━━━━━━━━━━━"]
    if sub_url:
        lines += [
            "🌐 <b>Ссылка на подписку</b> (рекомендуется):",
            "<i>Импортируйте в Happ / v2rayNG — обновляется автоматически.</i>",
            hcode(sub_url), "",
        ]
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"📖 Инструкция: {CHANNEL_LINK}"]
    return "\n".join(lines)

def calc_price(base: int, months: int) -> int:
    return round(base * MONTH_OPTIONS[months]["multiplier"])

def calc_days(base: int, months: int) -> int:
    return base * months

async def _decrement_promo(code: str, uses: int):
    async with pool.acquire() as conn:
        if uses <= 1:
            await conn.execute("DELETE FROM promos WHERE code=$1", code)
        else:
            await conn.execute("UPDATE promos SET uses=uses-1 WHERE code=$1", code)

# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def main_kb(is_admin: bool = False):
    rows = [
        [InlineKeyboardButton(text="💰 Купить VPN",  callback_data="tariffs"),
         InlineKeyboardButton(text="👤 Профиль",     callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы",    callback_data="ref_program"),
         InlineKeyboardButton(text="📞 Промокод",    callback_data="promo_enter")],
        [InlineKeyboardButton(text="💬 Поддержка",   callback_data="support_open"),
         InlineKeyboardButton(text="ℹ️ Инфо",        callback_data="info_tab")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="back")]
    ])

def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])

def tos_agree_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Пользовательское соглашение",
                              url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
        [InlineKeyboardButton(text="🔐 Политика конфиденциальности",
                              url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
        [InlineKeyboardButton(text="✅ Я согласен", callback_data="agree_tos")],
    ])

def months_kb(tariff_key: str):
    info = TARIFFS[tariff_key]
    rows = []
    for months, opt in MONTH_OPTIONS.items():
        total = calc_price(info["price"], months)
        if months == 1:
            label = f"{opt['label']} — {total} ₽"
        else:
            per  = round(total / months)
            disc = round((1 - per / info["price"]) * 100)
            label = f"{opt['label']} — {total} ₽  (−{disc}%)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buym_{tariff_key}_{months}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="tariffs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def free_tariff_kb(code: str):
    rows = []
    for k, v in TARIFFS.items():
        if v.get("trial"):
            continue
        rows.append([InlineKeyboardButton(text=v["name"], callback_data=f"pfree_{k}_{code}")])
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def hwid_kb(prefix: str):
    rows = []
    row  = []
    for limit, label in HWID_OPTIONS.items():
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}{limit}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="gk_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def support_user_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть обращение", callback_data="support_close_user")],
        [InlineKeyboardButton(text="← Назад",      callback_data="support_to_main")],
    ])

def support_ticket_kb(ticket_id: int, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"sreply_{ticket_id}_{user_id}"),
         InlineKeyboardButton(text="📋 Шаблон",  callback_data=f"stmpl_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="✅ Закрыть",  callback_data=f"sclose_{ticket_id}")],
    ])

def _check_kb(user_id: int, hwid: int, has_whitelist: bool = False) -> InlineKeyboardMarkup:
    preset_rows = []
    row = []
    for limit, label in HWID_OPTIONS.items():
        mark = "✅" if limit == hwid else ""
        row.append(InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"setlim_{user_id}_{limit}"
        ))
        if len(row) == 3:
            preset_rows.append(row); row = []
    if row:
        preset_rows.append(row)
    whitelist_btn_text = "📡 Забрать белые списки" if has_whitelist else "📡 Дать белые списки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Дни",  callback_data=f"ca_adddays_{user_id}"),
            InlineKeyboardButton(text="➖ Дни",  callback_data=f"ca_subdays_{user_id}"),
            InlineKeyboardButton(text="📅 Дата", callback_data=f"ca_setdate_{user_id}"),
        ],
        [InlineKeyboardButton(text="📱 Устройства — ввести число", callback_data=f"ca_sethwid_{user_id}")],
        [InlineKeyboardButton(text="📋 Список устройств",          callback_data=f"ca_devices_{user_id}")],
        [InlineKeyboardButton(text=whitelist_btn_text, callback_data=f"ca_whitelist_{user_id}")],
        *preset_rows,
        [InlineKeyboardButton(text="🚫 Забрать подписку", callback_data=f"quicktake_{user_id}")],
        [
            InlineKeyboardButton(text="👥 Подписчики", callback_data="admin_subs"),
            InlineKeyboardButton(text="🔍 Поиск",      callback_data="admin_search"),
        ],
    ])

# ─────────────────────────────────────────────
#  СТАРТ
# ─────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    u_id = message.from_user.id
    r_id = None
    if command.args and command.args.isdigit():
        candidate = int(command.args)
        if candidate != u_id:
            r_id = candidate

    now = int(time.time())
    async with pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT user_id, agreed_tos FROM users WHERE user_id=$1", u_id)
        if not exists:
            await conn.execute(
                "INSERT INTO users (user_id, username, referrer_id, created_at) VALUES ($1,$2,$3,$4)",
                u_id, message.from_user.username, r_id, now,
            )
            agreed_tos = False
        else:
            await conn.execute("UPDATE users SET username=$1 WHERE user_id=$2",
                               message.from_user.username, u_id)
            agreed_tos = exists["agreed_tos"] or False

    if not agreed_tos:
        await message.answer(
            f"🌏 {hbold('TrubaVPN')}\n\n"
            "Прежде чем продолжить, ознакомьтесь с документами и подтвердите согласие:",
            reply_markup=tos_agree_kb(), parse_mode="HTML",
        )
        return

    if not await is_subscribed(u_id):
        await message.answer(
            f"🌏 {hbold('TrubaVPN')}\n\nПодпишитесь на канал чтобы пользоваться ботом.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    text, kb = await _build_profile_view(u_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "agree_tos")
async def agree_tos_cb(cb: CallbackQuery):
    await cb.answer()
    u_id = cb.from_user.id
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET agreed_tos=TRUE WHERE user_id=$1", u_id)

    if not await is_subscribed(u_id):
        await cb.message.edit_text(
            f"🌏 {hbold('TrubaVPN')}\n\nПодпишитесь на канал чтобы пользоваться ботом.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    text, kb = await _build_profile_view(u_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(cb: CallbackQuery):
    await cb.answer()
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Вы ещё не подписаны.", show_alert=True)
        return
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(cb.from_user.id in ADMIN_IDS), parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ТАРИФЫ И ОПЛАТА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "tariffs")
async def show_tariffs(cb: CallbackQuery):
    await cb.answer()
    btns = []
    for k, v in TARIFFS.items():
        label = f"{v['name']} — {v['price']} ₽" if v.get("trial") else f"{v['name']} — от {v['price']} ₽/мес."
        btns.append([InlineKeyboardButton(text=label, callback_data=f"buy_{k}")])
    btns.append([InlineKeyboardButton(text="← Назад", callback_data="back")])
    await cb.message.edit_text(
        "💰 <b>Выберите тариф:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("buy_") & ~F.data.startswith("buym_"))
async def process_buy(cb: CallbackQuery):
    await cb.answer()
    t_key = cb.data.removeprefix("buy_")
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    info = TARIFFS[t_key]
    if info.get("trial"):
        await _show_payment_page(cb, t_key, 1)
        return
    await cb.message.edit_text(
        f"<b>{info['name']}</b>\n\n{info['desc']}\n\n📅 Выберите период:",
        reply_markup=months_kb(t_key), parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("buym_"))
async def process_buy_months(cb: CallbackQuery):
    await cb.answer()
    parts  = cb.data.removeprefix("buym_").rsplit("_", 1)
    t_key  = parts[0]
    months = int(parts[1])
    await _show_payment_page(cb, t_key, months)

async def _show_payment_page(cb: CallbackQuery, t_key: str, months: int,
                              discount: int = 0, promo_code: str = ""):
    info       = TARIFFS[t_key]
    days       = calc_days(info["days"], months)
    price_full = calc_price(info["price"], months) if not info.get("trial") else info["price"]
    price      = round(price_full * (1 - discount / 100)) if discount > 0 else price_full
    month_label = "24 часа" if info.get("trial") else MONTH_OPTIONS.get(months, {}).get("label", f"{months} мес.")
    desc_parts  = [f"TrubaVPN — {info['name']} / {month_label}"]
    if promo_code:
        desc_parts.append(f"промокод {promo_code}")
    try:
        payment = Payment.create({
            "amount":       {"value": f"{price}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture":      True,
            "description":  " | ".join(desc_parts),
            "metadata":     {
                "user_id":    str(cb.from_user.id),
                "days":       str(days),
                "tariff_key": t_key,
                "hwid":       str(info.get("hwid", 1)),
                "price":      str(price),
                "is_trial":   "1" if info.get("trial") else "0",
            },
        }, str(uuid.uuid4()))
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа.", show_alert=True)
        return

    price_line = f"💰 К оплате: <b>{price} ₽</b>"
    if discount > 0:
        price_line += f"  <s>{price_full} ₽</s>  🎁 Скидка {discount}%"

    promo_btn_text = f"🎟 Промокод применён: {promo_code} (−{discount}%)" if promo_code else "🎟 Ввести промокод"
    promo_cb       = "opromo:applied" if promo_code else f"opromo:{t_key}:{months}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",        url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text=promo_btn_text,        callback_data=promo_cb)],
        [InlineKeyboardButton(text="← Назад",            callback_data=f"buy_{t_key}")],
        [InlineKeyboardButton(text="🏠 Главное меню",     callback_data="back")],
    ])
    await cb.message.edit_text(
        f"<b>{info['name']}</b>  ·  {month_label}\n\n{info['desc']}\n\n"
        f"{price_line}\n\nПосле оплаты нажмите «Проверить оплату».",
        reply_markup=kb, parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("check_"))
async def check_payment(cb: CallbackQuery):
    await cb.answer()
    pay_id = cb.data.removeprefix("check_")
    try:
        payment = Payment.find_one(pay_id)
    except Exception as e:
        log.exception("Payment find error: %s", e)
        await cb.answer("Ошибка проверки платежа.", show_alert=True)
        return
    if payment.status != "succeeded":
        await cb.answer("Платёж ещё не подтверждён. Попробуйте через минуту.", show_alert=True)
        return

    u_id     = int(payment.metadata["user_id"])
    days     = int(payment.metadata["days"])
    hwid     = int(payment.metadata.get("hwid", 1))
    price    = float(payment.metadata.get("price", 0))
    t_key    = payment.metadata.get("tariff_key", "")
    is_trial = payment.metadata.get("is_trial", "0") == "1"

    tariff_info  = TARIFFS.get(t_key, {})
    squad_uuid   = tariff_info.get("squad", SQUAD_UUID_BASIC)
    whitelist_gb = tariff_info.get("whitelist_gb", 0)

    user = await activate_subscription(u_id, days, hwid, squad_uuid=squad_uuid, whitelist_gb=whitelist_gb)
    if not user:
        await cb.answer("Ошибка активации. Напишите в поддержку.", show_alert=True)
        return

    uname_for_notify = None
    referrer_id       = None
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, created_at) VALUES ($1,$2,$3,$4,$5,$6)",
            u_id, price, t_key, days, is_trial, int(time.time()),
        )
        row = await conn.fetchrow("SELECT referrer_id, has_paid, username FROM users WHERE user_id=$1", u_id)
        if row:
            uname_for_notify = row["username"]
            referrer_id      = row["referrer_id"]
            if row["referrer_id"] and row["has_paid"] == 0:
                ref_id = row["referrer_id"]
                await activate_subscription(u_id,   7)
                await activate_subscription(ref_id, 7)
                try:
                    await bot.send_message(ref_id,
                        "🤝 Ваш друг оплатил подписку!\nВам и ему начислено по <b>+7 дней</b>.",
                        parse_mode="HTML")
                except Exception:
                    pass
        await conn.execute(
            "UPDATE users SET has_paid=1, remna_uuid=$1 WHERE user_id=$2",
            user.get("uuid"), u_id,
        )

    tariff_name = TARIFFS.get(t_key, {}).get("name", t_key)

    # Уведомление о покупке — всем админам с включённым sale_notify
    await notify_admins_sale(u_id, uname_for_notify, tariff_name, days, price, is_trial)

    # Уведомление медиа-партнёру (если покупатель пришёл по его ссылке)
    if referrer_id and not is_trial:
        await notify_media_partner_sale(referrer_id, u_id, uname_for_notify, tariff_name, days, price)

    await cb.message.edit_text(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n{format_key_message(user)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  ПРОМОКОД НА СКИДКУ ПРИ ОФОРМЛЕНИИ ЗАКАЗА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "opromo:applied")
async def order_promo_already(cb: CallbackQuery):
    await cb.answer("✅ Промокод уже применён!", show_alert=True)

@router.callback_query(F.data.startswith("opromo:") & (F.data != "opromo:applied"))
async def order_promo_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    rest = cb.data[len("opromo:"):]
    t_key, months_str = rest.rsplit(":", 1)
    await state.set_state(OrderPromoState.waiting_code)
    await state.update_data(t_key=t_key, months=int(months_str))
    await cb.message.edit_text(
        "🎟 <b>Введите промокод на скидку:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"buym_{t_key}_{months_str}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
        ]),
    )

@router.message(OrderPromoState.waiting_code)
async def order_promo_check(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    data = await state.get_data()
    t_key  = data["t_key"]
    months = data["months"]
    await state.clear()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT promo_type, discount_percent, uses FROM promos WHERE code=$1", code
        )

    if not row or row["promo_type"] != "discount" or (row["discount_percent"] or 0) <= 0:
        await message.answer(
            "❌ Промокод не найден или не является промокодом на скидку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← К оплате", callback_data=f"buym_{t_key}_{months}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
            ]),
        )
        return

    discount = row["discount_percent"]
    await message.answer(
        f"✅ Промокод <b>{code}</b> принят — скидка <b>{discount}%</b>!\n\nНажмите кнопку ниже:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Применить и перейти к оплате",
                                  callback_data=f"oapply:{t_key}:{months}:{discount}:{code}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
        ]),
    )

@router.callback_query(F.data.startswith("oapply:"))
async def order_apply_discount(cb: CallbackQuery):
    await cb.answer()
    rest = cb.data[len("oapply:"):]
    t_key, months_str, discount_str, code = rest.split(":", 3)
    months   = int(months_str)
    discount = int(discount_str)
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    await _show_payment_page(cb, t_key, months, discount=discount, promo_code=code)

# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def _build_profile_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Общий рендер профиля — используется и в /start, и в кнопке «Профиль»."""
    user = await remna_get_user(user_id)
    now  = int(time.time())
    if user and parse_dt(user.get("expireAt")) > now and user.get("status") != "DISABLED":
        expire    = parse_dt(user.get("expireAt"))
        days_left = (expire - now) // 86400
        date_str  = fmt_dt(expire, "%d.%m.%Y")
        sub_url   = format_sub_url(user)
        sub_line  = f"\n\n🌐 <b>Ссылка на подписку:</b>\n{hcode(sub_url)}" if sub_url else ""
        hwid      = user.get("hwidDeviceLimit", 0)
        hwid_lbl  = HWID_OPTIONS.get(hwid, f"{hwid} уст.")
        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна · до <b>{date_str}</b>\n"
            f"⏳ Осталось: <b>{days_left} дн.</b>\n"
            f"📱 Устройств: <b>{hwid_lbl}</b>"
            f"{sub_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n❌ Подписка не активна.\n"
            "Нажмите «💰 Купить VPN» для оформления."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Купить VPN", callback_data="tariffs")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
    ])
    return text, kb

@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    await cb.answer()
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

async def _show_sub_info_inplace(message: types.Message, user_id: int):
    user = await remna_get_user(user_id)
    now  = int(time.time())
    if not user or parse_dt(user.get("expireAt")) <= now or user.get("status") == "DISABLED":
        await message.edit_text(
            "❌ У в��с нет активной подписки.\nНажмите «💰 Купить VPN» для оформления.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Купить VPN", callback_data="tariffs")],
                [InlineKeyboardButton(text="← Назад", callback_data="back")],
            ]),
            parse_mode="HTML",
        )
        return
    expire    = parse_dt(user.get("expireAt"))
    days_left = (expire - now) // 86400
    date_str  = fmt_dt(expire)
    sub_url   = format_sub_url(user)
    used_gb   = round((user.get("userTraffic", {}).get("usedTrafficBytes") or 0) / 1024**3, 2)
    hwid      = user.get("hwidDeviceLimit", 0)
    hwid_lbl  = HWID_OPTIONS.get(hwid, f"{hwid} уст.")
    await message.edit_text(
        f"📋 <b>Ваша подписка</b>\n\n"
        f"✅ Статус: <b>Активна</b>\n"
        f"📱 Тариф: <b>{hwid_lbl}</b>\n"
        f"📅 До: <b>{date_str}</b>\n"
        f"⏳ Осталось: <b>{days_left} дн.</b>\n"
        f"📊 Использо��ано: <b>{used_gb} GB</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Ссылка на подписку:</b>\n{hcode(sub_url)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 Инструкция: {CHANNEL_LINK}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить", callback_data="tariffs")],
            [InlineKeyboardButton(text="← Назад", callback_data="back")],
        ]),
    )
# ─────────────────────────────────────────────
#  ПРОМОКОД (пользовательский)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(
        "📞 <b>Введите промокод:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="promo_cancel")],
        ]),
    )

@router.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(cb.from_user.id in ADMIN_IDS), parse_mode="HTML",
    )

@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT days, uses, promo_type, tariff_key FROM promos WHERE code=$1", code
        )
    if not row:
        await message.answer(
            "❌ Неверный промокод.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")],
            ]),
        )
        return
    promo_type = row["promo_type"] or "days"
    tariff_key = row["tariff_key"]
    days = row["days"]; uses = row["uses"]

    if promo_type == "free_tariff" and tariff_key and tariff_key in TARIFFS:
        await state.clear()
        info = TARIFFS[tariff_key]
        user = await activate_subscription(
            message.from_user.id, days, info.get("hwid", 1),
            squad_uuid=info.get("squad", SQUAD_UUID_BASIC),
            whitelist_gb=info.get("whitelist_gb", 0),
        )
        await _decrement_promo(code, uses)
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован!\n"
            f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
            f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
            parse_mode="HTML", reply_markup=back_kb(),
        )
        return
    if promo_type == "free_choice":
        await state.set_state(PromoState.choosing_tariff)
        await state.update_data(promo_code=code, promo_days=days, promo_uses=uses)
        await message.answer(
            f"📞 Промокод <b>{code}</b> — <b>{days} дней</b> бесплатно!\n\nВыберите тариф:",
            parse_mode="HTML", reply_markup=free_tariff_kb(code),
        )
        return
    user = await activate_subscription(message.from_user.id, days)
    await _decrement_promo(code, uses)
    await state.clear()
    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован — добавлено <b>{days} дн.</b>\n\n"
        f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

@router.callback_query(F.data.startswith("pfree_"))
async def handle_free_tariff_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, t_key, promo_code = cb.data.split("_", 2)
    data = await state.get_data()
    days = data.get("promo_days", 30); uses = data.get("promo_uses", 1)
    await state.clear()
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True); return
    info = TARIFFS[t_key]
    user = await activate_subscription(
        cb.from_user.id, days, info.get("hwid", 1),
        squad_uuid=info.get("squad", SQUAD_UUID_BASIC),
        whitelist_gb=info.get("whitelist_gb", 0),
    )
    await _decrement_promo(promo_code, uses)
    await cb.message.edit_text(
        f"✅ ��ромокод <b>{promo_code}</b> активирован!\n"
        f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
        f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  РЕФЕРАЛЫ / ИНФО
# ─────────────────────────────────────────────
@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    await cb.answer()
    me   = await bot.get_me()
    link = f"{{https://t.me/{me.username}}}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Пригласите друга — при его первой оплате вы оба получите <b>+7 дней</b>!\n\n"
        f"Ваша ссылка:\n{hcode(link)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "ℹ️ <b>Информация:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📖 Канал с инструкциями", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="📄 Пользовательское соглашение",
                                  url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
            [InlineKeyboardButton(text="🔐 Политика конфиденциальности",
                                  url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
            [InlineKeyboardButton(text="← Назад", callback_data="back")],
        ]),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ПОДДЕРЖКА — пользователь
# ─────────────────────────────────────────────
@router.callback_query(F.data == "support_open")
async def support_open(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    async with pool.acquire() as conn:
        ticket = await conn.fetchrow(
            "SELECT id FROM support_tickets WHERE user_id=$1 AND status='open'",
            cb.from_user.id
        )
    if ticket:
        await state.set_state(SupportState.waiting_message)
        await state.update_data(ticket_id=ticket["id"])
    else:
        await state.set_state(SupportState.waiting_message)
    await cb.message.edit_text(
        "💬 <b>Поддержка</b>\n\nОпишите вашу проблему (текст ил�� фото):",
        parse_mode="HTML", reply_markup=support_user_kb(),
    )

@router.callback_query(F.data == "support_close_user")
async def support_close_user(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if data.get("ticket_id"):
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE support_tickets SET status='closed', updated_at=$1 WHERE id=$2",
                int(time.time()), data["ticket_id"],
            )
    await state.clear()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(cb.from_user.id in ADMIN_IDS), parse_mode="HTML",
    )

@router.callback_query(F.data == "support_to_main")
async def support_to_main_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(cb.from_user.id in ADMIN_IDS), parse_mode="HTML",
    )

async def _process_support_message(message: types.Message, state: FSMContext):
    u_id  = message.from_user.id
    data  = await state.get_data()
    now   = int(time.time())
    uname = f"@{message.from_user.username}" if message.from_user.username else f"ID:{u_id}"

    is_photo    = bool(message.photo)
    text_body   = message.caption or message.text or ""
    display_txt = text_body if text_body else ("📷 Фото" if is_photo else "")

    async with pool.acquire() as conn:
        ticket_id = data.get("ticket_id")
        if not ticket_id:
            ticket_id = await conn.fetchval(
                "INSERT INTO support_tickets (user_id, username, status, created_at, updated_at) "
                "VALUES ($1,$2,'open',$3,$3) RETURNING id",
                u_id, message.from_user.username or str(u_id), now,
            )
            await state.update_data(ticket_id=ticket_id)
        else:
            await conn.execute("UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id)
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,FALSE,$3,$4)",
            ticket_id, u_id, display_txt, now,
        )

    await message.answer("✅ Сообщение отправлено. Ожидайте ответа.", reply_markup=support_user_kb())

    header = f"📨 <b>Поддержка</b> · #{ticket_id}\n\nОт: {uname}"
    if text_body:
        header += f"\n\n{text_body}"

    for admin_id in ADMIN_IDS:
        if not await is_admin_dnd(admin_id):
            try:
                if is_photo:
                    await bot.send_photo(
                        admin_id,
                        photo=message.photo[-1].file_id,
                        caption=header,
                        parse_mode="HTML",
                        reply_markup=support_ticket_kb(ticket_id, u_id),
                    )
                else:
                    await bot.send_message(
                        admin_id, header,
                        parse_mode="HTML",
                        reply_markup=support_ticket_kb(ticket_id, u_id),
                    )
            except Exception:
                pass

@router.message(SupportState.waiting_message, F.photo)
async def support_user_photo(message: types.Message, state: FSMContext):
    await _process_support_message(message, state)

@router.message(SupportState.waiting_message, F.text)
async def support_user_message(message: types.Message, state: FSMContext):
    await _process_support_message(message, state)

@router.callback_query(F.data.startswith("sreply_"))
async def admin_reply_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    _, ticket_id_str, user_id_str = cb.data.split("_", 2)
    await state.set_state(SupportState.admin_reply)
    await state.update_data(ticket_id=int(ticket_id_str), reply_to_user=int(user_id_str))
    await cb.message.answer(
        f"✍️ Ответ на тикет #{ticket_id_str}:\n/cancel — отмена",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"tview_{ticket_id_str}")],
        ]),
    )

@router.message(Command("cancel"), SupportState.admin_reply)
async def admin_reply_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.")

@router.message(SupportState.admin_reply)
async def admin_reply_send(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data()
    ticket_id = data["ticket_id"]; user_id = data["reply_to_user"]
    await state.clear()
    now = int(time.time())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,TRUE,$3,$4)",
            ticket_id, message.from_user.id, message.text, now,
        )
        await conn.execute("UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id)
    try:
        await bot.send_message(user_id,
            f"💬 <b>Ответ поддержки</b> (#{ticket_id}):\n\n{message.text}",
            parse_mode="HTML", reply_markup=support_user_kb(),
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        await message.answer(f"❌ Не удалось доставить: {e}")

@router.callback_query(F.data.startswith("sclose_"))
async def admin_close_ticket(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    ticket_id = int(cb.data.removeprefix("sclose_"))
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM support_tickets WHERE id=$1", ticket_id)
        await conn.execute(
            "UPDATE support_tickets SET status='closed', updated_at=$1 WHERE id=$2",
            int(time.time()), ticket_id,
        )
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"✅ Тикет #{ticket_id} закрыт.")
    if row:
        try:
            await bot.send_message(row["user_id"],
                f"✅ Ваше обращение #{ticket_id} закрыто. Если остались вопросы — напишите снова.")
        except Exception: pass

@router.callback_query(F.data.startswith("stmpl_"))
async def admin_show_templates(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    parts = cb.data.split("_")
    ticket_id = int(parts[1]); user_id = int(parts[2])
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM templates ORDER BY id")
    if not rows:
        await cb.answer("Шаблонов нет. Создайте через раздел «Шаблоны» в админке.", show_alert=True); return
    btns = [[InlineKeyboardButton(text=r["name"], callback_data=f"useTmpl_{r['id']}_{ticket_id}_{user_id}")] for r in rows]
    btns.append([InlineKeyboardButton(text="← Назад", callback_data=f"tview_{ticket_id}")])
    await cb.message.edit_text("📋 Выберите шаблон:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("useTmpl_"))
async def admin_use_template(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    parts = cb.data.split("_")
    tmpl_id = int(parts[1]); ticket_id = int(parts[2]); user_id = int(parts[3])
    async with pool.acquire() as conn:
        tmpl = await conn.fetchrow("SELECT text FROM templates WHERE id=$1", tmpl_id)
        if not tmpl:
            await cb.answer("Шаблон не найден.", show_alert=True); return
        now = int(time.time())
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,TRUE,$3,$4)",
            ticket_id, cb.from_user.id, tmpl["text"], now,
        )
        await conn.execute("UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id)
    try:
        await bot.send_message(user_id,
            f"💬 <b>Ответ поддержки</b> (#{ticket_id}):\n\n{tmpl['text']}",
            parse_mode="HTML", reply_markup=support_user_kb(),
        )
    except Exception: pass
    await cb.message.edit_text(f"✅ Шаблон отправлен в #{ticket_id}.")

# ───────────────────────────���─────────────��───
#  ТИКЕТЫ (inline keyboard navigation)
# ─────────────────────────────────────────────
def tickets_list_kb(tickets: list, page: int = 0, filter_: str = "open") -> InlineKeyboardMarkup:
    PAGE_SIZE = 5
    now = int(time.time())
    rows = []
    for t in tickets[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        uname   = f"@{t['username']}" if t["username"] else f"ID:{t['user_id']}"
        age_h   = (now - t["updated_at"]) // 3600
        age_lbl = f"⚠️{age_h//24}д" if age_h >= 48 else f"🕐{age_h}ч"
        rows.append([InlineKeyboardButton(text=f"#{t['id']} {uname} {age_lbl}", callback_data=f"tview_{t['id']}")])
    nav = []
    total_pages = (len(tickets) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"tpage_{filter_}_{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="tnoop"))
    if (page + 1) * PAGE_SIZE < len(tickets):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"tpage_{filter_}_{page+1}"))
    if nav: rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🟢 Открытые"  if filter_ == "open"   else "Открытые",  callback_data="tfilter_open"),
        InlineKeyboardButton(text="⚠️ Старые"    if filter_ == "old"    else "Старые",    callback_data="tfilter_old"),
        InlineKeyboardButton(text="✅ Закрытые"  if filter_ == "closed" else "Закрытые",  callback_data="tfilter_closed"),
    ])
    if filter_ in ("open", "old"):
        rows.append([InlineKeyboardButton(text="🗑 Закрыть все старые (48ч+)", callback_data="tclose_old")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _get_tickets(filter_: str) -> list:
    now = int(time.time())
    async with pool.acquire() as conn:
        if filter_ == "open":
            rows = await conn.fetch("SELECT * FROM support_tickets WHERE status='open' ORDER BY updated_at ASC")
        elif filter_ == "old":
            rows = await conn.fetch("SELECT * FROM support_tickets WHERE status='open' AND updated_at<$1 ORDER BY updated_at ASC", now - 48*3600)
        else:
            rows = await conn.fetch("SELECT * FROM support_tickets WHERE status='closed' ORDER BY updated_at DESC LIMIT 30")
    return [dict(r) for r in rows]

@router.callback_query(F.data.startswith("tfilter_"))
async def tickets_filter(cb: CallbackQuery):
    await cb.answer()
    filter_  = cb.data.removeprefix("tfilter_")
    tickets  = await _get_tickets(filter_)
    now = int(time.time())
    old = sum(1 for t in tickets if (now - t.get("updated_at", 0)) >= 48*3600) if filter_ == "open" else 0
    label = {"open":"🟢 Открытые","old":"⚠️ Старые","closed":"✅ Закрытые"}.get(filter_, filter_)
    header = f"🎫 <b>{label}: {len(tickets)}</b>"
    if old: header += f" · ⚠️ Старых: {old}"
    if not tickets:
        header += "\n\nТикетов нет."
    await cb.message.edit_text(header, parse_mode="HTML", reply_markup=tickets_list_kb(tickets, 0, filter_))

@router.callback_query(F.data.startswith("tpage_"))
async def tickets_page(cb: CallbackQuery):
    await cb.answer()
    _, filter_, page_str = cb.data.split("_", 2)
    tickets = await _get_tickets(filter_)
    label = {"open":"🟢 Открытые","old":"⚠️ Старые","closed":"✅ Закрытые"}.get(filter_, filter_)
    await cb.message.edit_text(f"🎫 <b>{label}: {len(tickets)}</b>", parse_mode="HTML",
                               reply_markup=tickets_list_kb(tickets, int(page_str), filter_))

@router.callback_query(F.data == "tnoop")
async def tnoop(cb: CallbackQuery): await cb.answer()

@router.callback_query(F.data == "tclose_old")
async def tickets_close_old(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    now = int(time.time()); cutoff = now - 48*3600
    async with pool.acquire() as conn:
        old_tickets = await conn.fetch("SELECT id, user_id FROM support_tickets WHERE status='open' AND updated_at<$1", cutoff)
        count = len(old_tickets)
        await conn.execute("UPDATE support_tickets SET status='closed', updated_at=$1 WHERE status='open' AND updated_at<$2", now, cutoff)
    for t in old_tickets:
        try:
            await bot.send_message(t["user_id"], "✅ Ваше обращение автоматичес��и закрыто. Если вопрос остался — напишите снова.")
        except Exception: pass
    tickets = await _get_tickets("open")
    await cb.message.edit_text(f"✅ Закрыто <b>{count}</b> старых тикетов.\n\n🎫 Открытых: <b>{len(tickets)}</b>",
                               parse_mode="HTML", reply_markup=tickets_list_kb(tickets, 0, "open"))

@router.callback_query(F.data.startswith("tview_"))
async def ticket_view(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    ticket_id = int(cb.data.removeprefix("tview_"))
    now = int(time.time())
    async with pool.acquire() as conn:
        ticket   = await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1", ticket_id)
        msgs     = await conn.fetch(
            "SELECT is_admin, text, sent_at FROM support_messages WHERE ticket_id=$1 ORDER BY sent_at ASC LIMIT 10", ticket_id)
    if not ticket:
        await cb.answer("Тикет не найден.", show_alert=True); return
    uname    = f"@{ticket['username']}" if ticket["username"] else f"ID:{ticket['user_id']}"
    age_h    = (now - ticket["updated_at"]) // 3600
    created  = fmt_dt(ticket["created_at"])
    status   = "🟢 Открыт" if ticket["status"] == "open" else "✅ Закрыт"
    age_warn = f"\n⚠️ <b>Последняя активность {age_h//24} дн. назад!</b>" if age_h >= 48 else ""
    lines = [f"🎫 <b>Тикет #{ticket_id}</b>", f"👤 {uname}", f"📅 {created}",
             f"{status}{age_warn}", "", "━━━━━━━━━━━━━━━━━━━━", "<b>Переписка:</b>"]
    for msg in msgs:
        prefix = "🔧 <b>Поддержка</b>" if msg["is_admin"] else f"👤 {uname}"
        dt     = fmt_dt(msg["sent_at"], "%d.%m %H:%M")
        txt    = msg["text"][:200] + "..." if len(msg["text"]) > 200 else msg["text"]
        lines.append(f"\n{prefix} [{dt}]:\n{txt}")
    kb_rows = []
    if ticket["status"] == "open":
        kb_rows.append([
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"sreply_{ticket_id}_{ticket['user_id']}"),
            InlineKeyboardButton(text="📋 Шаблон",   callback_data=f"stmpl_{ticket_id}_{ticket['user_id']}"),
        ])
        kb_rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"sclose_{ticket_id}")])
    kb_rows.append([InlineKeyboardButton(text="← К списку", callback_data="admin_tickets")])
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ — главное меню админки (кнопочное)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_panel")
async def admin_panelcb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text(
        "⚙️ <b>Админ-панель TrubaVPN</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика",  callback_data="admin_stats"),
                InlineKeyboardButton(text="📋 Отчёт",      callback_data="admin_report"),
            ],
            [
                InlineKeyboardButton(text="🎫 Тикеты",      callback_data="admin_tickets"),
                InlineKeyboardButton(text="👥 Подписчики", callback_data="admin_subs"),
            ],
            [
                InlineKeyboardButton(text="🔑 Выдать",      callback_data="admin_genkey"),
                InlineKeyboardButton(text="🔍 Найти юзера", callback_data="admin_search"),
            ],
            [
                InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_promos"),
            ],
            [
                InlineKeyboardButton(text="💼 Медиа-партнёры", callback_data="admin_media"),
            ],
            [
                InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast"),
            ],
            [
                InlineKeyboardButton(text="⭐️ Опрос",      callback_data="admin_survey"),
                InlineKeyboardButton(text="📋 Шаблоны",     callback_data="admin_templates"),
            ],
            [
                InlineKeyboardButton(text="👤 Найти (поиск)",   callback_data="admin_search"),
                InlineKeyboardButton(text="🟢 Кто онлайн",     callback_data="admin_online"),
            ],
            [
                InlineKeyboardButton(text="🔔 Настройки", callback_data="admin_settings"),
            ],
            [InlineKeyboardButton(text="← Главное меню", callback_data="back")],
        ]),
    )

# ─────────────────────────────────────────────
#  STATS / REPORT (inline)
# ────────────────────────────��────────────────
@router.callback_query(F.data == "admin_stats")
async def admin_stats_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    now = int(time.time())
    async with pool.acquire() as conn:
        total  = await conn.fetchval("SELECT COUNT(*) FROM users")
        paid   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE has_paid=1")
        promos = await conn.fetchval("SELECT COUNT(*) FROM promos")
        open_t = await conn.fetchval("SELECT COUNT(*) FROM support_tickets WHERE status='open'")
        media_count = await conn.fetchval("SELECT COUNT(*) FROM media_partners")
    all_users = await remna_get_all_users()
    our    = [u for u in all_users if u.get("username", "").startswith("truba_")]
    active = sum(1 for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED")
    dnd         = await is_admin_dnd(cb.from_user.id)
    sale_notify = await is_admin_sale_notify(cb.from_user.id)
    await cb.message.edit_text(
        f"◎ <b>Статистика TrubaVPN</b>\n\n"
        f"Всего: <b>{total}</b> · Платили: <b>{paid}</b>\n"
        f"Активных: <b>{active}</b> · Промокодов: <b>{promos}</b>\n"
        f"Открытых тикетов: <b>{open_t}</b>\n"
        f"Медиа-партнёров: <b>{media_count}</b>\n\n"
        f"DND тикеты: {'🔕 ВКЛ' if dnd else '🔔 ВЫКЛ'}\n"
        f"Уведомления о покупках: {'🔔 ВКЛ' if sale_notify else '🔕 ВЫКЛ'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "admin_report")
async def admin_report_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.message.edit_text("⏳ Формирую отчёт...")
    await send_daily_report(target=cb.message)
    await cb.message.edit_text(
        "✅ Отчёт отправлен (см. сообщение выше).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

async def send_daily_report(target=None):
    now       = int(time.time())
    date      = msk_now().strftime("%d.%m.%Y")
    day_start = int(msk_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    try:
        async with pool.acquire() as conn:
            new_users   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at>=$1", day_start) or 0
            pay_rows    = await conn.fetch("SELECT is_trial, amount FROM payments WHERE created_at>=$1", day_start)
            new_trials  = sum(1 for p in pay_rows if p["is_trial"])
            new_paid    = sum(1 for p in pay_rows if not p["is_trial"])
            revenue     = sum(float(p["amount"]) for p in pay_rows if not p["is_trial"])
            trial_rev   = sum(float(p["amount"]) for p in pay_rows if p["is_trial"])
            conversion  = round(new_paid / new_trials * 100, 1) if new_trials > 0 else 0
            total_paid  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE has_paid=1") or 0
            new_tickets = await conn.fetchval("SELECT COUNT(*) FROM support_tickets WHERE created_at>=$1", day_start) or 0
            open_tickets= await conn.fetchval("SELECT COUNT(*) FROM support_tickets WHERE status='open'") or 0
            top_refs    = await conn.fetch(
                "SELECT referrer_id, COUNT(*) as cnt FROM users WHERE referrer_id IS NOT NULL AND created_at>=$1 "
                "GROUP BY referrer_id ORDER BY cnt DESC LIMIT 5", day_start)
        all_users = await remna_get_all_users()
        our       = [u for u in all_users if u.get("username", "").startswith("truba_")]
        active    = sum(1 for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED")
        report = (
            f"📊 <b>Отчёт за {date} (МСК)</b>\n\n"
            f"⏱ <b>За день</b>\n"
            f"• Новых пользователей: <b>{new_users}</b>\n"
            f"• Новых триалов: <b>{new_trials}</b>\n"
            f"• Конверсия: <b>{new_paid} ({conversion}%)</b>\n"
            f"• Поступлений: <b>{revenue + trial_rev:.2f} ₽</b>\n\n"
            f"💎 <b>Подписки</b>\n"
            f"• Активных: <b>{active}</b>\n\n"
            f"💰 <b>Финансы</b>\n"
            f"• Платные: <b>{new_paid} · {revenue:.2f} ₽</b>\n"
            f"• Триалы: <b>{new_trials} · {trial_rev:.2f} ₽</b>\n\n"
            f"🎫 <b>Подде��жка</b>\n"
            f"• Новых тикетов: <b>{new_tickets}</b>\n"
            f"• Открытых: <b>{open_tickets}</b>\n\n"
            f"👤 <b>Плат��ли хоть раз: {total_paid}</b>\n"
        )
        if top_refs:
            report += "\n🏆 <b>Топ рефералы:</b>\n"
            for i, r in enumerate(top_refs, 1):
                async with pool.acquire() as conn:
                    ref_u = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", r["referrer_id"])
                uname = f"@{ref_u['username']}" if ref_u and ref_u["username"] else f"ID:{r['referrer_id']}"
                report += f"{i}. {uname}: <b>{r['cnt']}</b>\n"
        dests = [target] if target else []
        if not target:
            dests = []
            for admin_id in ADMIN_IDS:
                dests.append(admin_id)
        for d in dests:
            try:
                if isinstance(d, int):
                    await bot.send_message(d, report, parse_mode="HTML")
                else:
                    await d.answer(report, parse_mode="HTML")
            except Exception: pass
    except Exception as e:
        log.error("send_daily_report error: %s", e)

async def daily_report_scheduler():
    while True:
        now    = msk_now()
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await send_daily_report()

# ─────────────────────────────────────────────
#  ЛИМИТ ТРАФИКА НА СЕРВЕРЕ "БЕЛЫЕ СПИСКИ"
# ─────────────────────────────────────────────
async def check_whitelist_limits():
    """
    Раз в цикл: смотрит расход каждого отслеживаемого юзера ИМЕННО на ноде
    'белые списки' с момента его period_start. Если лимит превышен —
    убирает ТОЛЬКО SQUAD_UUID_WHITELIST из его сквадов (доступ к остальным
    серверам не трогается). Если юзер уже отключён (cut_off=TRUE), но
    почему-то снова в SQUAD_UUID_WHITELIST — тоже поправит.
    """
    if not WHITELIST_NODE_UUID:
        return
    try:
        async with pool.acquire() as conn:
            tracked = await conn.fetch(
                "SELECT user_id, gb_limit, period_start, cut_off FROM whitelist_limits"
            )
        if not tracked:
            return

        # Одним запросом тянем сырые дневные записи для всех юзеров разом
        records = await fetch_whitelist_daily_records(days_back=40)

        for row in tracked:
            user_id      = row["user_id"]
            gb_limit     = row["gb_limit"]
            period_start = row["period_start"]
            already_cut  = row["cut_off"]

            used_bytes  = sum_whitelist_bytes_for_user(records, user_id, period_start)
            limit_bytes = gb_limit * 1024 ** 3

            if used_bytes >= limit_bytes and not already_cut:
                remna = await remna_get_user(user_id)
                if not remna:
                    continue
                current_squads = _squad_uuids(remna.get("activeInternalSquads"))
                # Убираем ТОЛЬКО whitelist-сквад, остальные (basic) оставляем как есть
                new_squads = [s for s in current_squads if s != SQUAD_UUID_WHITELIST]
                if SQUAD_UUID_BASIC not in new_squads:
                    new_squads.append(SQUAD_UUID_BASIC)
                result = await remna_update_user(remna["uuid"], {"activeInternalSquads": new_squads})
                if result:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE whitelist_limits SET cut_off=TRUE WHERE user_id=$1", user_id
                        )
                    used_gb = round(used_bytes / 1024 ** 3, 2)
                    log.info("[Whitelist] user=%s exceeded limit (%s/%sGB) — squad removed",
                             user_id, used_gb, gb_limit)
                    try:
                        await bot.send_message(
                            user_id,
                            f"⚠️ Вы исчерпали лимит трафика на сервере «белые списки» "
                            f"({used_gb:.1f}/{gb_limit} GB за текущий период).\n"
                            f"Доступ к остальным серверам сохранён без изменений.",
                        )
                    except Exception:
                        pass
    except Exception as e:
        log.error("check_whitelist_limits error: %s", e)

async def whitelist_limit_scheduler():
    """Проверка расхода на 'белых списках' каждые 30 минут."""
    while True:
        await asyncio.sleep(30 * 60)
        await check_whitelist_limits()

# ─────────────────────────────────────────────
#  SUBS — все подписчики (inline editing)
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  ПОДПИСЧИКИ — кнопки с пагинацией
# ─────────────────────────────────────────────
SUBS_PAGE_SIZE = 8  # кол-во подписчиков на странице

async def _get_sorted_subs() -> list:
    """Получить только АКТИВНЫХ подписчиков, отсортированных по дате истечения."""
    now = int(time.time())
    all_users = await remna_get_all_users()
    our = [u for u in all_users if u.get("username", "").startswith("truba_")]
    active = [u for u in our
              if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED"]
    return sorted(active, key=lambda x: parse_dt(x.get("expireAt")), reverse=True)

def _subs_page_kb(users_page: list, page: int, total: int, now: int) -> InlineKeyboardMarkup:
    """Клавиатура: каждый подписчик — кнопка, навигация по страницам."""
    STATUS_ICON = {"ACTIVE": "✅", "EXPIRED": "❌", "DISABLED": "🚫", "LIMITED": "⚠️"}
    rows = []

    for u in users_page:
        uid    = u["username"].replace("truba_", "")
        expire = parse_dt(u.get("expireAt"))
        days_left = max(0, (expire - now) // 86400)
        st    = u.get("status", "?")
        icon  = STATUS_ICON.get(st, "🟡")
        tg    = u.get("_tg_label", f"ID:{uid}")
        label = f"{icon} {tg} · {days_left}д"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"sub_view_{uid}"
        )])

    # Навигация
    total_pages = (total + SUBS_PAGE_SIZE - 1) // SUBS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"subs_page_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="subs_noop"))
    if (page + 1) * SUBS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"subs_page_{page+1}"))
    if len(nav) > 1 or total_pages > 1:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_subs"),
        InlineKeyboardButton(text="← Назад",    callback_data="admin_panel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _render_subs_page(cb: CallbackQuery, page: int):
    """Отрисовать страницу подписчиков."""
    now = int(time.time())
    await cb.message.edit_text("⏳ Загружаю подписчиков...")
    try:
        all_subs = await _get_sorted_subs()
        total = len(all_subs)

        # Все активные (total уже содержит только их из _get_sorted_subs)
        active_cnt = total

        # Срез текущей страницы
        page_users = all_subs[page * SUBS_PAGE_SIZE:(page + 1) * SUBS_PAGE_SIZE]

        # Batch-запрос usernames из БД для всех юзеров на странице
        uid_list = []
        for u in page_users:
            uid_str = u["username"].replace("truba_", "")
            if uid_str.isdigit():
                uid_list.append(int(uid_str))

        if uid_list:
            async with pool.acquire() as conn:
                db_rows = await conn.fetch(
                    "SELECT user_id, username FROM users WHERE user_id = ANY($1::bigint[])",
                    uid_list,
                )
            db_map = {r["user_id"]: r["username"] for r in db_rows}
        else:
            db_map = {}

        for u in page_users:
            uid_str = u["username"].replace("truba_", "")
            if uid_str.isdigit():
                uid_int = int(uid_str)
                tg_name = db_map.get(uid_int)
                u["_tg_label"] = f"@{tg_name}" if tg_name else f"ID:{uid_int}"
            else:
                u["_tg_label"] = f"ID:{uid_str}"

        header = (
            "👥 <b>Активные подписчики TrubaVPN</b>\n"
            f"✅ Активных: <b>{active_cnt}</b>\n"
            "<i>Нажмите на подписчика для управления</i>"
        )

        await cb.message.edit_text(
            header,
            parse_mode="HTML",
            reply_markup=_subs_page_kb(page_users, page, total, now),
        )

    except Exception as e:
        log.exception("_render_subs_page error: %s", e)
        await cb.message.edit_text(
            f"❌ Ошибка загрузки: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data="admin_subs")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
            ]),
        )

@router.callback_query(F.data == "admin_subs")
async def admin_subs_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await _render_subs_page(cb, 0)

@router.callback_query(F.data.startswith("subs_page_"))
async def subs_page_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    page = int(cb.data.removeprefix("subs_page_"))
    await _render_subs_page(cb, page)

@router.callback_query(F.data == "subs_noop")
async def subs_noop(cb: CallbackQuery):
    await cb.answer()

@router.callback_query(F.data.startswith("sub_view_"))
async def sub_view_cb(cb: CallbackQuery):
    """Открыть карточку подписчика по его Telegram ID."""
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    uid_str = cb.data.removeprefix("sub_view_")
    if uid_str.isdigit():
        user_id = int(uid_str)
    else:
        # Это может быть username (если ID не числовой)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", uid_str)
        if not row:
            await cb.answer("Пользователь не найден в БД.", show_alert=True)
            return
        user_id = row["user_id"]
    await _render_check(cb, user_id)

# ─────────────────────────────────────────────
#  ONLINE (inline)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_online")
async def admin_online_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.message.edit_text("⏳ Запрашиваю...")
    now = int(time.time())
    all_users = await remna_get_all_users()
    online = [u for u in all_users if u.get("username", "").startswith("truba_")
              and parse_dt(u.get("userTraffic", {}).get("onlineAt")) > (now - 180)]
    if not online:
        await cb.message.edit_text(
            "🔌 Сейчас никто не подключён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_online")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
            ]),
        )
        return
    lines = [f"🟢 <b>Онлайн: {len(online)} чел.</b>\n"]
    for u in online[:30]:
        uid  = u["username"].replace("truba_", "")
        last = fmt_dt(parse_dt(u.get("userTraffic", {}).get("onlineAt")), "%H:%M:%S")
        async with pool.acquire() as conn:
            db = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", int(uid) if uid.isdigit() else 0)
        tg = f"@{db['username']}" if db and db["username"] else f"ID:{uid}"
        lines.append(f"• {tg} · {last}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_online")],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

# ─────────────────────────────────────────────
#  PROMOS (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_promos")
async def admin_promos_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.message.edit_text(
        "🎟 <b>Промокоды</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promos")],
            [InlineKeyboardButton(text="➕ Создать промокод",  callback_data="admin_genpromo")],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "admin_list_promos")
async def admin_list_promos_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code,days,uses,promo_type,tariff_key,discount_percent FROM promos ORDER BY promo_type,days DESC")
    if not rows:
        await cb.message.edit_text(
            "Промокодов нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать", callback_data="admin_genpromo")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_promos")],
            ]),
        )
        return
    lines = ["✦ <b>Промокоды:</b>\n"]
    for r in rows:
        ptype = r["promo_type"] or "days"
        if ptype == "discount":
            extra = f" · 🏷 скидка {r['discount_percent']}%"
        elif ptype == "free_tariff":
            extra = f" · 🆓 {TARIFFS.get(r['tariff_key'] or '', {}).get('name', r['tariff_key'])}"
        elif ptype == "free_choice":
            extra = " · 🆓 на выбор"
        else:
            extra = ""
        days_str = f"{r['days']} дн." if r["days"] else "—"
        lines.append(f"<code>{r['code']}</code> — {days_str}, {r['uses']} исп.{extra}")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_promos")],
        ]),
    )

@router.callback_query(F.data == "admin_genpromo")
async def admin_genpromo_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminPromoState.waiting_input)
    await cb.message.edit_text(
        "✦ <b>Генерация промокода</b>\n\n"
        "<code>КОД ДНИ [исп.]</code> — добавляет дни\n"
        "<code>КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>КОД ДНИ [исп.] free:choice</code> — на выбор\n"
        "<code>КОД 0 [исп.] discount:ПРОЦЕНТ</code> — скидка %\n\n"
        "Число вместо кода → авто генерация\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_promos")],
        ]),
    )

@router.message(Command("cancel"), AdminPromoState.waiting_input)
async def genpromo_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.")

@router.message(AdminPromoState.waiting_input)
async def admin_genpromo_handle(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if parts[0].isdigit(): parts = [uuid.uuid4().hex[:8].upper()] + parts
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("❌ Неверный формат."); return
    await _save_promo(message, parts)

async def _save_promo(message: types.Message, parts: list):
    code = parts[0].upper(); days = int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    free_arg     = next((p for p in parts if p.startswith("free:")),     None)
    discount_arg = next((p for p in parts if p.startswith("discount:")), None)
    promo_type = "days"; tariff_key = None; discount_percent = 0
    if free_arg:
        value = free_arg.removeprefix("free:")
        if value == "choice": promo_type = "free_choice"
        elif value in TARIFFS: promo_type = "free_tariff"; tariff_key = value
        else:
            await message.answer(f"❌ Тариф {value} не найден."); return
    elif discount_arg:
        value = discount_arg.removeprefix("discount:")
        if not value.isdigit() or not (1 <= int(value) <= 99):
            await message.answer("❌ Скидка должна быть числом от 1 до 99."); return
        promo_type = "discount"; discount_percent = int(value)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promos (code,days,uses,promo_type,tariff_key,discount_percent) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5,discount_percent=$6",
            code, days, uses, promo_type, tariff_key, discount_percent,
        )
    if promo_type == "discount":
        type_label = f"🏷 скидка {discount_percent}% при оплате"
    else:
        type_label = {"days":"добавляет дни","free_tariff":f"🆓 {TARIFFS.get(tariff_key,{}).get('name','')}" if tariff_key else "","free_choice":"🆓 на выбор"}.get(promo_type, promo_type)
    await message.answer(f"✅ Промокод <code>{code}</code> создан.\nТип: {type_label}\nДней: <b>{days}</b> · Исп.: <b>{uses}</b>", parse_mode="HTML")

# ─────────────────────────────────────────────
#  GENKEY (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_genkey")
async def admin_genkey_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminKeyState.waiting_username)
    await cb.message.edit_text(
        "🔑 <b>Выдача ключа</b>\n\nВведите username (без @):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_panel")],
        ]),
    )

@router.message(AdminKeyState.waiting_username)
async def admin_genkey_username(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", username)
    if not row:
        await message.answer(f"❌ @{username} не найден."); return
    await state.update_data(target_id=row["user_id"], target_username=username)
    await state.set_state(AdminKeyState.waiting_days)
    await message.answer(f"👤 @{username}\n\nСколько дней?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="30 дней",  callback_data="gk_30"),
         InlineKeyboardButton(text="60 дней",  callback_data="gk_60")],
        [InlineKeyboardButton(text="90 дней",  callback_data="gk_90"),
         InlineKeyboardButton(text="365 дней", callback_data="gk_365")],
        [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_panel")],
    ]))

@router.callback_query(F.data.startswith("gk_"), AdminKeyState.waiting_days)
async def admin_genkey_days(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "gk_cancel":
        await state.clear(); await cb.message.edit_text("Отменено."); return
    days = int(cb.data.removeprefix("gk_"))
    await state.update_data(days=days)
    await state.set_state(AdminKeyState.waiting_devices)
    await cb.message.edit_text(f"Дней: <b>{days}</b>\n\nЛимит устройств:", parse_mode="HTML", reply_markup=hwid_kb("gkdev_"))

@router.callback_query(F.data.startswith("gkdev_"))
async def admin_genkey_devices(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    hwid = int(cb.data.removeprefix("gkdev_"))
    data = await state.get_data()
    if not data.get("target_id"):
        await state.clear(); await cb.answer("Сессия истекла.", show_alert=True); return
    await state.clear()
    user = await activate_subscription(data["target_id"], data["days"], hwid)
    if not user:
        await cb.message.edit_text("❌ Ошибка активации."); return
    expire   = parse_dt(user.get("expireAt"))
    date_str = fmt_dt(expire, "%d.%m.%Y") if expire else "inf"
    await cb.message.edit_text(
        f"✅ @{data['target_username']} выдано <b>{data['days']}</b> дн. · {HWID_OPTIONS.get(hwid, str(hwid))}\nДо: <b>{date_str}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )
    try:
        await bot.send_message(data["target_id"], f"🎁 Администратор выдал вам <b>{data['days']}</b> дней!\n\n{format_key_message(user)}", parse_mode="HTML")
    except Exception: pass

@router.callback_query(F.data == "gk_cancel")
async def admin_genkey_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear(); await cb.message.edit_text("Отменено.")

# ─────────────────────────────────────────────
#  SEARCH USER (inline admin) — /check replaced by button
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_search")
async def admin_search_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(CheckActionState.waiting_search)
    await cb.message.edit_text(
        "🔍 <b>Поиск пол��зователя</b>\n\n"
        "Введите username или user_id:\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_panel")],
        ]),
    )

@router.message(Command("cancel"), CheckActionState.waiting_search)
async def admin_search_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отмене��о.")

@router.message(CheckActionState.waiting_search)
async def admin_search_handle(message: types.Message, state: FSMContext):
    target = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id=$1" if target.isdigit() else "SELECT user_id FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not db_row:
        await message.answer(f"❌ Пользователь <code>{target}</code> не найден.", parse_mode="HTML"); return
    await state.clear()
    await _render_check(message, db_row["user_id"])
# ─────────────────────────────────────────────
#  /check — карточка пользователя (админ)
# ─────────────────────────────────────────────
async def _render_check(target_send, user_id: int):
    now = int(time.time())
    async with pool.acquire() as conn:
        db_row       = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        payments     = await conn.fetch(
            "SELECT amount, tariff_key, days, is_trial, created_at FROM payments "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user_id)
        ref_count    = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", user_id)
        ticket_count = await conn.fetchval("SELECT COUNT(*) FROM support_tickets WHERE user_id=$1", user_id)
        wl_row       = await conn.fetchrow("SELECT gb_limit, period_start, cut_off FROM whitelist_limits WHERE user_id=$1", user_id)
    if not db_row:
        txt = f"❌ Пользователь <code>{user_id}</code> не найден."
        if isinstance(target_send, types.Message):
            await target_send.answer(txt, parse_mode="HTML")
        else:
            await target_send.message.answer(txt, parse_mode="HTML")
        return
    username = db_row["username"] or str(user_id)
    remna    = await remna_get_user(user_id)
    lines = [
        f"👤 <b>@{username}</b> (ID: <code>{user_id}</code>)\n",
        f"💳 Платил: {'✅ Да' if db_row['has_paid'] else '❌ Нет'}",
        f"👥 Рефералов: <b>{ref_count}</b>  🎫 Тикетов: <b>{ticket_count}</b>",
    ]
    hwid = 1
    has_whitelist_squad = False
    if remna:
        expire    = parse_dt(remna.get("expireAt"))
        days_left = max(0, (expire - now) // 86400)
        date_str  = fmt_dt(expire)
        used_gb   = round((remna.get("userTraffic", {}).get("usedTrafficBytes") or 0) / 1024**3, 2)
        hwid      = remna.get("hwidDeviceLimit", 1)
        status    = "✅ Активна" if expire > now and remna.get("status") != "DISABLED" else "❌ Истекла/откл."
        online_at = parse_dt(remna.get("userTraffic", {}).get("onlineAt"))
        is_online = online_at > (now - 180)
        sub_url   = format_sub_url(remna)
        current_squads = _squad_uuids(remna.get("activeInternalSquads"))
        has_whitelist_squad = SQUAD_UUID_WHITELIST in current_squads
        lines += [
            "", f"📡 <b>Подписка:</b> {status}",
            f"📅 До: <b>{date_str}</b> ({days_left} дн.)",
            f"📊 Трафик: <b>{used_gb} GB</b>",
            f"📱 Устройств: <b>{HWID_OPTIONS.get(hwid, str(hwid))}</b>",
            f"📶 Статус панели: <b>{remna.get('status', '?')}</b>",
        ]
        if is_online:
            lines.append(f"🟢 <b>Онлайн</b> ({fmt_dt(online_at, '%H:%M:%S')})")
        else:
            last = fmt_dt(online_at, "%d.%m %H:%M") if online_at else "никогда"
            lines.append(f"⚫️ Офлайн (был: {last})")
        if sub_url:
            lines += ["", f"🌐 <code>{sub_url}</code>"]
        # Статус белых списков
        wl_icon = "✅" if has_whitelist_squad else "🚫"
        lines.append(f"📡 <b>Белые списки:</b> {wl_icon} {'есть доступ' if has_whitelist_squad else 'нет доступа'}")
        if wl_row:
            since_ts = wl_row["period_start"]
            records  = await fetch_whitelist_daily_records(days_back=40)
            used_wl  = sum_whitelist_bytes_for_user(records, user_id, since_ts)
            used_wl_gb = used_wl / 1024 ** 3
            lines.append(
                f"   Лимит: <b>{used_wl_gb:.1f}/{wl_row['gb_limit']} GB</b>"
                f"{' · ⛔ ОТКЛЮЧЁН' if wl_row['cut_off'] else ''} (с {fmt_dt(since_ts, '%d.%m')})"
            )
    else:
        lines.append("\n📡 <b>Подписки нет</b>")
    if payments:
        lines += ["", "💳 <b>Платежи:</b>"]
        for p in payments:
            dt     = fmt_dt(p["created_at"], "%d.%m.%Y")
            t_name = TARIFFS.get(p["tariff_key"] or "", {}).get("name", p["tariff_key"] or "—")
            lines.append(f"  • {dt} · {p['amount']:.0f}₽ · {t_name}{'(триал)' if p['is_trial'] else ''}")
    lines += ["", "🛠 <b>Действия:</b>"]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>… обрезано</i>"
    kb   = _check_kb(user_id, hwid, has_whitelist_squad)
    if isinstance(target_send, types.Message):
        await target_send.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        try:
            await target_send.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target_send.message.answer(text, parse_mode="HTML", reply_markup=kb)

# --- Быстрые пресеты устройств ---
@router.callback_query(F.data.startswith("setlim_"))
async def set_hwid_limit(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    parts    = cb.data.split("_")
    user_id  = int(parts[1]); new_hwid = int(parts[2])
    remna    = await remna_get_user(user_id)
    if not remna:
        await cb.answer("Пользователь не найден в Remnawave.", show_alert=True); return
    result = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": new_hwid})
    if not result:
        await cb.answer("Ошибка обновления.", show_alert=True); return
    await cb.answer(f"✅ Лимит: {HWID_OPTIONS.get(new_hwid, str(new_hwid))}", show_alert=True)
    remna2 = await remna_get_user(user_id)
    hwid2  = remna2.get("hwidDeviceLimit", new_hwid) if remna2 else new_hwid
    has_wl = SQUAD_UUID_WHITELIST in _squad_uuids(remna2.get("activeInternalSquads")) if remna2 else False
    try:
        await cb.message.edit_reply_markup(reply_markup=_check_kb(user_id, hwid2, has_wl))
    except Exception: pass

# ─────────────────────────────────────────────
#  CheckActionState FSM — добавить дни
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_adddays_"))
async def ca_adddays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_adddays_"))
    await state.set_state(CheckActionState.waiting_days_add)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"➕ <b>Добавить дни</b> для ID:{user_id}\n\n"
        f"Введите количество дней (например: <code>30</code>):\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"back_to_check_{user_id}")],
        ]),
    )

@router.callback_query(F.data.startswith("back_to_check_"))
async def back_to_check_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("back_to_check_"))
    await state.clear()
    await _render_check(cb, user_id)

@router.message(CheckActionState.waiting_days_add)
async def ca_adddays_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите положительное целое число.")
        return
    days = int(message.text.strip())
    if days <= 0:
        await message.answer("❌ Число должно быть больше 0.")
        return
    data    = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("❌ Пользователь не найден в Remnawave.")
        return
    now_utc = datetime.now(timezone.utc)
    current = datetime.fromisoformat(remna["expireAt"].replace("Z", "+00:00"))
    base    = max(current, now_utc)
    new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp})
    if not result:
        await message.answer("❌ Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(
        f"✅ ID:{user_id} — добавлено <b>+{days} дн.</b>\n"
        f"📅 Новая дата: <b>{fmt_dt(new_ts)}</b>",
        parse_mode="HTML"
    )
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  CheckActionState FSM — убрать дни
# ────────��────────────────────────────────────
@router.callback_query(F.data.startswith("ca_subdays_"))
async def ca_subdays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_subdays_"))
    await state.set_state(CheckActionState.waiting_days_sub)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"➖ <b>Убрать дни</b> у ID:{user_id}\n\n"
        f"Введите количество дней:\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"back_to_check_{user_id}")],
        ]),
    )

@router.message(CheckActionState.waiting_days_sub)
async def ca_subdays_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите положительное целое число.")
        return
    days = int(message.text.strip())
    if days <= 0:
        await message.answer("❌ Число должно быть больше 0.")
        return
    data    = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("❌ Пользователь не найден в Remnawave.")
        return
    now_utc    = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(remna["expireAt"].replace("Z", "+00:00"))
    new_exp_dt = current - timedelta(days=days)
    if new_exp_dt <= now_utc:
        new_exp_dt = now_utc + timedelta(minutes=5)
    new_exp = new_exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp})
    if not result:
        await message.answer("❌ Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(
        f"✅ ID:{user_id} — убрано <b>−{days} дн.</b>\n"
        f"📅 Новая дата: <b>{fmt_dt(new_ts)}</b>",
        parse_mode="HTML"
    )
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  CheckActionState FSM — установить точну�� дату
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_setdate_"))
async def ca_setdate_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_setdate_"))
    await state.set_state(CheckActionState.waiting_days_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"📅 <b>Установить дату истечения</b> для ID:{user_id}\n\n"
        f"Введите дату в формате <code>ДД.ММ.ГГГГ</code> (по МСК, время будет 23:59):\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"back_to_check_{user_id}")],
        ]),
    )

@router.message(CheckActionState.waiting_days_set)
async def ca_setdate_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data    = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    try:
        dt_msk = datetime.strptime(message.text.strip(), "%d.%m.%Y").replace(
            hour=23, minute=59, second=59, tzinfo=MSK
        )
        dt_utc_str = dt_msk.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        await message.answer("❌ Неверный формат. Нужно: ДД.ММ.ГГГГ")
        return
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("❌ Пользователь не найден в Remnawave.")
        return
    result = await remna_update_user(remna["uuid"], {"expireAt": dt_utc_str})
    if not result:
        await message.answer("❌ Ошибка обновления.")
        return
    await message.answer(
        f"✅ ID:{user_id} — дата установлен��: <b>{message.text.strip()} 23:59 МСК</b>",
        parse_mode="HTML"
    )
    await _render_check(message, user_id)

# ───���─────────────────────────────────────────
#  CheckActionState FSM — установить кол-во устройств
# ────────────────────────────────────��────────
@router.callback_query(F.data.startswith("ca_sethwid_"))
async def ca_sethwid_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_sethwid_"))
    await state.set_state(CheckActionState.waiting_hwid_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"📱 <b>Установить лимит устройств</b> для ID:{user_id}\n\n"
        f"Введите любое число (0 = без лимита, например 1, 2, 5, 7, 23 — что угодно):\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data=f"back_to_check_{user_id}")],
        ]),
    )

@router.message(CheckActionState.waiting_hwid_set)
async def ca_sethwid_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число от 0 до 1000.")
        return
    hwid = int(message.text.strip())
    if hwid > 1000:
        await message.answer("❌ Слишком большое число (максимум 1000).")
        return
    data    = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("❌ Пользователь не найден в Remnawave.")
        return
    result = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": hwid})
    if not result:
        await message.answer("❌ Ошибка обновления.")
        return
    label = HWID_OPTIONS.get(hwid, f"{hwid} уст.")
    await message.answer(
        f"✅ ID:{user_id} — лимит устройств: <b>{label}</b>",
        parse_mode="HTML"
    )
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  Ручная выдача/отзыв доступа к "белым спискам" + лимит ГБ
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_whitelist_"))
async def ca_whitelist_toggle(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_whitelist_"))
    remna = await remna_get_user(user_id)
    if not remna:
        await cb.message.answer("❌ Пользователь не найден в Remnawave.")
        return
    current_squads = _squad_uuids(remna.get("activeInternalSquads"))
    has_whitelist  = SQUAD_UUID_WHITELIST in current_squads

    if has_whitelist:
        new_squads = [s for s in current_squads if s != SQUAD_UUID_WHITELIST]
        if SQUAD_UUID_BASIC not in new_squads:
            new_squads.append(SQUAD_UUID_BASIC)
        result, err_text = await remna_update_user_verbose(remna["uuid"], {"activeInternalSquads": new_squads})
        if not result:
            await cb.message.answer(
                f"❌ Ошибка обновления.\n\n"
                f"<code>Отправленный список: {new_squads}</code>\n\n"
                f"Ответ Remnawave:\n<code>{err_text}</code>",
                parse_mode="HTML",
            )
            return
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)
        await cb.message.answer(f"✅ ID:{user_id} — доступ к «белым спискам» отозван.")
        await _render_check(cb, user_id)
    else:
        if not WHITELIST_NODE_UUID:
            await cb.message.answer(
                "⚠️ WHITELIST_NODE_UUID не задан — лимит отслеживаться не будет, "
                "но доступ всё равно можно выдать.\n\n"
                "Введите лимит в ГБ (0 = без лимита, без отслеживания):\n/cancel — отмена"
            )
        else:
            await cb.message.answer(
                f"📡 <b>Выдать доступ к «белым спискам»</b> для ID:{user_id}\n\n"
                f"Введите лимит трафика в ГБ именно на этом сервере "
                f"(0 = без лимита, без отслеживания):\n/cancel — отмена",
                parse_mode="HTML",
            )
        await state.set_state(CheckActionState.waiting_whitelist_gb)
        await state.update_data(ca_uid=user_id)

@router.message(CheckActionState.waiting_whitelist_gb)
async def ca_whitelist_gb_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите целое число (0 = без лимита).")
        return
    gb_limit = int(message.text.strip())
    data     = await state.get_data()
    user_id  = data["ca_uid"]
    await state.clear()

    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("❌ Пользователь не найден в Remnawave.")
        return
    current_squads = _squad_uuids(remna.get("activeInternalSquads"))
    new_squads = list(current_squads)
    if SQUAD_UUID_WHITELIST not in new_squads:
        new_squads.append(SQUAD_UUID_WHITELIST)
    result, err_text = await remna_update_user_verbose(remna["uuid"], {"activeInternalSquads": new_squads})
    if not result:
        await message.answer(
            f"❌ Ошибка обновления сквада.\n\n"
            f"<code>SQUAD_UUID_WHITELIST = {SQUAD_UUID_WHITELIST}</code>\n"
            f"<code>Отправленный список: {new_squads}</code>\n\n"
            f"Ответ Remnawave:\n<code>{err_text}</code>",
            parse_mode="HTML",
        )
        return

    async with pool.acquire() as conn:
        if gb_limit > 0:
            await conn.execute(
                "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                "VALUES ($1,$2,$3,FALSE) "
                "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, period_start=$3, cut_off=FALSE",
                user_id, gb_limit, int(time.time()),
            )
        else:
            await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)

    limit_label = f"{gb_limit} GB" if gb_limit > 0 else "без лимита"
    await message.answer(
        f"✅ ID:{user_id} — доступ к «белым спискам» выдан.\n"
        f"Лимит: <b>{limit_label}</b>",
        parse_mode="HTML",
    )
    await _render_check(message, user_id)

@router.message(Command("cancel"), CheckActionState.waiting_whitelist_gb)
async def ca_whitelist_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

# ─────────────────────────────────────────���───
#  Список устройств пользователя (HWID inspector)
# ────────────────────────────────────────────���
@router.callback_query(F.data.startswith("ca_devices_"))
async def ca_devices_show(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_devices_"))
    remna = await remna_get_user(user_id)
    if not remna:
        await cb.message.answer("❌ Пользователь не найден в Remnawave.")
        return

    uuid_   = remna["uuid"]
    devices = await remna_get_user_hwid(uuid_)

    if not devices:
        await cb.message.answer(
            f"📱 <b>Устройства ID:{user_id}</b>\n\nНет зарегистрированных устройств.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Назад", callback_data=f"back_to_check_{user_id}")],
            ]),
        )
        return

    PLATFORM_ICONS = {
        "ios":     "🍎 iOS",
        "android": "🤖 Android",
        "windows": "🪟 Windows",
        "macos":   "🍏 macOS",
        "linux":   "🐧 Linux",
        "ipados":  "📱 iPadOS",
    }

    lines = [f"📱 <b>Устройства ID:{user_id}</b> ({len(devices)} шт.)\n"]
    for i, d in enumerate(devices, 1):
        platform = (d.get("platform") or "").lower()
        platform_label = PLATFORM_ICONS.get(platform, f"💻 {d.get('platform', '?')}")
        model    = d.get("deviceModel") or d.get("model") or "—"
        hwid_val = d.get("hwid", "?")
        version  = d.get("version") or d.get("osVersion") or ""
        created  = d.get("createdAt") or d.get("created_at") or ""
        if created:
            created = fmt_dt(parse_dt(created), "%d.%m.%Y")
        entry = f"{i}. {platform_label}"
        if version:
            entry += f" {version}"
        entry += f"\n   📟 {model}"
        if created:
            entry += f"\n   📅 {created}"
        entry += f"\n   <code>{str(hwid_val)[:20]}</code>"
        lines.append(entry)

    text = "\n".join(lines)
    await cb.message.answer(
        text[:4000] if len(text) > 4000 else text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=f"back_to_check_{user_id}")],
        ]),
    )

# ─────────────────────────────────────────────
#  /cancel для всех CheckActionState
# ─────────────────────────────────────────────
@router.message(Command("cancel"), CheckActionState.waiting_days_add)
@router.message(Command("cancel"), CheckActionState.waiting_days_sub)
@router.message(Command("cancel"), CheckActionState.waiting_days_set)
@router.message(Command("cancel"), CheckActionState.waiting_hwid_set)
async def ca_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.callback_query(F.data.startswith("quicktake_"))
async def quick_take(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    user_id = int(cb.data.removeprefix("quicktake_"))
    remna = await remna_get_user(user_id)
    if remna:
        await remna_disable_user(remna["uuid"])
    await cb.answer("✅ Подписка отозвана.", show_alert=True)
    try:
        await bot.send_message(user_id, "⚠️ Ваша подписка отозвана администратором.")
    except Exception: pass

# ─────────────────────────────────────────────
#  BROADCAST (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.waiting_text)
    await cb.message.edit_text(
        "◌ <b>Рассылка</b>\n\nВведите текст.\n/cancel — отмена.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_panel")],
        ]),
    )

@router.message(Command("cancel"), BroadcastState.waiting_text)
async def broadcast_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Рассылка отменена.")

@router.message(Command("cancel"), BroadcastState.confirming)
async def broadcast_cancel2(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Рассылка отменена.")

@router.message(BroadcastState.waiting_text)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastState.confirming)
    await message.answer(
        f"Предпросмотр:\n\n<b>TrubaVPN:</b>\n\n{message.text}\n\nПодтвердите:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="→ Разослать всем",         callback_data="bc_confirm")],
            [InlineKeyboardButton(text="📨 Разослать подписчикам", callback_data="bc_confirm_subs")],
            [InlineKeyboardButton(text="← Отмена",                 callback_data="bc_cancel")],
        ]),
    )

async def _do_broadcast(cb: CallbackQuery, state: FSMContext, subs_only: bool = False):
    data = await state.get_data()
    text_body = data.get("broadcast_text", "")
    await state.clear()
    if not text_body:
        await cb.answer("Текст не найден.", show_alert=True); return
    await cb.message.edit_text("Рассылка запущена...")
    async with pool.acquire() as conn:
        if subs_only:
            users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
        else:
            users = await conn.fetch("SELECT user_id FROM users")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], f"<b>TrubaVPN:</b>\n\n{text_body}", parse_mode="HTML")
            ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(
        f"✓ Готово.\nОтправлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "bc_confirm")
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await _do_broadcast(cb, state, subs_only=False)

@router.callback_query(F.data == "bc_confirm_subs")
async def broadcast_confirm_subs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await _do_broadcast(cb, state, subs_only=True)

@router.callback_query(F.data == "bc_cancel")
async def broadcast_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text(
        "Рассылка отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

# ─────��───────────────────────────────────────
#  SETTINGS (DND / sale_notify)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    dnd         = await is_admin_dnd(cb.from_user.id)
    sale_notify = await is_admin_sale_notify(cb.from_user.id)
    await cb.message.edit_text(
        "🔔 <b>Настройки уведомлений</b>\n\n"
        f"🔕 DND тикеты: <b>{'ВКЛ' if dnd else 'ВЫКЛ'}</b>\n"
        f"🛒 Уведомления о покупках: <b>{'ВКЛ' if sale_notify else 'ВЫКЛ'}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=("🔕 Выкл DND" if dnd else "🔔 Вкл DND"),
                callback_data="toggle_dnd"
            )],
            [InlineKeyboardButton(
                text=("🔕 Выкл покупки" if sale_notify else "🛒 Вкл покупки"),
                callback_data="toggle_sale_notify"
            )],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "toggle_dnd")
async def toggle_dnd_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    admin_id = cb.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dnd FROM admin_settings WHERE admin_id=$1", admin_id)
        new_dnd = not row["dnd"] if row else True
        if row:
            await conn.execute("UPDATE admin_settings SET dnd=$1 WHERE admin_id=$2", new_dnd, admin_id)
        else:
            await conn.execute("INSERT INTO admin_settings (admin_id, dnd) VALUES ($1,$2)", admin_id, new_dnd)
    await admin_settings_cb(cb)

@router.callback_query(F.data == "toggle_sale_notify")
async def toggle_sale_notify_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    admin_id = cb.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sale_notify FROM admin_settings WHERE admin_id=$1", admin_id)
        current = (row["sale_notify"] if row and row["sale_notify"] is not None else True)
        new_val = not current
        if row:
            await conn.execute(
                "UPDATE admin_settings SET sale_notify=$1 WHERE admin_id=$2", new_val, admin_id)
        else:
            await conn.execute(
                "INSERT INTO admin_settings (admin_id, sale_notify) VALUES ($1,$2)", admin_id, new_val)
    await admin_settings_cb(cb)

# ─────────────────��─���─────────────────────────
#  ШАБЛОНЫ (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_templates")
async def admin_templates_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, text FROM templates ORDER BY id")
    if not rows:
        await cb.message.edit_text(
            "📋 <b>Шаблоны</b>\n\nШаблонов нет.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать", callback_data="add_template")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
            ]),
        )
        return
    lines = ["📋 <b>Шаблоны:</b>\n"]
    for r in rows:
        preview = r["text"][:60] + "..." if len(r["text"]) > 60 else r["text"]
        lines.append(f"<b>#{r['id']}</b> {r['name']}\n<i>{preview}</i>\n")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data="add_template")],
            *[[InlineKeyboardButton(text=f"🗑 Удалить #{r['id']}", callback_data=f"del_template_{r['id']}")] for r in rows],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "add_template")
async def add_template_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(TemplateState.waiting_name)
    await cb.message.edit_text(
        "📋 Введите название ш��блона:\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_templates")],
        ]),
    )

@router.message(Command("cancel"), TemplateState.waiting_name)
@router.message(Command("cancel"), TemplateState.waiting_text)
async def template_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.")

@router.message(TemplateState.waiting_name)
async def template_name(message: types.Message, state: FSMContext):
    await state.update_data(template_name=message.text.strip())
    await state.set_state(TemplateState.waiting_text)
    await message.answer("✍️ Введите текст шаблона:")

@router.message(TemplateState.waiting_text)
async def template_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO templates (name, text, admin_id) VALUES ($1,$2,$3)",
                           data["template_name"], message.text, message.from_user.id)
    await message.answer(
        f"✅ Шаблон «{data['template_name']}» создан.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← К списку", callback_data="admin_templates")],
        ]),
    )

@router.callback_query(F.data.startswith("del_template_"))
async def del_template_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    tmpl_id = int(cb.data.removeprefix("del_template_"))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM templates WHERE id=$1", tmpl_id)
    await admin_templates_cb(cb)

# ─────��───────────────────────────────────────
#  МЕДИА-ПАРНЁРЫ (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_media")
async def admin_media_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, percent, created_at FROM media_partners ORDER BY created_at DESC")
    if not rows:
        await cb.message.edit_text(
            "💼 <b>Медиа-партнёры</b>\n\nПартнёров пока нет.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Назначить", callback_data="media_add"),
                 InlineKeyboardButton(text="📊 Сводка",   callback_data="media_stats")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
            ]),
        )
        return
    lines = ["💼 <b>Медиа-партнёры:</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        lines.append(f"• {uname} — <b>{r['percent']}%</b>")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Назначить", callback_data="media_add"),
             InlineKeyboardButton(text="📊 Сводка",   callback_data="media_stats")],
            *[[InlineKeyboardButton(text=f"🔍 {r['username'] or r['user_id']}", callback_data=f"media_check_{r['username'] or r['user_id']}")] for r in rows],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "media_add")
async def media_add_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(MediaState.waiting_username)
    await cb.message.edit_text(
        "💼 <b>Назначить медиа-партнёра</b>\n\n"
        "Введите username (без @):\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="admin_media")],
        ]),
    )

@router.message(Command("cancel"), MediaState.waiting_username)
@router.message(Command("cancel"), MediaState.waiting_percent)
async def media_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.")

@router.message(MediaState.waiting_username)
async def media_username_handler(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, username FROM users WHERE username=$1", username)
    if not row:
        await message.answer(f"❌ @{username} не найден в базе (он должен хотя бы раз ��аписать /start)."); return
    await state.update_data(media_target_id=row["user_id"], media_target_username=username)
    await state.set_state(MediaState.waiting_percent)
    await message.answer(
        f"👤 @{username}\n\nВведите процент (1–90), по умолчанию 10:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10%", callback_data="media_percent_10"),
             InlineKeyboardButton(text="15%", callback_data="media_percent_15"),
             InlineKeyboardButton(text="20%", callback_data="media_percent_20")],
            [InlineKeyboardButton(text="✕ От��ена", callback_data="admin_media")],
        ]),
    )

@router.callback_query(F.data.startswith("media_percent_"), MediaState.waiting_percent)
async def media_percent_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    percent = int(cb.data.removeprefix("media_percent_"))
    data = await state.get_data()
    await state.clear()
    username = data["media_target_username"]
    user_id  = data["media_target_id"]
    if not (1 <= percent <= 90):
        await cb.answer("❌ Процент 1–90.", show_alert=True); return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO media_partners (user_id, username, percent, created_at) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_id) DO UPDATE SET percent=$3, username=$2",
            user_id, username, percent, int(time.time()),
        )
    await cb.message.edit_text(
        f"✅ @{username} назначен <b>медиа-партнёром</b>! Процент: <b>{percent}%</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
        ]),
    )
    try:
        me = await bot.get_me()
        link = f"{{https://t.me/{me.username}}}?start={user_id}"
        await bot.send_message(
            user_id,
            f"🎉 <b>Поздравляем!</b> Вы назначены медиа-партнёром TrubaVPN.\n\n"
            f"🎯 Ваш процент с продаж: <b>{percent}%</b>\n"
            f"🔗 Ваша партнёрская ссылка:\n{hcode(link)}\n\n"
            f"За каждую оплаченную подписку по вашей ссылк�� вы получите уведомление "
            f"с расчётом дохода.",
            parse_mode="HTML",
        )
    except Exception:
        pass

@router.message(MediaState.waiting_percent)
async def media_percent_text(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число 1–90."); return
    percent = int(message.text.strip())
    data = await state.get_data()
    await state.clear()
    username = data["media_target_username"]
    user_id  = data["media_target_id"]
    if not (1 <= percent <= 90):
        await message.answer("❌ Процент 1–90."); return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO media_partners (user_id, username, percent, created_at) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_id) DO UPDATE SET percent=$3, username=$2",
            user_id, username, percent, int(time.time()),
        )
    await message.answer(
        f"✅ @{username} назначен <b>медиа-партнёром</b>! Процент: <b>{percent}%</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
        ]),
    )

@router.callback_query(F.data.startswith("media_check_"))
async def media_check_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    username = cb.data.removeprefix("media_check_").lstrip("@")
    await _render_media_check(cb, username)

async def _render_media_check(cb: CallbackQuery, username: str):
    async with pool.acquire() as conn:
        partner = await conn.fetchrow("SELECT * FROM media_partners WHERE username=$1", username)
        if not partner:
            await cb.message.edit_text(
                f"❌ @{username} не является медиа-партнёром.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
                ]),
            )
            return
        referrer_id = partner["user_id"]
        percent     = partner["percent"]
        referrals = await conn.fetch(
            "SELECT user_id, username, created_at FROM users WHERE referrer_id=$1 ORDER BY created_at DESC",
            referrer_id,
        )
        if not referrals:
            await cb.message.edit_text(
                f"💼 <b>Медиа-партнёр @{username}</b> ({percent}%)\n\nПока нет ни одного реферала.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🗑 Снять", callback_data=f"media_unmake_{username}"),
                     InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
                ]),
            )
            return
        ref_ids = [r["user_id"] for r in referrals]
        payments = await conn.fetch(
            "SELECT user_id, amount, tariff_key, days, is_trial, created_at FROM payments "
            "WHERE user_id = ANY($1::bigint[]) ORDER BY created_at DESC",
            ref_ids,
        )
    total_referrals = len(referrals)
    paid_referrals  = len({p["user_id"] for p in payments if not p["is_trial"]})
    total_revenue   = sum(float(p["amount"]) for p in payments if not p["is_trial"])
    total_earned    = round(total_revenue * percent / 100, 2)
    lines = [
        f"💼 <b>Медиа-партнёр @{username}</b> · {percent}%\n",
        f"👥 Всего рефералов: <b>{total_referrals}</b>",
        f"💳 Из них оплатили: <b>{paid_referrals}</b>",
        f"💰 Общая сумма их платежей: <b>{total_revenue:.0f} ₽</b>",
        f"🎯 Доход партнёра ({percent}%): <b>{total_earned:.2f} ₽</b>",
        "", "━━━━━━━━━━━━━━━━━━━━", "<b>Детали по платежам:</b>",
    ]
    if not payments:
        lines.append("\nПока никто из рефералов не оплачивал подписку.")
    else:
        buyer_names = {r["user_id"]: r["username"] for r in referrals}
        for p in payments:
            if p["is_trial"]:
                continue
            buyer_uname = buyer_names.get(p["user_id"])
            buyer_label = f"@{buyer_uname}" if buyer_uname else f"ID:{p['user_id']}"
            t_name  = TARIFFS.get(p["tariff_key"] or "", {}).get("name", p["tariff_key"] or "—")
            amount  = float(p["amount"])
            earned  = round(amount * percent / 100, 2)
            dt      = fmt_dt(p["created_at"], "%d.%m.%Y")
            lines.append(
                f"\n👤 {buyer_label} взял <b>{t_name}</b> ({p['days']} дн.) за <b>{amount:.0f} ₽</b>\n"
                f"   📅 {dt} · 🎯 {percent}% = <b>{earned:.2f} ₽</b>"
            )
    text = "\n".join(lines)
    # Telegram лимит на текст сообщения — 4096 символов, режем с запасом
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>… список обрезан</i>"
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Снять", callback_data=f"media_unmake_{username}"),
             InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
        ]),
    )

@router.callback_query(F.data.startswith("media_unmake_"))
async def media_unmake_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    username = cb.data.removeprefix("media_unmake_")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM media_partners WHERE username=$1", username)
        if row:
            await conn.execute("DELETE FROM media_partners WHERE user_id=$1", row["user_id"])
    await admin_media_cb(cb)

@router.callback_query(F.data == "media_stats")
async def media_stats_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        partners = await conn.fetch("SELECT * FROM media_partners ORDER BY created_at DESC")
    if not partners:
        await cb.message.edit_text(
            "Медиа-партнёров пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
            ]),
        )
        return
    lines = ["💼 <b>Сводка по медиа-партнёрам:</b>\n"]
    grand_total_earned = 0.0
    for partner in partners:
        referrer_id = partner["user_id"]
        percent     = partner["percent"]
        uname       = partner["username"] or str(referrer_id)
        async with pool.acquire() as conn:
            ref_ids_rows = await conn.fetch("SELECT user_id FROM users WHERE referrer_id=$1", referrer_id)
            ref_ids = [r["user_id"] for r in ref_ids_rows]
            if ref_ids:
                pay_rows = await conn.fetch(
                    "SELECT amount FROM payments WHERE user_id = ANY($1::bigint[]) AND is_trial=FALSE",
                    ref_ids,
                )
            else:
                pay_rows = []
        revenue = sum(float(p["amount"]) for p in pay_rows)
        earned  = round(revenue * percent / 100, 2)
        grand_total_earned += earned
        lines.append(f"• @{uname} ({percent}%) — {len(ref_ids)} реф., оборот {revenue:.0f}₽, доход <b>{earned:.2f}₽</b>")
    lines.append(f"\n💰 <b>Итог�� к выплате всем партнёрам: {grand_total_earned:.2f} ₽</b>")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_media")],
        ]),
    )

# ─────────────────────────────────────────────
#  ОПРОС (inline admin)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_survey")
async def admin_survey_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.message.edit_text(
        "⭐️ <b>Опрос</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Расслать опрос",  callback_data="admin_survey_send")],
            [InlineKeyboardButton(text="📊 Результаты",      callback_data="admin_survey_results")],
            [InlineKeyboardButton(text="← Назад", callback_data="admin_panel")],
        ]),
    )

@router.callback_query(F.data == "admin_survey_send")
async def admin_survey_send_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.message.edit_text("⏳ Рассылаю опрос платникам...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(
                row["user_id"],
                "⭐️ <b>Оцените работу TrubaVPN</b>\n\n"
                "��асколько вы довольны сервисом? Выберите оценку от 1 до 10:",
                parse_mode="HTML",
                reply_markup=_rating_kb(),
            )
            ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(
        f"✅ Опрос разослан.\nДоставлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_survey")],
        ]),
    )

@router.callback_query(F.data == "admin_survey_results")
async def admin_survey_results_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM survey_responses") or 0
        if not total:
            await cb.message.edit_text(
                "📊 Ответов на опрос пока нет.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="← Назад", callback_data="admin_survey")],
                ]),
            )
            return
        avg  = await conn.fetchval("SELECT AVG(rating) FROM survey_responses") or 0
        dist = await conn.fetch(
            "SELECT rating, COUNT(*) as cnt FROM survey_responses GROUP BY rating ORDER BY rating DESC"
        )
        comments = await conn.fetch(
            "SELECT username, rating, comment, created_at FROM survey_responses "
            "WHERE comment IS NOT NULL ORDER BY created_at DESC LIMIT 10"
        )
    avg_r  = round(float(avg), 2)
    emoji  = "😍" if avg_r >= 9 else "😊" if avg_r >= 7 else "😐" if avg_r >= 5 else "😕"
    lines  = [
        f"📊 <b>Результаты опроса</b>\n",
        f"Всего ответов: <b>{total}</b>",
        f"Средняя оценка: <b>{avg_r}/10</b> {emoji}\n",
        "📈 <b>Распределение:</b>",
    ]
    for r in dist:
        bar = "█" * min(r["cnt"], 20) + (f"+{r['cnt']-20}" if r["cnt"] > 20 else "")
        lines.append(f"  {r['rating']:2d}/10 · {r['cnt']:3d} чел.  {bar}")
    if comments:
        lines += ["", "💬 <b>Последние комментарии:</b>"]
        for c in comments:
            uname   = f"@{c['username']}" if c["username"] else "аноним"
            dt      = fmt_dt(c["created_at"], "%d.%m")
            preview = c["comment"][:120] + "..." if len(c["comment"]) > 120 else c["comment"]
            lines.append(f"\n⭐️{c['rating']} · {uname} [{dt}]:\n<i>{preview}</i>")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000]
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="admin_survey")],
        ]),
    )

def _rating_kb() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])

@router.callback_query(F.data.startswith("survey_rate_"))
async def survey_rating_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT has_paid FROM users WHERE user_id=$1", cb.from_user.id)
    if not row or not row["has_paid"]:
        await cb.answer("Опрос доступен только для платных подписчиков.", show_alert=True)
        return
    rating = int(cb.data.removeprefix("survey_rate_"))
    await state.set_state(SurveyState.waiting_comment)
    await state.update_data(survey_rating=rating)
    emoji = "😍" if rating >= 9 else "😊" if rating >= 7 else "😐" if rating >= 5 else "😕" if rating >= 3 else "😞"
    await cb.message.edit_text(
        f"{emoji} Спасибо! Вы поставили оценку <b>{rating}/10</b>.\n\n"
        "✍️ Напишите короткий комментарий — что понравилось или что можно улучшить?\n\n"
        "<i>Можно пропустить, нажав кнопку ниже.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить →", callback_data="survey_skip_comment")]
        ]),
    )

@router.callback_query(F.data == "survey_skip_comment")
async def survey_skip_comment(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data   = await state.get_data()
    rating = data.get("survey_rating", 0)
    await state.clear()
    await _save_survey(cb.from_user, rating, None)
    await cb.message.edit_text(
        "🙏 <b>Спасибо за вашу оценку!</b>\n\nВаш отзыв поможет нам стать лучше.",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await _notify_admins_survey(cb.from_user, rating, None)

@router.message(SurveyState.waiting_comment)
async def survey_comment_handler(message: types.Message, state: FSMContext):
    data    = await state.get_data()
    rating  = data.get("survey_rating", 0)
    comment = message.text.strip()
    await state.clear()
    await _save_survey(message.from_user, rating, comment)
    await message.answer(
        "🙏 <b>Спасибо за ваш отзыв!</b>\n\nМы учтём ваше мнение для улучшения сервиса.",
        parse_mode="HTML", reply_markup=back_kb(),
    )
    await _notify_admins_survey(message.from_user, rating, comment)

async def _save_survey(user, rating: int, comment: str | None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO survey_responses (user_id, username, rating, comment, created_at) VALUES ($1,$2,$3,$4,$5)",
            user.id, user.username, rating, comment, int(time.time()),
        )

async def _notify_admins_survey(user, rating: int, comment: str | None):
    emoji = "😍" if rating >= 9 else "😊" if rating >= 7 else "😐" if rating >= 5 else "😕" if rating >= 3 else "😞"
    uname = f"@{user.username}" if user.username else f"ID:{user.id}"
    text  = (
        f"📊 <b>Новый отзыв</b>\n\n"
        f"👤 {uname}\n"
        f"⭐️ Оценка: <b>{rating}/10</b> {emoji}\n"
    )
    text += f"💬 Комментарий:\n<i>{comment}</i>" if comment else "💬 <i>Без комментария</i>"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception: pass

# ─────────────────────────────────────────────
#  Команды (оставлены для совместимости, дублируют кнопки)
# ───────────────────────────────────���─────────
@router.message(Command("admin"))
async def admin_help_msg(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer(
        "⚙️ <b>Админ-панель</b> — нажмите кнопку ниже для управления:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Открыть админку", callback_data="admin_panel")],
        ]),
    )

@router.message(Command("stats"))
async def admin_stats_msg(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer(
        "📊 Открыть статистику:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Открыть", callback_data="admin_stats")],
        ]),
    )

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  admin_users — возврат из check к поиску
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_users")
async def admin_users_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    # Перенаправляем на список подписчиков
    await _render_subs_page(cb, 0)

# ─────────────────────────────────────────────
#  /debug_bandwidth — смотрим реальную структуру ответа
#  GET /api/bandwidth-stats/nodes/{uuid}/users/legacy
# ─────────────────────────────────────────────
@router.message(Command("debug_bandwidth"))
async def admin_debug_bandwidth(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    node_uuid = (command.args or "").strip() or WHITELIST_NODE_UUID
    if not node_uuid:
        await message.answer(
            "Формат: <code>/debug_bandwidth NODE_UUID</code>\n\n"
            "Или сначала задай переменную окружения WHITELIST_NODE_UUID "
            "с UUID ноды 'белые списки' (Ноды → нужная нода → More actions → "
            "Copy Node UUID), тогда можно без аргумента.",
            parse_mode="HTML",
        )
        return

    await message.answer(f"Проверяю bandwidth-stats для ноды <code>{node_uuid}</code>...", parse_mode="HTML")

    now_dt   = datetime.now(timezone.utc)
    start_dt = now_dt - timedelta(days=90)

    # Пробуем разные форматы дат — эндпоинт помечен как "legacy",
    # формат не задокументирован явно.
    attempts = [
        ("ISO с миллисекундами", start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"), now_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")),
        ("ISO без миллисекунд",  start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),      now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("Просто дата",          start_dt.strftime("%Y-%m-%d"),                now_dt.strftime("%Y-%m-%d")),
        ("Unix timestamp (сек)", str(int(start_dt.timestamp())),               str(int(now_dt.timestamp()))),
    ]

    results = []
    try:
        async with httpx.AsyncClient(verify=True) as client:
            for label, start_val, end_val in attempts:
                try:
                    r = await client.get(
                        f"{REMNAWAVE_URL}/api/bandwidth-stats/nodes/{node_uuid}/users/legacy",
                        params={"start": start_val, "end": end_val},
                        headers=_remna_headers(), timeout=20,
                    )
                    preview = r.text[:800]
                    results.append(f"<b>{label}</b> (start={start_val})\nStatus: {r.status_code}\n{preview}")
                    if r.status_code == 200:
                        break  # нашли рабочий формат — не тратим лишние запросы
                except Exception as ex:
                    results.append(f"<b>{label}</b>\nERR: {ex}")
    except Exception as e:
        await message.answer(f"Error: {e}")
        return

    text = "\n\n━━━━━━━━━━\n\n".join(results)
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>…обрезано</i>"
    await message.answer(text, parse_mode="HTML")

# ─────────────────────────────────────────────
#  /debug_hwid — ищем рабочий endpoint для списка устройств
# ─────────────────────────────────────────────
@router.message(Command("debug_hwid"))
async def admin_debug_hwid(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    target = (command.args or "").strip().lstrip("@")
    if not target:
        await message.answer("Формат: <code>/debug_hwid username</code> или <code>/debug_hwid user_id</code>", parse_mode="HTML")
        return

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id=$1" if target.isdigit() else "SELECT user_id FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not row:
        await message.answer(f"❌ {target} не найден в базе.")
        return

    remna = await remna_get_user(row["user_id"])
    if not remna:
        await message.answer("❌ Не найден в Remnawave.")
        return
    uuid_ = remna["uuid"]

    await message.answer(f"Проверяю endpoint'ы для uuid <code>{uuid_}</code>...", parse_mode="HTML")

    candidates = [
        f"/api/users/{uuid_}/hwid",
        f"/api/hwid/devices/{uuid_}",
        f"/api/hwid/devices?userUuid={uuid_}",
        f"/api/hwid/user/{uuid_}",
        f"/api/hwid?userUuid={uuid_}",
    ]

    results = []
    try:
        async with httpx.AsyncClient(verify=True) as client:
            for ep in candidates:
                try:
                    r = await client.get(
                        f"{REMNAWAVE_URL}{ep}",
                        headers=_remna_headers(), timeout=15,
                    )
                    preview = r.text[:600]
                    results.append(f"<b>{ep}</b>\nStatus: {r.status_code}\n{preview}")
                except Exception as ex:
                    results.append(f"<b>{ep}</b>\nERR: {ex}")
    except Exception as e:
        await message.answer(f"Error: {e}")
        return

    text = "\n\n━━━━━━━━━━\n\n".join(results)
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>…обрезано, читай по частям выше</i>"
    await message.answer(text, parse_mode="HTML")

# ─────────────────────────────────────────────
#  /whitelist_check — принудительно запустить проверку прямо сейчас
# ─────────────────────────────────────────────
@router.message(Command("whitelist_check"))
async def admin_whitelist_check_now(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not WHITELIST_NODE_UUID:
        await message.answer("❌ WHITELIST_NODE_UUID не задан.")
        return
    await message.answer("⏳ Проверяю лимиты 'белых списков'...")
    await check_whitelist_limits()
    await message.answer("✅ Готово. Смотри /whitelist_status для деталей.")

# ─────────────────────────────────────────────
#  /whitelist_status — расход по всем отслеживаемым юзерам
# ─────────────────────────────────────────────
@router.message(Command("whitelist_status"))
async def admin_whitelist_status(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not WHITELIST_NODE_UUID:
        await message.answer(
            "❌ WHITELIST_NODE_UUID не задан.\n\n"
            "Задай переменную окружения с UUID ноды 'белые списки'.",
        )
        return
    async with pool.acquire() as conn:
        tracked = await conn.fetch(
            "SELECT wl.user_id, wl.gb_limit, wl.period_start, wl.cut_off, u.username "
            "FROM whitelist_limits wl LEFT JOIN users u ON u.user_id = wl.user_id "
            "ORDER BY wl.period_start DESC"
        )
    if not tracked:
        await message.answer("Пока никто не отслеживается по лимиту 'белых списков'.")
        return

    await message.answer("⏳ Считаю расход...")
    records = await fetch_whitelist_daily_records(days_back=40)

    lines = ["📡 <b>Лимиты 'белые списки'</b>\n"]
    for row in tracked:
        used = sum_whitelist_bytes_for_user(records, row["user_id"], row["period_start"])
        used_gb  = used / 1024 ** 3
        limit_gb = row["gb_limit"]
        pct = round(used_gb / limit_gb * 100) if limit_gb > 0 else 0
        uname = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
        status_icon = "🚫" if row["cut_off"] else ("⚠️" if pct >= 90 else "✅")
        since = fmt_dt(row["period_start"], "%d.%m")
        lines.append(
            f"{status_icon} {uname} — <b>{used_gb:.1f}/{limit_gb} GB</b> ({pct}%)"
            f"{' — ОТКЛЮЧЁН' if row['cut_off'] else ''} · с {since}"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>…обрезано</i>"
    await message.answer(text, parse_mode="HTML")


async def main():
    await init_db()
    dp.include_router(router)
    asyncio.create_task(daily_report_scheduler())
    asyncio.create_task(whitelist_limit_scheduler())
    log.info("TrubaVPN Bot starting (Remnawave)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
