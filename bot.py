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

from yookassa import Configuration, Payment

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

SQUAD_UUID = "ed383cc2-c7c0-46ea-9237-19ebe8f10465"

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "@Truba_VPN")

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
    "trial":  {"name": "Пробный",      "price": 10,  "days": 1,  "desc": "⏱️ Тестовый доступ на 24 часа",                              "trial": True, "hwid": 1},
    "1_dev":  {"name": "1 устройство", "price": 99,  "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 1},
    "2_dev":  {"name": "2 устройства", "price": 179, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 2},
    "5_dev":  {"name": "5 устройств",  "price": 349, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость",                  "hwid": 5},
    "family": {"name": "Семейный",     "price": 449, "days": 30, "desc": "🔒 Безлимитный трафик\n🌐 Высокая скорость\n👨‍👩‍👧‍👦 До 10 устройств", "hwid": 10},
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
    waiting_days_add = State()
    waiting_days_sub = State()
    waiting_days_set = State()
    waiting_hwid_set = State()

class MediaState(StatesGroup):
    waiting_username = State()   # /makemedia: ждём username
    waiting_percent  = State()   # /makemedia: ждём процент

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
#  DATABASE
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
        # Миграции
        for col in ["remna_uuid TEXT", "created_at BIGINT DEFAULT 0"]:
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

async def remna_create_user(user_id: int, days: int, hwid: int = 1) -> dict | None:
    payload = {
        "username":             remna_username(user_id),
        "trafficLimitBytes":    0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt":             _expire_at(days),
        "hwidDeviceLimit":      hwid,
        "telegramId":           user_id,
        "activeInternalSquads": [SQUAD_UUID],
    }
    try:
        async with httpx.AsyncClient(verify=True) as client:
            r = await client.post(
                f"{REMNAWAVE_URL}/api/users",
                json=payload, headers=_remna_headers(), timeout=15,
            )
            if r.status_code == 409:
                return await remna_extend_user(user_id, days, hwid)
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] create_user: %s", e)
        return None

async def remna_extend_user(user_id: int, days: int, hwid: int | None = None) -> dict | None:
    user = await remna_get_user(user_id)
    if not user:
        return await remna_create_user(user_id, days, hwid or 1)

    now        = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
    base       = max(current, now)
    new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload: dict = {"uuid": user["uuid"], "expireAt": new_expire, "activeInternalSquads": [SQUAD_UUID]}
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
            r.raise_for_status()
            return r.json().get("response")
    except Exception as e:
        log.error("[Remna] update_user: %s", e)
        return None

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

async def activate_subscription(user_id: int, days: int, hwid: int = 1) -> dict | None:
    user = await remna_get_user(user_id)
    if user:
        result = await remna_extend_user(user_id, days, hwid)
    else:
        result = await remna_create_user(user_id, days, hwid)

    if result:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET remna_uuid=$1 WHERE user_id=$2",
                result.get("uuid"), user_id,
            )
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
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Купить VPN",  callback_data="tariffs"),
         InlineKeyboardButton(text="👤 Профиль",     callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы",    callback_data="ref_program"),
         InlineKeyboardButton(text="📞 Промокод",    callback_data="promo_enter")],
        [InlineKeyboardButton(text="💬 Поддержка",   callback_data="support_open"),
         InlineKeyboardButton(text="ℹ️ Инфо",        callback_data="info_tab")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")]
    ])

def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
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
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")])
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
        [InlineKeyboardButton(text="🏠 Главное меню",      callback_data="support_to_main")],
    ])

def support_ticket_kb(ticket_id: int, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"sreply_{ticket_id}_{user_id}"),
         InlineKeyboardButton(text="📋 Шаблон",  callback_data=f"stmpl_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="✅ Закрыть",  callback_data=f"sclose_{ticket_id}")],
    ])

