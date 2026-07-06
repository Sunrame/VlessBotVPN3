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

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "@Truba_VPN")

# Юзернейм поддержки — кнопка "Тех.Поддержка" ведёт в личку с этим аккаунтом
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "vvvvvpppnn")
SUPPORT_URL      = f"https://t.me/{SUPPORT_USERNAME}"

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# Основной сквад (все сервера, кроме белых списков)
SQUAD_UUID = os.environ.get("SQUAD_UUID_BASIC", "")
SQUAD_UUID_BASIC = SQUAD_UUID
# Сквад с доступом к серверу белых списков
SQUAD_UUID_WHITELIST = os.environ.get("SQUAD_UUID_WHITELIST", SQUAD_UUID)
# UUID самой ноды "белые списки" — нужен для проверки индивидуального расхода трафика
WHITELIST_NODE_UUID = os.environ.get("WHITELIST_NODE_UUID", "")

MSK = timezone(timedelta(hours=3))

def fmt_dt(ts: int, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not ts:
        return "нет данных"
    return datetime.fromtimestamp(ts, tz=MSK).strftime(fmt) + " МСК"

def msk_now() -> datetime:
    return datetime.now(MSK)

# ─────────────────────────────────────────────
#  ТАРИФЫ
#
#  Два постоянных плана + разовый пробный доступ. Цены за месяц берутся из
#  price_month и умножаются на кол-во месяцев БЕЗ каких-либо скидок за срок
#  (по требованию: "расчёт везде автоматический без скидок").
#
#  device_price — цена ОДНОГО дополнительного устройства сверх базового.
#  Базовое кол-во устройств у обоих планов = 1.
# ─────────────────────────────────────────────
TRIAL = {
    "name":         "Пробная подписка",
    "price":        10,
    "days":         1,
    "hwid":         1,
    "squad":        SQUAD_UUID_WHITELIST,
    "whitelist_gb": 3,
    "desc": (
        "24 часа доступа ко всем серверам, включая сервер белых списков. "
        "Лимит трафика на белых списках — 3 ГБ."
    ),
}

PLANS = {
    "vpn": {
        "key":          "vpn",
        "name":         "VPN",
        "price_month":  99,
        "device_price": 50,
        "squad":        SQUAD_UUID_BASIC,
        "whitelist_gb": 0,
        "desc": "Более трёх локаций, 1 устройство, трафик не ограничен.",
    },
    "vpn_bypass": {
        "key":          "vpn_bypass",
        "name":         "VPN с обходом белых списков",
        "price_month":  149,
        "device_price": 70,
        "squad":        SQUAD_UUID_WHITELIST,
        "whitelist_gb": 20,
        "desc": (
            "Более трёх локаций, 1 устройство, трафик не ограничен. "
            "Трафик на обход белых списков ограничен 20 ГБ."
        ),
    },
}

MONTH_CHOICES = [1, 3, 6, 12]

# Цена докупки доп. трафика на белых списках для тарифа "VPN с обходом".
# Цена не была указана в ТЗ — ЗАПОЛНИ перед использованием этой функции,
# иначе кнопка будет показывать заглушку и не даст оплатить.
WHITELIST_TOPUP_GB     = int(os.environ.get("WHITELIST_TOPUP_GB", "10"))
WHITELIST_TOPUP_PRICE  = int(os.environ.get("WHITELIST_TOPUP_PRICE", "0"))  # 0 = цена не задана

# Реферальная система: процент с оплаты друга и порог вывода
REFERRAL_PERCENT      = 50
REFERRAL_MIN_WITHDRAW = 1000

HWID_LABELS = {i: f"{i} устр." for i in range(1, 21)}
HWID_LABELS[0] = "без лимита"

# ─────────────────────────────────────────────
#  FSM STATES
# ─────────────────────────────────────────────
class PromoState(StatesGroup):
    waiting_code    = State()
    choosing_tariff = State()

class BroadcastState(StatesGroup):
    waiting_text = State()
    confirming   = State()

class AdminPromoState(StatesGroup):
    waiting_input = State()

class OrderPromoState(StatesGroup):
    waiting_code = State()

class CheckActionState(StatesGroup):
    waiting_days_add     = State()
    waiting_days_sub      = State()
    waiting_days_set      = State()
    waiting_hwid_set      = State()
    waiting_whitelist_gb  = State()

class BuyDevicesState(StatesGroup):
    waiting_confirm = State()

class WithdrawState(StatesGroup):
    waiting_confirm = State()

class AdminGiveState(StatesGroup):
    waiting_username = State()
    waiting_days     = State()
    waiting_devices  = State()

class AdminFindState(StatesGroup):
    waiting_query = State()

class SurveyState(StatesGroup):
    waiting_comment = State()

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
                user_id          BIGINT PRIMARY KEY,
                username         TEXT,
                referrer_id      BIGINT,
                has_paid         INTEGER DEFAULT 0,
                remna_uuid       TEXT,
                created_at       BIGINT DEFAULT 0,
                plan             TEXT DEFAULT NULL,
                extra_devices    INTEGER DEFAULT 0,
                trial_used       BOOLEAN DEFAULT FALSE,
                referral_balance NUMERIC DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY, days INTEGER,
                uses INTEGER DEFAULT 1,
                promo_type TEXT DEFAULT 'days',
                tariff_key TEXT DEFAULT NULL,
                discount_percent INTEGER DEFAULT 0
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
            CREATE TABLE IF NOT EXISTS admin_settings (
                admin_id    BIGINT PRIMARY KEY,
                sale_notify BOOLEAN DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_limits (
                user_id      BIGINT PRIMARY KEY,
                gb_limit     INTEGER DEFAULT 0,
                period_start BIGINT DEFAULT 0,
                cut_off      BOOLEAN DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_payouts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount NUMERIC,
                created_at BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS survey_responses (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                rating INTEGER,
                comment TEXT,
                created_at BIGINT DEFAULT 0
            )
        """)
        # Миграции на случай старой БД
        for col in [
            "remna_uuid TEXT", "created_at BIGINT DEFAULT 0",
            "plan TEXT DEFAULT NULL", "extra_devices INTEGER DEFAULT 0",
            "trial_used BOOLEAN DEFAULT FALSE", "referral_balance NUMERIC DEFAULT 0",
        ]:
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
    Remnawave в GET-ответе возвращает activeInternalSquads как список объектов
    {"uuid": "...", "name": "..."}, а не список голых строк. PATCH же ожидает
    список строк. Нормализует любой вариант в чистый список UUID-строк.
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

async def remna_create_user(user_id: int, days: int, hwid: int = 1, squad_uuid: str = SQUAD_UUID_BASIC) -> dict | None:
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
        return await remna_create_user(user_id, days, hwid or 1, squad_uuid or SQUAD_UUID_BASIC)

    now        = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
    base       = max(current, now)
    new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload: dict = {"uuid": user["uuid"], "expireAt": new_expire}
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
    payload = dict(payload)
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
    Трафик по каждому юзеру на конкретной ноде, по дням.
    GET /api/bandwidth-stats/nodes/{uuid}/users/legacy?start=...&end=...
    Формат дат: ISO с миллисекундами.
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
    """Сырые дневные записи трафика для ВСЕХ юзеров на ноде белых списков."""
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
        out.append({"username": uname, "ts": day_ts, "bytes": int(rec.get("total", 0) or 0)})
    return out

def sum_whitelist_bytes_for_user(records: list[dict], user_id: int, since_ts: int) -> int:
    uname = remna_username(user_id)
    return sum(r["bytes"] for r in records if r["username"] == uname and r["ts"] >= since_ts)

async def activate_subscription(user_id: int, days: int, hwid: int = 1,
                                 squad_uuid: str | None = None,
                                 whitelist_gb: int = 0) -> dict | None:
    """
    squad_uuid=None — не менять текущий сквад (для admin-действий без явного тарифа).
    whitelist_gb>0 — заводим/обновляем отслеживание лимита белых списков.
    """
    user = await remna_get_user(user_id)
    if user:
        result = await remna_extend_user(user_id, days, hwid, squad_uuid)
    else:
        result = await remna_create_user(user_id, days, hwid, squad_uuid or SQUAD_UUID_BASIC)

    if result:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET remna_uuid=$1 WHERE user_id=$2",
                result.get("uuid"), user_id,
            )
            if whitelist_gb > 0:
                await conn.execute(
                    "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                    "VALUES ($1,$2,$3,FALSE) "
                    "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, period_start=$3, cut_off=FALSE",
                    user_id, whitelist_gb, int(time.time()),
                )
            elif squad_uuid is not None and squad_uuid != SQUAD_UUID_WHITELIST:
                await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)
    return result

# ─────────────────────────────────────────────
#  ОБЩИЕ ХЕЛПЕРЫ
# ─────────────────────────────────────────────
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

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True

async def is_admin_sale_notify(admin_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sale_notify FROM admin_settings WHERE admin_id=$1", admin_id)
        if row is None:
            return True
        v = row["sale_notify"]
        return v if v is not None else True

async def notify_admins_sale(u_id: int, username: str | None, item_name: str,
                              days: int, price: float, is_trial: bool):
    uname = f"@{username}" if username else f"ID:{u_id}"
    kind  = "Триал" if is_trial else "Оплата"
    now_str = fmt_dt(int(time.time()))
    text = (
        f"Новая покупка\n\n"
        f"Пользователь: {uname} (ID: {u_id})\n"
        f"Позиция: {item_name}\n"
        f"Дней: {days}\n"
        f"Сумма: {price:.0f} руб.\n"
        f"Тип: {kind}\n"
        f"Время: {now_str}"
    )
    for admin_id in ADMIN_IDS:
        if await is_admin_sale_notify(admin_id):
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                pass

async def credit_referral(referrer_id: int, buyer_id: int, buyer_username: str | None,
                           item_name: str, price: float):
    """
    Начисление рефереру REFERRAL_PERCENT% от суммы оплаты приглашённого друга.
    ДОПУЩЕНИЕ (не было явно уточнено в ТЗ): начисление идёт с КАЖДОЙ оплаты
    приглашённого, а не только с первой. Если нужно только с первой покупки —
    легко поменять на условие has_paid==0, скажи и поправлю.
    """
    earned = round(float(price) * REFERRAL_PERCENT / 100, 2)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET referral_balance = referral_balance + $1 WHERE user_id=$2",
            earned, referrer_id,
        )
        new_balance = await conn.fetchval(
            "SELECT referral_balance FROM users WHERE user_id=$1", referrer_id
        )
    buyer_label = f"@{buyer_username}" if buyer_username else f"ID:{buyer_id}"
    try:
        await bot.send_message(
            referrer_id,
            f"Ваш реферал {buyer_label} оплатил «{item_name}» на {price:.0f} руб.\n"
            f"Начислено {REFERRAL_PERCENT}%: {earned:.2f} руб.\n"
            f"Текущий баланс: {float(new_balance):.2f} руб.",
        )
    except Exception:
        pass

def calc_plan_price(plan_key: str, months: int) -> int:
    """Линейная цена без скидок за срок: price_month * months."""
    return PLANS[plan_key]["price_month"] * months

def calc_upgrade_price(days_left: int, extra_devices: int) -> int:
    """
    Доплата за апгрейд VPN -> VPN с обходом белых списков.

    ДОПУЩЕНИЕ (формула не была задана явно в ТЗ, реализована по смыслу
    "доплата к лучшему тарифу + доплата за устройства, если есть разница"):
      1) Пропорциональная доплата за оставшиеся дни текущей подписки:
         (price_month_bypass - price_month_vpn) / 30 * days_left
      2) Доплата за уже купленные доп. устройства, у которых различается
         device_price между тарифами:
         (device_price_bypass - device_price_vpn) * extra_devices
    Итог округляется до рубля. Если формула должна быть другой — поправь ТЗ,
    легко переписать под конкретную схему.
    """
    vpn    = PLANS["vpn"]
    bypass = PLANS["vpn_bypass"]
    plan_diff_per_day = (bypass["price_month"] - vpn["price_month"]) / 30
    prorated_plan_diff = plan_diff_per_day * max(days_left, 0)
    device_diff = max(0, bypass["device_price"] - vpn["device_price"]) * max(extra_devices, 0)
    return round(prorated_plan_diff + device_diff)

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="Я подписался", callback_data="check_sub")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="back")]
    ])

# ─────────────────────────────────────────────
#  СТАРТ / ПРОФИЛЬ (единственный домашний экран)
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
            f"{hbold('TrubaVPN')}\n\nПодпишитесь на канал, чтобы пользоваться ботом.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    text, kb = await _build_profile_view(u_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(cb: CallbackQuery):
    await cb.answer()
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Вы ещё не подписаны.", show_alert=True)
        return
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

async def _build_profile_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Профиль = единственный домашний экран. Показывает вариант подписки,
    кол-во устройств, дату окончания, ссылку и контакт поддержки.
    Кнопки строго вертикальные (по одной в ряд).
    """
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT plan, extra_devices, trial_used, referral_balance FROM users WHERE user_id=$1",
            user_id,
        )
    plan          = db_row["plan"] if db_row else None
    trial_used    = db_row["trial_used"] if db_row else False
    remna = await remna_get_user(user_id)
    now   = int(time.time())

    lines = [hbold("Профиль"), ""]
    if remna and parse_dt(remna.get("expireAt")) > now and remna.get("status") != "DISABLED":
        expire   = parse_dt(remna.get("expireAt"))
        date_str = fmt_dt(expire, "%d.%m.%Y")
        hwid     = remna.get("hwidDeviceLimit", 1)
        sub_url  = format_sub_url(remna)
        if plan == "trial":
            plan_name = "Пробная подписка"
        elif plan in PLANS:
            plan_name = PLANS[plan]["name"]
        else:
            plan_name = "не оформлена"
        lines += [
            f"Вариант подписки: {plan_name}",
            f"Устройств: {hwid}",
            f"Активна до: {date_str}",
        ]
        if sub_url:
            lines += ["", "Ссылка на подписку:", hcode(sub_url)]
    else:
        lines += ["Подписка не активна."]

    lines += ["", f"Поддержка: @{SUPPORT_USERNAME}"]
    text = "\n".join(lines)

    rows = []
    # Кнопки "Купить VPN"/"Пробная" показываются, пока не куплен РЕАЛЬНЫЙ тариф
    # (vpn / vpn_bypass). Пробная подписка (plan == "trial") — это не покупка
    # тарифа, поэтому кнопки не пропадают, только пропадает сама кнопка триала,
    # если он уже использован.
    if plan not in PLANS:
        if not trial_used:
            rows.append([InlineKeyboardButton(text="Пробная подписка", callback_data="trial_buy")])
        rows.append([InlineKeyboardButton(text="Купить VPN", callback_data="buy_open")])
    else:
        rows.append([InlineKeyboardButton(text="Добавить устройства", callback_data="dev_add")])
        if plan == "vpn":
            rows.append([InlineKeyboardButton(text="Улучшить тариф", callback_data="plan_upgrade")])
        elif plan == "vpn_bypass":
            rows.append([InlineKeyboardButton(text="Докупить трафик (белые списки)", callback_data="wl_topup")])

    rows.append([InlineKeyboardButton(text="Заработать", callback_data="earn_open")])
    rows.append([InlineKeyboardButton(text="Промокод", callback_data="promo_enter")])
    rows.append([InlineKeyboardButton(text="О сервисе", callback_data="info_tab")])
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(text="Панель", callback_data="admin_panel")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb

