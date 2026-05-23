import os
import uuid
import logging
import time
import sqlite3
import asyncio
import urllib3
import requests as _requests
import json

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.markdown import hcode, hbold
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from yookassa import Configuration, Payment

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────
API_TOKEN      = os.environ["BOT_TOKEN"]
SHOP_ID        = os.environ["SHOP_ID"]
YOOKASSA_KEY   = os.environ["YOOKASSA_KEY"]

PANEL_URL      = os.environ["PANEL_URL"].rstrip("/")
PANEL_LOGIN    = os.environ["PANEL_LOGIN"]
PANEL_PASSWORD = os.environ["PANEL_PASSWORD"]

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "@Truba_VPN")
INBOUND_TAG     = os.environ.get("INBOUND_TAG", "VLESS_TCP_REALITY")
SUB_BASE_URL    = os.environ.get("SUB_BASE_URL", "").rstrip("/")

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
TARIFFS: dict = {
    "trial": {
        "name":  "Пробный",
        "price": 10,
        "days":  1,
        "desc":  "Тестовый доступ на 24 часа",
        "trial": True,
    },
    "1_dev": {
        "name":    "1 устройство",
        "price":   99,
        "days":    30,
        "devices": 1,
        "desc": (
            "🔒 Безлимитный трафик\n\n"
            "🌐 Высокая скорость"
        ),
    },
    "2_dev": {
        "name":    "2 устройства",
        "price":   179,
        "days":    30,
        "devices": 2,
        "desc": (
            "🔒 Безлимитный трафик\n\n"
            "🌐 Высокая скорость"
        ),
    },
    "5_dev": {
        "name":    "5 устройств",
        "price":   349,
        "days":    30,
        "devices": 5,
        "desc": (
            "🔒 Безлимитный трафик\n\n"
            "🌐 Высокая скорость"
        ),
    },
}

MONTH_OPTIONS = {
    1:  {"label": "1 месяц",   "multiplier": 1.0},
    3:  {"label": "3 месяца",  "multiplier": 2.7},
    6:  {"label": "6 месяцев", "multiplier": 5.1},
    12: {"label": "1 год",     "multiplier": 9.6},
}

DEVICE_OPTIONS = {
    0:  "Без лимита",
    1:  "1 устройство",
    2:  "2 устройства",
    3:  "3 устройства",
    5:  "5 устройств",
    10: "10 устройств",
}


# ─────────────────────────────────────────────
#  ПРОВЕРКА ПОДПИСКИ НА КАНАЛ
# ─────────────────────────────────────────────

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True


def sub_required_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


class PromoState(StatesGroup):
    waiting_code    = State()
    choosing_tariff = State()

class BroadcastState(StatesGroup):
    waiting_text = State()
    confirm      = State()

class AdminKeyState(StatesGroup):
    waiting_username = State()
    waiting_days     = State()
    waiting_devices  = State()

class AdminPromoState(StatesGroup):
    waiting_input = State()


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("trubavpn")