def _check_kb(user_id: int, hwid: int) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Дни",  callback_data=f"ca_adddays_{user_id}"),
            InlineKeyboardButton(text="➖ Дни",  callback_data=f"ca_subdays_{user_id}"),
            InlineKeyboardButton(text="📅 Дата", callback_data=f"ca_setdate_{user_id}"),
        ],
        [InlineKeyboardButton(text="📱 Устройства — ввести число", callback_data=f"ca_sethwid_{user_id}")],
        [InlineKeyboardButton(text="📋 Список устройств",          callback_data=f"ca_devices_{user_id}")],
        *preset_rows,
        [InlineKeyboardButton(text="🚫 Забрать подписку", callback_data=f"quicktake_{user_id}")],
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
        exists = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", u_id)
        if not exists:
            await conn.execute(
                "INSERT INTO users (user_id, username, referrer_id, created_at) VALUES ($1,$2,$3,$4)",
                u_id, message.from_user.username, r_id, now,
            )
        else:
            await conn.execute("UPDATE users SET username=$1 WHERE user_id=$2",
                               message.from_user.username, u_id)

    if not await is_subscribed(u_id):
        await message.answer(
            f"🌏 {hbold('TrubaVPN')}\n\nПодпишитесь на канал чтобы пользоваться ботом.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    await message.answer(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "⚡️ Высокоскоростной VPN.\nВыберите действие:",
        reply_markup=main_kb(), parse_mode="HTML",
    )

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(cb: CallbackQuery):
    await cb.answer()
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Вы ещё не подписаны.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "⚡️ Высокоскоростной VPN.\nВыберите действие:",
        reply_markup=main_kb(), parse_mode="HTML",
    )

@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(), parse_mode="HTML",
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

@router.callback_query(F.data.startswith("buy_"))
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

    user = await activate_subscription(u_id, days, hwid)
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
#  /subs — показывает ВСЕХ подписчиков с разбивкой по статусу
# ─────────────────────────────────────────────
@router.message(Command("subs"))
async def cmd_subs(message: types.Message):
    now = int(time.time())
    if message.from_user.id in ADMIN_IDS:
        await message.answer("⏳ Загружаю список подписчиков...")
        all_users = await remna_get_all_users()
        our = [u for u in all_users if u.get("username", "").startswith("truba_")]

        active   = [u for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") not in ("DISABLED",)]
        disabled = [u for u in our if u.get("status") == "DISABLED"]
        expired  = [u for u in our if parse_dt(u.get("expireAt")) <= now and u.get("status") != "DISABLED"]

        STATUS_ICON = {"ACTIVE": "✅", "EXPIRED": "❌", "DISABLED": "🚫", "LIMITED": "⚠️"}

        subs_lines = [
            f"📋 <b>Подписчики TrubaVPN</b>",
            f"Всего в панели: <b>{len(our)}</b> | ✅ Активных: <b>{len(active)}</b> | ❌ Истёкших: <b>{len(expired)}</b> | 🚫 Откл.: <b>{len(disabled)}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        sorted_users = (
            sorted(active,   key=lambda x: parse_dt(x.get("expireAt")), reverse=True) +
            sorted(expired,  key=lambda x: parse_dt(x.get("expireAt")), reverse=True) +
            sorted(disabled, key=lambda x: parse_dt(x.get("expireAt")), reverse=True)
        )

        for u in sorted_users:
            uid       = u["username"].replace("truba_", "")
            expire    = parse_dt(u.get("expireAt"))
            days_left = max(0, (expire - now) // 86400)
            date_str  = fmt_dt(expire, "%d.%m.%Y")
            used_gb   = round((u.get("userTraffic", {}).get("usedTrafficBytes") or 0) / 1024**3, 2)
            hwid      = u.get("hwidDeviceLimit", 0)
            hwid_lbl  = HWID_OPTIONS.get(hwid, f"{hwid}уст.")
            st        = u.get("status", "?")
            icon      = STATUS_ICON.get(st, "🟡")
            async with pool.acquire() as conn:
                db = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1",
                                         int(uid) if uid.isdigit() else 0)
            tg = f"@{db['username']}" if db and db["username"] else f"ID:{uid}"
            subs_lines.append(f"{icon} {tg}\n   {hwid_lbl} · до {date_str} ({days_left}д) · {used_gb}GB")

        chunk = ""
        for line in subs_lines:
            if len(chunk) + len(line) + 1 > 4000:
                await message.answer(chunk, parse_mode="HTML")
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await message.answer(chunk, parse_mode="HTML")
        return

    user = await remna_get_user(message.from_user.id)
    if not user or parse_dt(user.get("expireAt")) <= now:
        await message.answer(
            "❌ У вас нет активной подписки.\nНажмите /start чтобы купить.",
            reply_markup=back_kb(),
        )
        return
    expire    = parse_dt(user.get("expireAt"))
    days_left = (expire - now) // 86400
    date_str  = fmt_dt(expire)
    sub_url   = format_sub_url(user)
    used_gb   = round((user.get("userTraffic", {}).get("usedTrafficBytes") or 0) / 1024**3, 2)
    hwid      = user.get("hwidDeviceLimit", 0)
    hwid_lbl  = HWID_OPTIONS.get(hwid, f"{hwid} уст.")
    await message.answer(
        f"📋 <b>Ваша подписка</b>\n\n"
        f"✅ Статус: <b>Активна</b>\n"
        f"📱 Тариф: <b>{hwid_lbl}</b>\n"
        f"📅 До: <b>{date_str}</b>\n"
        f"⏳ Осталось: <b>{days_left} дн.</b>\n"
        f"📊 Использовано: <b>{used_gb} GB</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Ссылка на подписку:</b>\n{hcode(sub_url)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 Инструкция: {CHANNEL_LINK}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить", callback_data="tariffs")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
        ]),
    )

# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    await cb.answer()
    user = await remna_get_user(cb.from_user.id)
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
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

# ─────────────────────────────────────────────
#  ПРОМОКОД
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
        reply_markup=main_kb(), parse_mode="HTML",
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
        user = await activate_subscription(message.from_user.id, days, info.get("hwid", 1))
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
    user = await activate_subscription(cb.from_user.id, days, info.get("hwid", 1))
    await _decrement_promo(promo_code, uses)
    await cb.message.edit_text(
        f"✅ Промокод <b>{promo_code}</b> активирован!\n"
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
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
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
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")],
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
        "💬 <b>Поддержка</b>\n\nОпишите вашу проблему (текст или фото):",
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
        reply_markup=main_kb(), parse_mode="HTML",
    )

@router.callback_query(F.data == "support_to_main")
async def support_to_main_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(), parse_mode="HTML",
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
    await cb.message.answer(f"✍️ Ответ на тикет #{ticket_id_str}:\n/cancel — отмена")

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
        await cb.answer("Шаблонов нет. Создайте через /add_template", show_alert=True); return
    btns = [[InlineKeyboardButton(text=r["name"], callback_data=f"useTmpl_{r['id']}_{ticket_id}_{user_id}")] for r in rows]
    await cb.message.answer("📋 Выберите шаблон:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

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

# ─────────────────────────────────────────────
#  ШАБЛОНЫ
# ─────────────────────────────────────────────
@router.message(Command("add_template"))
async def add_template_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(TemplateState.waiting_name)
    await message.answer("📋 Введите название шаблона:")

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
    await message.answer(f"✅ Шаблон «{data['template_name']}» создан.")

@router.message(Command("list_templates"))
async def list_templates(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, text FROM templates ORDER BY id")
    if not rows:
        await message.answer("Шаблонов нет. /add_template"); return
    lines = ["📋 <b>Шаблоны:</b>\n"]
    for r in rows:
        preview = r["text"][:60] + "..." if len(r["text"]) > 60 else r["text"]
        lines.append(f"<b>#{r['id']}</b> {r['name']}\n<i>{preview}</i>")
    await message.answer("\n\n".join(lines), parse_mode="HTML")

@router.message(Command("del_template"))
async def del_template(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    if not command.args or not command.args.isdigit():
        await message.answer("Формат: <code>/del_template ID</code>", parse_mode="HTML"); return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM templates WHERE id=$1", int(command.args))
    await message.answer(f"✅ Шаблон #{command.args} удалён.")

# ─────────────────────────────────────────────
#  DND
# ─────────────────────────────────────────────
@router.message(Command("dnd"))
async def toggle_dnd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    admin_id = message.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dnd FROM admin_settings WHERE admin_id=$1", admin_id)
        new_dnd = not row["dnd"] if row else True
        if row:
            await conn.execute("UPDATE admin_settings SET dnd=$1 WHERE admin_id=$2", new_dnd, admin_id)
        else:
            await conn.execute("INSERT INTO admin_settings (admin_id, dnd) VALUES ($1,$2)", admin_id, new_dnd)
    await message.answer("🔕 DND включён" if new_dnd else "🔔 Уведомления о тикетах включены")

# ─────────────────────────────────────────────
#  УВЕДОМЛЕНИЯ О ПРОДАЖАХ
# ─────────────────────────────────────────────
@router.message(Command("sale_notify"))
async def toggle_sale_notify(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    admin_id = message.from_user.id
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
    if new_val:
        await message.answer("🛒 Уведомления о покупках <b>включены</b>.", parse_mode="HTML")
    else:
        await message.answer("🔕 Уведомления о покупках <b>выключены</b>.", parse_mode="HTML")

# ─────────────────────────────────────────────
#  МЕДИА-ПАРТНЁРЫ (крутые рефералы с % от платежей)
# ─────────────────────────────────────────────
@router.message(Command("makemedia"))
async def admin_makemedia(message: types.Message, command: CommandObject):
    """/makemedia username [процент] — сделать юзера медиа-партнёром."""
    if message.from_user.id not in ADMIN_IDS: return
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Формат: <code>/makemedia username [процент]</code>\n"
            "Процент по умолчанию — 10%.\n\n"
            "Пример: <code>/makemedia yurec 15</code>",
            parse_mode="HTML"
        )
        return
    username = args[0].lstrip("@")
    percent  = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 10
    if not (1 <= percent <= 90):
        await message.answer("❌ Процент должен быть от 1 до 90."); return

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, username FROM users WHERE username=$1", username)
        if not row:
            await message.answer(f"❌ Пользователь @{username} не найден в базе (он должен хотя бы раз написать /start)."); return
        await conn.execute(
            "INSERT INTO media_partners (user_id, username, percent, created_at) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_id) DO UPDATE SET percent=$3, username=$2",
            row["user_id"], username, percent, int(time.time()),
        )

    await message.answer(
        f"✅ @{username} назначен <b>медиа-партнёром</b>!\n"
        f"🎯 Процент с продаж по его ссылке: <b>{percent}%</b>\n\n"
        f"Теперь все, кто перейдёт по его реферальной ссылке и оплатят подписку, "
        f"будут засчитаны в его статистику (<code>/checkmedia {username}</code>), "
        f"а ему будет приходить уведомление с расчётом дохода.",
        parse_mode="HTML",
    )
    try:
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={row['user_id']}"
        await bot.send_message(
            row["user_id"],
            f"🎉 <b>Поздравляем!</b> Вы назначены медиа-партнёром TrubaVPN.\n\n"
            f"🎯 Ваш процент с продаж: <b>{percent}%</b>\n"
            f"🔗 Ваша партнёрская ссылка:\n{hcode(link)}\n\n"
            f"За каждую оплаченную подписку по вашей ссылке вы получите уведомление "
            f"с расчётом дохода.",
            parse_mode="HTML",
        )
    except Exception:
        pass

@router.message(Command("unmakemedia"))
async def admin_unmakemedia(message: types.Message, command: CommandObject):
    """/unmakemedia username — снять статус медиа-партнёра."""
    if message.from_user.id not in ADMIN_IDS: return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Формат: <code>/unmakemedia username</code>", parse_mode="HTML"); return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM media_partners WHERE username=$1", username)
        if not row:
            await message.answer(f"❌ @{username} не является медиа-партнёром."); return
        await conn.execute("DELETE FROM media_partners WHERE user_id=$1", row["user_id"])
    await message.answer(f"✅ @{username} больше не медиа-партнёр.")

@router.message(Command("list_media"))
async def admin_list_media(message: types.Message):
    """Список всех медиа-партнёров."""
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, percent, created_at FROM media_partners ORDER BY created_at DESC")
    if not rows:
        await message.answer("Медиа-партнёров пока нет. /makemedia username"); return
    lines = ["💼 <b>Медиа-партнёры:</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        lines.append(f"• {uname} — <b>{r['percent']}%</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")

@router.message(Command("checkmedia"))
async def admin_checkmedia(message: types.Message, command: CommandObject):
    """/checkmedia username — статистика по рефералам медиа-партнёра."""
    if message.from_user.id not in ADMIN_IDS: return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Формат: <code>/checkmedia username</code>", parse_mode="HTML"); return

    async with pool.acquire() as conn:
        partner = await conn.fetchrow("SELECT * FROM media_partners WHERE username=$1", username)
        if not partner:
            await message.answer(f"❌ @{username} не является медиа-партнёром. Используйте /makemedia."); return

        referrer_id = partner["user_id"]
        percent     = partner["percent"]

        # Все рефералы этого партнёра
        referrals = await conn.fetch(
            "SELECT user_id, username, created_at FROM users WHERE referrer_id=$1 ORDER BY created_at DESC",
            referrer_id,
        )

        if not referrals:
            await message.answer(
                f"💼 <b>Медиа-партнёр @{username}</b> ({percent}%)\n\n"
                f"Пока нет ни одного реферала.",
                parse_mode="HTML",
            )
            return

        ref_ids = [r["user_id"] for r in referrals]
        # Все платежи рефералов этого партнёра
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
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "<b>Детали по платежам:</b>",
    ]

    if not payments:
        lines.append("\nПока никто из рефералов не оплачивал подписку.")
    else:
        # username покупателей
        buyer_names = {}
        for r in referrals:
            buyer_names[r["user_id"]] = r["username"]

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
    if len(text) > 4000:
        await message.answer("\n".join(lines[:12]), parse_mode="HTML")
        await message.answer("\n".join(lines[12:]), parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

@router.message(Command("media_stats"))
async def admin_media_stats_all(message: types.Message):
    """Сводная статистика по ВСЕМ медиа-партнёрам сразу."""
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        partners = await conn.fetch("SELECT * FROM media_partners ORDER BY created_at DESC")
        if not partners:
            await message.answer("Медиа-партнёров пока нет."); return

        lines = ["💼 <b>Сводка по медиа-партнёрам:</b>\n"]
        grand_total_earned = 0.0

        for partner in partners:
            referrer_id = partner["user_id"]
            percent     = partner["percent"]
            uname       = partner["username"] or str(referrer_id)

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

            lines.append(
                f"• @{uname} ({percent}%) — {len(ref_ids)} реф., "
                f"оборот {revenue:.0f}₽, доход <b>{earned:.2f}₽</b>"
            )

        lines.append(f"\n💰 <b>Итого к выплате всем партнёрам: {grand_total_earned:.2f} ₽</b>")

    await message.answer("\n".join(lines), parse_mode="HTML")

# ─────────────────────────────────────────────
#  ТИКЕТЫ
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

@router.message(Command("tickets"))
async def admin_tickets(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    tickets = await _get_tickets("open")
    if not tickets:
        await message.answer("🎉 Открытых тикетов нет!"); return
    now = int(time.time())
    old = sum(1 for t in tickets if (now - t["updated_at"]) >= 48*3600)
    header = f"🎫 <b>Открытые тикеты: {len(tickets)}</b>"
    if old: header += f" · ⚠️ Старых: {old}"
    await message.answer(header, parse_mode="HTML", reply_markup=tickets_list_kb(tickets, 0, "open"))

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
            await bot.send_message(t["user_id"], "✅ Ваше обращение автоматически закрыто. Если вопрос остался — напишите снова.")
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
    kb_rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="tback_open")])
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@router.callback_query(F.data.startswith("tback_"))
async def ticket_back(cb: CallbackQuery):
    await cb.answer()
    filter_  = cb.data.removeprefix("tback_")
    tickets  = await _get_tickets(filter_)
    label = {"open":"🟢 Открытые","old":"⚠️ Старые","closed":"✅ Закрытые"}.get(filter_, filter_)
    await cb.message.edit_text(f"🎫 <b>{label}: {len(tickets)}</b>", parse_mode="HTML",
                               reply_markup=tickets_list_kb(tickets, 0, filter_))

# ─────────────────────────────────────────────
#  ADMIN — /give  /genkey
# ─────────────────────────────────────────────
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>/give username дни [устройств]</code>", parse_mode="HTML"); return
    target = parts[0].lstrip("@"); days = int(parts[1])
    hwid   = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", target)
    if not row:
        await message.answer(f"❌ @{target} не найден."); return
    user = await activate_subscription(row["user_id"], days, hwid)
    if not user:
        await message.answer("❌ Ошибка активации."); return
    expire   = parse_dt(user.get("expireAt"))
    date_str = fmt_dt(expire, "%d.%m.%Y") if expire else "inf"
    await message.answer(f"✅ @{target} выдано <b>{days}</b> дн. · {HWID_OPTIONS.get(hwid, str(hwid))}\nДо: <b>{date_str}</b>", parse_mode="HTML")
    try:
        await bot.send_message(row["user_id"], f"🎁 Администратор выдал вам <b>{days}</b> дней!\n\n{format_key_message(user)}", parse_mode="HTML")
    except Exception: pass

@router.message(Command("genkey"))
async def admin_genkey_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminKeyState.waiting_username)
    await message.answer("🔑 <b>Выдача ключа</b>\n\nВведите username (без @):", parse_mode="HTML")

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
        [InlineKeyboardButton(text="✕ Отмена", callback_data="gk_cancel")],
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
        parse_mode="HTML")
    try:
        await bot.send_message(data["target_id"], f"🎁 Администратор выдал вам <b>{data['days']}</b> дней!\n\n{format_key_message(user)}", parse_mode="HTML")
    except Exception: pass