async def _show_home(cb: CallbackQuery):
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "back")
async def back_to_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _show_home(cb)

@router.callback_query(F.data == "profile")
async def profile_cb(cb: CallbackQuery):
    await cb.answer()
    await _show_home(cb)

# ─────────────────────────────────────────────
#  ОБЩАЯ СТРАНИЦА ОПЛАТЫ (YooKassa)
# ─────────────────────────────────────────────
async def _create_payment_page(cb: CallbackQuery, *, kind: str, item_name: str,
                                price: int, days: int = 0, hwid: int | None = None,
                                squad: str | None = None, whitelist_gb: int = 0,
                                plan_key: str | None = None, is_trial: bool = False):
    """
    kind: "trial" | "plan" | "device" | "upgrade" | "wl_topup"
    Единая точка создания платежа ЮKassa для всех видов покупок в новой схеме.
    """
    try:
        payment = Payment.create({
            "amount":       {"value": f"{price}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture":      True,
            "description":  f"TrubaVPN — {item_name}",
            "metadata": {
                "user_id":      str(cb.from_user.id),
                "kind":         kind,
                "days":         str(days),
                "hwid":         str(hwid) if hwid is not None else "",
                "squad":        squad or "",
                "whitelist_gb": str(whitelist_gb),
                "plan_key":     plan_key or "",
                "price":        str(price),
                "is_trial":     "1" if is_trial else "0",
                "item_name":    item_name,
            },
        }, str(uuid.uuid4()))
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data=f"paycheck_{payment.id}")],
        [InlineKeyboardButton(text="Назад", callback_data="back")],
    ])
    await cb.message.edit_text(
        f"{hbold(item_name)}\n\nК оплате: {price} руб.\n\nПосле оплаты нажмите «Проверить оплату».",
        parse_mode="HTML", reply_markup=kb,
    )

@router.callback_query(F.data.startswith("paycheck_"))
async def check_payment_cb(cb: CallbackQuery):
    await cb.answer()
    pay_id = cb.data.removeprefix("paycheck_")
    try:
        payment = Payment.find_one(pay_id)
    except Exception as e:
        log.exception("Payment find error: %s", e)
        await cb.answer("Ошибка проверки платежа.", show_alert=True)
        return
    if payment.status != "succeeded":
        await cb.answer("Платёж ещё не подтверждён. Попробуйте через минуту.", show_alert=True)
        return

    md          = payment.metadata
    u_id        = int(md["user_id"])
    kind        = md.get("kind", "plan")
    days        = int(md.get("days", 0) or 0)
    hwid_str    = md.get("hwid", "")
    hwid        = int(hwid_str) if hwid_str.isdigit() else None
    squad       = md.get("squad") or None
    whitelist_gb= int(md.get("whitelist_gb", 0) or 0)
    plan_key    = md.get("plan_key") or None
    price       = float(md.get("price", 0))
    is_trial    = md.get("is_trial", "0") == "1"
    item_name   = md.get("item_name", "Покупка")

    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT username, referrer_id, has_paid, extra_devices, plan FROM users WHERE user_id=$1", u_id
        )
    uname       = db_row["username"] if db_row else None
    referrer_id = db_row["referrer_id"] if db_row else None
    extra_devices_now = db_row["extra_devices"] if db_row else 0
    current_plan_now  = db_row["plan"] if db_row else None

    result_user = None

    if kind in ("trial", "plan"):
        result_user = await activate_subscription(u_id, days, hwid or 1, squad_uuid=squad, whitelist_gb=whitelist_gb)
        if not result_user:
            await cb.answer("Ошибка активации. Обратитесь в поддержку.", show_alert=True)
            return
        async with pool.acquire() as conn:
            if kind == "trial":
                await conn.execute(
                    "UPDATE users SET trial_used=TRUE, plan='trial' WHERE user_id=$1", u_id
                )
            else:
                await conn.execute(
                    "UPDATE users SET plan=$1, extra_devices=0, has_paid=1, remna_uuid=$2 WHERE user_id=$3",
                    plan_key, result_user.get("uuid"), u_id,
                )

    elif kind == "device":
        remna = await remna_get_user(u_id)
        if not remna:
            await cb.answer("Пользователь не найден в панели.", show_alert=True)
            return
        new_hwid = remna.get("hwidDeviceLimit", 1) + 1
        result_user = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": new_hwid})
        if not result_user:
            await cb.answer("Ошибка обновления устройств.", show_alert=True)
            return
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET extra_devices = extra_devices + 1 WHERE user_id=$1", u_id
            )

    elif kind == "upgrade":
        remna = await remna_get_user(u_id)
        if not remna:
            await cb.answer("Пользователь не найден в панели.", show_alert=True)
            return
        result_user = await remna_update_user(remna["uuid"], {"activeInternalSquads": [SQUAD_UUID_WHITELIST]})
        if not result_user:
            await cb.answer("Ошибка обновления тарифа.", show_alert=True)
            return
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET plan='vpn_bypass' WHERE user_id=$1", u_id)
            wl_gb = PLANS["vpn_bypass"]["whitelist_gb"]
            await conn.execute(
                "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                "VALUES ($1,$2,$3,FALSE) "
                "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, period_start=$3, cut_off=FALSE",
                u_id, wl_gb, int(time.time()),
            )

    elif kind == "wl_topup":
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT gb_limit FROM whitelist_limits WHERE user_id=$1", u_id)
            if row:
                await conn.execute(
                    "UPDATE whitelist_limits SET gb_limit = gb_limit + $1, cut_off=FALSE WHERE user_id=$2",
                    WHITELIST_TOPUP_GB, u_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                    "VALUES ($1,$2,$3,FALSE)",
                    u_id, WHITELIST_TOPUP_GB, int(time.time()),
                )
        remna = await remna_get_user(u_id)
        if remna:
            current_squads = _squad_uuids(remna.get("activeInternalSquads"))
            if SQUAD_UUID_WHITELIST not in current_squads:
                current_squads.append(SQUAD_UUID_WHITELIST)
                await remna_update_user(remna["uuid"], {"activeInternalSquads": current_squads})
        result_user = remna or {}

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, created_at) VALUES ($1,$2,$3,$4,$5,$6)",
            u_id, price, kind, days, is_trial, int(time.time()),
        )

    await notify_admins_sale(u_id, uname, item_name, days, price, is_trial)

    if referrer_id and not is_trial:
        await credit_referral(referrer_id, u_id, uname, item_name, price)

    text, kb = await _build_profile_view(u_id)
    await cb.message.edit_text(
        f"Оплата прошла успешно.\n\n{text}", parse_mode="HTML", reply_markup=kb,
    )

