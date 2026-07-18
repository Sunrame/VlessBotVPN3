import os
import uuid
import string
import secrets
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, WebAppInfo

from yookassa import Configuration, Payment
from payment_titles import for_yookassa

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

# Динамически добавленные админы (через панель) — хранятся в БД, кэшируются
# в память при старте и при каждом добавлении/удалении, чтобы не дёргать БД
# на каждую проверку прав. ADMIN_IDS (из переменных окружения) — "главные"
# админы, только они могут добавлять/убирать остальных.
EXTRA_ADMIN_IDS: set[int] = set()

def is_admin(user_id: int) -> bool:
    """Любой админ — главный (из env) или добавленный через панель."""
    return user_id in ADMIN_IDS or user_id in EXTRA_ADMIN_IDS

def is_main_admin(user_id: int) -> bool:
    """Только главный админ (ADMIN_ID_1/ADMIN_ID_2) — может управлять списком админов."""
    return user_id in ADMIN_IDS

def all_admin_ids() -> list[int]:
    """Главные + динамически добавленные админы — для массовых рассылок уведомлений."""
    return ADMIN_IDS + list(EXTRA_ADMIN_IDS)

CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "@Truba_VPN")

# Юзернейм поддержки — кнопка "Тех.Поддержка" ведёт в личку с этим аккаунтом
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "vvvvvpppnn")
SUPPORT_URL      = f"https://t.me/{SUPPORT_USERNAME}"

# Личный кабинет на сайте (отдельный от бота веб-проект). Если SITE_URL не
# задан, кнопка всё равно показывается, но при нажатии скажет "недоступен".
SITE_URL           = os.environ.get("SITE_URL", "").rstrip("/")
LOGIN_CODE_TTL_MIN = int(os.environ.get("LOGIN_CODE_TTL_MIN", "5"))

# URL мини-приложения «Личный кабинет» (Telegram Mini App). Кнопка "Личный
# кабинет" открывает его прямо в Telegram как WebApp; вход в аккаунт
# автоматический — по подписанным Telegram initData (без кодов). Если явно не
# задан, берётся SITE_URL + "/tgapp". Если ничего не задано — кнопка покажет
# "временно недоступен".
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").rstrip("/") or (f"{SITE_URL}/tgapp" if SITE_URL else "")

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
#  PREMIUM-ЭМОДЗИ
#
#  Работают только в тексте сообщений (parse_mode="HTML"), НЕ в кнопках —
#  кнопки Telegram принимают только простой текст без форматирования.
#    ля непремиум-пользователей и старых клиентов показывается fallback-символ
#  вместо кастомного эмодзи. Расставлены по одному разу на смысловой момент,
#  без перегруза одного и того же места несколькими штуками.
# ─────────────────────────────────────────────
def premium_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def btn(text: str, *, emoji_id: str | None = None, style: str | None = None, **kwargs) -> InlineKeyboardButton:
    """
    Обёртка над InlineKeyboardButton с premium-иконкой (icon_custom_emoji_id) и/или
    цветом (style: 'primary'/'success'/'danger') — Bot API 9.4, требует aiogram>=3.29.0.
    icon_custom_emoji_id доступен только если у аккаунта бота есть Telegram Premium
    либо куплен доп. юзернейм на Fragment.
    """
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=emoji_id, style=style, **kwargs)

# ─────────────────────────────────────────────
#  ID КАСТОМНЫХ EMOJI ДЛЯ КНОПОК (icon_custom_emoji_id)
# ─────────────────────────────────────────────
BTN_ICON_CHANNEL_SUB     = "5424818078833715060"  # Подписаться на канал / Канал с инструкциями
BTN_ICON_CHECK_SUB       = "5206607081334906820"  # Я подписался / Проверить оплату
BTN_ICON_TRIAL           = "5280615440928758599"  # Пробная подписка
BTN_ICON_TOS             = "5197269100878907942"  # Пользовательское соглашение
BTN_ICON_SUPPORT         = "5443038326535759644"  # Тех.Поддержка
BTN_ICON_PRIVACY         = "5251203410396458957"  # Политика конфиденциальности
BTN_ICON_PAY             = "5445353829304387411"  # Оплатить
BTN_ICON_BUY_VPN         = "5312361253610475399"  # Купить VPN
BTN_ICON_PROMO           = "5465169893580086142"  # Промокод
BTN_ICON_INFO            = "5334544901428229844"  # О сервисе
BTN_ICON_EARN            = "5287231198098117669"  # Заработать
BTN_ICON_PLAN_BYPASS     = "5447410659077661506"  # VPN с обходом белых списков
BTN_ICON_PLAN_VPN        = "5427168083074628963"  # VPN
BTN_ICON_ADMIN           = "5231200819986047254"  # Панель (админ)
BTN_ICON_DEV_TOPUP       = "5407025283456835913"  # Добавить устройства
BTN_ICON_GB_TOPUP        = "5283080528818360566"  # Докупить трафик
BTN_ICON_UPGRADE         = "5449683594425410231"  # Улучшить тариф

EMOJI_TRUBAVPN      = premium_emoji("5224450179368767019", "\U0001f310")  # "TrubaVPN" в приветствии
EMOJI_INFO          = premium_emoji("5334544901428229844", "\u2728")   # заголовок "О сервисе"
EMOJI_EARN          = premium_emoji("5287231198098117669", "\U0001f4b0")  # заголовок "Заработать"
EMOJI_PROFILE       = premium_emoji("5341715473882955310", "\u2b50")   # заголовок "Профиль"
EMOJI_PLAN_BYPASS   = premium_emoji("5447410659077661506", "\U0001f7e3")  # заголовок "VPN с обходом белых списков"
EMOJI_PLAN_VPN      = premium_emoji("5427168083074628963", "\U0001f535")  # заголовок "VPN"
EMOJI_CHOOSE_TERM   = premium_emoji("5382194935057372936", "\U0001f4c5")  # "Выберите срок"
EMOJI_SUB_LINK      = premium_emoji("5271604874419647061", "\U0001f517")  # "Ссылка на подписку:"
EMOJI_ACTIVE_UNTIL  = premium_emoji("5274055917766202507", "\U0001f4c6")  # "Активна до:"
EMOJI_PLAN_LABEL    = premium_emoji("5197288647275071607", "\U0001f4cb")  # "Вариант подписки:"
EMOJI_ADMIN         = premium_emoji("5231200819986047254", "\u2699\ufe0f")  # заголовок "Админ-панель"
EMOJI_DEV_TOPUP     = premium_emoji("5407025283456835913", "\U0001f4f1")  # заголовок "Добавить устройства"
EMOJI_GB_TOPUP      = premium_emoji("5283080528818360566", "\U0001f4ca")  # заголовок "Докупить трафик"
EMOJI_UPGRADE       = premium_emoji("5449683594425410231", "\u2b06\ufe0f")  # "Улучшение тарифа" (оплата)
EMOJI_INVITE        = premium_emoji("5264713049637409446", "\U0001f465")  # "Приглашайте друзей.."

# ─────────────────────────────────────────────
#  Технически невозможно вставить (кнопки Telegram не поддерживают формати-
#  рование/кастомные эмодзи, только plain text) — оставлено для справки,
#  какие ID из присланного списка на что были рассчитаны:
#    5424818078833715060 — "Подписаться на канал" / "Канал с инструкциями" (кнопки)
#    5206607081334906820 — "Я подписался" / "Проверить оплату" (кнопки)
#    5280615440928758599 — "Пробная подписка" (кнопка)
#    5197269100878907942 — "Пользовательское соглашение" (кнопка)
#    5443038326535759644 — "Тех.Поддержка" (кнопка)
#    5251203410396458957 — "Политика конфиденциальности" (кнопка)
#    5445353829304387411 — "Оплатить" (кнопка)
#    5312361253610475399 — "Купить VPN" (кнопка)
#    5465169893580086142 — "Промокод" (кнопка)
# ─────────────────────────────────────────────


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
    "squad":        [SQUAD_UUID_BASIC, SQUAD_UUID_WHITELIST],
    "whitelist_gb": 3,
    "desc": (
        "24 часа доступа ко всем серверам. Трафик VPN не ограничен, трафик "
        "на обход белых списков ограничен 3 ГБ. Лимит устройств — 1."
    ),
}

PLANS = {
    "vpn": {
        "key":          "vpn",
        "name":         "VPN",
        "price_month":  99,
        "device_price": 50,
        "squad":        [SQUAD_UUID_BASIC],
        "whitelist_gb": 0,
        "desc": "Более трёх локаций, 1 устройство, трафик не ограничен.",
    },
    "vpn_bypass": {
        "key":          "vpn_bypass",
        "name":         "VPN с обходом белых списков",
        "price_month":  149,
        "device_price": 70,
        "squad":        [SQUAD_UUID_BASIC, SQUAD_UUID_WHITELIST],
        "whitelist_gb": 20,
        "desc": (
            "Более трёх локаций, 1 устройство, трафик не ограничен. "
            "Трафик на обход белых списков ограничен 20 ГБ."
        ),
    },
}

MONTH_CHOICES = [1, 3, 6, 12]

# Докупка трафика на белых списках: цена за 1 ГБ. Пользователь сам вводит
# сколько ГБ хочет купить, итоговая цена = кол-во * цена за ГБ.
WHITELIST_PRICE_PER_GB = int(os.environ.get("WHITELIST_PRICE_PER_GB", "3"))

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

class PromoGenState(StatesGroup):
    waiting_days       = State()
    waiting_uses_custom = State()
    waiting_min_age_custom = State()
    waiting_code       = State()

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

class DeviceTopupState(StatesGroup):
    waiting_count = State()

