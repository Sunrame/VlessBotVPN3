import os
import uuid
import logging
import time
import asyncio
import asyncpg
import httpx

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
API_TOKEN      = os.environ["BOT_TOKEN"]
SHOP_ID        = os.environ["SHOP_ID"]
YOOKASSA_KEY   = os.environ["YOOKASSA_KEY"]
DATABASE_URL   = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)

MARZBAN_URL    = os.environ["MARZBAN_URL"].rstrip("/")
MARZBAN_USER   = os.environ["MARZBAN_USER"]
MARZBAN_PASS   = os.environ["MARZBAN_PASS"]

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "@Truba_VPN")

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
TARIFFS: dict = {
    "trial": {
        "name":     "Пробный",
        "price":    10,
        "days":     1,
        "desc":     "⏱️ Тестовый доступ на 24 часа",
        "trial":    True,
        "limit_ip": 1,
    },
    "1_dev": {
        "name":     "1 устройство",
        "price":    99,
        "days":     30,
        "desc":     "🔒 Безлимитный трафик\n\n🌐 Высокая скорость",
        "limit_ip": 1,
    },
    "2_dev": {
        "name":     "2 устройства",
        "price":    179,
        "days":     30,
        "desc":     "🔒 Безлимитный трафик\n\n🌐 Высокая скорость",
        "limit_ip": 2,
    },
    "5_dev": {
        "name":     "5 устройств",
        "price":    349,
        "days":     30,
        "desc":     "🔒 Безлимитный трафик\n\n🌐 Высокая скорость",
        "limit_ip": 5,
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
                marzban_username TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code        TEXT PRIMARY KEY,
                days        INTEGER,
                uses        INTEGER DEFAULT 1,
                promo_type  TEXT    DEFAULT 'days',
                tariff_key  TEXT    DEFAULT NULL
            )
        """)
    log.info("PostgreSQL ready.")

# ─────────────────────────────────────────────
#  MARZBAN API
# ─────────────────────────────────────────────
_marzban_token: str  = ""
_token_expires: float = 0.0

async def get_marzban_token() -> str:
    global _marzban_token, _token_expires
    if time.time() < _token_expires - 60:
        return _marzban_token
    async with httpx.AsyncClient(verify=False) as client:
        r = await client.post(
            f"{MARZBAN_URL}/api/admin/token",
            data={"username": MARZBAN_USER, "password": MARZBAN_PASS},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        _marzban_token = r.json()["access_token"]
        _token_expires = time.time() + 86000
        return _marzban_token

async def marz_headers() -> dict:
    return {"Authorization": f"Bearer {await get_marzban_token()}"}

def marz_username(user_id: int) -> str:
    return f"truba_{user_id}"

async def marzban_get_user(user_id: int) -> dict | None:
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{MARZBAN_URL}/api/user/{marz_username(user_id)}",
                headers=await marz_headers(), timeout=15,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.error("[Marzban] get_user: %s", e)
        return None

async def marzban_create_user(user_id: int, days: int, limit_ip: int = 0) -> dict | None:
    expire_ts = int(time.time()) + days * 86400
    payload = {
        "username":   marz_username(user_id),
        "proxies":    {"vless": {"flow": "xtls-rprx-vision"}},
        "inbounds":   {"vless": ["VLESS TCP REALITY"]},
        "expire":     expire_ts,
        "data_limit": 0,
        "ip_limit":   limit_ip,
        "status":     "active",
    }
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.post(
                f"{MARZBAN_URL}/api/user",
                json=payload, headers=await marz_headers(), timeout=15,
            )
            if r.status_code == 409:
                return await marzban_extend_user(user_id, days, limit_ip)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.error("[Marzban] create_user: %s", e)
        return None

async def marzban_extend_user(user_id: int, days: int, limit_ip: int | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{MARZBAN_URL}/api/user/{marz_username(user_id)}",
                headers=await marz_headers(), timeout=15,
            )
            r.raise_for_status()
            user = r.json()
            now        = int(time.time())
            current    = user.get("expire") or now
            new_expire = max(current, now) + days * 86400
            payload: dict = {"expire": new_expire}
            if limit_ip is not None:
                payload["ip_limit"] = limit_ip
            r2 = await client.put(
                f"{MARZBAN_URL}/api/user/{marz_username(user_id)}",
                json=payload, headers=await marz_headers(), timeout=15,
            )
            r2.raise_for_status()
            return r2.json()
    except Exception as e:
        log.error("[Marzban] extend_user: %s", e)
        return None

async def activate_subscription(user_id: int, days: int, limit_ip: int = 0) -> dict | None:
    user = await marzban_get_user(user_id)
    if user:
        return await marzban_extend_user(user_id, days, limit_ip if limit_ip else None)
    return await marzban_create_user(user_id, days, limit_ip)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True

def format_key_message(user: dict) -> str:
    expire   = user.get("expire", 0)
    sub_url  = user.get("subscription_url", "")
    date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expire)) if expire else "∞"
    full_sub = sub_url if sub_url.startswith("http") else f"{MARZBAN_URL}{sub_url}"

    lines = [f"🗓 Подписка активна до: <b>{date_str}</b>", "", "━━━━━━━━━━━━━━━━━━━━"]
    if full_sub:
        lines += [
            "🌐 <b>Ссылка на подписку</b> (рекомендуется):",
            "<i>Импортируйте в Happ / v2rayNG — конфиг обновится автоматически.</i>",
            hcode(full_sub),
            "",
        ]
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"Инструкция по подключению: {CHANNEL_LINK}",
    ]
    return "\n".join(lines)

def calc_price(base_price: int, months: int) -> int:
    return round(base_price * MONTH_OPTIONS[months]["multiplier"])

def calc_days(base_days: int, months: int) -> int:
    return base_days * months

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
        [InlineKeyboardButton(text="💬 Поддержка",   callback_data="support_tab"),
         InlineKeyboardButton(text="ℹ️ Инфо",        callback_data="info_tab")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="back")]
    ])

def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался",         callback_data="check_sub")],
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
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buym_{tariff_key}_{months}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="tariffs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def free_tariff_kb(promo_code: str):
    rows = []
    for k, v in TARIFFS.items():
        if v.get("trial"):
            continue
        rows.append([InlineKeyboardButton(text=v["name"], callback_data=f"pfree_{k}_{promo_code}")])
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

    async with pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", u_id)
        if not exists:
            await conn.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES ($1,$2,$3)",
                u_id, message.from_user.username, r_id,
            )
        else:
            await conn.execute("UPDATE users SET username=$1 WHERE user_id=$2",
                               message.from_user.username, u_id)

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
        f"<b>{info['name']}</b>\n\n{info['desc']}\n\nВыберите период подписки:",
        reply_markup=months_kb(t_key), parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("buym_"))
async def process_buy_months(cb: CallbackQuery):
    await cb.answer()
    parts  = cb.data.removeprefix("buym_").rsplit("_", 1)
    t_key  = parts[0]
    months = int(parts[1])
    await _show_payment_page(cb, t_key, months)

async def _show_payment_page(cb: CallbackQuery, t_key: str, months: int):
    info  = TARIFFS[t_key]
    days  = calc_days(info["days"], months)
    price = calc_price(info["price"], months) if not info.get("trial") else info["price"]

    # Пробный — показываем "24 часа", остальные — период
    if info.get("trial"):
        month_label = "24 часа"
    else:
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
                "limit_ip":   str(info.get("limit_ip", 0)),
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
    limit_ip = int(payment.metadata.get("limit_ip", 0))

    user = await activate_subscription(u_id, days, limit_ip)
    if not user:
        await cb.answer("Ошибка активации. Напишите в поддержку.", show_alert=True)
        return

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT referrer_id, has_paid FROM users WHERE user_id=$1", u_id)
        if row and row["referrer_id"] and row["has_paid"] == 0:
            ref_id = row["referrer_id"]
            await activate_subscription(u_id,   7)
            await activate_subscription(ref_id, 7)
            try:
                await bot.send_message(
                    ref_id,
                    "🤝 Ваш друг оплатил подписку!\nВам и ему начислено по <b>+7 дней</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await conn.execute(
            "UPDATE users SET has_paid=1, marzban_username=$1 WHERE user_id=$2",
            marz_username(u_id), u_id,
        )

    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n{format_key_message(user)}",
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
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT days, uses, promo_type, tariff_key FROM promos WHERE code=$1", code
        )
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
        info     = TARIFFS[tariff_key]
        limit_ip = info.get("limit_ip", 0)
        user     = await activate_subscription(message.from_user.id, days, limit_ip)
        await _decrement_promo(code, uses)
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован!\n"
            f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
            f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
            parse_mode="HTML", reply_markup=main_kb(),
        )
        return

    if promo_type == "free_choice":
        await state.set_state(PromoState.choosing_tariff)
        await state.update_data(promo_code=code, promo_days=days, promo_uses=uses)
        await message.answer(
            f"📞 Промокод <b>{code}</b> даёт бесплатную подписку на <b>{days} дней</b>!\n\nВыберите тариф:",
            parse_mode="HTML", reply_markup=free_tariff_kb(code),
        )
        return

    user = await activate_subscription(message.from_user.id, days)
    await _decrement_promo(code, uses)
    await state.clear()
    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован — добавлено <b>{days} дн.</b>\n\n"
        f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
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
    info     = TARIFFS[t_key]
    limit_ip = info.get("limit_ip", 0)
    user     = await activate_subscription(cb.from_user.id, days, limit_ip)
    await _decrement_promo(promo_code, uses)
    await cb.message.edit_text(
        f"✅ Промокод <b>{promo_code}</b> активирован!\n"
        f"Тариф: <b>{info['name']}</b> · <b>{days} дней</b> бесплатно\n\n"
        f"{format_key_message(user) if user else '⚠️ Ошибка активации'}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    await cb.answer()
    user = await marzban_get_user(cb.from_user.id)
    now  = int(time.time())

    if user and (user.get("expire") or 0) > now:
        expire    = user["expire"]
        days_left = (expire - now) // 86400
        date_str  = time.strftime("%d.%m.%Y", time.localtime(expire))
        sub_url   = user.get("subscription_url", "")
        full_sub  = sub_url if sub_url.startswith("http") else f"{MARZBAN_URL}{sub_url}"
        sub_line  = f"\n\n🌐 <b>Ссылка на подписку:</b>\n{hcode(full_sub)}" if full_sub else ""

        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна · до <b>{date_str}</b>\n"
            f"⏳ Осталось: <b>{days_left} дн.</b>"
            f"{sub_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "❌ Подписка не активна.\n"
            "Нажмите «💰 Купить VPN» для оформления."
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
        f"Пригласите друга — при его первой оплате вы оба получите <b>+7 дней</b>!\n\n"
        f"Ваша ссылка:\n{hcode(link)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )

@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "💬 <b>Поддержка</b>\n\nЕсть вопросы? Мы на связи.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="→ Написать в поддержку",
                                  url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}")],
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
            [InlineKeyboardButton(text="→ Канал с инструкциями", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="→ Пользовательское соглашение",
                                  url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
            [InlineKeyboardButton(text="→ Политика конфиденциальности",
                                  url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
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
            "Формат: <code>/give username дни [устройств]</code>\n"
            "Пример: <code>/give ivan 30 2</code>",
            parse_mode="HTML"
        )
        return
    target_username = parts[0].lstrip("@")
    days     = int(parts[1])
    limit_ip = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", target_username)
    if not row:
        await message.answer(f"Пользователь @{target_username} не найден.")
        return

    user      = await activate_subscription(row["user_id"], days, limit_ip)
    dev_label = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} уст.")
    if not user:
        await message.answer("Ошибка активации в Marzban.")
        return

    expire   = user.get("expire", 0)
    date_str = time.strftime("%d.%m.%Y", time.localtime(expire)) if expire else "∞"
    await message.answer(
        f"✅ @{target_username} выдано <b>{days}</b> дн. · {dev_label}\nДо: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            row["user_id"],
            f"🎁 Администратор выдал вам <b>{days}</b> дней подписки!\n\n{format_key_message(user)}",
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
    await message.answer("🔑 <b>Выдача ключа</b>\n\nВведите username (без @):", parse_mode="HTML")

@router.message(AdminKeyState.waiting_username)
async def admin_genkey_username(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE username=$1", username)
    if not row:
        await message.answer(f"@{username} не найден. Введите другой username:")
        return
    await state.update_data(target_id=row["user_id"], target_username=username)
    await state.set_state(AdminKeyState.waiting_days)
    await message.answer(
        f"👤 @{username}\n\nСколько дней?",
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
        parse_mode="HTML", reply_markup=devices_kb("gkdev_"),
    )

@router.callback_query(F.data.startswith("gkdev_"))
async def admin_genkey_devices(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    limit_ip = int(cb.data.removeprefix("gkdev_"))
    data = await state.get_data()
    if not data.get("target_id"):
        await state.clear()
        await cb.answer("Сессия истекла. Начните заново: /genkey", show_alert=True)
        return
    await state.clear()
    user      = await activate_subscription(data["target_id"], data["days"], limit_ip)
    dev_label = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} уст.")
    if not user:
        await cb.message.edit_text("Ошибка активации в Marzban.")
        return
    expire   = user.get("expire", 0)
    date_str = time.strftime("%d.%m.%Y", time.localtime(expire)) if expire else "∞"
    await cb.message.edit_text(
        f"✅ @{data['target_username']} выдано <b>{data['days']}</b> дн. · {dev_label}\nДо: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            data["target_id"],
            f"🎁 Администратор выдал вам <b>{data['days']}</b> дней!\n\n{format_key_message(user)}",
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
#  ADMIN — промокоды
# ─────────────────────────────────────────────
async def _save_promo(message: types.Message, parts: list):
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
            await message.answer(f"Тариф <code>{value}</code> не найден.", parse_mode="HTML")
            return
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO promos (code,days,uses,promo_type,tariff_key) VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5""",
            code, days, uses, promo_type, tariff_key,
        )
    type_label = {
        "days":        "добавляет дни",
        "free_tariff": f"бесплатный тариф «{TARIFFS[tariff_key]['name']}»" if tariff_key else "",
        "free_choice": "бесплатный тариф на выбор",
    }.get(promo_type, promo_type)
    await message.answer(
        f"✅ Промокод <code>{code}</code> создан.\nТип: {type_label}\n"
        f"Дней: <b>{days}</b> · Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n<code>/add_promo КОД ДНИ [исп.]</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:1_dev</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:choice</code>",
            parse_mode="HTML",
        )
        return
    await _save_promo(message, parts)