# ─────────────────────────────────────────────
#  ПРОБНАЯ ПОДПИСКА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "trial_buy")
async def trial_buy_cb(cb: CallbackQuery):
    await cb.answer()
    async with pool.acquire() as conn:
        used = await conn.fetchval("SELECT trial_used FROM users WHERE user_id=$1", cb.from_user.id)
    if used:
        await cb.answer("Пробная подписка уже использована.", show_alert=True)
        return
    await _create_payment_page(
        cb, kind="trial", item_name=TRIAL["name"], price=TRIAL["price"], days=TRIAL["days"],
        hwid=TRIAL["hwid"], squad=TRIAL["squad"], whitelist_gb=TRIAL["whitelist_gb"], is_trial=True,
    )

# ─────────────────────────────────────────────
#  КУПИТЬ VPN — выбор тарифа, затем срока
# ─────────────────────────────────────────────
@router.callback_query(F.data == "buy_open")
async def buy_open_cb(cb: CallbackQuery):
    await cb.answer()
    vpn    = PLANS["vpn"]
    bypass = PLANS["vpn_bypass"]
    text = (
        f"{hbold(vpn['name'])}\n{vpn['desc']}\nОт {vpn['price_month']} руб./мес.\n\n"
        f"{hbold(bypass['name'])}\n{bypass['desc']}\nОт {bypass['price_month']} руб./мес.\n\n"
        f"Дополнительные устройства и трафик для обхода белых списков "
        f"докупаются в главном меню после покупки тарифа."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=vpn["name"], callback_data="buyplan_vpn")],
        [InlineKeyboardButton(text=bypass["name"], callback_data="buyplan_vpn_bypass")],
        [InlineKeyboardButton(text="Назад", callback_data="back")],
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("buyplan_"))
async def buyplan_cb(cb: CallbackQuery):
    await cb.answer()
    plan_key = cb.data.removeprefix("buyplan_")
    if plan_key not in PLANS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    plan = PLANS[plan_key]
    rows = []
    for months in MONTH_CHOICES:
        price = calc_plan_price(plan_key, months)
        label = f"{months} мес. — {price} руб." if months > 1 else f"{months} мес. — {price} руб."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buymonths_{plan_key}_{months}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="buy_open")])
    await cb.message.edit_text(
        f"{hbold(plan['name'])}\n{plan['desc']}\n\n"
        f"Дополнительные устройства и трафик для обхода белых списков "
        f"докупаются в главном меню после покупки.\n\n"
        f"Выберите срок:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )

@router.callback_query(F.data.startswith("buymonths_"))
async def buymonths_cb(cb: CallbackQuery):
    await cb.answer()
    rest = cb.data.removeprefix("buymonths_")
    plan_key, months_str = rest.rsplit("_", 1)
    months = int(months_str)
    if plan_key not in PLANS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    plan  = PLANS[plan_key]
    price = calc_plan_price(plan_key, months)
    days  = months * 30
    await _create_payment_page(
        cb, kind="plan", item_name=f"{plan['name']} · {months} мес.", price=price, days=days,
        hwid=1, squad=plan["squad"], whitelist_gb=plan["whitelist_gb"], plan_key=plan_key,
    )

# ─────────────────────────────────────────────
#  ДОБАВИТЬ УСТРОЙСТВА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "dev_add")
async def dev_add_cb(cb: CallbackQuery):
    await cb.answer()
    async with pool.acquire() as conn:
        plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", cb.from_user.id)
    if plan not in PLANS:
        await cb.answer("Сначала оформите подписку.", show_alert=True)
        return
    price = PLANS[plan]["device_price"]
    await _create_payment_page(
        cb, kind="device", item_name=f"Доп. устройство ({PLANS[plan]['name']})",
        price=price, days=0,
    )

# ─────────────────────────────────────────────
#  УЛУЧШИТЬ ТАРИФ (VPN -> VPN с обходом белых списков)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "plan_upgrade")
async def plan_upgrade_cb(cb: CallbackQuery):
    await cb.answer()
    u_id = cb.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT plan, extra_devices FROM users WHERE user_id=$1", u_id)
    if not row or row["plan"] != "vpn":
        await cb.answer("Апгрейд доступен только с тарифа VPN.", show_alert=True)
        return
    remna = await remna_get_user(u_id)
    if not remna:
        await cb.answer("Подписка не найдена.", show_alert=True)
        return
    expire    = parse_dt(remna.get("expireAt"))
    days_left = max(0, (expire - int(time.time())) // 86400)
    price = calc_upgrade_price(days_left, row["extra_devices"] or 0)
    if price <= 0:
        await cb.answer("Улучшение недоступно для текущего срока подписки.", show_alert=True)
        return
    await _create_payment_page(
        cb, kind="upgrade", item_name="Улучшение тарифа до VPN с обходом белых списков",
        price=price, days=0,
    )

# ─────────────────────────────────────────────
#  ДОКУПИТЬ ТРАФИК НА БЕЛЫХ СПИСКАХ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "wl_topup")
async def wl_topup_cb(cb: CallbackQuery):
    await cb.answer()
    async with pool.acquire() as conn:
        plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", cb.from_user.id)
    if plan != "vpn_bypass":
        await cb.answer("Докупка доступна только на тарифе VPN с обходом.", show_alert=True)
        return
    if WHITELIST_TOPUP_PRICE <= 0:
        await cb.message.answer(
            "Цена докупки трафика ещё не настроена администратором. "
            "Обратитесь в поддержку."
        )
        return
    await _create_payment_page(
        cb, kind="wl_topup",
        item_name=f"+{WHITELIST_TOPUP_GB} ГБ на белых списках",
        price=WHITELIST_TOPUP_PRICE, days=0,
    )

# ─────────────────────────────────────────────
#  ЗАРАБОТАТЬ (реферальная система)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "earn_open")
async def earn_open_cb(cb: CallbackQuery):
    await cb.answer()
    u_id = cb.from_user.id
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={u_id}"
    async with pool.acquire() as conn:
        balance = await conn.fetchval("SELECT referral_balance FROM users WHERE user_id=$1", u_id) or 0
        ref_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", u_id) or 0
    balance = float(balance)

    text = (
        f"{hbold('Заработать')}\n\n"
        f"Приглашайте друзей — получайте {REFERRAL_PERCENT}% с их оплат.\n\n"
        f"Ваша ссылка:\n{hcode(link)}\n\n"
        f"Приглашено: {ref_count}\n"
        f"Баланс: {balance:.2f} руб.\n"
        f"Вывод доступен от {REFERRAL_MIN_WITHDRAW} руб."
    )
    rows = []
    if balance >= REFERRAL_MIN_WITHDRAW:
        withdraw_text = f"Хочу вывести реферальный баланс ({balance:.2f} руб.)"
        withdraw_url  = f"{SUPPORT_URL}?text={withdraw_text.replace(' ', '%20')}"
        rows.append([InlineKeyboardButton(text="Написать для вывода", url=withdraw_url)])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

# ─────────────────────────────────────────────
#  ПРОМОКОД (логика не меняется — просто ссылается на новые планы)
# ─────────────────────────────────────────────
def _free_plan_kb(code: str):
    rows = []
    for key, plan in PLANS.items():
        rows.append([InlineKeyboardButton(text=plan["name"], callback_data=f"pfree_{key}_{code}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="promo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(
        "Введите промокод:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="promo_cancel")],
        ]),
    )

@router.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _show_home(cb)

@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT days, uses, promo_type, tariff_key FROM promos WHERE code=$1", code
        )
    if not row:
        await message.answer(
            "Неверный промокод. Попробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="promo_cancel")],
            ]),
        )
        return
    promo_type = row["promo_type"] or "days"
    tariff_key = row["tariff_key"]
    days = row["days"]; uses = row["uses"]

    if promo_type == "free_tariff" and tariff_key and tariff_key in PLANS:
        await state.clear()
        plan = PLANS[tariff_key]
        user = await activate_subscription(message.from_user.id, days, 1,
                                            squad_uuid=plan["squad"], whitelist_gb=plan["whitelist_gb"])
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET plan=$1, extra_devices=0 WHERE user_id=$2",
                               tariff_key, message.from_user.id)
            if uses <= 1:
                await conn.execute("DELETE FROM promos WHERE code=$1", code)
            else:
                await conn.execute("UPDATE promos SET uses=uses-1 WHERE code=$1", code)
        text, kb = await _build_profile_view(message.from_user.id)
        await message.answer(f"Промокод {code} активирован — {plan['name']}, {days} дн.\n\n{text}",
                             parse_mode="HTML", reply_markup=kb)
        return

    if promo_type == "free_choice":
        await state.set_state(PromoState.choosing_tariff)
        await state.update_data(promo_code=code, promo_days=days, promo_uses=uses)
        await message.answer(f"Промокод {code} — {days} дней бесплатно. Выберите тариф:",
                             reply_markup=_free_plan_kb(code))
        return

    user = await activate_subscription(message.from_user.id, days)
    async with pool.acquire() as conn:
        if uses <= 1:
            await conn.execute("DELETE FROM promos WHERE code=$1", code)
        else:
            await conn.execute("UPDATE promos SET uses=uses-1 WHERE code=$1", code)
    await state.clear()
    text, kb = await _build_profile_view(message.from_user.id)
    await message.answer(f"Промокод {code} активирован — добавлено {days} дн.\n\n{text}",
                         parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pfree_"))
async def handle_free_plan_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, plan_key, promo_code = cb.data.split("_", 2)
    data = await state.get_data()
    days = data.get("promo_days", 30); uses = data.get("promo_uses", 1)
    await state.clear()
    if plan_key not in PLANS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    plan = PLANS[plan_key]
    await activate_subscription(cb.from_user.id, days, 1,
                                 squad_uuid=plan["squad"], whitelist_gb=plan["whitelist_gb"])
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET plan=$1, extra_devices=0 WHERE user_id=$2",
                           plan_key, cb.from_user.id)
        if uses <= 1:
            await conn.execute("DELETE FROM promos WHERE code=$1", promo_code)
        else:
            await conn.execute("UPDATE promos SET uses=uses-1 WHERE code=$1", promo_code)
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(f"Промокод {promo_code} активирован — {plan['name']}, {days} дн.\n\n{text}",
                               parse_mode="HTML", reply_markup=kb)