class WhitelistTopupState(StatesGroup):
    waiting_gb = State()

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
                discount_percent INTEGER DEFAULT 0,
                min_account_age_days INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                user_id BIGINT NOT NULL,
                code TEXT NOT NULL,
                redeemed_at BIGINT DEFAULT 0,
                source TEXT DEFAULT 'bot',
                PRIMARY KEY (user_id, code)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY, user_id BIGINT,
                amount NUMERIC, tariff_key TEXT, days INTEGER,
                is_trial BOOLEAN DEFAULT FALSE, created_at BIGINT DEFAULT 0,
                payment_id TEXT
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
            CREATE TABLE IF NOT EXISTS extra_admins (
                user_id    BIGINT PRIMARY KEY,
                username   TEXT,
                added_by   BIGINT,
                added_at   BIGINT DEFAULT 0
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_payments (
                payment_id   TEXT PRIMARY KEY,
                processed_at BIGINT DEFAULT 0
            )
        """)
        # Отправленные напоминания об окончании подписки (по срокам 3д/1д/1ч).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS expiry_reminders (
                user_id   BIGINT,
                kind      TEXT,
                expire_ts BIGINT,
                sent_at   BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, kind, expire_ts)
            )
        """)
        # Токены личного кабинета на сайте (общий с веб-панелью механизм входа).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cabinet_tokens (
                user_id    BIGINT PRIMARY KEY,
                code       TEXT UNIQUE NOT NULL,
                created_at BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cabinet_login_codes (
                user_id    BIGINT PRIMARY KEY,
                code       TEXT NOT NULL,
                expires_at BIGINT DEFAULT 0,
                attempts   INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT,
                kind       TEXT,
                text       TEXT,
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
            await conn.execute("ALTER TABLE promos ADD COLUMN min_account_age_days INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE payments ADD COLUMN source TEXT DEFAULT 'purchase'")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE payments ADD COLUMN note TEXT")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE payments ADD COLUMN payment_id TEXT")
        except Exception:
            pass
        try:
            await conn.execute("""
                INSERT INTO promo_redemptions (user_id, code, redeemed_at, source)
                SELECT user_id, UPPER(TRIM(note)), MIN(created_at), 'history'
                FROM payments
                WHERE source='promo' AND note IS NOT NULL AND TRIM(note)<>''
                GROUP BY user_id, UPPER(TRIM(note))
                ON CONFLICT (user_id, code) DO NOTHING
            """)
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE admin_settings ADD COLUMN sale_notify BOOLEAN DEFAULT TRUE")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE user_activity ADD COLUMN seen BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        try:
            await conn.execute("""
                CREATE OR REPLACE FUNCTION trubavpn_log_promo_activity()
                RETURNS trigger AS $$
                BEGIN
                    IF NEW.source='promo' AND NOT EXISTS (
                        SELECT 1 FROM user_activity a
                        WHERE a.user_id=NEW.user_id
                          AND LOWER(COALESCE(a.kind,''))='промокод'
                          AND a.created_at BETWEEN NEW.created_at-10 AND NEW.created_at+10
                          AND COALESCE(a.text,'') ILIKE '%' || COALESCE(NEW.note,'') || '%'
                    ) THEN
                        INSERT INTO user_activity (user_id, kind, text, created_at, seen)
                        VALUES (
                            NEW.user_id, 'Промокод',
                            'Пользователь активировал промокод ' || COALESCE(NULLIF(NEW.note,''),'—') ||
                            ' — получил: +' || COALESCE(NEW.days,0)::TEXT || ' дн.',
                            NEW.created_at, FALSE
                        );
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """)
            await conn.execute("DROP TRIGGER IF EXISTS trg_trubavpn_promo_activity ON payments")
            await conn.execute("""
                CREATE TRIGGER trg_trubavpn_promo_activity
                AFTER INSERT ON payments
                FOR EACH ROW EXECUTE FUNCTION trubavpn_log_promo_activity()
            """)
        except Exception:
            pass
    log.info("PostgreSQL ready.")

async def load_extra_admins():
    """Подгружает динамически добавленных админов из БД в память (при старте
    и после каждого добавления/удаления)."""
    global EXTRA_ADMIN_IDS
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM extra_admins")
    EXTRA_ADMIN_IDS = {r["user_id"] for r in rows}
    log.info("Loaded %d extra admin(s) from DB.", len(EXTRA_ADMIN_IDS))


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

def _normalize_squads(squad_uuid) -> list[str]:
    """squad_uuid может быть одной строкой (старый формат) или списком строк
    (нужно для vpn_bypass/trial — им нужен доступ сразу к 2 скваdам). Всегда
    возвращает список для activeInternalSquads."""
    if isinstance(squad_uuid, list):
        return squad_uuid
    return [squad_uuid]