@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminPromoState.waiting_input)
    tariff_list = "\n".join(f"  <code>{k}</code> — {v['name']}" for k, v in TARIFFS.items() if not v.get("trial"))
    await message.answer(
        "✦ <b>Генерация промокода</b>\n\n"
        "<code>КОД ДНИ [исп.]</code> — добавляет дни\n"
        "<code>КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>КОД ДНИ [исп.] free:choice</code> — на выбор\n\n"
        f"<b>Тарифы:</b>\n{tariff_list}\n\n"
        "Только число → код генерируется автоматически\n/cancel — отмена",
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
        parts = [uuid.uuid4().hex[:8].upper()] + parts
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Неверный формат. Попробуйте снова: /genpromo")
        return
    await _save_promo(message, parts)

@router.message(Command("list_promos"))
async def admin_list_promos(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code,days,uses,promo_type,tariff_key FROM promos ORDER BY promo_type,days DESC"
        )
    if not rows:
        await message.answer("Активных промокодов нет.")
        return
    lines = ["✦ <b>Активные промокоды:</b>\n"]
    for r in rows:
        ptype = r["promo_type"] or "days"
        extra = (f" · 🆓 {TARIFFS.get(r['tariff_key'] or '', {}).get('name', r['tariff_key'])}"
                 if ptype == "free_tariff" else " · 🆓 на выбор" if ptype == "free_choice" else "")
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
        "◌ <b>Рассылка</b>\n\nВведите текст (HTML поддерживается).\n/cancel — отмена.",
        parse_mode="HTML",
    )

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
        f"Предпросмотр:\n\n<b>TrubaVPN:</b>\n\n{message.text}\n\nПодтвердите рассылку:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="→ Разослать всем", callback_data="bc_confirm")],
            [InlineKeyboardButton(text="← Отмена",         callback_data="bc_cancel")],
        ]),
    )