@router.callback_query(F.data == "gk_cancel")
async def admin_genkey_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear(); await cb.message.edit_text("Отменено.")

# ─────────────────────────────────────────────
#  /check — карточка пользователя
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
    if not db_row:
        txt = f"❌ Пользователь <code>{user_id}</code> не найден."
        if hasattr(target_send, 'answer'):
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
    kb   = _check_kb(user_id, hwid)
    if hasattr(target_send, 'answer'):
        await target_send.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        try:
            await target_send.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target_send.message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.message(Command("check"))
async def admin_check(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    if not command.args:
        await message.answer("Формат: <code>/check username</code> или <code>/check user_id</code>", parse_mode="HTML"); return
    target = command.args.strip().lstrip("@")
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id=$1" if target.isdigit() else "SELECT user_id FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not db_row:
        await message.answer(f"❌ Пользователь <code>{target}</code> не найден.", parse_mode="HTML"); return
    await _render_check(message, db_row["user_id"])

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
    try:
        await cb.message.edit_reply_markup(reply_markup=_check_kb(user_id, hwid2))
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
        parse_mode="HTML"
    )

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
# ─────────────────────────────────────────────
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
        parse_mode="HTML"
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
#  CheckActionState FSM — установить точную дату
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
        parse_mode="HTML"
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
        f"✅ ID:{user_id} — дата установлена: <b>{message.text.strip()} 23:59 МСК</b>",
        parse_mode="HTML"
    )
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  CheckActionState FSM — установить кол-во устройств вручную
# ─────────────────────────────────────────────
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
        parse_mode="HTML"
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
#  Список устройств пользователя (HWID inspector)
# ─────────────────────────────────────────────
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
    if len(text) > 4000:
        await cb.message.answer(text[:4000], parse_mode="HTML")
        await cb.message.answer(text[4000:], parse_mode="HTML")
    else:
        await cb.message.answer(text, parse_mode="HTML")

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
    await cb.message.answer(f"✅ Подписка ID:{user_id} отозвана.")
    try:
        await bot.send_message(user_id, "⚠️ Ваша подписка отозвана администратором.")
    except Exception: pass