# ─────────────────────────────────────────────
#  О СЕРВИСЕ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "О сервисе",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Канал с инструкциями", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="Пользовательское соглашение",
                                  url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
            [InlineKeyboardButton(text="Политика конфиденциальности",
                                  url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
            [InlineKeyboardButton(text="Тех.Поддержка", url=SUPPORT_URL)],
            [InlineKeyboardButton(text="Назад", callback_data="back")],
        ]),
    )

# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ
# ─────────────────────────────────────────────
def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats"),
         InlineKeyboardButton(text="Отчёт", callback_data="admin_report")],
        [InlineKeyboardButton(text="Подписчики", callback_data="admin_subs"),
         InlineKeyboardButton(text="Выдать", callback_data="admin_give_start")],
        [InlineKeyboardButton(text="Найти юзера", callback_data="admin_find_start"),
         InlineKeyboardButton(text="Кто онлайн", callback_data="admin_online")],
        [InlineKeyboardButton(text="Промокоды", callback_data="admin_promos"),
         InlineKeyboardButton(text="Опрос", callback_data="admin_survey")],
        [InlineKeyboardButton(text="Рефералы", callback_data="admin_referrals"),
         InlineKeyboardButton(text="Рассылка", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(text="Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton(text="Назад", callback_data="back")],
    ])

@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await cb.message.edit_text("Админ-панель", reply_markup=admin_panel_kb())

# ─────────────────────────────────────────────
#  ПОДПИСЧИКИ — кнопки с пагинацией (только активные)
# ─────────────────────────────────────────────
SUBS_PAGE_SIZE = 8

async def _get_sorted_subs() -> list:
    now = int(time.time())
    all_users = await remna_get_all_users()
    our = [u for u in all_users if u.get("username", "").startswith("truba_")]
    active = [u for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED"]
    return sorted(active, key=lambda x: parse_dt(x.get("expireAt")), reverse=True)

def _subs_page_kb(users_page: list, page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users_page:
        uid = u["username"].replace("truba_", "")
        expire = parse_dt(u.get("expireAt"))
        days_left = max(0, (expire - int(time.time())) // 86400)
        tg = u.get("_tg_label", f"ID:{uid}")
        rows.append([InlineKeyboardButton(text=f"{tg} · {days_left}д", callback_data=f"sub_view_{uid}")])
    total_pages = (total + SUBS_PAGE_SIZE - 1) // SUBS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="<", callback_data=f"subs_page_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{max(total_pages,1)}", callback_data="subs_noop"))
    if (page + 1) * SUBS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text=">", callback_data=f"subs_page_{page+1}"))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Обновить", callback_data="admin_subs")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _render_subs_page(cb: CallbackQuery, page: int):
    await cb.message.edit_text("Загружаю подписчиков...")
    try:
        all_subs = await _get_sorted_subs()
        total = len(all_subs)
        page_users = all_subs[page * SUBS_PAGE_SIZE:(page + 1) * SUBS_PAGE_SIZE]

        uid_list = []
        for u in page_users:
            uid_str = u["username"].replace("truba_", "")
            if uid_str.isdigit():
                uid_list.append(int(uid_str))

        db_map = {}
        if uid_list:
            async with pool.acquire() as conn:
                db_rows = await conn.fetch(
                    "SELECT user_id, username FROM users WHERE user_id = ANY($1::bigint[])", uid_list,
                )
            db_map = {r["user_id"]: r["username"] for r in db_rows}

        for u in page_users:
            uid_str = u["username"].replace("truba_", "")
            if uid_str.isdigit():
                uid_int = int(uid_str)
                tg_name = db_map.get(uid_int)
                u["_tg_label"] = f"@{tg_name}" if tg_name else f"ID:{uid_int}"
            else:
                u["_tg_label"] = f"ID:{uid_str}"

        header = f"Активных подписчиков: {total}\nНажмите на подписчика для управления"
        await cb.message.edit_text(header, reply_markup=_subs_page_kb(page_users, page, total))
    except Exception as e:
        log.exception("_render_subs_page error: %s", e)
        await cb.message.edit_text(
            f"Ошибка загрузки: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Повторить", callback_data="admin_subs")],
                [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
            ]),
        )

@router.callback_query(F.data == "admin_subs")
async def admin_subs_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await _render_subs_page(cb, 0)

@router.callback_query(F.data.startswith("subs_page_"))
async def subs_page_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await _render_subs_page(cb, int(cb.data.removeprefix("subs_page_")))

@router.callback_query(F.data == "subs_noop")
async def subs_noop(cb: CallbackQuery):
    await cb.answer()

@router.callback_query(F.data.startswith("sub_view_"))
async def sub_view_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    uid_str = cb.data.removeprefix("sub_view_")
    if not uid_str.isdigit():
        await cb.answer("Некорректный ID.", show_alert=True)
        return
    await _render_check(cb, int(uid_str))

# ─────────────────────────────────────────────
#  КАРТОЧКА ПОДПИСЧИКА (/check)
# ─────────────────────────────────────────────
def _check_kb(user_id: int, hwid: int, has_whitelist: bool = False) -> InlineKeyboardMarkup:
    preset_row = []
    rows = []
    for limit in (1, 2, 3, 5, 10):
        mark = "* " if limit == hwid else ""
        preset_row.append(InlineKeyboardButton(text=f"{mark}{limit}", callback_data=f"setlim_{user_id}_{limit}"))
        if len(preset_row) == 5:
            rows.append(preset_row); preset_row = []
    if preset_row:
        rows.append(preset_row)

    whitelist_btn_text = "Забрать белые списки" if has_whitelist else "Дать белые списки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+ Дни", callback_data=f"ca_adddays_{user_id}"),
         InlineKeyboardButton(text="- Дни", callback_data=f"ca_subdays_{user_id}"),
         InlineKeyboardButton(text="Дата", callback_data=f"ca_setdate_{user_id}")],
        [InlineKeyboardButton(text="Устройства — ввести число", callback_data=f"ca_sethwid_{user_id}")],
        *rows,
        [InlineKeyboardButton(text="Список устройств", callback_data=f"ca_devices_{user_id}")],
        [InlineKeyboardButton(text=whitelist_btn_text, callback_data=f"ca_whitelist_{user_id}")],
        [InlineKeyboardButton(text="Забрать подписку", callback_data=f"quicktake_{user_id}")],
        [InlineKeyboardButton(text="Подписчики", callback_data="admin_subs")],
    ])