bot    = Bot(token=API_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()

DB = "users.db"

def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                referrer_id INTEGER,
                expiry_date INTEGER DEFAULT 0,
                sub_token   TEXT,
                marz_username TEXT,
                has_paid    INTEGER DEFAULT 0,
                tariff_key  TEXT,
                limit_ip    INTEGER DEFAULT 0,
                sub_url     TEXT
            );
            CREATE TABLE IF NOT EXISTS promos (
                code        TEXT PRIMARY KEY,
                days        INTEGER,
                uses        INTEGER DEFAULT 1,
                promo_type  TEXT DEFAULT 'days',
                tariff_key  TEXT DEFAULT NULL
            );
        """)
        for col in ("marz_username TEXT", "tariff_key TEXT", "limit_ip INTEGER DEFAULT 0",
                    "sub_url TEXT", "sub_token TEXT"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        for col in ("promo_type TEXT DEFAULT 'days'", "tariff_key TEXT DEFAULT NULL"):
            try:
                conn.execute(f"ALTER TABLE promos ADD COLUMN {col}")
            except Exception:
                pass


# ─────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────

def build_subscription_url(marz_username: str) -> str:
    base = SUB_BASE_URL if SUB_BASE_URL else PANEL_URL
    return f"{base}/sub/{marz_username}"


def format_key_message(expiry: int, vless_link: str, sub_url: str | None) -> str:
    date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))

    if not vless_link or vless_link.startswith("PANEL_ERROR_"):
        return (
            f"📅 Подписка до: <b>{date_str}</b>\n\n"
            f"⚠️ Ключ временно недоступен — панель не ответила.\n"
            f"Напишите в поддержку: {SUPPORT_CONTACT}"
        )

    lines = [f"🗓 Подписка активна до: <b>{date_str}</b>", "", "━━━━━━━━━━━━━━━━━━━━"]

    if sub_url:
        lines += [
            "🌐 <b>Ссылка на подписку</b> (рекомендуется):",
            "<i>Импортируйте в Happ / v2rayNG — конфиг обновится автоматически.</i>",
            hcode(sub_url),
            "",
        ]

    lines += [
        "🔑 <b>VLESS-ключ</b> (вручную):",
        hcode(vless_link),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Инструкция по подключению: {CHANNEL_LINK}",
    ]

    return "\n".join(lines)


def calc_price(base_price: int, months: int) -> int:
    return round(base_price * MONTH_OPTIONS[months]["multiplier"])

def calc_days(base_days: int, months: int) -> int:
    return base_days * months

def _decrement_promo(code: str, uses: int):
    with db_conn() as conn:
        if uses <= 1:
            conn.execute("DELETE FROM promos WHERE code=?", (code,))
        else:
            conn.execute("UPDATE promos SET uses=uses-1 WHERE code=?", (code,))


# ─────────────────────────────────────────────
#  MARZBAN API
# ─────────────────────────────────────────────

def _marz_token() -> str | None:
    """Получить JWT токен администратора."""
    try:
        res = _requests.post(
            f"{PANEL_URL}/api/admin/token",
            data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD},
            timeout=15, verify=False,
        )
        if res.status_code == 200:
            token = res.json().get("access_token")
            log.info("[Marzban] Login OK")
            return token
        log.error("[Marzban] Login failed: %d %s", res.status_code, res.text[:200])
    except Exception as e:
        log.error("[Marzban] Login error: %s", e)
    return None


def _marz_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _marz_username(user_id: int) -> str:
    """Генерирует стабильный username для Marzban на основе user_id."""
    return f"truba_{user_id}"


def _sync_create_user(user_id: int, days: int, limit_ip: int = 0) -> tuple[str | None, str | None, str | None]:
    """
    Создаёт пользователя в Marzban или обновляет если уже существует.
    Возвращает (vless_link, sub_url, marz_username).
    """
    token = _marz_token()
    if not token:
        return None, None, None

    headers      = _marz_headers(token)
    marz_uname   = _marz_username(user_id)
    expire_ts    = int(time.time() + days * 86400)

    # Пробуем получить username из БД для note
    tg_username = ""
    try:
        with db_conn() as c:
            r = c.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
            if r and r["username"]:
                tg_username = f"@{r['username']}"
    except Exception:
        pass

    payload = {
        "username": marz_uname,
        "proxies": {
            INBOUND_TAG: {}
        },
        "inbounds": {
            INBOUND_TAG: [INBOUND_TAG]
        },
        "expire": expire_ts,
        "data_limit": 0,           # безлимит
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": tg_username or str(user_id),
    }

    # Проверяем — существует ли уже
    try:
        check = _requests.get(
            f"{PANEL_URL}/api/user/{marz_uname}",
            headers=headers, timeout=15, verify=False,
        )
        exists = check.status_code == 200
    except Exception:
        exists = False

    try:
        if exists:
            # Обновляем expire
            res = _requests.put(
                f"{PANEL_URL}/api/user/{marz_uname}",
                headers=headers,
                json={"expire": expire_ts, "status": "active"},
                timeout=15, verify=False,
            )
        else:
            res = _requests.post(
                f"{PANEL_URL}/api/user",
                headers=headers,
                json=payload,
                timeout=15, verify=False,
            )

        log.info("[Marzban] %s user -> %d | %s",
                 "update" if exists else "create", res.status_code, res.text[:300])

        if res.status_code not in (200, 201):
            log.error("[Marzban] user op failed: %s", res.text[:300])
            return None, None, None

        data = res.json()

    except Exception as e:
        log.error("[Marzban] user op error: %s", e)
        return None, None, None

    # Получаем subscription_url и ссылки из ответа
    sub_url    = build_subscription_url(marz_uname)
    vless_link = _extract_vless(data, marz_uname)

    return vless_link, sub_url, marz_uname


def _extract_vless(user_data: dict, marz_uname: str) -> str | None:
    """Извлекает VLESS-ссылку из данных пользователя Marzban."""
    # Marzban возвращает список links
    links = user_data.get("links", [])
    for link in links:
        if link.startswith("vless://"):
            # Заменяем тег на красивый
            if "#" in link:
                link = link[:link.rfind("#")] + "#" + _make_tag()
            else:
                link = link + "#" + _make_tag()
            return link

    # Если links пустой — пробуем получить отдельным запросом
    try:
        token = _marz_token()
        if not token:
            return None
        res = _requests.get(
            f"{PANEL_URL}/api/user/{marz_uname}",
            headers=_marz_headers(token),
            timeout=15, verify=False,
        )
        if res.status_code == 200:
            links = res.json().get("links", [])
            for link in links:
                if link.startswith("vless://"):
                    if "#" in link:
                        link = link[:link.rfind("#")] + "#" + _make_tag()
                    return link
    except Exception as e:
        log.error("[Marzban] extract vless error: %s", e)

    return None


def _make_tag() -> str:
    from urllib.parse import quote
    return quote("🌐 TrubaVPN · Сервер 1", safe="")


def _sync_extend_user(marz_uname: str, extra_days: int) -> bool:
    """Продлевает подписку пользователя в Marzban."""
    token = _marz_token()
    if not token:
        return False

    headers = _marz_headers(token)

    # Получаем текущий expire
    try:
        res = _requests.get(
            f"{PANEL_URL}/api/user/{marz_uname}",
            headers=headers, timeout=15, verify=False,
        )
        if res.status_code != 200:
            log.error("[Marzban] get user failed: %d", res.status_code)
            return False
        current_expire = res.json().get("expire") or int(time.time())
    except Exception as e:
        log.error("[Marzban] get user error: %s", e)
        return False

    now        = int(time.time())
    new_expire = max(current_expire, now) + extra_days * 86400

    try:
        res = _requests.put(
            f"{PANEL_URL}/api/user/{marz_uname}",
            headers=headers,
            json={"expire": new_expire, "status": "active"},
            timeout=15, verify=False,
        )
        log.info("[Marzban] extend user -> %d | %s", res.status_code, res.text[:200])
        return res.status_code == 200
    except Exception as e:
        log.error("[Marzban] extend user error: %s", e)
        return False


async def panel_create_user(user_id: int, days: int, limit_ip: int = 0):
    return await asyncio.get_event_loop().run_in_executor(
        None, _sync_create_user, user_id, days, limit_ip
    )

async def panel_extend_user(marz_uname: str, extra_days: int) -> bool:
    return await asyncio.get_event_loop().run_in_executor(
        None, _sync_extend_user, marz_uname, extra_days
    )


# ─────────────────────────────────────────────
#  АКТИВАЦИЯ ПОДПИСКИ
# ─────────────────────────────────────────────

async def activate_subscription(user_id: int, days: int, limit_ip: int = 0):
    now   = int(time.time())
    delta = days * 86400

    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token, marz_username, sub_url FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    current_expiry = row["expiry_date"]  if row else 0
    token          = row["sub_token"]    if row and row["sub_token"]    else None
    marz_uname     = row["marz_username"] if row and row["marz_username"] else None
    saved_sub_url  = row["sub_url"]      if row and row["sub_url"]      else None
    new_expiry     = max(current_expiry, now) + delta
    sub_url        = None

    if marz_uname:
        ok = await panel_extend_user(marz_uname, days)
        if ok:
            sub_url = saved_sub_url or build_subscription_url(marz_uname)
        else:
            log.warning("[Sub] Could not extend %s, recreating", marz_uname)
            marz_uname = None

    if not marz_uname:
        vless_link, sub_url, marz_uname = await panel_create_user(user_id, days, limit_ip)
        if not vless_link:
            vless_link = f"PANEL_ERROR_{uuid.uuid4().hex[:8]}"
            sub_url    = None
            marz_uname = _marz_username(user_id)
        token = vless_link

    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET expiry_date=?, sub_token=?, marz_username=?, sub_url=? WHERE user_id=?",
            (new_expiry, token, marz_uname, sub_url, user_id),
        )

    return new_expiry, token, sub_url


# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить VPN",   callback_data="tariffs"),
         InlineKeyboardButton(text="👤 Профиль",      callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы",     callback_data="ref_program"),
         InlineKeyboardButton(text="📞 Промокод",     callback_data="promo_enter")],
        [InlineKeyboardButton(text="💬 Поддержка",    callback_data="support_tab"),
         InlineKeyboardButton(text="ℹ️ Инфо",         callback_data="info_tab")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="back")]
    ])

def months_kb(tariff_key: str):
    info = TARIFFS[tariff_key]
    base = info["price"]
    rows = []
    for months, opt in MONTH_OPTIONS.items():
        total = calc_price(base, months)
        if months == 1:
            label = f"{opt['label']} — {total} ₽"
        else:
            per_month = round(total / months)
            discount  = round((1 - per_month / base) * 100)
            label = f"{opt['label']} — {total} ₽  (−{discount}%)"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"buym_{tariff_key}_{months}"
        )])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="tariffs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def free_tariff_kb(promo_code: str):
    rows = []
    for k, v in TARIFFS.items():
        if v.get("trial"):
            continue
        rows.append([InlineKeyboardButton(
            text=v["name"],
            callback_data=f"pfree_{k}_{promo_code}"
        )])
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def devices_kb(prefix: str):
    rows = []
    row  = []
    for limit, label in DEVICE_OPTIONS.items():
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}{limit}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="gk_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────
#  HANDLERS — СТАРТ
# ─────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    u_id = message.from_user.id
    r_id = None
    if command.args and command.args.isdigit():
        candidate = int(command.args)
        if candidate != u_id:
            r_id = candidate

    with db_conn() as conn:
        exists = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u_id,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES (?,?,?)",
                (u_id, message.from_user.username, r_id),
            )
        else:
            conn.execute("UPDATE users SET username=? WHERE user_id=?",
                         (message.from_user.username, u_id))

    if not await is_subscribed(u_id):
        await message.answer(
            f"🌏 {hbold('TrubaVPN')}\n\n"
            "Чтобы пользоваться ботом, подпишитесь на наш канал.\n"
            "Там вы найдёте инструкции по подключению и новости.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return

    await message.answer(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\n"
        "Выберите действие:",
        reply_markup=main_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data == "check_sub")
async def check_sub_callback(cb: CallbackQuery):
    await cb.answer()
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Вы ещё не подписаны. Подпишитесь и попробуйте ещё раз.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\n"
        "Выберите действие:",
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
        if v.get("trial"):
            label = f"{v['name']} — {v['price']} ₽"
        else:
            label = f"{v['name']} — от {v['price']} ₽/мес."
        btns.append([InlineKeyboardButton(text=label, callback_data=f"buy_{k}")])
    btns.append([InlineKeyboardButton(text="← Назад", callback_data="back")])
    await cb.message.edit_text(
        "💰 <b>Выберите тариф:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        parse_mode="HTML",
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
        f"<b>{info['name']}</b>\n\n"
        f"{info['desc']}\n\n"
        "Выберите период подписки:",
        reply_markup=months_kb(t_key),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("buym_"))
async def process_buy_months(cb: CallbackQuery):
    parts  = cb.data.removeprefix("buym_").rsplit("_", 1)
    t_key  = parts[0]
    months = int(parts[1])
    await _show_payment_page(cb, t_key, months)


async def _show_payment_page(cb: CallbackQuery, t_key: str, months: int):
    info        = TARIFFS[t_key]
    days        = calc_days(info["days"], months)
    price       = calc_price(info["price"], months) if not info.get("trial") else info["price"]
    month_label = MONTH_OPTIONS.get(months, {}).get("label", f"{months} мес.")

    try:
        payment = Payment.create({
            "amount":       {"value": f"{price}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture":      True,
            "description":  f"TrubaVPN — {info['name']} / {month_label}",
            "metadata":     {
                "user_id":    str(cb.from_user.id),
                "days":       str(days),
                "tariff_key": t_key,
                "limit_ip":   str(info.get("devices", 0)),
            },
        }, str(uuid.uuid4()))
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа, попробуйте позже.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",        url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="← Назад",            callback_data=f"buy_{t_key}")],
    ])
    await cb.message.edit_text(
        f"<b>{info['name']}</b>  ·  {month_label}\n\n"
        f"{info['desc']}\n\n"
        f"💰 К оплате: <b>{price} ₽</b>\n\n"
        "После оплаты нажмите «Проверить оплату».",
        reply_markup=kb, parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("check_"))
async def check_payment(cb: CallbackQuery):
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

    u_id = int(payment.metadata["user_id"])
    days = int(payment.metadata["days"])
    expiry, token, sub_url = await activate_subscription(u_id, days)

    with db_conn() as conn:
        row = conn.execute(
            "SELECT referrer_id, has_paid FROM users WHERE user_id=?", (u_id,)
        ).fetchone()

        if row and row["referrer_id"] and row["has_paid"] == 0:
            ref_id = row["referrer_id"]
            await activate_subscription(u_id,   7)
            await activate_subscription(ref_id, 7)
            try:
                await bot.send_message(
                    ref_id,
                    "🤝 Ваш друг оплатил подписку!\n"
                    "Вам и ему начислено по <b>+7 дней</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        t_key_meta = payment.metadata.get("tariff_key", "")
        l_ip_meta  = int(payment.metadata.get("limit_ip", "0"))
        conn.execute(
            "UPDATE users SET has_paid=1, tariff_key=?, limit_ip=? WHERE user_id=?",
            (t_key_meta or None, l_ip_meta, u_id),
        )

    key_msg = format_key_message(expiry, token, sub_url)
    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n{key_msg}",
        parse_mode="HTML", reply_markup=back_kb(),
    )


# ─────────────────────────────────────────────
#  ПРОМОКОД
# ─────────────────────────────────────────────

@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(
        "📞 <b>Введите промокод:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")]
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

    with db_conn() as conn:
        row = conn.execute(
            "SELECT days, uses, promo_type, tariff_key FROM promos WHERE code=?", (code,)
        ).fetchone()

    if not row:
        await message.answer(
            "Неверный или уже использованный промокод.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✕ Отмена", callback_data="promo_cancel")]
            ]),
        )
        return

    promo_type = row["promo_type"] or "days"
    tariff_key = row["tariff_key"]
    days       = row["days"]
    uses       = row["uses"]

    if promo_type == "free_tariff" and tariff_key and tariff_key in TARIFFS:
        await state.clear()
        expiry, token, sub_url = await activate_subscription(message.from_user.id, days)
        _decrement_promo(code, uses)
        info    = TARIFFS[tariff_key]
        key_msg = format_key_message(expiry, token, sub_url)
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован!\n"
            f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
            f"{key_msg}",
            parse_mode="HTML", reply_markup=main_kb(),
        )
        return

    if promo_type == "free_choice":
        await state.set_state(PromoState.choosing_tariff)
        await state.update_data(promo_code=code, promo_days=days, promo_uses=uses)
        await message.answer(
            f"📞 Промокод <b>{code}</b> даёт бесплатную подписку на <b>{days} дней</b>!\n\n"
            "Выберите тариф:",
            parse_mode="HTML",
            reply_markup=free_tariff_kb(code),
        )
        return

    expiry, token, sub_url = await activate_subscription(message.from_user.id, days)
    _decrement_promo(code, uses)
    await state.clear()
    key_msg = format_key_message(expiry, token, sub_url)
    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован — добавлено <b>{days} дн.</b>\n\n{key_msg}",
        parse_mode="HTML", reply_markup=main_kb(),
    )


@router.callback_query(F.data.startswith("pfree_"))
async def handle_free_tariff_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, t_key, promo_code = cb.data.split("_", 2)

    data = await state.get_data()
    days = data.get("promo_days", 30)
    uses = data.get("promo_uses", 1)
    await state.clear()

    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return

    info = TARIFFS[t_key]
    expiry, token, sub_url = await activate_subscription(cb.from_user.id, days)
    _decrement_promo(promo_code, uses)
    key_msg = format_key_message(expiry, token, sub_url)

    await cb.message.edit_text(
        f"✅ Промокод <b>{promo_code}</b> активирован!\n"
        f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
        f"{key_msg}",
        parse_mode="HTML", reply_markup=back_kb(),
    )


# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────

@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    await cb.answer()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token, marz_username, tariff_key, limit_ip, sub_url "
            "FROM users WHERE user_id=?",
            (cb.from_user.id,)
        ).fetchone()

    now = int(time.time())
    if row and row["expiry_date"] > now:
        days_left = (row["expiry_date"] - now) // 86400
        date_str  = time.strftime("%d.%m.%Y", time.localtime(row["expiry_date"]))
        token     = row["sub_token"] or ""

        t_key = row["tariff_key"]
        if t_key and t_key in TARIFFS:
            t_info      = TARIFFS[t_key]
            devices     = t_info.get("devices", 0)
            dev_label   = f"{devices} устр." if devices else "без лимита"
            tariff_line = f"\nТариф: <b>{t_info['name']}</b> · {dev_label}"
        else:
            limit_ip    = row["limit_ip"] if row["limit_ip"] else 0
            dev_label   = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} устр.")
            tariff_line = f"\nТариф: <b>{dev_label}</b>" if limit_ip else ""

        saved_sub = row["sub_url"]
        if not saved_sub and row["marz_username"]:
            saved_sub = build_subscription_url(row["marz_username"])

        sub_line = f"\n\n🌐 <b>Ссылка на подписку:</b>\n{hcode(saved_sub)}" if saved_sub else ""

        if token.startswith("vless://"):
            key_line = f"\n\n🔑 <b>VLESS-ключ:</b>\n{hcode(token)}"
        elif token.startswith("PANEL_ERROR_") or not token:
            key_line = "\n\n⚠️ Ключ не был выдан — обратитесь в поддержку."
        else:
            key_line = f"\n\n🔑 Ключ:\n{hcode(token)}"

        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна · до <b>{date_str}</b>\n"
            f"Осталось: <b>{days_left} дн.</b>"
            f"{tariff_line}"
            f"{sub_line}"
            f"{key_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "Подписка не активна.\n"
            "Нажмите «💳 Купить VPN» для оформления."
        )

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())


# ─────────────────────────────────────────────
#  РЕФЕРАЛЫ / ПОДДЕРЖКА / ИНФО
# ─────────────────────────────────────────────

@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    await cb.answer()
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 <b>Реферальная программа</b>\n\n"
        "Поделитесь ссылкой с другом. Когда он оплатит любой тариф — "
        "вы оба автоматически получите <b>+7 дней</b> к своим подпискам.\n\n"
        f"Ваша реферальная ссылка:\n{hcode(link)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )


@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "💬 <b>Поддержка</b>\n\nЕсть вопросы? Мы на связи.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Написать в поддержку",
                url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}",
            )],
            [InlineKeyboardButton(text="← Назад", callback_data="back")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "ℹ️ <b>Информация:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Канал с инструкциями", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="Пользовательское соглашение",
                                  url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
            [InlineKeyboardButton(text="🔐 Политика конфиденциальности",
                                  url="https://telegra.ph/Politika-obrabotki-04-27")],
            [InlineKeyboardButton(text="← Назад", callback_data="back")],
        ]),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ADMIN — /give
# ─────────────────────────────────────────────

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(
            "Формат: <code>/give username дни [лимит_устройств]</code>\n"
            "Пример: <code>/give ivan 30 2</code>",
            parse_mode="HTML"
        )
        return

    target_username = parts[0].lstrip("@")
    days     = int(parts[1])
    limit_ip = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0

    with db_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE username=?", (target_username,)).fetchone()

    if not row:
        await message.answer(f"Пользователь @{target_username} не найден.")
        return

    target_id              = row["user_id"]
    expiry, token, sub_url = await activate_subscription(target_id, days, limit_ip)
    date_str               = time.strftime("%d.%m.%Y", time.localtime(expiry))
    dev_label              = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} уст.")

    await message.answer(
        f"✅ @{target_username} выдано <b>{days}</b> дней · {dev_label}\n"
        f"До: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        key_msg = format_key_message(expiry, token, sub_url)
        await bot.send_message(
            target_id,
            f"Администратор выдал вам <b>{days}</b> дней подписки.\n\n{key_msg}",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  ADMIN — /genkey
# ─────────────────────────────────────────────

@router.message(Command("genkey"))
async def admin_genkey_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminKeyState.waiting_username)
    await message.answer(
        "🔑 <b>Выдача ключа</b>\n\nВведите username пользователя (без @):",
        parse_mode="HTML"
    )


@router.message(AdminKeyState.waiting_username)
async def admin_genkey_username(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    with db_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()

    if not row:
        await message.answer(f"Пользователь @{username} не найден. Введите другой username:")
        return

    await state.update_data(target_id=row["user_id"], target_username=username)
    await state.set_state(AdminKeyState.waiting_days)
    await message.answer(
        f"👤 Пользователь: @{username}\n\nСколько дней выдать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="30 дней",  callback_data="gk_30"),
             InlineKeyboardButton(text="60 дней",  callback_data="gk_60")],
            [InlineKeyboardButton(text="90 дней",  callback_data="gk_90"),
             InlineKeyboardButton(text="365 дней", callback_data="gk_365")],
            [InlineKeyboardButton(text="✕ Отмена", callback_data="gk_cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("gk_"), AdminKeyState.waiting_days)
async def admin_genkey_days(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "gk_cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        return

    days = int(cb.data.removeprefix("gk_"))
    await state.update_data(days=days)
    await state.set_state(AdminKeyState.waiting_devices)

    await cb.message.edit_text(
        f"Дней: <b>{days}</b>\n\nЛимит устройств:",
        parse_mode="HTML",
        reply_markup=devices_kb("gkd_"),
    )


@router.callback_query(F.data.startswith("gkd_"))
async def admin_genkey_devices(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    limit_ip = int(cb.data.removeprefix("gkd_"))
    data     = await state.get_data()
    await state.clear()

    target_id       = data["target_id"]
    target_username = data["target_username"]
    days            = data["days"]

    expiry, token, sub_url = await activate_subscription(target_id, days, limit_ip)
    date_str  = time.strftime("%d.%m.%Y", time.localtime(expiry))
    dev_label = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} уст.")

    await cb.message.edit_text(
        f"✅ @{target_username} выдано <b>{days}</b> дней · {dev_label}\n"
        f"До: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        key_msg = format_key_message(expiry, token, sub_url)
        await bot.send_message(
            target_id,
            f"Администратор выдал вам <b>{days}</b> дней подписки.\n\n{key_msg}",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "gk_cancel")
async def admin_genkey_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Отменено.")


# ─────────────────────────────────────────────
#  ADMIN — /add_promo
# ─────────────────────────────────────────────

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n"
            "<code>/add_promo КОД ДНИ [исп.]</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:1_dev</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:choice</code>",
            parse_mode="HTML",
        )
        return

    code       = parts[0].upper()
    days       = int(parts[1])
    uses       = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    free_arg   = next((p for p in parts if p.startswith("free:")), None)
    promo_type = "days"
    tariff_key = None

    if free_arg:
        value = free_arg.removeprefix("free:")
        if value == "choice":
            promo_type = "free_choice"
        elif value in TARIFFS:
            promo_type = "free_tariff"
            tariff_key = value
        else:
            await message.answer(
                f"Тариф <code>{value}</code> не найден.\nДоступны: {', '.join(TARIFFS)}",
                parse_mode="HTML",
            )
            return

    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promos (code, days, uses, promo_type, tariff_key) VALUES (?,?,?,?,?)",
            (code, days, uses, promo_type, tariff_key),
        )

    type_label = {
        "days":        "добавляет дни",
        "free_tariff": f"бесплатный тариф «{TARIFFS[tariff_key]['name']}»" if tariff_key else "",
        "free_choice": "бесплатный тариф на выбор пользователя",
    }.get(promo_type, promo_type)

    await message.answer(
        f"✅ Промокод <code>{code}</code> создан.\n"
        f"Тип: {type_label}\n"
        f"Дней: <b>{days}</b> · Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ADMIN — /genpromo
# ─────────────────────────────────────────────

@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminPromoState.waiting_input)
    tariff_list = "\n".join(
        f"  <code>{k}</code> — {v['name']}" for k, v in TARIFFS.items() if not v.get("trial")
    )
    await message.answer(
        "📞 <b>Генерация промокода</b>\n\n"
        "<b>Форматы:</b>\n\n"
        "<code>КОД ДНИ [исп.]</code> — добавляет дни\n"
        "  Пример: <code>SUMMER 30 50</code>\n\n"
        "<code>КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "  Пример: <code>VIP 30 1 free:1_dev</code>\n\n"
        "<code>КОД ДНИ [исп.] free:choice</code> — на выбор пользователя\n"
        "  Пример: <code>GIFT 30 10 free:choice</code>\n\n"
        f"<b>Тарифы:</b>\n{tariff_list}\n\n"
        "Только число → код сгенерируется автоматически:\n"
        "  <code>30 50</code> — случайный код на 30 дн., 50 исп.\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), AdminPromoState.waiting_input)
async def admin_genpromo_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@router.message(AdminPromoState.waiting_input)
async def admin_genpromo_handle(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()

    if parts[0].isdigit():
        code     = uuid.uuid4().hex[:8].upper()
        days     = int(parts[0])
        uses     = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 1
        free_arg = next((p for p in parts if p.startswith("free:")), None)
    else:
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Неверный формат. Попробуйте снова: /genpromo")
            return
        code     = parts[0].upper()
        days     = int(parts[1])
        uses     = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
        free_arg = next((p for p in parts if p.startswith("free:")), None)

    promo_type = "days"
    tariff_key = None

    if free_arg:
        value = free_arg.removeprefix("free:")
        if value == "choice":
            promo_type = "free_choice"
        elif value in TARIFFS:
            promo_type = "free_tariff"
            tariff_key = value
        else:
            await message.answer(f"Тариф <code>{value}</code> не найден.", parse_mode="HTML")
            return

    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promos (code, days, uses, promo_type, tariff_key) VALUES (?,?,?,?,?)",
            (code, days, uses, promo_type, tariff_key),
        )

    type_label = {
        "days":        "добавляет дни",
        "free_tariff": f"бесплатный тариф «{TARIFFS[tariff_key]['name']}»" if tariff_key else "",
        "free_choice": "бесплатный тариф на выбор пользователя",
    }.get(promo_type, promo_type)

    await message.answer(
        f"✅ Промокод создан:\n\n"
        f"📞 Код: <code>{code}</code>\n"
        f"Тип: {type_label}\n"
        f"Дней: <b>{days}</b> · Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )


@router.message(Command("list_promos"))
async def admin_list_promos(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, days, uses, promo_type, tariff_key FROM promos ORDER BY promo_type, days DESC"
        ).fetchall()

    if not rows:
        await message.answer("Активных промокодов нет.")
        return

    lines = ["📞 <b>Активные промокоды:</b>\n"]
    for r in rows:
        ptype = r["promo_type"] or "days"
        if ptype == "free_tariff":
            t_name = TARIFFS.get(r["tariff_key"] or "", {}).get("name", r["tariff_key"])
            extra  = f" · {t_name} (бесплатно)"
        elif ptype == "free_choice":
            extra  = " · на выбор (бесплатно)"
        else:
            extra  = ""
        lines.append(f"<code>{r['code']}</code> — {r['days']} дн., {r['uses']} исп.{extra}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────
#  ADMIN — /broadcast
# ─────────────────────────────────────────────

@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Введите текст сообщения (HTML поддерживается).\n"
        "/cancel — отмена.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), BroadcastState.waiting_text)
async def broadcast_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.")


@router.message(BroadcastState.waiting_text)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastState.confirm)

    await message.answer(
        f"👁 <b>Предпросмотр:</b>\n\n{message.text}\n\n"
        "Подтвердите рассылку:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Разослать всем", callback_data="bc_confirm")],
            [InlineKeyboardButton(text="✕ Отмена",         callback_data="bc_cancel")],
        ]),
    )


@router.callback_query(F.data == "bc_confirm")
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return

    data      = await state.get_data()
    text_body = data.get("broadcast_text", "")
    await state.clear()

    if not text_body:
        await cb.answer("Текст не найден. Начните заново: /broadcast", show_alert=True)
        return

    await cb.message.edit_text("📢 Рассылка запущена...")

    with db_conn() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()

    ok, fail = 0, 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], text_body, parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await cb.message.edit_text(
        f"✅ Рассылка завершена!\n"
        f"Отправлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "bc_cancel")
async def broadcast_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Рассылка отменена.")


# ─────────────────────────────────────────────
#  ADMIN — /stats
# ─────────────────────────────────────────────

@router.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    now = int(time.time())
    with db_conn() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE expiry_date>?", (now,)).fetchone()[0]
        paid   = conn.execute("SELECT COUNT(*) FROM users WHERE has_paid=1").fetchone()[0]
        promos = conn.execute("SELECT COUNT(*) FROM promos").fetchone()[0]

    await message.answer(
        f"📊 <b>Статистика TrubaVPN</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"✅ Активных подписок:   <b>{active}</b>\n"
        f"💳 Платили хоть раз:    <b>{paid}</b>\n"
        f"📞 Активных промокодов: <b>{promos}</b>",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ADMIN — /subs
# ─────────────────────────────────────────────

@router.message(Command("subs"))
async def admin_subs(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    now = int(time.time())
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, username, expiry_date, tariff_key, limit_ip "
            "FROM users WHERE expiry_date > ? ORDER BY expiry_date DESC",
            (now,)
        ).fetchall()

    if not rows:
        await message.answer("Активных подписчиков нет.")
        return

    lines = [f"👥 <b>Активные подписчики ({len(rows)}):</b>\n"]
    for r in rows:
        days_left = (r["expiry_date"] - now) // 86400
        name      = f"@{r['username']}" if r["username"] else f"id{r['user_id']}"
        t_key     = r["tariff_key"]
        if t_key and t_key in TARIFFS:
            tariff_label = TARIFFS[t_key]["name"]
        else:
            limit        = r["limit_ip"] or 0
            tariff_label = DEVICE_OPTIONS.get(limit, f"{limit} устр.")
        lines.append(f"{name} — {tariff_label}, {days_left} дн.")

    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 51:
            await message.answer("\n".join(chunk), parse_mode="HTML")
            chunk = []
    if chunk:
        await message.answer("\n".join(chunk), parse_mode="HTML")


# ─────────────────────────────────────────────
#  ADMIN — /panel_check
# ─────────────────────────────────────────────

@router.message(Command("panel_check"))
async def admin_panel_check(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("Проверяю подключение к Marzban...")

    def _check():
        lines = []
        token = _marz_token()
        lines.append(f"{'✅' if token else '❌'} Auth /api/admin/token")
        if not token:
            return lines

        headers = _marz_headers(token)
        for path in ("/api/inbounds", "/api/users?limit=1", f"/api/user/truba_test"):
            try:
                r       = _requests.get(f"{PANEL_URL}{path}", headers=headers,
                                        timeout=10, verify=False)
                snippet = r.text.strip()[:120].replace("\n", " ")
                icon    = "✅" if r.status_code in (200, 422, 404) else "❌"
                lines.append(f"{icon} {path} → {r.status_code} | {snippet}")
            except Exception as e:
                lines.append(f"❌ {path} → {e}")
        return lines

    lines = await asyncio.get_event_loop().run_in_executor(None, _check)
    await message.answer(
        "🔧 <b>Marzban diagnostics:</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ADMIN — /admin
# ─────────────────────────────────────────────

@router.message(Command("admin"))
async def admin_help(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚙️ <b>Команды администратора:</b>\n\n"
        "👤 <b>Выдача подписки:</b>\n"
        "<code>/give username дни [устройств]</code> — выдать дни\n"
        "<code>/genkey</code> — интерактивная выдача ключа\n\n"
        "📞 <b>Промокоды:</b>\n"
        "<code>/add_promo КОД ДНИ [исп.]</code> — промокод на дни\n"
        "<code>/add_promo КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>/add_promo КОД ДНИ [исп.] free:choice</code> — тариф на выбор\n"
        "<code>/genpromo</code> — интерактивная генерация\n"
        "<code>/list_promos</code> — список промокодов\n\n"
        "📢 <b>Прочее:</b>\n"
        "<code>/broadcast</code> — рассылка всем\n"
        "<code>/stats</code> — статистика\n"
        "<code>/subs</code> — список активных подписчиков\n"
        "<code>/panel_check</code> — диагностика панели",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ФОНОВЫЕ ЗАДАЧИ
# ─────────────────────────────────────────────

async def sync_db_with_panel():
    """При старте подтягивает актуальные данные из Marzban."""
    log.info("[Sync] Starting DB sync with Marzban...")
    await asyncio.sleep(5)

    def _fetch():
        token = _marz_token()
        if not token:
            return {}
        headers = _marz_headers(token)
        try:
            res = _requests.get(
                f"{PANEL_URL}/api/users?limit=1000",
                headers=headers, timeout=20, verify=False,
            )
            if res.status_code != 200:
                return {}
            users_list = res.json().get("users", [])
            return {u["username"]: u for u in users_list}
        except Exception as e:
            log.error("[Sync] fetch error: %s", e)
            return {}

    panel_users = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    if not panel_users:
        log.warning("[Sync] No panel users fetched.")
        return

    with db_conn() as conn:
        db_users = conn.execute(
            "SELECT user_id, marz_username, sub_token FROM users WHERE expiry_date > ?",
            (int(time.time()),)
        ).fetchall()

    fixed = 0
    for row in db_users:
        marz_uname = row["marz_username"] or _marz_username(row["user_id"])
        pu = panel_users.get(marz_uname)
        if not pu:
            continue

        links      = pu.get("links", [])
        vless_link = next((l for l in links if l.startswith("vless://")), None)
        if vless_link and "#" in vless_link:
            vless_link = vless_link[:vless_link.rfind("#")] + "#" + _make_tag()

        if vless_link and (not row["sub_token"] or row["sub_token"].startswith("PANEL_ERROR_")):
            sub_url = build_subscription_url(marz_uname)
            with db_conn() as conn:
                conn.execute(
                    "UPDATE users SET sub_token=?, marz_username=?, sub_url=? WHERE user_id=?",
                    (vless_link, marz_uname, sub_url, row["user_id"]),
                )
            fixed += 1

    log.info("[Sync] Done. Fixed %d users.", fixed)


async def expiry_notifier():
    """Каждые 6 часов уведомляет тех, у кого подписка истекает через ~24 часа."""
    await asyncio.sleep(60)
    while True:
        try:
            now       = int(time.time())
            window_lo = now + 23 * 3600
            window_hi = now + 25 * 3600
            with db_conn() as conn:
                users = conn.execute(
                    "SELECT user_id FROM users WHERE expiry_date >= ? AND expiry_date <= ?",
                    (window_lo, window_hi)
                ).fetchall()
            for row in users:
                try:
                    await bot.send_message(
                        row["user_id"],
                        "⏳ <b>Напоминание</b>\n\n"
                        "Ваша подписка истекает через <b>~24 часа</b>.\n"
                        "Продлите её, чтобы не потерять доступ.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="tariffs")]
                        ]),
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            log.info("[Notifier] Notified %d users about expiry.", len(users))
        except Exception as e:
            log.error("[Notifier] Error: %s", e)
        await asyncio.sleep(6 * 3600)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

async def main():
    init_db()
    dp.include_router(router)
    log.info("TrubaVPN Bot starting... INBOUND_TAG=%s", INBOUND_TAG)
    asyncio.create_task(sync_db_with_panel())
    asyncio.create_task(expiry_notifier())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