@router.message(Command("take"))
async def admin_take(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    if not command.args:
        await message.answer("Формат: <code>/take username</code>", parse_mode="HTML"); return
    target = command.args.strip().lstrip("@")
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT user_id, username FROM users WHERE user_id=$1" if target.isdigit() else
            "SELECT user_id, username FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not db_row:
        await message.answer(f"❌ {target} не найден."); return
    remna = await remna_get_user(db_row["user_id"])
    ok = await remna_disable_user(remna["uuid"]) if remna else False
    username = db_row["username"] or str(db_row["user_id"])
    if ok:
        await message.answer(f"✅ Подписка @{username} отозвана.")
        try:
            await bot.send_message(db_row["user_id"], "⚠️ Ваша подписка отозвана администратором.")
        except Exception: pass
    else:
        await message.answer("❌ Ошибка или подписка уже не активна.")

@router.message(Command("online"))
async def admin_online(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("⏳ Запрашиваю...")
    now = int(time.time())
    all_users = await remna_get_all_users()
    online = [u for u in all_users if u.get("username", "").startswith("truba_")
              and parse_dt(u.get("userTraffic", {}).get("onlineAt")) > (now - 180)]
    if not online:
        await message.answer("🔌 Сейчас никто не подключён."); return
    lines = [f"🟢 <b>Онлайн: {len(online)} чел.</b>\n"]
    for u in online[:30]:
        uid  = u["username"].replace("truba_", "")
        last = fmt_dt(parse_dt(u.get("userTraffic", {}).get("onlineAt")), "%H:%M:%S")
        async with pool.acquire() as conn:
            db = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", int(uid) if uid.isdigit() else 0)
        tg = f"@{db['username']}" if db and db["username"] else f"ID:{uid}"
        lines.append(f"• {tg} · {last}")
    await message.answer("\n".join(lines), parse_mode="HTML")

# ─────────────────────────────────────────────
#  ПРОМОКОДЫ
# ─────────────────────────────────────────────
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

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n"
            "<code>/add_promo КОД ДНИ [исп.]</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:ТАРИФ</code>\n"
            "<code>/add_promo КОД 0 [исп.] discount:ПРОЦЕНТ</code>",
            parse_mode="HTML"
        ); return
    await _save_promo(message, parts)

@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminPromoState.waiting_input)
    await message.answer(
        "✦ <b>Генерация промокода</b>\n\n"
        "<code>КОД ДНИ [исп.]</code> — добавляет дни\n"
        "<code>КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>КОД ДНИ [исп.] free:choice</code> — на выбор\n"
        "<code>КОД 0 [исп.] discount:ПРОЦЕНТ</code> — скидка %\n\n"
        "Число вместо кода → авто генерация\n/cancel — отмена",
        parse_mode="HTML"
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

@router.message(Command("list_promos"))
async def admin_list_promos(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code,days,uses,promo_type,tariff_key,discount_percent FROM promos ORDER BY promo_type,days DESC")
    if not rows:
        await message.answer("Промокодов нет."); return
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
    await message.answer("\n".join(lines), parse_mode="HTML")

# ─────────────────────────────────────────────
#  РАССЫЛКА
# ─────────────────────────────────────────────
@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("◌ <b>Рассылка</b>\n\nВведите текст.\n/cancel — отмена.", parse_mode="HTML")

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
    await cb.message.edit_text(f"✓ Готово.\nОтправлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>", parse_mode="HTML")

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
    await cb.answer(); await state.clear(); await cb.message.edit_text("Рассылка отменена.")

# ─────────────────────────────────────────────
#  ОПРОС — оценка сервиса (только для платников)
# ─────────────────────────────────────────────
def _rating_kb() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])