async def _render_check(target_send, user_id: int):
    now = int(time.time())
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT username, has_paid, plan, extra_devices, referral_balance FROM users WHERE user_id=$1",
            user_id,
        )
        payments = await conn.fetch(
            "SELECT amount, tariff_key, days, is_trial, created_at FROM payments "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user_id,
        )
        ref_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", user_id)
        wl_row    = await conn.fetchrow("SELECT gb_limit, period_start, cut_off FROM whitelist_limits WHERE user_id=$1", user_id)

    if not db_row:
        txt = f"Пользователь {user_id} не найден."
        if isinstance(target_send, types.Message):
            await target_send.answer(txt)
        else:
            await target_send.message.answer(txt)
        return

    username = db_row["username"] or str(user_id)
    plan_name = PLANS.get(db_row["plan"], {}).get("name", "нет") if db_row["plan"] else "нет"
    remna = await remna_get_user(user_id)

    lines = [
        f"@{username} (ID: {user_id})",
        f"Тариф: {plan_name}",
        f"Платил: {'да' if db_row['has_paid'] else 'нет'}",
        f"Рефералов: {ref_count}   Реф. баланс: {float(db_row['referral_balance'] or 0):.2f} руб.",
    ]
    hwid = 1
    has_whitelist_squad = False
    if remna:
        expire    = parse_dt(remna.get("expireAt"))
        days_left = max(0, (expire - now) // 86400)
        date_str  = fmt_dt(expire)
        used_gb   = round((remna.get("userTraffic", {}).get("usedTrafficBytes") or 0) / 1024**3, 2)
        hwid      = remna.get("hwidDeviceLimit", 1)
        status    = "активна" if expire > now and remna.get("status") != "DISABLED" else "истекла/откл."
        current_squads = _squad_uuids(remna.get("activeInternalSquads"))
        has_whitelist_squad = SQUAD_UUID_WHITELIST in current_squads
        lines += [
            "",
            f"Подписка: {status}",
            f"До: {date_str} ({days_left} дн.)",
            f"Трафик: {used_gb} GB",
            f"Устройств: {hwid}",
            f"Белые списки: {'да' if has_whitelist_squad else 'нет'}",
        ]
        if wl_row:
            since_ts = wl_row["period_start"]
            records  = await fetch_whitelist_daily_records(days_back=40)
            used_wl_gb = sum_whitelist_bytes_for_user(records, user_id, since_ts) / 1024 ** 3
            lines.append(
                f"  Лимит белых списков: {used_wl_gb:.1f}/{wl_row['gb_limit']} GB"
                f"{' (отключён)' if wl_row['cut_off'] else ''}"
            )
    else:
        lines.append("Подписки нет")

    if payments:
        lines += ["", "Платежи:"]
        for p in payments:
            dt = fmt_dt(p["created_at"], "%d.%m.%Y")
            lines.append(f"  {dt} · {p['amount']:.0f} руб. · {p['tariff_key'] or '-'}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... обрезано"
    kb = _check_kb(user_id, hwid, has_whitelist_squad)

    if isinstance(target_send, types.Message):
        await target_send.answer(text, reply_markup=kb)
    else:
        try:
            await target_send.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target_send.message.answer(text, reply_markup=kb)

@router.message(Command("check"))
async def admin_check_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        await message.answer("Формат: /check username или /check user_id")
        return
    target = command.args.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id=$1" if target.isdigit() else "SELECT user_id FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not row:
        await message.answer(f"Пользователь {target} не найден.")
        return
    await _render_check(message, row["user_id"])

# ─────────────────────────────────────────────
#  БЫСТРЫЕ ПРЕСЕТЫ УСТРОЙСТВ
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("setlim_"))
async def set_hwid_limit(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    parts = cb.data.split("_")
    user_id = int(parts[1]); new_hwid = int(parts[2])
    remna = await remna_get_user(user_id)
    if not remna:
        await cb.answer("Пользователь не найден в Remnawave.", show_alert=True)
        return
    result = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": new_hwid})
    if not result:
        await cb.answer("Ошибка обновления.", show_alert=True)
        return
    await cb.answer(f"Лимит: {new_hwid}", show_alert=True)
    remna2 = await remna_get_user(user_id)
    hwid2  = remna2.get("hwidDeviceLimit", new_hwid) if remna2 else new_hwid
    has_wl = SQUAD_UUID_WHITELIST in _squad_uuids(remna2.get("activeInternalSquads")) if remna2 else False
    try:
        await cb.message.edit_reply_markup(reply_markup=_check_kb(user_id, hwid2, has_wl))
    except Exception:
        pass

# ─────────────────────────────────────────────
#  ДНИ: добавить / убрать / установить дату
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_adddays_"))
async def ca_adddays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_adddays_"))
    await state.set_state(CheckActionState.waiting_days_add)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Добавить дни для ID:{user_id}\n\nВведите количество дней:\n/cancel — отмена")

@router.message(CheckActionState.waiting_days_add)
async def ca_adddays_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите положительное целое число.")
        return
    days = int(message.text.strip())
    if days <= 0:
        await message.answer("Число должно быть больше 0.")
        return
    data = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("Пользователь не найден в Remnawave.")
        return
    now_utc = datetime.now(timezone.utc)
    current = datetime.fromisoformat(remna["expireAt"].replace("Z", "+00:00"))
    base    = max(current, now_utc)
    new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(f"ID:{user_id} — добавлено +{days} дн. Новая дата: {fmt_dt(new_ts)}")
    await _render_check(message, user_id)

@router.callback_query(F.data.startswith("ca_subdays_"))
async def ca_subdays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_subdays_"))
    await state.set_state(CheckActionState.waiting_days_sub)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Убрать дни у ID:{user_id}\n\nВведите количество дней:\n/cancel — отмена")

@router.message(CheckActionState.waiting_days_sub)
async def ca_subdays_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите положительное целое число.")
        return
    days = int(message.text.strip())
    if days <= 0:
        await message.answer("Число должно быть больше 0.")
        return
    data = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("Пользователь не найден в Remnawave.")
        return
    now_utc    = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(remna["expireAt"].replace("Z", "+00:00"))
    new_exp_dt = current - timedelta(days=days)
    if new_exp_dt <= now_utc:
        new_exp_dt = now_utc + timedelta(minutes=5)
    new_exp = new_exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(f"ID:{user_id} — убрано -{days} дн. Новая дата: {fmt_dt(new_ts)}")
    await _render_check(message, user_id)

@router.callback_query(F.data.startswith("ca_setdate_"))
async def ca_setdate_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_setdate_"))
    await state.set_state(CheckActionState.waiting_days_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"Установить дату истечения для ID:{user_id}\n\n"
        f"Введите дату в формате ДД.ММ.ГГГГ (по МСК, время 23:59):\n/cancel — отмена"
    )

@router.message(CheckActionState.waiting_days_set)
async def ca_setdate_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    try:
        dt_msk = datetime.strptime(message.text.strip(), "%d.%m.%Y").replace(
            hour=23, minute=59, second=59, tzinfo=MSK
        )
        dt_utc_str = dt_msk.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        await message.answer("Неверный формат. Нужно: ДД.ММ.ГГГГ")
        return
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("Пользователь не найден в Remnawave.")
        return
    result = await remna_update_user(remna["uuid"], {"expireAt": dt_utc_str})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    await message.answer(f"ID:{user_id} — дата установлена: {message.text.strip()} 23:59 МСК")
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  УСТРОЙСТВА: установить число вручную
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_sethwid_"))
async def ca_sethwid_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_sethwid_"))
    await state.set_state(CheckActionState.waiting_hwid_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Установить лимит устройств для ID:{user_id}\n\nВведите число (0 = без лимита):\n/cancel — отмена")

@router.message(CheckActionState.waiting_hwid_set)
async def ca_sethwid_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите число от 0 до 1000.")
        return
    hwid = int(message.text.strip())
    if hwid > 1000:
        await message.answer("Слишком большое число.")
        return
    data = await state.get_data()
    user_id = data["ca_uid"]
    await state.clear()
    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("Пользователь не найден в Remnawave.")
        return
    result = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": hwid})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    await message.answer(f"ID:{user_id} — лимит устройств: {hwid}")
    await _render_check(message, user_id)

@router.message(Command("cancel"), CheckActionState.waiting_days_add)
@router.message(Command("cancel"), CheckActionState.waiting_days_sub)
@router.message(Command("cancel"), CheckActionState.waiting_days_set)
@router.message(Command("cancel"), CheckActionState.waiting_hwid_set)
@router.message(Command("cancel"), CheckActionState.waiting_whitelist_gb)
async def ca_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

# ─────────────────────────────────────────────
#  СПИСОК УСТРОЙСТВ (HWID inspector)
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_devices_"))
async def ca_devices_show(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_devices_"))
    remna = await remna_get_user(user_id)
    if not remna:
        await cb.message.answer("Пользователь не найден в Remnawave.")
        return
    devices = await remna_get_user_hwid(remna["uuid"])
    if not devices:
        await cb.message.answer(f"Устройства ID:{user_id}\n\nНет зарегистрированных устройств.")
        return
    lines = [f"Устройства ID:{user_id} ({len(devices)} шт.)"]
    for i, d in enumerate(devices, 1):
        platform = d.get("platform", "?")
        model    = d.get("deviceModel") or d.get("model") or "-"
        hwid_val = d.get("hwid", "?")
        lines.append(f"{i}. {platform} · {model} · {str(hwid_val)[:16]}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... обрезано"
    await cb.message.answer(text)

# ─────────────────────────────────────────────
#  БЕЛЫЕ СПИСКИ: выдать/забрать вручную + лимит
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_whitelist_"))
async def ca_whitelist_toggle(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("ca_whitelist_"))
    remna = await remna_get_user(user_id)
    if not remna:
        await cb.message.answer("Пользователь не найден в Remnawave.")
        return
    current_squads = _squad_uuids(remna.get("activeInternalSquads"))
    has_whitelist  = SQUAD_UUID_WHITELIST in current_squads

    if has_whitelist:
        new_squads = [s for s in current_squads if s != SQUAD_UUID_WHITELIST]
        if SQUAD_UUID_BASIC not in new_squads:
            new_squads.append(SQUAD_UUID_BASIC)
        result, err_text = await remna_update_user_verbose(remna["uuid"], {"activeInternalSquads": new_squads})
        if not result:
            await cb.message.answer(f"Ошибка обновления.\nОтправлено: {new_squads}\nОтвет: {err_text}")
            return
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)
        await cb.message.answer(f"ID:{user_id} — доступ к белым спискам отозван.")
        await _render_check(cb, user_id)
    else:
        await cb.message.answer(
            f"Выдать доступ к белым спискам для ID:{user_id}\n\n"
            f"Введите лимит в ГБ (0 = без лимита, без отслеживания):\n/cancel — отмена"
        )
        await state.set_state(CheckActionState.waiting_whitelist_gb)
        await state.update_data(ca_uid=user_id)

@router.message(CheckActionState.waiting_whitelist_gb)
async def ca_whitelist_gb_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое число (0 = без лимита).")
        return
    gb_limit = int(message.text.strip())
    data     = await state.get_data()
    user_id  = data["ca_uid"]
    await state.clear()

    remna = await remna_get_user(user_id)
    if not remna:
        await message.answer("Пользователь не найден в Remnawave.")
        return
    current_squads = _squad_uuids(remna.get("activeInternalSquads"))
    new_squads = list(current_squads)
    if SQUAD_UUID_WHITELIST not in new_squads:
        new_squads.append(SQUAD_UUID_WHITELIST)
    result, err_text = await remna_update_user_verbose(remna["uuid"], {"activeInternalSquads": new_squads})
    if not result:
        await message.answer(f"Ошибка обновления сквада.\nОтправлено: {new_squads}\nОтвет: {err_text}")
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
    await message.answer(f"ID:{user_id} — доступ к белым спискам выдан. Лимит: {limit_label}")
    await _render_check(message, user_id)

# ─────────────────────────────────────────────
#  ЗАБРАТЬ ПОДПИСКУ
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("quicktake_"))
async def quick_take(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    user_id = int(cb.data.removeprefix("quicktake_"))
    remna = await remna_get_user(user_id)
    if remna:
        await remna_disable_user(remna["uuid"])
    await cb.message.answer(f"Подписка ID:{user_id} отозвана.")
    try:
        await bot.send_message(user_id, "Ваша подписка отозвана администратором.")
    except Exception:
        pass

# ─────────────────────────────────────────────
#  ПРОМОКОДЫ (админ)
# ─────────────────────────────────────────────
async def _save_promo(message: types.Message, parts: list):
    code = parts[0].upper(); days = int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    free_arg     = next((p for p in parts if p.startswith("free:")), None)
    discount_arg = next((p for p in parts if p.startswith("discount:")), None)
    promo_type = "days"; tariff_key = None; discount_percent = 0
    if free_arg:
        value = free_arg.removeprefix("free:")
        if value == "choice":
            promo_type = "free_choice"
        elif value in PLANS:
            promo_type = "free_tariff"; tariff_key = value
        else:
            await message.answer(f"Тариф {value} не найден. Доступны: vpn, vpn_bypass, choice")
            return
    elif discount_arg:
        value = discount_arg.removeprefix("discount:")
        if not value.isdigit() or not (1 <= int(value) <= 99):
            await message.answer("Скидка должна быть числом от 1 до 99.")
            return
        promo_type = "discount"; discount_percent = int(value)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promos (code,days,uses,promo_type,tariff_key,discount_percent) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5,discount_percent=$6",
            code, days, uses, promo_type, tariff_key, discount_percent,
        )
    await message.answer(f"Промокод {code} создан. Тип: {promo_type}. Дней: {days}. Исп.: {uses}")

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n"
            "/add_promo КОД ДНИ [исп.]\n"
            "/add_promo КОД ДНИ [исп.] free:vpn|vpn_bypass|choice\n"
            "/add_promo КОД 0 [исп.] discount:ПРОЦЕНТ"
        )
        return
    await _save_promo(message, parts)

@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminPromoState.waiting_input)
    await message.answer(
        "Генерация промокода\n\n"
        "КОД ДНИ [исп.] — добавляет дни\n"
        "КОД ДНИ [исп.] free:vpn|vpn_bypass|choice — бесплатный тариф\n"
        "КОД 0 [исп.] discount:ПРОЦЕНТ — скидка %\n\n"
        "Число вместо кода → авто генерация\n/cancel — отмена"
    )

@router.message(Command("cancel"), AdminPromoState.waiting_input)
async def genpromo_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(AdminPromoState.waiting_input)
async def admin_genpromo_handle(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if parts[0].isdigit():
        parts = [uuid.uuid4().hex[:8].upper()] + parts
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Неверный формат.")
        return
    await _save_promo(message, parts)

@router.message(Command("list_promos"))
async def admin_list_promos(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code,days,uses,promo_type,tariff_key,discount_percent FROM promos ORDER BY promo_type,days DESC")
    if not rows:
        await message.answer("Промокодов нет.")
        return
    lines = ["Промокоды:"]
    for r in rows:
        ptype = r["promo_type"] or "days"
        extra = ""
        if ptype == "discount":
            extra = f" · скидка {r['discount_percent']}%"
        elif ptype == "free_tariff":
            extra = f" · {PLANS.get(r['tariff_key'] or '', {}).get('name', r['tariff_key'])}"
        elif ptype == "free_choice":
            extra = " · на выбор"
        days_str = f"{r['days']} дн." if r["days"] else "-"
        lines.append(f"{r['code']} — {days_str}, {r['uses']} исп.{extra}")
    await message.answer("\n".join(lines))

@router.callback_query(F.data == "admin_promos")
async def admin_promos_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code,days,uses,promo_type FROM promos ORDER BY promo_type,days DESC")
    lines = ["Промокоды", ""]
    if not rows:
        lines.append("Промокодов нет.")
    else:
        for r in rows:
            days_str = f"{r['days']} дн." if r["days"] else "-"
            lines.append(f"{r['code']} — {days_str}, {r['uses']} исп. ({r['promo_type']})")
    lines += ["", "Команды: /add_promo, /genpromo, /list_promos"]
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

# ─────────────────────────────────────────────
#  РАССЫЛКА
# ─────────────────────────────────────────────
@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("Рассылка\n\nВведите текст.\n/cancel — отмена.")

@router.message(Command("cancel"), BroadcastState.waiting_text)
async def broadcast_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.")

@router.message(Command("cancel"), BroadcastState.confirming)
async def broadcast_cancel2(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.")

@router.message(BroadcastState.waiting_text)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastState.confirming)
    await message.answer(
        f"Предпросмотр:\n\n{message.text}\n\nПодтвердите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Разослать всем", callback_data="bc_confirm")],
            [InlineKeyboardButton(text="Разослать подписчикам", callback_data="bc_confirm_subs")],
            [InlineKeyboardButton(text="Отмена", callback_data="bc_cancel")],
        ]),
    )