async def remna_create_user(user_id: int, days: int, hwid: int = 1,
                             squad_uuid: str | list[str] = SQUAD_UUID_BASIC) -> dict | None:
    payload = {
        "username":             remna_username(user_id),
        "trafficLimitBytes":    0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt":             _expire_at(days),
        "hwidDeviceLimit":      hwid,
        "telegramId":           user_id,
        "activeInternalSquads": _normalize_squads(squad_uuid),
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
                             squad_uuid: str | list[str] | None = None) -> dict | None:
    user = await remna_get_user(user_id)
    if not user:
        return await remna_create_user(user_id, days, hwid or 1, squad_uuid or SQUAD_UUID_BASIC)

    now        = datetime.now(timezone.utc)
    current    = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
    base       = max(current, now)
    new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload: dict = {"uuid": user["uuid"], "expireAt": new_expire, "status": "ACTIVE"}
    if squad_uuid is not None:
        payload["activeInternalSquads"] = _normalize_squads(squad_uuid)
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
    Формат д  т: ISO с миллисекундами.
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
    """Сырые дневные записи трафика для ВСЕХ юз                    в на ноде белых списков."""
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
                                 squad_uuid: str | list[str] | None = None,
                                 whitelist_gb: int = 0) -> dict | None:
    """
    squad_uuid=None — не менять текущий сквад (для admin-действий без явного тарифа).
    squad_uuid может быть одной строкой или списком (vpn_bypass/trial нужны сразу
    и Basic, и White List сквады одновременно, а не замена одного на другой).
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
            elif squad_uuid is not None and SQUAD_UUID_WHITELIST not in _normalize_squads(squad_uuid):
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
    for admin_id in all_admin_ids():
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

def calc_upgrade_price(extra_devices: int) -> int:
    """
    Доплата за апгрейд VPN -> VPN с обходом белых списков.

    Дн   подписки при   пгрейде не пересчитываются и не трогаются — остаются
    ровно те же, что были. Поэтом   доплата — просто фиксированная разница
    между тарифами:
      1) Разница цены тарифов за месяц: price_month_bypass - price_month_vpn
      2) Плюс разница в цене устройства, умноженная на кол-во уже купленных
         доп. устройств: (device_price_bypass - device_price_vpn) * extra_devices
    """
    vpn    = PLANS["vpn"]
    bypass = PLANS["vpn_bypass"]
    plan_diff   = bypass["price_month"] - vpn["price_month"]
    device_diff = max(0, bypass["device_price"] - vpn["device_price"]) * max(extra_devices, 0)
    return plan_diff + device_diff

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Подписаться на канал", emoji_id=BTN_ICON_CHANNEL_SUB, url=CHANNEL_LINK)],
        [btn("Я подписался", emoji_id=BTN_ICON_CHECK_SUB, callback_data="check_sub")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="back")]
    ])

# ─────────────────────────────────────────────
#  СТАРТ / ПРОФИЛЬ (единственный домашний экран)
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  ДИПЛИНК-РОУТИНГ ИЗ ЛИЧНОГО КАБИНЕТА
# ─────────────────────────────────────────────
# Когда пользователь хочет что-то оплатить в мини-приложении (личном
# кабинете), мини-приложение открывает бота по ссылке
# t.me/<bot>?start=cab_<section> (например Telegram.WebApp.openTelegramLink)
# и закрывается. Здесь мы разбираем <section> и сразу открываем
# соответствующий раздел/кнопку оплаты уже в боте.
#
# Поддерживаемые разделы (с необязательным количеством в хвосте):
#   trial                     — пробная подписка
#   buy_<план>_<месяцев>      — купить тариф (напр. buy_vpn_3, buy_vpn_bypass_6)
#   extend_<месяцев>          — продлить текущий тариф (напр. extend_3)
#   dev_<кол-во>              — докупить устройства (напр. dev_5) → сразу оплата
#   wl_<ГБ>                   — докупить трафик (напр. wl_10) → сразу оплата
#   upgrade                   — улучшить тариф до VPN с обходом
# Если количество не передано, бот открывает соответствующий экран/ввод.
async def _open_paysection_from_message(message: types.Message, state: FSMContext,
                                        section: str) -> bool:
    """Открывает раздел оплаты по ключу из диплинка. Возвращает True, если
    раздел распознан и обработан, иначе False."""
    u_id    = message.from_user.id
    section = (section or "").lower().strip()
    parts   = section.split("_")
    head    = parts[0] if parts else ""

    def _tail_int() -> int | None:
        """Число в хвосте диплинка (например 5 из dev_5) либо None."""
        if len(parts) >= 2 and parts[-1].isdigit():
            return int(parts[-1])
        return None

    if head == "buy" or section == "vpn":
        # buy_<план>_<месяцев>: сразу открываем оплату выбранного тарифа.
        plan_key = None
        months   = None
        if len(parts) >= 3 and parts[-1].isdigit():
            plan_key = "_".join(parts[1:-1])
            months   = int(parts[-1])
        elif len(parts) >= 2 and not parts[-1].isdigit():
            plan_key = "_".join(parts[1:])
        if plan_key in PLANS and months and months > 0:
            plan  = PLANS[plan_key]
            price = calc_plan_price(plan_key, months)
            await _create_payment_page_from_message(
                message, kind="plan", item_name=f"{plan['name']} · {months} мес.",
                price=price, days=months * 30, hwid=1, squad=plan["squad"],
                whitelist_gb=plan["whitelist_gb"], plan_key=plan_key,
            )
            return True
        # Без конкретного тарифа/срока — показываем экран выбора тарифа.
        text, kb = _buy_open_content()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return True

    if head == "extend":
        # extend_<месяцев>: продлеваем текущий тариф пользователя.
        months = _tail_int()
        async with pool.acquire() as conn:
            plan_key = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", u_id)
        if plan_key not in PLANS:
            await message.answer("Сначала оформите подписку.")
            return True
        if not months or months <= 0:
            text, kb = _buy_open_content()
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
            return True
        plan  = PLANS[plan_key]
        price = calc_plan_price(plan_key, months)
        await _create_payment_page_from_message(
            message, kind="plan", item_name=f"Продление {plan['name']} · {months} мес.",
            price=price, days=months * 30, hwid=1, squad=plan["squad"],
            whitelist_gb=plan["whitelist_gb"], plan_key=plan_key,
        )
        return True

    if head == "trial":
        async with pool.acquire() as conn:
            used = await conn.fetchval("SELECT trial_used FROM users WHERE user_id=$1", u_id)
        if used:
            await message.answer("Пробная подписка уже использована.")
            return True
        await _create_payment_page_from_message(
            message, kind="trial", item_name=TRIAL["name"], price=TRIAL["price"],
            days=TRIAL["days"], hwid=TRIAL["hwid"], squad=TRIAL["squad"],
            whitelist_gb=TRIAL["whitelist_gb"], is_trial=True,
        )
        return True

    if head in ("dev", "devices"):
        # dev_<кол-во>: если количество передано — сразу создаём оплату,
        # иначе спрашиваем количество (старый сценарий).
        qty = _tail_int()
        async with pool.acquire() as conn:
            plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", u_id)
        if plan not in PLANS:
            await message.answer("Сначала оформите подписку.")
            return True
        device_price = PLANS[plan]["device_price"]
        if qty and qty > 0:
            price = device_price * qty
            word = "устройство" if qty == 1 else (
                "устройства" if 2 <= qty % 10 <= 4 and not (11 <= qty % 100 <= 14) else "устройств")
            await _create_payment_page_from_message(
                message, kind="device", item_name=f"+{qty} {word} ({PLANS[plan]['name']})",
                price=price, days=0, qty=qty,
            )
            return True
        await state.set_state(DeviceTopupState.waiting_count)
        await state.update_data(plan=plan)
        await message.answer(
            f"{EMOJI_DEV_TOPUP} Добавить устройства\n\n"
            f"Цена одного устройства на   ашем тарифе: {device_price} руб.\n\n"
            f"Введите, сколько устройств хотите докупить:",
            parse_mode="HTML", reply_markup=cancel_kb(),
        )
        return True

    if head == "upgrade" or section == "plan_upgrade":
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT plan, extra_devices FROM users WHERE user_id=$1", u_id)
        if not row or row["plan"] != "vpn":
            await message.answer("Апгрейд доступен только с тарифа VPN.")
            return True
        remna = await remna_get_user(u_id)
        if not remna:
            await message.answer("Подписка не найдена.")
            return True
        price = calc_upgrade_price(row["extra_devices"] or 0)
        await _create_payment_page_from_message(
            message, kind="upgrade",
            item_name="Улучшение тарифа до VPN с обходом белых списков",
            price=price, days=0, display_prefix=EMOJI_UPGRADE,
        )
        return True

    if head in ("wl", "whitelist"):
        # wl_<ГБ>: если объём передан — сразу оплата, иначе спрашиваем ГБ.
        gb = _tail_int()
        async with pool.acquire() as conn:
            plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", u_id)
        if plan != "vpn_bypass":
            await message.answer("Докупка доступна только на тарифе VPN с обходом.")
            return True
        if gb and gb > 0:
            price = gb * WHITELIST_PRICE_PER_GB
            await _create_payment_page_from_message(
                message, kind="wl_topup", item_name=f"+{gb} ГБ на белых списках",
                price=price, days=0, whitelist_gb=gb,
            )
            return True
        await state.set_state(WhitelistTopupState.waiting_gb)
        await message.answer(
            f"{EMOJI_GB_TOPUP} Докупить трафик (белые списки)\n\n"
            f"Цена: {WHITELIST_PRICE_PER_GB} руб. за 1 ГБ.\n\n"
            f"Введите, сколько ГБ хотите докупить:",
            parse_mode="HTML", reply_markup=cancel_kb(),
        )
        return True

    return False

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    u_id = message.from_user.id
    r_id = None
    section = None
    if command.args:
        arg = command.args.strip()
        if arg.isdigit():
            candidate = int(arg)
            if candidate != u_id:
                r_id = candidate
        else:
            # Нечисловой start-параметр — диплинк из мини-приложения
            # (личного кабинета) для переброса в раздел оплаты.
            # Поддерживаем как "cab_buy", так и просто "buy".
            section = arg[4:] if arg.startswith("cab_") else arg

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
            f"{EMOJI_TRUBAVPN} {hbold('TrubaVPN')}\n\nПодпишитесь на канал, чтобы пользоваться ботом.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    # Переброс из личного кабинета в конкретный раздел оплаты.
    if section:
        await state.clear()
        if await _open_paysection_from_message(message, state, section):
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

def get_cabinet_webapp_url() -> str | None:
    """URL мини-приложения личного кабинета для кнопки-WebApp.
    Вход в аккаунт — автоматический по Telegram initData, без кодов."""
    return MINIAPP_URL or None

def _cabinet_button_row() -> list[InlineKeyboardButton]:
    """Строка клавиатуры с кнопкой «Личный кабинет».
    Если URL мини-приложения задан — кнопка открывает его как Telegram Mini App
    (WebApp) с автоматическим входом (без кодов). Иначе — заглушка о том, что
    кабинет временно недоступен."""
    url = get_cabinet_webapp_url()
    if url:
        return [btn("Личный кабинет", emoji_id="5282843764451195532",
                    web_app=WebAppInfo(url=url))]
    return [btn("Личный кабинет", emoji_id="5282843764451195532",
                callback_data="cabinet_unavailable")]

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

    lines = [f"{EMOJI_PROFILE} {hbold('Профиль')}", ""]
    subscription_active = bool(
        remna and parse_dt(remna.get("expireAt")) > now and remna.get("status") != "DISABLED"
    )
    if subscription_active:
        expire   = parse_dt(remna.get("expireAt"))
        date_str = fmt_dt(expire, "%d.%m.%Y")
        hwid     = remna.get("hwidDeviceLimit", 1)
        sub_url  = format_sub_url(remna)
        current_squads = _squad_uuids(remna.get("activeInternalSquads"))
        has_whitelist  = SQUAD_UUID_WHITELIST in current_squads

        # Тариф определяется ЖИВОЙ проверкой сквадов на каждый показ профиля
        # (не по значению в БД) — так текст и кнопки всегда соответствуют
        # реальному состоянию в Remnawave, даже если plan в БД устарел/не
        # задан. Триал — единственное исключение (тот же сквад белых списков,
        # что и у vpn_bypass, поэтому его нельзя отличить по скваду и он
        # хранится отдельным явным флагом).
        if plan == "trial":
            plan_name  = "Пробная подписка"
            live_plan  = "trial"
        else:
            live_plan = "vpn_bypass" if has_whitelist else "vpn"
            plan_name = PLANS[live_plan]["name"]

        if plan != live_plan:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET plan=$1, extra_devices=$2 WHERE user_id=$3",
                    live_plan, max(0, hwid - 1), user_id,
                )
        plan = live_plan

        # Остаток трафика на белых списках — только если реально отслеживается
        # (есть строка в whitelist_limits с лимит  м > 0). Своя отдельная строка,
        # не смешивается с тарифом/устройствами.
        gb_line = ""
        if has_whitelist:
            async with pool.acquire() as conn:
                wl_row = await conn.fetchrow(
                    "SELECT gb_limit, period_start FROM whitelist_limits WHERE user_id=$1", user_id
                )
            if wl_row and wl_row["gb_limit"] > 0:
                records   = await fetch_whitelist_daily_records(days_back=40)
                used_gb   = sum_whitelist_bytes_for_user(records, user_id, wl_row["period_start"]) / 1024 ** 3
                remaining = max(0.0, wl_row["gb_limit"] - used_gb)
                gb_line = f"Осталось трафика на белых списках: {remaining:.1f}/{wl_row['gb_limit']} ГБ"

        lines += [
            f"{EMOJI_PLAN_LABEL} Вариант подписки: {plan_name}",
            f"Устройств: {hwid}",
        ]
        if gb_line:
            lines.append(gb_line)
        lines.append(f"{EMOJI_ACTIVE_UNTIL} Активна до: {date_str}")

        if sub_url:
            lines += ["", f"{EMOJI_SUB_LINK} Ссылка на подписку:", hcode(sub_url)]
    else:
        lines += ["Подписка не активна."]

    lines += ["", f"Поддержка: @{SUPPORT_USERNAME}"]
    text = "\n".join(lines)

    rows = []
    cabinet_row    = _cabinet_button_row()
    user_is_admin  = is_admin(user_id)
    admin_site_url = "https://accept-finances-cyber-itself.trycloudflare.com/"

    def _place_cabinet():
        # Кнопка «Личный кабинет», а для админов — сразу под ней ссылка на
        # админ-сайт.
        rows.append(cabinet_row)
        if user_is_admin:
            rows.append([btn("Админ-сайт", emoji_id=BTN_ICON_ADMIN, url=admin_site_url)])

    # Кнопка «Пробная подписка» показывается только если подписка не активна
    # и пробный период ещё не использован.
    show_trial = (not subscription_active or plan not in PLANS) and not trial_used

    # Расположение кнопки «Личный кабинет»:
    #     пробная подписка УЖЕ использована (trial_used) → ЛК стоит выше всех
    #    остальных кнопок;
    #  • пробная подписка ещё НЕ использована → ЛК стоит сразу под кнопкой
    #    «Пробная подписка».
    # «Личный кабинет» стоит выше всех кнопок, КРОМЕ случая, когда показывается
    # кнопка «Пробная подписка» — тогда ЛК ставится сразу под ней (ниже).
    if not show_trial:
        _place_cabinet()

    # Кнопки "Купить VPN"/"Пробная" показываются, пока не куплен РЕАЛЬНЫЙ тариф
    # (vpn / vpn_bypass) И подписка при этом реально активна. Проверка именно
    # subscription_active (а не только plan) защищает от случая, когда старое
    # значение plan осталось в БД после отзыва подписки ("Забрать подписку") —
    # без этого кнопки "Добавить устройства"/"Улучшить тариф" продолжали бы
    # показываться, хотя подписки уже нет.
    if not subscription_active or plan not in PLANS:
        # Если платный тариф уже был выбран, его можно продлить даже после
        # окончания срока — оплата снова активирует ту же подписку.
        if plan in PLANS:
            rows.append([btn("Продлить подписку", emoji_id=BTN_ICON_PAY,
                             style="success", callback_data="renew_open")])
        if not trial_used:
            rows.append([btn("Пробная подписка", emoji_id=BTN_ICON_TRIAL, style="success",
                             callback_data="trial_buy")])
            # ЛК (и ссылка на админ-сайт для админов) — сразу под пробной подпиской
            _place_cabinet()
        rows.append([btn("Купить VPN", emoji_id=BTN_ICON_BUY_VPN,
                         callback_data="buy_open")])
    else:
        rows.append([btn("Продлить подписку", emoji_id=BTN_ICON_PAY,
                         style="success", callback_data="renew_open")])
        rows.append([btn("Добавить устройства", emoji_id=BTN_ICON_DEV_TOPUP,
                         callback_data="dev_add")])
        if plan == "vpn":
            rows.append([btn("Улучшить тариф", emoji_id=BTN_ICON_UPGRADE,
                             callback_data="plan_upgrade")])
        elif plan == "vpn_bypass":
            # Без custom-emoji иконки: на части клиентов этот premium-эмодзи
            # рендерился поверх текста и ломал надпись кнопки («Доку  ить»).
            rows.append([btn("Докупить трафик (белые списки)", emoji_id=BTN_ICON_GB_TOPUP,
                 callback_data="wl_topup")])

    rows.append([btn("Заработать", emoji_id=BTN_ICON_EARN, callback_data="earn_open")])
    rows.append([btn("Промокод", emoji_id=BTN_ICON_PROMO, callback_data="promo_enter")])
    rows.append([btn("О сервисе", emoji_id=BTN_ICON_INFO, callback_data="info_tab")])
    if is_admin(user_id):
        rows.append([btn("Панель", emoji_id=BTN_ICON_ADMIN, callback_data="admin_panel")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb

# ─────────────────────────────────────────────
#  ЛИЧНЫЙ КАБИНЕТ (веб-панель, отдельная от бота)
# ─────────────────────────────────────────────
_CAB_ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits

async def get_cabinet_url(user_id: int) -> str | None:
    """Ссылка на страницу входа в личный кабинет на сайте.
    Вход только по 9-значному коду (без UID). Если SITE_URL не задан —
    возвращает None (кнопка покажет 'времен  о недоступен')."""
    if not SITE_URL:
        return None
    return f"{SITE_URL}/cab"

# Алфавит для кода входа (без похожих символов I, O, 0, 1).
_LOGIN_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOGIN_DIGITS  = "23456789"

def _gen_login_code() -> str:
    """Код вида XXXX-YYYY: в каждой половине ровно 2 буквы и 2 цифры."""
    rng = secrets.SystemRandom()
    def half() -> str:
        chars = [secrets.choice(_LOGIN_LETTERS) for _ in range(2)] + \
                [secrets.choice(_LOGIN_DIGITS) for _ in range(2)]
        rng.shuffle(chars)
        return "".join(chars)
    return f"{half()}-{half()}"

# Кнопка «Личный кабинет» теперь — это WebApp-кнопка (Telegram Mini App),
# которая открывает мини-приложение прямо в Telegram. Вход в аккаунт
# происходит автоматическ   — ми  и-приложение получает подписанные
# Telegram initData с данными пользователя, поэтому никакие коды входа не
# требуются. От  ельный callback-хендлер на открытие больше не нужен;
# остаётся только заглушка на случай, когда URL мини-приложения не задан.
@router.callback_query(F.data == "cabinet_unavailable")
async def cabinet_unavailable_cb(cb: CallbackQuery):
    await cb.answer("Личный кабинет временно недоступен.", show_alert=True)

async def _show_home(cb: CallbackQuery):
    text, kb = await _build_profile_view(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

def cancel_kb() -> InlineKeyboardMarkup:
    """Универсальная кнопка отмены — очищает любое активное FSM-состояние и
    возвращает в профиль. Используется вместо текстовой подсказки /cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_to_profile")]
    ])