@router.callback_query(F.data == "bc_confirm")
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    data      = await state.get_data()
    text_body = data.get("broadcast_text", "")
    await state.clear()
    if not text_body:
        await cb.answer("Текст не найден.", show_alert=True)
        return
    await cb.message.edit_text("Рассылка запущена...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    ok, fail = 0, 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], f"<b>TrubaVPN:</b>\n\n{text_body}", parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(
        f"✓ Рассылка завершена.\nОтправлено: <b>{ok}</b> · Ошибок: <b>{fail}</b>",
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
    async with pool.acquire() as conn:
        total  = await conn.fetchval("SELECT COUNT(*) FROM users")
        paid   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE has_paid=1")
        promos = await conn.fetchval("SELECT COUNT(*) FROM promos")
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{MARZBAN_URL}/api/users?status=active",
                headers=await marz_headers(), timeout=15,
            )
            active = r.json().get("total", "?")
    except Exception:
        active = "?"
    await message.answer(
        f"◎ <b>Статистика TrubaVPN</b>\n\n"
        f"Всего пользователей: <b>{total}</b>\n"
        f"Активных подписок:   <b>{active}</b>\n"
        f"Платили хоть раз:    <b>{paid}</b>\n"
        f"Активных промокодов: <b>{promos}</b>",
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
        "◎ <b>Команды администратора:</b>\n\n"
        "<code>/give username дни [устройств]</code> — выдать дни\n"
        "<code>/genkey</code> — интерактивная выдача ключа\n\n"
        "<code>/add_promo КОД ДНИ [исп.]</code> — промокод на дни\n"
        "<code>/add_promo КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>/add_promo КОД ДНИ [исп.] free:choice</code> — тариф на выбор\n"
        "<code>/genpromo</code> — интерактивная генерация\n"
        "<code>/list_promos</code> — список промокодов\n\n"
        "<code>/broadcast</code> — рассылка всем\n"
        "<code>/stats</code> — статистика",
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    await init_db()
    await get_marzban_token()
    dp.include_router(router)
    log.info("TrubaVPN Bot starting (Marzban)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