async def _do_broadcast(cb: CallbackQuery, state: FSMContext, subs_only: bool = False):
    data = await state.get_data()
    text_body = data.get("broadcast_text", "")
    await state.clear()
    if not text_body:
        await cb.answer("Текст не найден.", show_alert=True)
        return
    await cb.message.edit_text("Рассылка запущена...")
    async with pool.acquire() as conn:
        if subs_only:
            users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
        else:
            users = await conn.fetch("SELECT user_id FROM users")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], text_body)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(f"Готово.\nОтправлено: {ok} · Ошибок: {fail}")

@router.callback_query(F.data == "bc_confirm")
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await _do_broadcast(cb, state, subs_only=False)

@router.callback_query(F.data == "bc_confirm_subs")
async def broadcast_confirm_subs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await _do_broadcast(cb, state, subs_only=True)

@router.callback_query(F.data == "bc_cancel")
async def broadcast_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Рассылка отменена.")

# ─────────────────────────────────────────────
#  РЕФЕРАЛЫ (админ)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_referrals")
async def admin_referrals_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.user_id, u.username, u.referral_balance, "
            "(SELECT COUNT(*) FROM users r WHERE r.referrer_id = u.user_id) AS ref_count "
            "FROM users u WHERE u.referral_balance > 0 OR EXISTS ("
            "  SELECT 1 FROM users r WHERE r.referrer_id = u.user_id"
            ") ORDER BY u.referral_balance DESC"
        )
    if not rows:
        await cb.message.edit_text("Рефералов пока нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
        ]))
        return
    lines = ["Рефералы", ""]
    total_balance = 0.0
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        bal = float(r["referral_balance"] or 0)
        total_balance += bal
        lines.append(f"{uname} — приглашено: {r['ref_count']}, баланс: {bal:.2f} руб.")
    lines += ["", f"Итого на балансах: {total_balance:.2f} руб.",
              "", "Для выплаты и обнуления баланса: /payout username"]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... обрезано"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