@router.callback_query(F.data == "cancel_to_profile")
async def cancel_to_profile_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _show_home(cb)

@router.callback_query(F.data == "back")
async def back_to_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _show_home(cb)

@router.callback_query(F.data == "profile")
async def profile_cb(cb: CallbackQuery):
    await cb.answer()
    await _show_home(cb)

# ─────────   ───────────────────────────────────
#  ОБЩАЯ СТРАНИЦА ОПЛАТЫ (YooKassa)
# ─────────────────────────────────────────────
async def _create_payment_core(user_id: int, *, kind: str, item_name: str,
                                price: int, days: int = 0, hwid: int | None = None,
                                squad: str | list[str] | None = None, whitelist_gb: int = 0,
                                plan_key: str | None = None, is_trial: bool = False,
                                qty: int = 0):
    """
    kind: "trial" | "plan" | "device" | "upgrade" | "wl_topup"
    qty — для kind="device": сколько устройств докупается; для kind="wl_topup"
    смотри whitelist_gb (сколько ГБ докупается) — там оно и так уже есть.
    Возвращает (payment, kb) либо (None, None) при ошибке создани   платежа.
    """
    try:
        payment = Payment.create({
            "amount":       {"value": f"{price}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture":      True,
            # item_name остаётся прежним для Telegram и metadata. Только
            # отображаемое название платежа в YooKassa заменяется на LTE.
            "description":  for_yookassa(f"TrubaVPN — {item_name}"),
            "metadata": {
                "user_id":      str(user_id),
                "kind":         kind,
                "days":         str(days),
                "hwid":         str(hwid) if hwid is not None else "",
                "squad":        ",".join(squad) if isinstance(squad, list) else (squad or ""),
                "whitelist_gb": str(whitelist_gb),
                "plan_key":     plan_key or "",
                "price":        str(price),
                "is_trial":     "1" if is_trial else "0",
                "item_name":    item_name,
                "qty":          str(qty),
            },
        }, str(uuid.uuid4()))
    except Exception as e:
        log.exception("Payment create error: %s", e)
        return None, None

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("Оплатить", emoji_id=BTN_ICON_PAY, style="success", url=payment.confirmation.confirmation_url)],
        [btn("Проверить оплату", emoji_id=BTN_ICON_CHECK_SUB, callback_data=f"paycheck_{payment.id}")],
        [InlineKeyboardButton(text="Назад", callback_data="back")],
    ])
    return payment, kb

async def _create_payment_page(cb: CallbackQuery, *, kind: str, item_name: str,
                                price: int, days: int = 0, hwid: int | None = None,
                                squad: str | list[str] | None = None, whitelist_gb: int = 0,
                                plan_key: str | None = None, is_trial: bool = False,
                                qty: int = 0, display_prefix: str = "", extra_desc: str = ""):
    """display_prefix — необязательный HTML-префикс (например premium-эмодзи)
    ТОЛЬКО для заголовка в Telegram; в описание/метаданные ЮKassa не попадает.
    extra_desc — доп. текст-описание (например состав тарифа), показывается
    сразу на этом же экране, вместе с кнопками оплаты — тоже не уходит в ЮKassa."""
    payment, kb = await _create_payment_core(
        cb.from_user.id, kind=kind, item_name=item_name, price=price, days=days, hwid=hwid,
        squad=squad, whitelist_gb=whitelist_gb, plan_key=plan_key, is_trial=is_trial, qty=qty,
    )
    if not payment:
        await cb.answer("Ошибка создания платежа.", show_alert=True)
        return
    prefix = f"{display_prefix} " if display_prefix else ""
    desc_block = f"\n{extra_desc}\n" if extra_desc else ""
    await cb.message.edit_text(
        f"{prefix}{hbold(item_name)}\n{desc_block}\nК оплате: {price} руб.\n\nПосле оплаты нажмите «Проверить оплату».",
        parse_mode="HTML", reply_markup=kb,
    )

async def _create_payment_page_from_message(message: types.Message, *, kind: str, item_name: str,
                                             price: int, days: int = 0, hwid: int | None = None,
                                             squad: str | list[str] | None = None, whitelist_gb: int = 0,
                                             plan_key: str | None = None, is_trial: bool = False,
                                             qty: int = 0, display_prefix: str = ""):
    """То же самое, что _create_payment_page, но когда вызов идёт из ответа на
    текстовое сообщение (ввод количества), а не из нажатия кнопки."""
    payment, kb = await _create_payment_core(
        message.from_user.id, kind=kind, item_name=item_name, price=price, days=days, hwid=hwid,
        squad=squad, whitelist_gb=whitelist_gb, plan_key=plan_key, is_trial=is_trial, qty=qty,
    )
    if not payment:
        await message.answer("Ошибка создания плат  жа.")
        return
    prefix = f"{display_prefix} " if display_prefix else ""
    await message.answer(
        f"{prefix}{hbold(item_name)}\n\nК оплате: {price} руб.\n\nПосле оплаты нажмите «Проверить оплату».",
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
    squad_raw   = md.get("squad") or None
    squad       = squad_raw.split(",") if squad_raw and "," in squad_raw else squad_raw
    whitelist_gb= int(md.get("whitelist_gb", 0) or 0)
    plan_key    = md.get("plan_key") or None
    price       = float(md.get("price", 0))
    is_trial    = md.get("is_trial", "0") == "1"
    item_name   = md.get("item_name", "Покупка")
    qty         = int(md.get("qty", 0) or 0)

    async with pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT username, referrer_id, has_paid, extra_devices, plan FROM users WHERE user_id=$1", u_id
        )
    uname       = db_row["username"] if db_row else None
    referrer_id = db_row["referrer_id"] if db_row else None
    extra_devices_now = db_row["extra_devices"] if db_row else 0
    current_plan_now  = db_row["plan"] if db_row else None

    # Идемпотентность: если "Проверить оплату" нажали повторно уже ПОСЛЕ
    # успешной обработки этого же платежа, всё что ниже (активация,
    # начисление устройств/ГБ, уведомление админам,   еферальный процент)
    # не должно повториться второй раз. Атомарный INSERT с ON CONFLICT
    # решает и гонку при почти одновременном двойном тапе.
    async with pool.acquire() as conn:
        inserted = await conn.fetchrow(
            "INSERT INTO processed_payments (payment_id, processed_at) VALUES ($1,$2) "
            "ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
            pay_id, int(time.time()),
        )
    already_processed = inserted is None

    result_user = None

    if not already_processed:
        if kind in ("trial", "plan"):
            result_user = await activate_subscription(u_id, days, hwid or 1, squad_uuid=squad, whitelist_gb=whitelist_gb)
            if not result_user:
                await cb.answer("Ошибка активации. Обратитесь в по  держку.", show_alert=True)
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
            add_count = qty if qty > 0 else 1
            new_hwid = remna.get("hwidDeviceLimit", 1) + add_count
            result_user = await remna_update_user(remna["uuid"], {"hwidDeviceLimit": new_hwid})
            if not result_user:
                await cb.answer("Ошибка обновления устройств.", show_alert=True)
                return
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET extra_devices = extra_devices + $1 WHERE user_id=$2", add_count, u_id
                )

        elif kind == "upgrade":
            remna = await remna_get_user(u_id)
            if not remna:
                await cb.answer("Пользователь не найден в панели.", show_alert=True)
                return
            result_user = await remna_update_user(remna["uuid"], {"activeInternalSquads": [SQUAD_UUID_BASIC, SQUAD_UUID_WHITELIST]})
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
            add_gb = whitelist_gb if whitelist_gb > 0 else 1
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT gb_limit FROM whitelist_limits WHERE user_id=$1", u_id)
                if row:
                    await conn.execute(
                        "UPDATE whitelist_limits SET gb_limit = gb_limit + $1, cut_off=FALSE WHERE user_id=$2",
                        add_gb, u_id,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                        "VALUES ($1,$2,$3,FALSE)",
                        u_id, add_gb, int(time.time()),
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
                "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, created_at, payment_id) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                u_id, price, kind, days, is_trial, int(time.time()), pay_id,
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
        extra_desc=TRIAL["desc"],
    )