@router.message(Command("survey"))
async def admin_survey_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("⏳ Рассылаю опрос платникам...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(
                row["user_id"],
                "⭐️ <b>Оцените работу TrubaVPN</b>\n\n"
                "Насколько вы довольны сервисом? Выберите оценку от 1 до 10:",
                parse_mode="HTML",
                reply_markup=_rating_kb(),
            )
            ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"✅ Опрос разослан.\nДоставлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>",
        parse_mode="HTML",
    )

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

@router.message(Command("survey_results"))
async def admin_survey_results(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM survey_responses") or 0
        if not total:
            await message.answer("📊 Ответов на опрос пока нет.")
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
        await message.answer("\n".join(lines[:20]), parse_mode="HTML")
        await message.answer("\n".join(lines[20:]), parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

# ─────────────────────────────────────────────
#  ОТЧЁТ
# ─────────────────────────────────────────────
async def send_daily_report():
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
            f"🎫 <b>Поддержка</b>\n"
            f"• Новых тикетов: <b>{new_tickets}</b>\n"
            f"• Открытых: <b>{open_tickets}</b>\n\n"
            f"👤 <b>Платили хоть раз: {total_paid}</b>\n"
        )
        if top_refs:
            report += "\n🏆 <b>Топ рефералы:</b>\n"
            for i, r in enumerate(top_refs, 1):
                async with pool.acquire() as conn:
                    ref_u = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", r["referrer_id"])
                uname = f"@{ref_u['username']}" if ref_u and ref_u["username"] else f"ID:{r['referrer_id']}"
                report += f"{i}. {uname}: <b>{r['cnt']}</b>\n"
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(admin_id, report, parse_mode="HTML")
            except Exception: pass
    except Exception as e:
        log.error("send_daily_report error: %s", e)
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(admin_id, f"⚠️ Ошибка отчёта:\n<code>{e}</code>", parse_mode="HTML")
            except Exception: pass

async def daily_report_scheduler():
    while True:
        now    = msk_now()
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await send_daily_report()

# ─────────────────────────────────────────────
#  STATS / REPORT / ADMIN
# ─────────────────────────────────────────────
@router.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
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
    dnd         = await is_admin_dnd(message.from_user.id)
    sale_notify = await is_admin_sale_notify(message.from_user.id)
    await message.answer(
        f"◎ <b>Статистика TrubaVPN</b>\n\n"
        f"Всего: <b>{total}</b> · Платили: <b>{paid}</b>\n"
        f"Активных: <b>{active}</b> · Промокодов: <b>{promos}</b>\n"
        f"Открытых тикетов: <b>{open_t}</b>\n"
        f"Медиа-партнёров: <b>{media_count}</b>\n\n"
        f"DND тикеты: {'🔕 ВКЛ' if dnd else '🔔 ВЫКЛ'} (/dnd)\n"
        f"Уведомления о покупках: {'🔔 ВКЛ' if sale_notify else '🔕 ВЫКЛ'} (/sale_notify)",
        parse_mode="HTML",
    )

@router.message(Command("report"))
async def admin_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("⏳ Формирую отчёт...")
    await send_daily_report()

@router.message(Command("admin"))
async def admin_help(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer(
        "⚙️ <b>Команды администратора:</b>\n\n"
        "👤 <b>Подписки:</b>\n"
        "<code>/give username дни [уст.]</code>\n"
        "<code>/genkey</code> — интерактивно\n"
        "<code>/check username|id</code> — карточка + действия\n"
        "<code>/take username|id</code> — забрать подписку\n"
        "<code>/subs</code> — все подписчики\n"
        "<code>/online</code> — кто онлайн\n\n"
        "🎟 <b>Промокоды:</b>\n"
        "<code>/add_promo КОД ДНИ [исп.]</code>\n"
        "<code>/add_promo КОД 0 [исп.] discount:ПРОЦЕНТ</code>\n"
        "<code>/genpromo</code> · <code>/list_promos</code>\n\n"
        "💼 <b>Медиа-партнёры (рефералы с %):</b>\n"
        "<code>/makemedia username [процент]</code> — назначить партнёром (по умолч. 10%)\n"
        "<code>/unmakemedia username</code> — снять статус\n"
        "<code>/list_media</code> — список всех партнёров\n"
        "<code>/checkmedia username</code> — кто что купил по его ссылке + доход\n"
        "<code>/media_stats</code> — сводка по всем партнёрам сразу\n\n"
        "💬 <b>Поддержка:</b>\n"
        "<code>/tickets</code> — тикеты\n"
        "<code>/dnd</code> — не беспокоить (тикеты)\n"
        "<code>/sale_notify</code> — вкл/выкл уведомления о покупках\n"
        "<code>/add_template</code> · <code>/list_templates</code>\n"
        "<code>/del_template ID</code>\n\n"
        "📊 <b>Статистика:</b>\n"
        "<code>/stats</code> · <code>/report</code>\n"
        "<code>/broadcast</code> — рассылка\n"
        "<code>/survey</code> — опрос платников\n"
        "<code>/survey_results</code> — результаты опроса",
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    await init_db()
    dp.include_router(router)
    asyncio.create_task(daily_report_scheduler())
    log.info("TrubaVPN Bot starting (Remnawave)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