@router.message(Command("payout"))
async def admin_payout(message: types.Message, command: CommandObject):
    """Отметить реферальный баланс юзера как выплаченный (обнулить + записать в историю)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    target = (command.args or "").strip().lstrip("@")
    if not target:
        await message.answer("Формат: /payout username")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, referral_balance FROM users WHERE user_id=$1" if target.isdigit()
            else "SELECT user_id, referral_balance FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
        if not row:
            await message.answer(f"{target} не найден.")
            return
        balance = float(row["referral_balance"] or 0)
        if balance <= 0:
            await message.answer("Баланс уже нулевой.")
            return
        await conn.execute("UPDATE users SET referral_balance=0 WHERE user_id=$1", row["user_id"])
        await conn.execute(
            "INSERT INTO referral_payouts (user_id, amount, created_at) VALUES ($1,$2,$3)",
            row["user_id"], balance, int(time.time()),
        )
    await message.answer(f"Выплата {balance:.2f} руб. пользователю {target} зафиксирована, баланс обнулён.")
    try:
        await bot.send_message(row["user_id"], f"Ваш реферальный баланс {balance:.2f} руб. выплачен.")
    except Exception:
        pass

# ─────────────────────────────────────────────
#  ОТЧЁТ (кнопка панели — отправить сразу)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_report")
async def admin_report_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await cb.message.edit_text("Формирую отчёт...")
    await send_daily_report()
    await cb.message.edit_text("Отчёт отправлен в личные сообщения.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

# ─────────────────────────────────────────────
#  ВЫДАТЬ (кнопка панели — conversational flow)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_give_start")
async def admin_give_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminGiveState.waiting_username)
    await cb.message.answer("Выдача подписки\n\nВведите username (без @):\n/cancel — отмена")

@router.message(Command("cancel"), AdminGiveState.waiting_username)
@router.message(Command("cancel"), AdminGiveState.waiting_days)
@router.message(Command("cancel"), AdminGiveState.waiting_devices)
async def admin_give_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(AdminGiveState.waiting_username)
async def admin_give_username(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", username)
    if not row:
        await message.answer(f"@{username} не найден. Введите другой username или /cancel.")
        return
    await state.update_data(target_id=row["user_id"], target_username=username)
    await state.set_state(AdminGiveState.waiting_days)
    await message.answer(f"@{username}\n\nСколько дней выдать?")

@router.message(AdminGiveState.waiting_days)
async def admin_give_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().lstrip("-").isdigit():
        await message.answer("Введите целое число дней.")
        return
    days = int(message.text.strip())
    await state.update_data(days=days)
    await state.set_state(AdminGiveState.waiting_devices)
    await message.answer("Сколько устройств выставить? (0 = не менять текущее значение)")

@router.message(AdminGiveState.waiting_devices)
async def admin_give_devices(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое число (0 = не менять).")
        return
    devices = int(message.text.strip())
    await state.update_data(devices=devices)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Не менять тариф/сквад", callback_data="giveplan_none")],
        [InlineKeyboardButton(text="VPN", callback_data="giveplan_vpn")],
        [InlineKeyboardButton(text="VPN с обходом белых списков", callback_data="giveplan_vpn_bypass")],
        [InlineKeyboardButton(text="Пробный доступ (белые списки, 3 ГБ)", callback_data="giveplan_trial")],
    ])
    await message.answer(
        "Какой тариф/доступ выставить?\n\n"
        "«Не менять» — только продлить дни, сквад и вариант подписки останутся как есть.\n"
        "«VPN» / «VPN с обходом» — выставит соответствующий тариф и профиль пользователя "
        "переключится в купленное состояние (появятся кнопки «Добавить устройства» и т.д.).\n"
        "«Пробный доступ» — доступ к белым спискам с лимитом 3 ГБ, но БЕЗ пометки как "
        "купленный тариф (кнопки покупки в профиле останутся).",
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("giveplan_"))
async def admin_give_finalize(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    choice = cb.data.removeprefix("giveplan_")
    data = await state.get_data()
    if "target_id" not in data:
        await cb.message.edit_text("Сессия истекла, начните заново через «Выдать».")
        return
    await state.clear()

    target_id    = data["target_id"]
    target_uname = data["target_username"]
    days         = data["days"]
    devices      = data.get("devices", 0)
    hwid         = devices if devices > 0 else None

    squad_uuid   = None
    whitelist_gb = 0
    new_plan     = None       # None = не трогать поле plan в БД
    clear_plan   = False      # explicit сброс plan на NULL (для "не менять" не используется)

    if choice == "vpn":
        squad_uuid = SQUAD_UUID_BASIC
        new_plan   = "vpn"
    elif choice == "vpn_bypass":
        squad_uuid   = SQUAD_UUID_WHITELIST
        whitelist_gb = PLANS["vpn_bypass"]["whitelist_gb"]
        new_plan     = "vpn_bypass"
    elif choice == "trial":
        squad_uuid   = SQUAD_UUID_WHITELIST
        whitelist_gb = TRIAL["whitelist_gb"]
        new_plan     = "trial"
    # choice == "none": оставляем squad_uuid=None, new_plan=None (ничего не трогаем)

    user = await activate_subscription(
        target_id, days, hwid or 1, squad_uuid=squad_uuid, whitelist_gb=whitelist_gb
    )
    if not user:
        await cb.message.edit_text("Ошибка активации.")
        return

    async with pool.acquire() as conn:
        if new_plan is not None:
            extra_devices = max(0, (hwid or 1) - 1)
            await conn.execute(
                "UPDATE users SET plan=$1, extra_devices=$2 WHERE user_id=$3",
                new_plan, extra_devices, target_id,
            )

    expire   = parse_dt(user.get("expireAt"))
    date_str = fmt_dt(expire, "%d.%m.%Y") if expire else "нет данных"
    label = {"none": "без изменения тарифа", "vpn": "VPN", "vpn_bypass": "VPN с обходом", "trial": "пробный доступ"}[choice]
    await cb.message.edit_text(f"@{target_uname} выдано {days} дн. ({label}). До: {date_str}")
    try:
        await bot.send_message(target_id, f"Администратор выдал вам {days} дней.")
    except Exception:
        pass

# ─────────────────────────────────────────────
#  НАЙТИ ЮЗЕРА (кнопка панели)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_find_start")
async def admin_find_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminFindState.waiting_query)
    await cb.message.answer("Введите username или user_id для поиска:\n/cancel — отмена")

@router.message(Command("cancel"), AdminFindState.waiting_query)
async def admin_find_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(AdminFindState.waiting_query)
async def admin_find_handler(message: types.Message, state: FSMContext):
    await state.clear()
    target = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id=$1" if target.isdigit() else "SELECT user_id FROM users WHERE username=$1",
            int(target) if target.isdigit() else target,
        )
    if not row:
        await message.answer(f"Пользователь {target} не найден.")
        return
    await _render_check(message, row["user_id"])

# ─────────────────────────────────────────────
#  КТО ОНЛАЙН
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_online")
async def admin_online_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await cb.message.edit_text("Запрашиваю...")
    now = int(time.time())
    all_users = await remna_get_all_users()
    online = [
        u for u in all_users
        if u.get("username", "").startswith("truba_")
        and parse_dt(u.get("userTraffic", {}).get("onlineAt")) > (now - 180)
    ]
    if not online:
        await cb.message.edit_text("Сейчас никто не подключён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
        ]))
        return
    lines = [f"Онлайн: {len(online)} чел.", ""]
    for u in online[:40]:
        uid  = u["username"].replace("truba_", "")
        last = fmt_dt(parse_dt(u.get("userTraffic", {}).get("onlineAt")), "%H:%M:%S")
        async with pool.acquire() as conn:
            db = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", int(uid) if uid.isdigit() else 0)
        tg = f"@{db['username']}" if db and db["username"] else f"ID:{uid}"
        lines.append(f"{tg} · {last}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... обрезано"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить", callback_data="admin_online")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

# ─────────────────────────────────────────────
#  РАССЫЛКА (запуск кнопкой из панели)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_broadcast_start")
async def admin_broadcast_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await cb.message.answer("Рассылка\n\nВведите текст.\n/cancel — отмена.")

# ─────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    sale_notify = await is_admin_sale_notify(cb.from_user.id)
    text = f"Настройки\n\nУведомления о покупках: {'вкл' if sale_notify else 'выкл'}"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Выключить уведомления" if sale_notify else "Включить уведомления",
            callback_data="admin_toggle_sale_notify",
        )],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

@router.callback_query(F.data == "admin_toggle_sale_notify")
async def admin_toggle_sale_notify_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    admin_id = cb.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sale_notify FROM admin_settings WHERE admin_id=$1", admin_id)
        current = row["sale_notify"] if row and row["sale_notify"] is not None else True
        new_val = not current
        if row:
            await conn.execute("UPDATE admin_settings SET sale_notify=$1 WHERE admin_id=$2", new_val, admin_id)
        else:
            await conn.execute("INSERT INTO admin_settings (admin_id, sale_notify) VALUES ($1,$2)", admin_id, new_val)
    text = f"Настройки\n\nУведомления о покупках: {'вкл' if new_val else 'выкл'}"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Выключить уведомления" if new_val else "Включить уведомления",
            callback_data="admin_toggle_sale_notify",
        )],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

# ─────────────────────────────────────────────
#  ОПРОС (рейтинг сервиса среди платников)
# ─────────────────────────────────────────────
def _rating_kb() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"survey_rate_{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])

@router.callback_query(F.data == "admin_survey")
async def admin_survey_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await cb.message.edit_text("Опрос", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Разослать опрос платникам", callback_data="admin_survey_send")],
        [InlineKeyboardButton(text="Результаты", callback_data="admin_survey_results")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

@router.callback_query(F.data == "admin_survey_send")
async def admin_survey_send_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    await cb.message.edit_text("Рассылаю опрос платникам...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(
                row["user_id"],
                "Оцените работу TrubaVPN\n\nНасколько вы довольны сервисом? Выберите оценку от 1 до 10:",
                reply_markup=_rating_kb(),
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(f"Опрос разослан.\nДоставлено: {ok} · Ошибок: {fail}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_survey")],
    ]))

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
    await cb.message.edit_text(
        f"Вы поставили оценку {rating}/10.\n\n"
        "Напишите короткий комментарий — что понравилось или что можно улучшить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="survey_skip_comment")],
        ]),
    )

@router.callback_query(F.data == "survey_skip_comment")
async def survey_skip_comment_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    rating = data.get("survey_rating", 0)
    await state.clear()
    await _save_survey(cb.from_user, rating, None)
    await cb.message.edit_text("Спасибо за вашу оценку.")

@router.message(SurveyState.waiting_comment)
async def survey_comment_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("survey_rating", 0)
    comment = message.text.strip()
    await state.clear()
    await _save_survey(message.from_user, rating, comment)
    await message.answer("Спасибо за ваш отзыв.")

async def _save_survey(user, rating: int, comment: str | None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO survey_responses (user_id, username, rating, comment, created_at) VALUES ($1,$2,$3,$4,$5)",
            user.id, user.username, rating, comment, int(time.time()),
        )
    for admin_id in ADMIN_IDS:
        try:
            uname = f"@{user.username}" if user.username else f"ID:{user.id}"
            text = f"Новый отзыв\n\n{uname}\nОценка: {rating}/10"
            text += f"\nКомментарий: {comment}" if comment else "\nБез комментария"
            await bot.send_message(admin_id, text)
        except Exception:
            pass

@router.callback_query(F.data == "admin_survey_results")
async def admin_survey_results_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM survey_responses") or 0
        if not total:
            await cb.message.edit_text("Ответов на опрос пока нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data="admin_survey")],
            ]))
            return
        avg = await conn.fetchval("SELECT AVG(rating) FROM survey_responses") or 0
        dist = await conn.fetch("SELECT rating, COUNT(*) as cnt FROM survey_responses GROUP BY rating ORDER BY rating DESC")
        comments = await conn.fetch(
            "SELECT username, rating, comment, created_at FROM survey_responses "
            "WHERE comment IS NOT NULL ORDER BY created_at DESC LIMIT 10"
        )
    avg_r = round(float(avg), 2)
    lines = [f"Результаты опроса", "", f"Всего ответов: {total}", f"Средняя оценка: {avg_r}/10", "", "Распределение:"]
    for r in dist:
        bar = "#" * min(r["cnt"], 20)
        lines.append(f"  {r['rating']:2d}/10 · {r['cnt']:3d} чел.  {bar}")
    if comments:
        lines += ["", "Последние комментарии:"]
        for c in comments:
            uname = f"@{c['username']}" if c["username"] else "аноним"
            dt = fmt_dt(c["created_at"], "%d.%m")
            preview = c["comment"][:120] + "..." if len(c["comment"]) > 120 else c["comment"]
            lines.append(f"  {c['rating']}/10 · {uname} [{dt}]: {preview}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... обрезано"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_survey")],
    ]))

# ─────────────────────────────────────────────
#  СТАТИСТИКА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_stats")
async def admin_stats_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    now = int(time.time())
    async with pool.acquire() as conn:
        total  = await conn.fetchval("SELECT COUNT(*) FROM users")
        paid   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE has_paid=1")
        promos = await conn.fetchval("SELECT COUNT(*) FROM promos")
        ref_balance_total = await conn.fetchval("SELECT COALESCE(SUM(referral_balance),0) FROM users")
    all_users = await remna_get_all_users()
    our    = [u for u in all_users if u.get("username", "").startswith("truba_")]
    active = sum(1 for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED")
    sale_notify = await is_admin_sale_notify(cb.from_user.id)
    text = (
        f"Статистика TrubaVPN\n\n"
        f"Всего: {total} · Платили: {paid}\n"
        f"Активных: {active} · Промокодов: {promos}\n"
        f"На реф. балансах: {float(ref_balance_total):.2f} руб.\n\n"
        f"Уведомления о покупках: {'вкл' if sale_notify else 'выкл'} (/sale_notify)"
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

@router.message(Command("sale_notify"))
async def toggle_sale_notify(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    admin_id = message.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT sale_notify FROM admin_settings WHERE admin_id=$1", admin_id)
        current = row["sale_notify"] if row and row["sale_notify"] is not None else True
        new_val = not current
        if row:
            await conn.execute("UPDATE admin_settings SET sale_notify=$1 WHERE admin_id=$2", new_val, admin_id)
        else:
            await conn.execute("INSERT INTO admin_settings (admin_id, sale_notify) VALUES ($1,$2)", admin_id, new_val)
    await message.answer("Уведомления о покупках включены." if new_val else "Уведомления о покупках выключены.")

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    """Выдать дни вручную, без привязки к тарифу (сквад не меняется)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /give username дни [устройств]")
        return
    target = parts[0].lstrip("@"); days = int(parts[1])
    hwid   = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", target)
    if not row:
        await message.answer(f"@{target} не найден.")
        return
    user = await activate_subscription(row["user_id"], days, hwid or 1, squad_uuid=None)
    if not user:
        await message.answer("Ошибка активации.")
        return
    expire   = parse_dt(user.get("expireAt"))
    date_str = fmt_dt(expire, "%d.%m.%Y") if expire else "нет данных"
    await message.answer(f"@{target} выдано {days} дн. До: {date_str}")
    try:
        await bot.send_message(row["user_id"], f"Администратор выдал вам {days} дней.")
    except Exception:
        pass