# ───────────   ─────────────────────────────────
#  КУПИТЬ VPN — выбор тарифа, затем срока
# ─────────────────────────────────────────────
def _buy_open_content() -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана «Купить VPN» (выбор тарифа). Вынесен  
    отдельно, чтобы использовать как из нажатия кнопки, так и при перебросе
    из личного кабинета (диплинк)."""
    vpn    = PLANS["vpn"]
    bypass = PLANS["vpn_bypass"]
    text = (
        f"{EMOJI_PLAN_VPN} {hbold(vpn['name'])}\n{vpn['desc']}\n{vpn['price_month']} руб./мес.\n\n"
        f"{EMOJI_PLAN_BYPASS} {hbold(bypass['name'])}\n{bypass['desc']}\n{bypass['price_month']} руб./мес.\n\n"
        f"Дополнительные устройства и трафик для обхода белых списков "
        f"докупаются в главном меню после покупки тарифа."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(vpn["name"], emoji_id=BTN_ICON_PLAN_VPN, callback_data="buyplan_vpn")],
        [btn(bypass["name"], emoji_id=BTN_ICON_PLAN_BYPASS, callback_data="buyplan_vpn_bypass")],
        [InlineKeyboardButton(text="Назад", callback_data="back")],
    ])
    return text, kb

@router.callback_query(F.data == "buy_open")
async def buy_open_cb(cb: CallbackQuery):
    await cb.answer()
    text, kb = _buy_open_content()
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "renew_open")
async def renew_open_cb(cb: CallbackQuery):
    """Открывает выбор срока продления прямо из главного экрана /start."""
    await cb.answer()
    async with pool.acquire() as conn:
        plan_key = await conn.fetchval(
            "SELECT plan FROM users WHERE user_id=$1", cb.from_user.id
        )
    if plan_key not in PLANS:
        await cb.answer(
            "Текущий тариф не найден. Сначала выберите тариф.",
            show_alert=True,
        )
        return
    plan = PLANS[plan_key]
    await cb.message.edit_text(
        f"{hbold('Продлить подписку')}\n\n"
        f"Тариф: {plan['name']}\n"
        f"Выберите срок продления:",
        parse_mode="HTML",
        reply_markup=_renew_kb(plan_key, include_back=True),
    )

@router.callback_query(F.data.startswith("buyplan_"))
async def buyplan_cb(cb: CallbackQuery):
    await cb.answer()
    plan_key = cb.data.removeprefix("buyplan_")
    if plan_key not in PLANS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    plan = PLANS[plan_key]
    plan_emoji = EMOJI_PLAN_BYPASS if plan_key == "vpn_bypass" else EMOJI_PLAN_VPN
    rows = []
    for months in MONTH_CHOICES:
        price = calc_plan_price(plan_key, months)
        label = f"{months} мес. — {price} руб." if months > 1 else f"{months} мес. — {price} руб."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buymonths_{plan_key}_{months}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="buy_open")])
    await cb.message.edit_text(
        f"{plan_emoji} {hbold(plan['name'])}\n{plan['desc']}\n\n"
        f"Дополнительные устройства и трафик для обхода белых списков "
        f"докупаются в главном меню после покупки.\n\n"
        f"{EMOJI_CHOOSE_TERM} Выберите срок:",
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
async def dev_add_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    async with pool.acquire() as conn:
        plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", cb.from_user.id)
    if plan not in PLANS:
        await cb.answer("Сначала оформите подписку.", show_alert=True)
        return
    device_price = PLANS[plan]["device_price"]
    await state.set_state(DeviceTopupState.waiting_count)
    await state.update_data(plan=plan)
    await cb.message.edit_text(
        f"{EMOJI_DEV_TOPUP} Добавить устройства\n\n"
        f"Цена одного устройства на вашем тарифе: {device_price} руб.\n\n"
        f"Введите, сколько устройств хотите докупить:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )

@router.message(Command("cancel"), DeviceTopupState.waiting_count)
async def dev_add_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(DeviceTopupState.waiting_count)
async def dev_add_count_handler(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое положительное число.")
        return
    count = int(message.text.strip())
    if count <= 0:
        await message.answer("Число должно быть больше 0.")
        return
    data = await state.get_data()
    plan = data.get("plan")
    await state.clear()
    if plan not in PLANS:
        await message.answer("Тариф не найден, попробуйте снова из профиля.")
        return
    device_price = PLANS[plan]["device_price"]
    price = device_price * count
    word = "устройство" if count == 1 else ("устройства" if 2 <= count % 10 <= 4 and not (11 <= count % 100 <= 14) else "устройств")
    await _create_payment_page_from_message(
        message, kind="device", item_name=f"+{count} {word} ({PLANS[plan]['name']})",
        price=price, days=0, qty=count,
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
    price = calc_upgrade_price(row["extra_devices"] or 0)
    await _create_payment_page(
        cb, kind="upgrade", item_name="Улучшение тарифа до VPN с обходом белых списков",
        price=price, days=0, display_prefix=EMOJI_UPGRADE,
    )

# ─────────────────────────────────────────────
#  ДОКУПИТЬ ТРАФИК НА БЕЛЫХ СПИСКАХ
# ─────────────────────────────   ───────────────
@router.callback_query(F.data == "wl_topup")
async def wl_topup_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    async with pool.acquire() as conn:
        plan = await conn.fetchval("SELECT plan FROM users WHERE user_id=$1", cb.from_user.id)
    if plan != "vpn_bypass":
        await cb.answer("Докупка доступна только на тарифе VPN с обходом.", show_alert=True)
        return
    await state.set_state(WhitelistTopupState.waiting_gb)
    await cb.message.edit_text(
        f"Докупить трафик (белые списки)\n\n"
        f"Цена: {WHITELIST_PRICE_PER_GB} руб. за 1 ГБ.\n\n"
        f"Введите, сколько ГБ хотите докупить:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )

@router.message(Command("cancel"), WhitelistTopupState.waiting_gb)
async def wl_topup_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

@router.message(WhitelistTopupState.waiting_gb)
async def wl_topup_gb_handler(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое положительное число.")
        return
    gb = int(message.text.strip())
    if gb <= 0:
        await message.answer("Число должно быть больше 0.")
        return
    await state.clear()
    price = gb * WHITELIST_PRICE_PER_GB
    await _create_payment_page_from_message(
        message, kind="wl_topup", item_name=f"+{gb} ГБ на белых списках",
        price=price, days=0, whitelist_gb=gb,
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
        f"{EMOJI_EARN} {hbold('Заработать')}\n\n"
        f"{EMOJI_INVITE} Приглашайте друзей — получайте {REFERRAL_PERCENT}% с их оплат.\n\n"
        f"Ваша ссылка:\n{hcode(link)}\n\n"
        f"Приглашено: {ref_count}\n"
        f"Баланс: {balance:.2f} руб.\n"
        f"Вывод   оступен от {REFERRAL_MIN_WITHDRAW} руб."
    )
    rows = []
    if balance >= REFERRAL_MIN_WITHDRAW:
        text += f"\n\nПорог вывода достигнут!"
        withdraw_text = f"Хочу вывес  и реферальный баланс ({balance:.2f} руб.)"
        withdraw_url  = f"{SUPPORT_URL}?text={withdraw_text.replace(' ', '%20')}"
        rows.append([InlineKeyboardButton(text="Написать для вывода", url=withdraw_url)])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

# ─────────────────────────────────────────────
#  ПРОМОКОД (логика не меняется — просто ссылается на новые планы)
# ─────────────────────────────────────────────
async def _claim_promo_once(user_id: int, code: str, selected_plan: str | None = None):
    """Атомарно резервирует одно использование кода за пользователем."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            promo = await conn.fetchrow(
                "SELECT days, uses, promo_type, tariff_key, min_account_age_days "
                "FROM promos WHERE code=$1 AND uses>0 FOR UPDATE",
                code,
            )
            if not promo:
                return None, "ended"
            user = await conn.fetchrow(
                "SELECT plan, created_at FROM users WHERE user_id=$1", user_id
            )
            min_age = int(promo["min_account_age_days"] or 0)
            registered_at = int(user["created_at"] or 0) if user else 0
            age_seconds = max(0, int(time.time()) - registered_at) if registered_at else 0
            if min_age > 0 and (not registered_at or age_seconds < min_age * 86400):
                left = max(1, (min_age * 86400 - age_seconds + 86399) // 86400)
                return None, f"age:{min_age}:{left}"
            current_plan = user["plan"] if user else None
            promo_type = promo["promo_type"] or "days"
            target_plan = promo["tariff_key"] if promo_type == "free_tariff" else (
                selected_plan if promo_type == "free_choice" else None
            )
            if target_plan and current_plan in PLANS and current_plan != target_plan:
                return None, f"tariff:{current_plan}:{target_plan}"
            # История payments — второй независимый барьер. Это не позволяет
            # повторить старую активацию даже при миграции со старой версии.
            used_before = await conn.fetchval(
                "SELECT 1 FROM payments WHERE user_id=$1 AND source='promo' "
                "AND UPPER(TRIM(COALESCE(note,'')))=$2 LIMIT 1",
                user_id, code,
            )
            if used_before:
                return None, "already"
            inserted = await conn.fetchval(
                "INSERT INTO promo_redemptions (user_id, code, redeemed_at, source) "
                "VALUES ($1,$2,$3,'bot') ON CONFLICT (user_id, code) DO NOTHING RETURNING code",
                user_id, code, int(time.time()),
            )
            if not inserted:
                return None, "already"
            row = await conn.fetchrow(
                "UPDATE promos SET uses=uses-1 WHERE code=$1 AND uses>0 "
                "RETURNING days, uses, promo_type, tariff_key, min_account_age_days",
                code,
            )
            if not row:
                await conn.execute(
                    "DELETE FROM promo_redemptions WHERE user_id=$1 AND code=$2", user_id, code
                )
                return None, "ended"
            return row, None


async def _release_promo_claim(user_id: int, code: str):
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchval(
                "DELETE FROM promo_redemptions WHERE user_id=$1 AND code=$2 RETURNING code",
                user_id, code,
            )
            if deleted:
                await conn.execute("UPDATE promos SET uses=uses+1 WHERE code=$1", code)


async def _finish_promo_claim(code: str):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM promos WHERE code=$1 AND uses<=0", code)


async def _promo_claim_error(target, error: str):
    if error == "already":
        text = "Вы уже активировали этот промокод ранее."
    elif error.startswith("age:"):
        # Не раскрываем антиабуз-ограничение и не создаём впечатление, что
        # кнопка не сработала: пользователь получает простую ошибку.
        text = "Ошибка активации промокода."
    elif error.startswith("tariff:"):
        _, current, target_plan = error.split(":")
        current_name = PLANS.get(current, {}).get("name", current)
        target_name = PLANS.get(target_plan, {}).get("name", target_plan)
        text = f"Промокод предназначен для тарифа «{target_name}», а у вас «{current_name}». Смена тарифа запрещена."
    else:
        text = "Промокод уже закончился."
    await target.answer(text)


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
    code = (message.text or "").upper().strip()
    if not code:
        await message.answer(
            "Отправьте промокод текстом:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="promo_cancel")],
            ]),
        )
        return
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

    # Скидочный промокод применяется на шаге оплаты, а не здесь. Иначе он
    # ошибочно «активировал» бы подписку на 0 дней и сгорал впустую.
    if promo_type == "discount":
        await state.clear()
        await message.answer(
            "Это промокод на скидку. Введите его на шаге оплаты при покупке "
            "тарифа — здесь он не активируется."
        )
        text, kb = await _build_profile_view(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if promo_type == "free_tariff" and tariff_key and tariff_key in PLANS:
        await state.clear()
        claimed, claim_error = await _claim_promo_once(message.from_user.id, code)
        if claim_error:
            await _promo_claim_error(message, claim_error)
            return
        days = int(claimed["days"] or 0)
        plan = PLANS[tariff_key]
        user = await activate_subscription(message.from_user.id, days, 1,
                                            squad_uuid=plan["squad"], whitelist_gb=plan["whitelist_gb"])
        if not user:
            await _release_promo_claim(message.from_user.id, code)
            await message.answer("Не удалось применить промокод. Попробуйте позже.")
            return
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET plan=$1, extra_devices=0 WHERE user_id=$2",
                               tariff_key, message.from_user.id)
            # Сначала фиксируем операцию: DB-триггер создаст активность, а
            # веб-панель также умеет показать её напрямую из payments.
            await conn.execute(
                "INSERT INTO payments "
                "(user_id, amount, tariff_key, days, is_trial, source, note, created_at) "
                "VALUES ($1,0,$2,$3,FALSE,'promo',$4,$5)",
                message.from_user.id, tariff_key, days, code, int(time.time()),
            )
        await _finish_promo_claim(code)
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

    claimed, claim_error = await _claim_promo_once(message.from_user.id, code)
    if claim_error:
        await _promo_claim_error(message, claim_error)
        return
    days = int(claimed["days"] or 0)
    user = await activate_subscription(message.from_user.id, days)
    if not user:
        await _release_promo_claim(message.from_user.id, code)
        await message.answer("Не удалось применить промокод. Попробуйте позже.")
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments "
            "(user_id, amount, tariff_key, days, is_trial, source, note, created_at) "
            "VALUES ($1,0,'promo',$2,FALSE,'promo',$3,$4)",
            message.from_user.id, days, code, int(time.time()),
        )
    await _finish_promo_claim(code)
    await state.clear()
    text, kb = await _build_profile_view(message.from_user.id)
    await message.answer(f"Промокод {code} активирован — добавлено {days} дн.\n\n{text}",
                         parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("pfree_"))
async def handle_free_plan_choice(cb: CallbackQuery, state: FSMContext):
    # plan_key может содержать «_» (например vpn_bypass), поэтому разбираем по
    # известным ключам тарифов, а не простым split (иначе выбор тарифа
    # «VPN с обходом» через промокод ломался).
    raw = cb.data[len("pfree_"):]
    plan_key = next((k for k in sorted(PLANS, key=len, reverse=True)
                     if raw == k or raw.startswith(k + "_")), None)
    promo_code = raw[len(plan_key) + 1:] if plan_key else ""
    data = await state.get_data()
    days = data.get("promo_days", 30)
    await state.clear()
    if plan_key not in PLANS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    claimed, claim_error = await _claim_promo_once(cb.from_user.id, promo_code, selected_plan=plan_key)
    if claim_error:
        if claim_error == "already":
            error_text = "Вы уже активировали этот промокод ранее."
        elif claim_error.startswith("age:"):
            error_text = "Ошибка активации промокода."
        elif claim_error.startswith("tariff:"):
            _, current, target = claim_error.split(":")
            error_text = (
                f"Промокод для тарифа «{PLANS.get(target, {}).get('name', target)}», "
                f"а у вас «{PLANS.get(current, {}).get('name', current)}». Смена тарифа запрещена."
            )
        else:
            error_text = "Промокод уже закончился."
        await cb.answer(error_text, show_alert=True)
        return
    days = int(claimed["days"] or 0)
    plan = PLANS[plan_key]
    activated = await activate_subscription(cb.from_user.id, days, 1,
                                            squad_uuid=plan["squad"], whitelist_gb=plan["whitelist_gb"])
    if not activated:
        await _release_promo_claim(cb.from_user.id, promo_code)
        await cb.answer("Не удалось применить промокод. Попробуйте позже.", show_alert=True)
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET plan=$1, extra_devices=0 WHERE user_id=$2",
                           plan_key, cb.from_user.id)
        await conn.execute(
            "INSERT INTO payments "
            "(user_id, amount, tariff_key, days, is_trial, source, note, created_at) "
            "VALUES ($1,0,$2,$3,FALSE,'promo',$4,$5)",
            cb.from_user.id, plan_key, days, promo_code, int(time.time()),
        )
    await _finish_promo_claim(promo_code)
    await cb.answer("Промокод активирован.")
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
        f"{EMOJI_INFO} О сервисе",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("Канал с инструкция  и", emoji_id=BTN_ICON_CHANNEL_SUB, url=CHANNEL_LINK)],
            [btn("Пользовательское соглашение", emoji_id=BTN_ICON_TOS,
                url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
            [btn("Политика конфиденциальности", emoji_id=BTN_ICON_PRIVACY,
                url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
            [btn("Тех.Поддержка", emoji_id=BTN_ICON_SUPPORT, url=SUPPORT_URL)],
            [InlineKeyboardButton(text="Назад", callback_data="back")],
        ]),
    )

# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ
# ─────────────────────────────────────────────
def admin_panel_kb(is_main: bool = False):
    rows = [
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
    ]
    if is_main:
        rows.append([InlineKeyboardButton(text="Админы", callback_data="admin_admins")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text(
        f"{EMOJI_ADMIN} Админ-панель", parse_mode="HTML",
        reply_markup=admin_panel_kb(is_main_admin(cb.from_user.id)),
    )

# ─────────────────────────────────────────────
#  УПРАВЛЕНИЕ АДМИНАМИ (только для главных админов из ADMIN_ID_1/ADMIN_ID_2)
# ─────────────────────────────────────────────
class AdminManageState(StatesGroup):
    waiting_username = State()

async def _render_admins_list(target_send):
    lines = ["Админы", ""]
    lines.append("Главные (из переменных окружения, нельзя удалить здесь):")
    for aid in ADMIN_IDS:
        lines.append(f"  ID:{aid}")
    lines.append("")
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username FROM extra_admins ORDER BY added_at DESC")
    kb_rows = []
    if rows:
        lines.append("Добавленные:")
        for r in rows:
            uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
            lines.append(f"  {uname}")
            kb_rows.append([InlineKeyboardButton(
                text=f"Удалить {uname}", callback_data=f"admin_del_{r['user_id']}"
            )])
    else:
        lines.append("Добавленных админов пока нет.")
    kb_rows.append([InlineKeyboardButton(text="Добавить админа", callback_data="admin_add_start")])
    kb_rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_panel")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(target_send, types.Message):
        await target_send.answer(text, reply_markup=kb)
    else:
        await target_send.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == "admin_admins")
async def admin_admins_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_main_admin(cb.from_user.id):
        await cb.answer("Доступно только главным админам.", show_alert=True)
        return
    await _render_admins_list(cb)

@router.callback_query(F.data == "admin_add_start")
async def admin_add_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_main_admin(cb.from_user.id):
        await cb.answer("Дост  пно только главным админам.", show_alert=True)
        return
    await state.set_state(AdminManageState.waiting_username)
    await cb.message.answer(
        "Введите username (без @) пользователя, которого нужно сделать админом.\n"
        "Пользователь должен хотя бы раз запускать бота (нажать /start).",
        reply_markup=cancel_kb(),
    )

@router.message(AdminManageState.waiting_username)
async def admin_add_username_handler(message: types.Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await state.clear()
        return
    username = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", username)
    if not row:
        await message.answer(
            f"@{username} не найден в базе — он должен хотя бы раз написать /start боту.",
            reply_markup=cancel_kb(),
        )
        return
    target_id = row["user_id"]
    await state.clear()
    if is_admin(target_id):
        await message.answer(f"@{username} уже является админом.")
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO extra_admins (user_id, username, added_by, added_at) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_id) DO UPDATE SET username=$2",
            target_id, username, message.from_user.id, int(time.time()),
        )
    EXTRA_ADMIN_IDS.add(target_id)
    await message.answer(f"@{username} добавлен в админы.")
    try:
        await bot.send_message(target_id, "Вы назначены администратором бота. Кнопка «Панель» появится в профиле.")
    except Exception:
        pass
    await _render_admins_list(message)

@router.callback_query(F.data.startswith("admin_del_"))
async def admin_del_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_main_admin(cb.from_user.id):
        await cb.answer("Доступно только   лавным админам.", show_alert=True)
        return
    target_id = int(cb.data.removeprefix("admin_del_"))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM extra_admins WHERE user_id=$1", target_id)
    EXTRA_ADMIN_IDS.discard(target_id)
    await cb.answer("Админ удалён.", show_alert=True)
    try:
        await bot.send_message(target_id, "С вас сняты права администратора бота.")
    except Exception:
        pass
    await _render_admins_list(cb)

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
    if not is_admin(cb.from_user.id):
        return
    await _render_subs_page(cb, 0)

@router.callback_query(F.data.startswith("subs_page_"))
async def subs_page_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await _render_subs_page(cb, int(cb.data.removeprefix("subs_page_")))

@router.callback_query(F.data == "subs_noop")
async def subs_noop(cb: CallbackQuery):
    await cb.answer()

@router.callback_query(F.data.startswith("sub_view_"))
async def sub_view_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(cb.from_user.id):
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
#  ДНИ: добавить / убрать / ус  ановить дату
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_adddays_"))
async def ca_adddays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    user_id = int(cb.data.removeprefix("ca_adddays_"))
    await state.set_state(CheckActionState.waiting_days_add)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Добавить дни для ID:{user_id}\n\nВведите количество дней:", reply_markup=cancel_kb())

@router.message(CheckActionState.waiting_days_add)
async def ca_adddays_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
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
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp, "status": "ACTIVE"})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(f"ID:{user_id} — добавлено +{days} дн. Новая дата: {fmt_dt(new_ts)}")
    await _render_check(message, user_id)

@router.callback_query(F.data.startswith("ca_subdays_"))
async def ca_subdays_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    user_id = int(cb.data.removeprefix("ca_subdays_"))
    await state.set_state(CheckActionState.waiting_days_sub)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Убрать дни у ID:{user_id}\n\nВведите количество дней:", reply_markup=cancel_kb())

@router.message(CheckActionState.waiting_days_sub)
async def ca_subdays_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
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
    result  = await remna_update_user(remna["uuid"], {"expireAt": new_exp, "status": "ACTIVE"})
    if not result:
        await message.answer("Ошибка обновления.")
        return
    new_ts = parse_dt(result.get("expireAt"))
    await message.answer(f"ID:{user_id} — убрано -{days} дн. Новая дата: {fmt_dt(new_ts)}")
    await _render_check(message, user_id)

@router.callback_query(F.data.startswith("ca_setdate_"))
async def ca_setdate_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    user_id = int(cb.data.removeprefix("ca_setdate_"))
    await state.set_state(CheckActionState.waiting_days_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(
        f"Установить дату истечения для ID:{user_id}\n\n"
        f"Введите дату в формате ДД.ММ.ГГГГ (по МСК, время 23:59):",
        reply_markup=cancel_kb(),
    )

@router.message(CheckActionState.waiting_days_set)
async def ca_setdate_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
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
    result = await remna_update_user(remna["uuid"], {"expireAt": dt_utc_str, "status": "ACTIVE"})
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
    if not is_admin(cb.from_user.id):
        return
    user_id = int(cb.data.removeprefix("ca_sethwid_"))
    await state.set_state(CheckActionState.waiting_hwid_set)
    await state.update_data(ca_uid=user_id)
    await cb.message.answer(f"Установить лимит устройств для ID:{user_id}\n\nВведите число (0 = без лимита):", reply_markup=cancel_kb())

@router.message(CheckActionState.waiting_hwid_set)
async def ca_sethwid_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
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
        await message.answer("Пользователь н   найден в Remnawave.")
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

# ──────   ──────────────────────────────────────
#  СПИСОК УСТРОЙСТВ (HWID inspector)
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("ca_devices_"))
async def ca_devices_show(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
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
            f"Введите лимит в ГБ (0 = без лимита, без отслеживания):",
            reply_markup=cancel_kb(),
        )
        await state.set_state(CheckActionState.waiting_whitelist_gb)
        await state.update_data(ca_uid=user_id)