@router.message(Command("admin"))
async def admin_help(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "Команды администратора:\n\n"
        "/give username дни [уст.] — быстро выдать дни (тариф/сквад не меняет; "
        "для выбора тарифа используйте кнопку «Выдать» в панели)\n"
        "/check username|id — карточка подписчика\n"
        "/add_promo, /genpromo, /list_promos — промокоды\n"
        "/broadcast — рассылка\n"
        "/payout username — выплатить реф. баланс\n"
        "/sale_notify — вкл/выкл уведомления о покупках\n"
        "/whitelist_check, /whitelist_status — лимиты белых списков\n\n"
        "Кнопка «Панель» в профиле открывает то же самое через интерфейс."
    )

# ─────────────────────────────────────────────
#  ЛИМИТ ТРАФИКА НА СЕРВЕРЕ БЕЛЫХ СПИСКОВ
# ─────────────────────────────────────────────
async def check_whitelist_limits():
    if not WHITELIST_NODE_UUID:
        return
    try:
        async with pool.acquire() as conn:
            tracked = await conn.fetch("SELECT user_id, gb_limit, period_start, cut_off FROM whitelist_limits")
        if not tracked:
            return
        records = await fetch_whitelist_daily_records(days_back=40)
        for row in tracked:
            user_id, gb_limit, period_start, already_cut = row["user_id"], row["gb_limit"], row["period_start"], row["cut_off"]
            used_bytes  = sum_whitelist_bytes_for_user(records, user_id, period_start)
            limit_bytes = gb_limit * 1024 ** 3
            if used_bytes >= limit_bytes and not already_cut:
                remna = await remna_get_user(user_id)
                if not remna:
                    continue
                current_squads = _squad_uuids(remna.get("activeInternalSquads"))
                new_squads = [s for s in current_squads if s != SQUAD_UUID_WHITELIST]
                if SQUAD_UUID_BASIC not in new_squads:
                    new_squads.append(SQUAD_UUID_BASIC)
                result = await remna_update_user(remna["uuid"], {"activeInternalSquads": new_squads})
                if result:
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE whitelist_limits SET cut_off=TRUE WHERE user_id=$1", user_id)
                    used_gb = round(used_bytes / 1024 ** 3, 2)
                    log.info("[Whitelist] user=%s exceeded limit (%s/%sGB) — squad removed", user_id, used_gb, gb_limit)
                    try:
                        await bot.send_message(
                            user_id,
                            f"Вы исчерпали лимит трафика на белых списках "
                            f"({used_gb:.1f}/{gb_limit} GB за текущий период). "
                            f"Доступ к остальным серверам сохранён.",
                        )
                    except Exception:
                        pass
    except Exception as e:
        log.error("check_whitelist_limits error: %s", e)

async def whitelist_limit_scheduler():
    while True:
        await asyncio.sleep(30 * 60)
        await check_whitelist_limits()

@router.message(Command("whitelist_check"))
async def admin_whitelist_check_now(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not WHITELIST_NODE_UUID:
        await message.answer("WHITELIST_NODE_UUID не задан.")
        return
    await message.answer("Проверяю лимиты белых списков...")
    await check_whitelist_limits()
    await message.answer("Готово. Смотри /whitelist_status для деталей.")

@router.message(Command("whitelist_status"))
async def admin_whitelist_status(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not WHITELIST_NODE_UUID:
        await message.answer("WHITELIST_NODE_UUID не задан.")
        return
    async with pool.acquire() as conn:
        tracked = await conn.fetch(
            "SELECT wl.user_id, wl.gb_limit, wl.period_start, wl.cut_off, u.username "
            "FROM whitelist_limits wl LEFT JOIN users u ON u.user_id = wl.user_id "
            "ORDER BY wl.period_start DESC"
        )
    if not tracked:
        await message.answer("Пока никто не отслеживается по лимиту белых списков.")
        return
    await message.answer("Считаю расход...")
    records = await fetch_whitelist_daily_records(days_back=40)
    lines = ["Лимиты белых списков", ""]
    for row in tracked:
        used = sum_whitelist_bytes_for_user(records, row["user_id"], row["period_start"])
        used_gb  = used / 1024 ** 3
        limit_gb = row["gb_limit"]
        pct = round(used_gb / limit_gb * 100) if limit_gb > 0 else 0
        uname = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
        since = fmt_dt(row["period_start"], "%d.%m")
        lines.append(f"{uname} — {used_gb:.1f}/{limit_gb} GB ({pct}%){' ОТКЛЮЧЁН' if row['cut_off'] else ''} · с {since}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... обрезано"
    await message.answer(text)

# ─────────────────────────────────────────────
#  ЕЖЕДНЕВНЫЙ ОТЧЁТ
# ─────────────────────────────────────────────
async def send_daily_report():
    now       = int(time.time())
    date      = msk_now().strftime("%d.%m.%Y")
    day_start = int(msk_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    try:
        async with pool.acquire() as conn:
            new_users  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at>=$1", day_start) or 0
            pay_rows   = await conn.fetch("SELECT is_trial, amount FROM payments WHERE created_at>=$1", day_start)
            new_trials = sum(1 for p in pay_rows if p["is_trial"])
            new_paid   = sum(1 for p in pay_rows if not p["is_trial"])
            revenue    = sum(float(p["amount"]) for p in pay_rows if not p["is_trial"])
            total_paid = await conn.fetchval("SELECT COUNT(*) FROM users WHERE has_paid=1") or 0
        all_users = await remna_get_all_users()
        our    = [u for u in all_users if u.get("username", "").startswith("truba_")]
        active = sum(1 for u in our if parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED")
        report = (
            f"Отчёт за {date} (МСК)\n\n"
            f"Новых пользователей: {new_users}\n"
            f"Новых триалов: {new_trials}\n"
            f"Новых оплат: {new_paid}\n"
            f"Поступления за день: {revenue:.2f} руб.\n\n"
            f"Активных подписок: {active}\n"
            f"Платили хоть раз: {total_paid}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, report)
            except Exception:
                pass
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

@router.message(Command("report"))
async def admin_report_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Формирую отчёт...")
    await send_daily_report()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
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