@router.message(CheckActionState.waiting_whitelist_gb)
async def ca_whitelist_gb_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
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
# ──────────────   ───   ──────────────────────────
@router.callback_query(F.data.startswith("quicktake_"))
async def quick_take(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    user_id = int(cb.data.removeprefix("quicktake_"))
    remna = await remna_get_user(user_id)
    if remna:
        await remna_disable_user(remna["uuid"])
    # Сбрасываем классификацию тарифа — иначе в профиле остаются кнопки
    # "Добавить устройства"/"Улучшить тариф" от старого plan в БД, хотя
    # подписка уже отключена.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET plan=NULL, extra_devices=0 WHERE user_id=$1", user_id
        )
        await conn.execute("DELETE FROM whitelist_limits WHERE user_id=$1", user_id)
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
    age_arg      = next((p for p in parts if p.startswith("age:")), None)
    min_age_days = 0
    if age_arg:
        age_value = age_arg.removeprefix("age:")
        if not age_value.isdigit():
            await message.answer("Минимальный возраст аккаунта задаётся как age:ДНИ, например age:1")
            return
        min_age_days = max(0, int(age_value))
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
            "INSERT INTO promos (code,days,uses,promo_type,tariff_key,discount_percent,min_account_age_days) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5,"
            "discount_percent=$6,min_account_age_days=$7",
            code, days, uses, promo_type, tariff_key, discount_percent, min_age_days,
        )
    age_text = f"от {min_age_days} дн." if min_age_days else "без ограничения"
    await message.answer(f"Промокод {code} создан. Тип: {promo_type}. Дней: {days}. Исп.: {uses}. Возраст: {age_text}")

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n"
            "/add_promo КОД ДНИ [исп.]\n"
            "/add_promo КОД ДНИ [исп.] free:vpn|vpn_bypass|choice\n"
            "/add_promo КОД 0 [исп.] discount:ПРОЦЕНТ\n"
            "Необязательно: age:ДНИ (например age:1)"
        )
        return
    await _save_promo(message, parts)

@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminPromoState.waiting_input)
    await message.answer(
        "Генерация промокода\n\n"
        "КОД ДНИ [исп.] — добавляет дни\n"
        "КОД ДНИ [исп.] free:vpn|vpn_bypass|choice — бесплатный тариф\n"
        "КОД 0 [исп.] discount:ПРОЦЕНТ — скидка %\n\n"
        "Необязательно добавьте age:ДНИ — минимальный возраст аккаунта.\n\n"
        "Число вместо кода → авто генерация",
        reply_markup=cancel_kb(),
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
    if not is_admin(message.from_user.id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code,days,uses,promo_type,tariff_key,discount_percent,min_account_age_days FROM promos ORDER BY promo_type,days DESC")
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
        age = f" · возраст от {r['min_account_age_days']} дн." if r["min_account_age_days"] else ""
        lines.append(f"{r['code']} — {days_str}, {r['uses']} исп.{extra}{age}")
    await message.answer("\n".join(lines))

@router.callback_query(F.data == "admin_promos")
async def admin_promos_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code,days,uses,promo_type,min_account_age_days "
            "FROM promos ORDER BY promo_type,days DESC"
        )
    lines = ["Промокоды", ""]
    if not rows:
        lines.append("Промокодов нет.")
    else:
        for r in rows:
            days_str = f"{r['days']} дн." if r["days"] else "-"
            age = f", возраст от {r['min_account_age_days']} дн." if r["min_account_age_days"] else ""
            lines.append(f"{r['code']} — {days_str}, {r['uses']} исп. ({r['promo_type']}){age}")
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать промокод", callback_data="promogen_start")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

# ─────────────────────────────────────────────
#  ГЕНЕРАЦИЯ ПРОМОКОДА КНОПКАМИ (без текстовых команд)
#
#  Шаги: тип → (если "бесплатный тариф" — ещё и какой план) → дни (текстом,
#  число нельзя выбрать кнопкой) → кол-во использований (пресеты кнопкой или
#  своё число) → код (текстом, либо 0 для автогенерации) → готово.
#  Тип "скидка" не предлагается — в текущей версии бота скидочные промокоды
#  нигде не обрабатываются при активации, создавать их значит создавать
#  не  абочую функциональность.
# ─────────────────────────────────────────────
async def _create_promo(code: str, days: int, uses: int, promo_type: str,
                        tariff_key: str | None = None, min_account_age_days: int = 0):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promos (code,days,uses,promo_type,tariff_key,discount_percent,min_account_age_days) "
            "VALUES ($1,$2,$3,$4,$5,0,$6) "
            "ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5,"
            "discount_percent=0,min_account_age_days=$6",
            code, days, uses, promo_type, tariff_key, min_account_age_days,
        )

@router.callback_query(F.data == "promogen_start")
async def promogen_start_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text(
        "Создание промокода\n\nЧто должен давать промокод?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Дни к текущей подписке", callback_data="promogen_type_days")],
            [InlineKeyboardButton(text="Бесплатный тариф", callback_data="promogen_type_free")],
            [InlineKeyboardButton(text="Назад", callback_data="admin_promos")],
        ]),
    )

@router.callback_query(F.data == "promogen_type_days")
async def promogen_type_days_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await state.update_data(promo_type="days", tariff_key=None)
    await state.set_state(PromoGenState.waiting_days)
    await cb.message.edit_text(
        "Сколько дней добавляет промокод? Введите число:", reply_markup=cancel_kb()
    )

@router.callback_query(F.data == "promogen_type_free")
async def promogen_type_free_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text(
        "Какой тариф выдавать бесплатно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=PLANS["vpn"]["name"], callback_data="promogen_freeplan_vpn")],
            [InlineKeyboardButton(text=PLANS["vpn_bypass"]["name"], callback_data="promogen_freeplan_vpn_bypass")],
            [InlineKeyboardButton(text="Пользователь выбирает сам", callback_data="promogen_freeplan_choice")],
            [InlineKeyboardButton(text="Назад", callback_data="promogen_start")],
        ]),
    )

@router.callback_query(F.data.startswith("promogen_freeplan_"))
async def promogen_freeplan_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    choice = cb.data.removeprefix("promogen_freeplan_")
    if choice == "choice":
        await state.update_data(promo_type="free_choice", tariff_key=None)
    else:
        await state.update_data(promo_type="free_tariff", tariff_key=choice)
    await state.set_state(PromoGenState.waiting_days)
    await cb.message.edit_text(
        "Сколько дней бесплатного доступа? Введите число:", reply_markup=cancel_kb()
    )

def _uses_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1", callback_data="promogen_uses_1"),
         InlineKeyboardButton(text="5", callback_data="promogen_uses_5"),
         InlineKeyboardButton(text="10", callback_data="promogen_uses_10"),
         InlineKeyboardButton(text="50", callback_data="promogen_uses_50")],
        [InlineKeyboardButton(text="Своё число", callback_data="promogen_uses_custom")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_to_profile")],
    ])


def _min_age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без ограничения", callback_data="promogen_age_0")],
        [InlineKeyboardButton(text="1 день", callback_data="promogen_age_1"),
         InlineKeyboardButton(text="3 дня", callback_data="promogen_age_3"),
         InlineKeyboardButton(text="7 дней", callback_data="promogen_age_7")],
        [InlineKeyboardButton(text="Своё число", callback_data="promogen_age_custom")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_to_profile")],
    ])


async def _ask_promo_min_age(target_message):
    await target_message.edit_text(
        "Минимальный возраст аккаунта в боте?\n\n"
        "Пользователь, зарегистрированный позже указанного срока, не сможет активировать код. "
        "Ограничение необязательное.",
        reply_markup=_min_age_kb(),
    )

@router.message(PromoGenState.waiting_days)
async def promogen_days_handler(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое положительное число.", reply_markup=cancel_kb())
        return
    days = int(message.text.strip())
    if days <= 0:
        await message.answer("Число должно быть больше 0.", reply_markup=cancel_kb())
        return
    await state.update_data(days=days)
    await message.answer("Сколько раз можно использовать промокод?", reply_markup=_uses_kb())

@router.callback_query(F.data.startswith("promogen_uses_"))
async def promogen_uses_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    choice = cb.data.removeprefix("promogen_uses_")
    if choice == "custom":
        await state.set_state(PromoGenState.waiting_uses_custom)
        await cb.message.edit_text("Введите число использований:", reply_markup=cancel_kb())
        return
    await state.update_data(uses=int(choice))
    await _ask_promo_min_age(cb.message)

@router.message(PromoGenState.waiting_uses_custom)
async def promogen_uses_custom_handler(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите целое положительное число.", reply_markup=cancel_kb())
        return
    uses = int(message.text.strip())
    if uses <= 0:
        await message.answer("Число должно быть больше 0.", reply_markup=cancel_kb())
        return
    await state.update_data(uses=uses)
    await message.answer(
        "Минимальный возраст аккаунта в боте? Ограничение необязательное.",
        reply_markup=_min_age_kb(),
    )


async def _ask_promo_code(target_message, state: FSMContext, min_age_days: int):
    await state.update_data(min_account_age_days=max(0, min_age_days))
    await state.set_state(PromoGenState.waiting_code)
    await target_message.edit_text(
        "Введите код промокода (латиница/цифры), либо отправьте 0 для автогенерации:",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data.startswith("promogen_age_"))
async def promogen_age_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    choice = cb.data.removeprefix("promogen_age_")
    if choice == "custom":
        await state.set_state(PromoGenState.waiting_min_age_custom)
        await cb.message.edit_text(
            "Введите минимальный возраст аккаунта в днях (0 — без ограничения):",
            reply_markup=cancel_kb(),
        )
        return
    await _ask_promo_code(cb.message, state, int(choice))


@router.message(PromoGenState.waiting_min_age_custom)
async def promogen_age_custom_handler(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите целое число от 0.", reply_markup=cancel_kb())
        return
    age = int(raw)
    await state.update_data(min_account_age_days=age)
    await state.set_state(PromoGenState.waiting_code)
    await message.answer(
        "Введите код промокода (латиница/цифры), либо отправьте 0 для автогенерации:",
        reply_markup=cancel_kb(),
    )

@router.message(PromoGenState.waiting_code)
async def promogen_code_handler(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    code = uuid.uuid4().hex[:8].upper() if raw == "0" else raw.upper()
    data = await state.get_data()
    await state.clear()
    await _create_promo(
        code=code, days=data["days"], uses=data["uses"],
        promo_type=data["promo_type"], tariff_key=data.get("tariff_key"),
        min_account_age_days=int(data.get("min_account_age_days") or 0),
    )
    type_label = {
        "days": "дни к подписке",
        "free_tariff": PLANS.get(data.get("tariff_key"), {}).get("name", "бесплатный тариф"),
        "free_choice": "бесплатный тариф на выбор",
    }.get(data["promo_type"], data["promo_type"])
    await message.answer(
        f"Промокод {hcode(code)} создан.\nТип: {type_label}\nДней: {data['days']} · "
        f"Исп.: {data['uses']} · Мин. возраст: "
        f"{(str(data.get('min_account_age_days')) + ' дн.') if data.get('min_account_age_days') else 'без ограничения'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="К списку промокодов", callback_data="admin_promos")],
        ]),
    )

# ─────────────────────────────────────────────
#  РАССЫЛКА
# ─────────────────────────────────────────────
@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("Рассылка\n\nВв  дите текст.", reply_markup=cancel_kb())

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
    if not is_admin(cb.from_user.id):
        return
    await _do_broadcast(cb, state, subs_only=False)

@router.callback_query(F.data == "bc_confirm_subs")
async def broadcast_confirm_subs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
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
    if not is_admin(message.from_user.id):
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
# ────────────────────────  ────────────────────
@router.callback_query(F.data == "admin_report")
async def admin_report_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminGiveState.waiting_username)
    await cb.message.answer("Выдача подписки\n\nВведите username (без @):", reply_markup=cancel_kb())

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
        await message.answer(f"@{username} не найден. Введите другой username.", reply_markup=cancel_kb())
        return
    await state.update_data(target_id=row["user_id"], target_username=username)
    await state.set_state(AdminGiveState.waiting_days)
    await message.answer(f"@{username}\n\nСколько дней выдать?", reply_markup=cancel_kb())

@router.message(AdminGiveState.waiting_days)
async def admin_give_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().lstrip("-").isdigit():
        await message.answer("Введите целое число дней.", reply_markup=cancel_kb())
        return
    days = int(message.text.strip())
    await state.update_data(days=days)
    await state.set_state(AdminGiveState.waiting_devices)
    await message.answer("Сколько устройств выставить? (0 = не менять текущее значение)", reply_markup=cancel_kb())

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
        "«Не менять» — только продлить дни, сквад и вариант подписки остан  тся как есть.\n"
        "«VPN» / «VPN с обходом» — выставит соответствующий тариф и профиль пользователя "
        "переключится в купленное состояние (появятся кнопки «Добавить устройства» и т.д.).\n"
        "«Пробный доступ» — доступ к белым спискам с лимитом 3 ГБ, но БЕЗ пометки как "
        "купленный тариф (кнопки покупки в профиле останутся).",
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("giveplan_"))
async def admin_give_finalize(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
        squad_uuid   = [SQUAD_UUID_BASIC, SQUAD_UUID_WHITELIST]
        whitelist_gb = PLANS["vpn_bypass"]["whitelist_gb"]
        new_plan     = "vpn_bypass"
    elif choice == "trial":
        squad_uuid   = [SQUAD_UUID_BASIC, SQUAD_UUID_WHITELIST]
        whitelist_gb = TRIAL["whitelist_gb"]
        new_plan     = "trial"
    # choice == "none":   ставляем squad_uuid=None, new_plan=None (ничего не трогаем)

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
        await bot.send_message(target_id, f"Администратор выдал вам {days} дней.", parse_mode="HTML")
    except Exception:
        pass

# ─────────────────────────────────────────────
#  НАЙТИ ЮЗЕРА (кнопка панели)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_find_start")
async def admin_find_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminFindState.waiting_query)
    await cb.message.answer("Введите username или user_id для поиска:", reply_markup=cancel_kb())

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
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_text)
    await cb.message.answer("Рассылка\n\nВведите текст.", reply_markup=cancel_kb())

# ─────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
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
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Опрос", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Разослать опрос платникам", callback_data="admin_survey_send")],
        [InlineKeyboardButton(text="Результаты", callback_data="admin_survey_results")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_panel")],
    ]))

@router.callback_query(F.data == "admin_survey_send")
async def admin_survey_send_cb(cb: CallbackQuery):
    await cb.answer()
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Рассылаю опрос платникам...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE has_paid=1")
    ok = fail = 0
    for row in users:
        try:
            await bot.send_message(
                row["user_id"],
                f"Оцените работу TrubaVPN\n\nНасколько вы довольны сервисом? Выберите оценку от 1 до 10:",
                parse_mode="HTML",
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
    for admin_id in all_admin_ids():
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
    if not is_admin(cb.from_user.id):
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
    lines = [f"Результаты опроса", "", f"Всего ответов: {total}", f"Ср  дняя оценка: {avg_r}/10", "", "Распределение:"]
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
    if not is_admin(cb.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
        await bot.send_message(row["user_id"], f"Администратор выдал вам {days} дней.", parse_mode="HTML")
    except Exception:
        pass

@router.message(Command("admin"))
async def admin_help(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Команды администратора:\n\n"
        "/give username дни [уст.] — быстро выдать дни (тариф/сквад не меняет; "
        "для выбора тарифа использу  те кнопку «Выдать» в панели)\n"
        "/check username|id — карточка подписчика\n"
        "/add_promo, /genpromo, /list_promos — промокоды\n"
        "/broadcast — рассылка\n"
        "/payout username — выплатить реф. баланс\n"
        "/sale_notify — вкл/выкл уведомления о покупках\n"
        "/whitelist_check, /whitelist_status — лимиты белых списков\n\n"
        "Кнопка «Панель» в профиле открывает то же самое через интерфейс."
    )

# ───────────────────────────────────────   ─────
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
                            parse_mode="HTML",
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
    if not is_admin(message.from_user.id):
        return
    if not WHITELIST_NODE_UUID:
        await message.answer("WHITELIST_NODE_UUID не задан.")
        return
    await message.answer("Проверяю лимиты белых списков...")
    await check_whitelist_limits()
    await message.answer("Готово. Смотри /whitelist_status для деталей.")

@router.message(Command("whitelist_status"))
async def admin_whitelist_status(message: types.Message):
    if not is_admin(message.from_user.id):
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
        for admin_id in all_admin_ids():
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
    if not is_admin(message.from_user.id):
        return
    await message.answer("Формирую отчёт...")
    await send_daily_report()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────   ───────────────────────────────
# ─────────────────────────────────────────────
#  НАПОМИНАНИЯ ОБ ОКОНЧАНИИ ПОДПИСКИ
# ─────────────────────────────────────────────
_EXPIRY_WINDOWS = [("3d", 3 * 86400), ("1d", 86400), ("1h", 3600)]
_EXPIRY_LABELS  = {"3d": "около 3 дней", "1d": "около 1 дня", "1h": "около 1 часа"}


def _renew_kb(plan_key: str, include_back: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура продления для напоминания. Тап по кнопке уже создаёт саму
    ссылку на оплату (экран «Оплатить» / «Проверить оплату»)."""
    rows = []
    for m in MONTH_CHOICES:
        price = calc_plan_price(plan_key, m)
        rows.append([InlineKeyboardButton(
            text=f"Продлить · {m} мес — {price} ₽",
            callback_data=f"rnw_{plan_key}_{m}",
        )])
    if plan_key == "vpn":
        # Обычный VPN: дополнительно предлагаем докупить обход белых списков.
        rows.append([InlineKeyboardButton(
            text="Добавить обход белых списков",
            callback_data="plan_upgrade",
        )])
    if include_back:
        rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("rnw_"))
async def renew_cb(cb: CallbackQuery):
    await cb.answer()
    plan_key, _, months_s = cb.data.removeprefix("rnw_").rpartition("_")
    if not plan_key or not months_s.isdigit():
        await cb.answer("Не удалось определить тариф.", show_alert=True)
        return
    months = int(months_s)
    if plan_key not in PLANS or months <= 0:
        await cb.answer("Тариф недоступен.", show_alert=True)
        return
    plan  = PLANS[plan_key]
    price = calc_plan_price(plan_key, months)
    await _create_payment_page(
        cb, kind="plan",
        item_name=f"Продление {plan['name']} · {months} мес.",
        price=price, days=months * 30, hwid=1, squad=plan["squad"],
        whitelist_gb=plan["whitelist_gb"], plan_key=plan_key,
    )


async def _send_expiry_reminder(user_id: int, plan_key: str, expire_ts: int, kind: str):
    plan = PLANS.get(plan_key)
    if not plan:
        return
    date_str = fmt_dt(expire_ts, "%d.%m.%Y %H:%M")
    text = (
        f"⏳ {hbold('Подписка скоро закончится')}\n\n"
        f"Тариф: {plan['name']}\n"
        f"Осталось: {_EXPIRY_LABELS.get(kind, '')}\n"
        f"Действует до: {date_str}\n\n"
        f"Продлите подписку, чтобы не потерять доступ:"
    )
    await bot.send_message(user_id, text, parse_mode="HTML",
                           reply_markup=_renew_kb(plan_key))


async def _run_expiry_reminders():
    users = await remna_get_all_users()
    now   = int(time.time())
    candidates = []
    for ru in users:
        if (ru.get("status") or "").upper() == "DISABLED":
            continue
        tg_id = ru.get("telegramId")
        if not tg_id:
            continue
        expire_ts = parse_dt(ru.get("expireAt"))
        if not expire_ts:
            continue
        seconds_left = int(expire_ts) - now
        if seconds_left <= 0 or seconds_left > _EXPIRY_WINDOWS[0][1]:
            continue
        candidates.append((int(tg_id), int(expire_ts), seconds_left))
    if not candidates:
        return
    ids = [c[0] for c in candidates]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, plan FROM users WHERE user_id = ANY($1::bigint[])", ids
        )
    plan_by_id = {r["user_id"]: r["plan"] for r in rows}
    windows_asc = sorted(_EXPIRY_WINDOWS, key=lambda x: x[1])
    for tg_id, expire_ts, seconds_left in candidates:
        plan_key = plan_by_id.get(tg_id)
        if plan_key not in ("vpn", "vpn_bypass"):
            continue
        reached = [k for k, t in windows_asc if seconds_left <= t]
        if not reached:
            continue
        target = reached[0]  # самое срочное достигнутое окно
        async with pool.acquire() as conn:
            sent = await conn.fetch(
                "SELECT kind FROM expiry_reminders WHERE user_id=$1 AND expire_ts=$2",
                tg_id, expire_ts,
            )
        sent_kinds = {r["kind"] for r in sent}
        if target not in sent_kinds:
            try:
                await _send_expiry_reminder(tg_id, plan_key, expire_ts, target)
            except Exception as e:
                log.error("expiry reminder send %s: %s", tg_id, e)
        # Фиксируем target и подавляем менее срочные (более длинные) окна.
        async with pool.acquire() as conn:
            for k in reached:
                await conn.execute(
                    "INSERT INTO expiry_reminders (user_id, kind, expire_ts, sent_at) "
                    "VALUES ($1,$2,$3,$4) ON CONFLICT (user_id, kind, expire_ts) DO NOTHING",
                    tg_id, k, expire_ts, now,
                )
        await asyncio.sleep(0.05)


async def expiry_reminder_scheduler():
    """Раз в 15 минут напоминает об окончании подписки за 3 дня, 1 день и 1 час."""
    while True:
        try:
            await _run_expiry_reminders()
        except Exception as e:
            log.error("expiry_reminder_scheduler: %s", e)
        await asyncio.sleep(15 * 60)


async def main():
    await init_db()
    await load_extra_admins()
    dp.include_router(router)
    asyncio.create_task(daily_report_scheduler())
    asyncio.create_task(whitelist_limit_scheduler())
    asyncio.create_task(expiry_reminder_scheduler())
    log.info("TrubaVPN Bot starting (Remnawave)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
