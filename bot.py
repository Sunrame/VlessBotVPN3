import os
import uuid
import logging
import time
import asyncio
import asyncpg
import httpx
from datetime import datetime, timedelta
 
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
MARZBAN_URL  = os.environ["MARZBAN_URL"].rstrip("/")
MARZBAN_USER = os.environ["MARZBAN_USER"]
MARZBAN_PASS = os.environ["MARZBAN_PASS"]
 
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
    "trial": {"name": " Пробный",       "price": 10,  "days": 1,  "desc": "⏱️ Тестовый доступ на 24 часа", "trial": True, "limit_ip": 1},
    "1_dev": {"name": "1 устройство",  "price": 99,  "days": 30, "desc": "🔒 Безлимитный трафик\n\n🌐 Высокая скорость", "limit_ip": 1},
    "2_dev": {"name": "2 устройства","price": 179, "days": 30, "desc": "🔒 Безлимитный трафик\n\n🌐 Высокая скорость", "limit_ip": 2},
    "5_dev": {"name": "5 устройств",  "price": 349, "days": 30, "desc": "🔒 Безлимитный трафик\n\n🌐 Высокая скорость", "limit_ip": 5},
}
 
MONTH_OPTIONS = {
    1:  {"label": "1 месяц",   "multiplier": 1.0},
    3:  {"label": "3 месяца",  "multiplier": 2.7},
    6:  {"label": "6 месяцев", "multiplier": 5.1},
    12: {"label": "1 год",     "multiplier": 9.6},
}
 
DEVICE_OPTIONS = {
    0: "Без лимита", 1: "1 устройство", 2: "2 устройства",
    3: "3 устройства", 5: "5 устройств", 10: "10 устройств",
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
 
class SupportState(StatesGroup):
    waiting_message  = State()   # пользователь пишет в поддержку
    admin_reply      = State()   # админ отвечает на тикет
 
class TemplateState(StatesGroup):
    waiting_name = State()
    waiting_text = State()
 
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
                marzban_username TEXT,
                created_at       BIGINT  DEFAULT 0
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
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                amount      NUMERIC,
                tariff_key  TEXT,
                days        INTEGER,
                is_trial    BOOLEAN DEFAULT FALSE,
                created_at  BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                username    TEXT,
                status      TEXT DEFAULT 'open',
                created_at  BIGINT DEFAULT 0,
                updated_at  BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id        SERIAL PRIMARY KEY,
                ticket_id INTEGER,
                user_id   BIGINT,
                is_admin  BOOLEAN DEFAULT FALSE,
                text      TEXT,
                sent_at   BIGINT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                admin_id    BIGINT PRIMARY KEY,
                dnd         BOOLEAN DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id      SERIAL PRIMARY KEY,
                name    TEXT,
                text    TEXT,
                admin_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date        TEXT PRIMARY KEY,
                new_users   INTEGER DEFAULT 0,
                new_trials  INTEGER DEFAULT 0,
                new_paid    INTEGER DEFAULT 0,
                revenue     NUMERIC DEFAULT 0
            )
        """)
        # Миграции
        for col in ["created_at BIGINT DEFAULT 0"]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
    log.info("PostgreSQL ready.")
 
# ─────────────────────────────────────────────
#  MARZBAN API
# ─────────────────────────────────────────────
_marzban_token: str   = ""
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
        "username": marz_username(user_id),
        "proxies":  {"vless": {"flow": "xtls-rprx-vision"}},
        "inbounds": {"vless": ["VLESS TCP REALITY"]},
        "expire":   expire_ts, "data_limit": 0,
        "ip_limit": limit_ip, "status": "active",
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
 
async def marzban_get_all_users() -> list:
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{MARZBAN_URL}/api/users?limit=1000",
                headers=await marz_headers(), timeout=30,
            )
            r.raise_for_status()
            return r.json().get("users", [])
    except Exception as e:
        log.error("[Marzban] get_all_users: %s", e)
        return []
 
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
            hcode(full_sub), "",
        ]
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"Инструкция по подключению: {CHANNEL_LINK}"]
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
 
async def record_daily_stats(amount: float = 0, is_trial: bool = False, is_new_user: bool = False):
    date = datetime.now().strftime("%Y-%m-%d")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO daily_stats (date, new_users, new_trials, new_paid, revenue)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (date) DO UPDATE SET
                new_users  = daily_stats.new_users  + $2,
                new_trials = daily_stats.new_trials + $3,
                new_paid   = daily_stats.new_paid   + $4,
                revenue    = daily_stats.revenue    + $5
        """, date,
            1 if is_new_user else 0,
            1 if is_trial else 0,
            1 if (not is_trial and amount > 0) else 0,
            amount if not is_trial else 0,
        )
 
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
        [InlineKeyboardButton(text="← Назад", callback_data="back")]
    ])
 
def sub_required_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
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
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="gk_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
 
def support_ticket_kb(ticket_id: int, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"sreply_{ticket_id}_{user_id}")],
        [InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"sclose_{ticket_id}")],
        [InlineKeyboardButton(text="📋 Шаблоны", callback_data=f"stmpl_{ticket_id}_{user_id}")],
    ])
 
def support_user_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть обращение", callback_data="support_close_user")],
    ])
 
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
 
    now = int(time.time())
    async with pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", u_id)
        if not exists:
            await conn.execute(
                "INSERT INTO users (user_id, username, referrer_id, created_at) VALUES ($1,$2,$3,$4)",
                u_id, message.from_user.username, r_id, now,
            )
            await record_daily_stats(is_new_user=True)
        else:
            await conn.execute("UPDATE users SET username=$1 WHERE user_id=$2",
                               message.from_user.username, u_id)
 
    if not await is_subscribed(u_id):
        await message.answer(
            f"🌏 {hbold('TrubaVPN')}\n\nЧтобы пользоваться ботом, подпишитесь на наш канал.",
            reply_markup=sub_required_kb(), parse_mode="HTML",
        )
        return
 
    await message.answer(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\nВыберите действие:",
        reply_markup=main_kb(), parse_mode="HTML",
    )
 
@router.callback_query(F.data == "check_sub")
async def check_sub_callback(cb: CallbackQuery):
    await cb.answer()
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Вы ещё не подписаны.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🌏 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\nВыберите действие:",
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
    info        = TARIFFS[t_key]
    days        = calc_days(info["days"], months)
    price       = calc_price(info["price"], months) if not info.get("trial") else info["price"]
    month_label = "24 часа" if info.get("trial") else MONTH_OPTIONS.get(months, {}).get("label", f"{months} мес.")
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
                "price":      str(price),
                "is_trial":   "1" if info.get("trial") else "0",
            },
        }, str(uuid.uuid4()))
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",        url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="← Назад",            callback_data=f"buy_{t_key}")],
    ])
    await cb.message.edit_text(
        f"<b>{info['name']}</b>  ·  {month_label}\n\n{info['desc']}\n\n"
        f"💰 К оплате: <b>{price} ₽</b>\n\nПосле оплаты нажмите «Проверить оплату».",
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
    price    = float(payment.metadata.get("price", 0))
    t_key    = payment.metadata.get("tariff_key", "")
    is_trial = payment.metadata.get("is_trial", "0") == "1"
 
    user = await activate_subscription(u_id, days, limit_ip)
    if not user:
        await cb.answer("Ошибка активации. Напишите в поддержку.", show_alert=True)
        return
 
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, created_at) VALUES ($1,$2,$3,$4,$5,$6)",
            u_id, price, t_key, days, is_trial, int(time.time()),
        )
        row = await conn.fetchrow("SELECT referrer_id, has_paid FROM users WHERE user_id=$1", u_id)
        if row and row["referrer_id"] and row["has_paid"] == 0:
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
            "UPDATE users SET has_paid=1, marzban_username=$1 WHERE user_id=$2",
            marz_username(u_id), u_id,
        )
    await record_daily_stats(amount=price, is_trial=is_trial)
    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n{format_key_message(user)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )
 
# ─────────────────────────────────────────────
#  /subs — подписки пользователя
# ─────────────────────────────────────────────
@router.message(Command("subs"))
async def cmd_subs(message: types.Message):
    user = await marzban_get_user(message.from_user.id)
    now  = int(time.time())
    if not user or (user.get("expire") or 0) <= now:
        await message.answer(
            "❌ У вас нет активной подписки.\nНажмите /start чтобы купить.",
        )
        return
    expire   = user["expire"]
    days_left = (expire - now) // 86400
    date_str  = time.strftime("%d.%m.%Y %H:%M", time.localtime(expire))
    sub_url   = user.get("subscription_url", "")
    full_sub  = sub_url if sub_url.startswith("http") else f"{MARZBAN_URL}{sub_url}"
    used_gb   = round((user.get("used_traffic", 0) or 0) / 1024**3, 2)
 
    text = (
        f"📋 <b>Ваша подписка</b>\n\n"
        f"✅ Статус: <b>Активна</b>\n"
        f"📅 До: <b>{date_str}</b>\n"
        f"⏳ Осталось: <b>{days_left} дн.</b>\n"
        f"📊 Использовано: <b>{used_gb} GB</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Ссылка на подписку:</b>\n{hcode(full_sub)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 Инструкция: {CHANNEL_LINK}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить", callback_data="tariffs")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
 
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
    days = row["days"]; uses = row["uses"]
    if promo_type == "free_tariff" and tariff_key and tariff_key in TARIFFS:
        await state.clear()
        info     = TARIFFS[tariff_key]
        user     = await activate_subscription(message.from_user.id, days, info.get("limit_ip", 0))
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
    days = data.get("promo_days", 30); uses = data.get("promo_uses", 1)
    await state.clear()
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    info = TARIFFS[t_key]
    user = await activate_subscription(cb.from_user.id, days, info.get("limit_ip", 0))
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
            f"⏳ Осталось: <b>{days_left} дн.</b>{sub_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n❌ Подписка не активна.\n"
            "Нажмите «💰 Купить VPN» для оформления."
        )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
 
# ─────────────────────────────────────────────
#  ПОДДЕРЖКА — пользователь
# ─────────────────────────────────────────────
@router.callback_query(F.data == "support_open")
async def support_open(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    # Проверяем есть ли открытый тикет
    async with pool.acquire() as conn:
        ticket = await conn.fetchrow(
            "SELECT id FROM support_tickets WHERE user_id=$1 AND status='open'",
            cb.from_user.id
        )
    if ticket:
        await cb.message.edit_text(
            "💬 <b>Поддержка</b>\n\nУ вас уже есть открытое обращение.\n"
            "Напишите ваш вопрос — мы ответим в ближайшее время:",
            parse_mode="HTML", reply_markup=support_user_kb(),
        )
        await state.set_state(SupportState.waiting_message)
        await state.update_data(ticket_id=ticket["id"])
        return
 
    await state.set_state(SupportState.waiting_message)
    await cb.message.edit_text(
        "💬 <b>Поддержка</b>\n\nОпишите вашу проблему — мы ответим в ближайшее время:",
        parse_mode="HTML", reply_markup=support_user_kb(),
    )
 
@router.callback_query(F.data == "support_close_user")
async def support_close_user(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if ticket_id:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE support_tickets SET status='closed', updated_at=$1 WHERE id=$2",
                int(time.time()), ticket_id,
            )
    await cb.message.edit_text(
        f"🌏 {hbold('TrubaVPN')} — быстрый и надёжный VPN.",
        reply_markup=main_kb(), parse_mode="HTML",
    )
 
@router.message(SupportState.waiting_message)
async def support_user_message(message: types.Message, state: FSMContext):
    u_id = message.from_user.id
    data = await state.get_data()
    now  = int(time.time())
 
    async with pool.acquire() as conn:
        ticket_id = data.get("ticket_id")
        if not ticket_id:
            # Создаём новый тикет
            ticket_id = await conn.fetchval(
                "INSERT INTO support_tickets (user_id, username, status, created_at, updated_at) "
                "VALUES ($1,$2,'open',$3,$3) RETURNING id",
                u_id, message.from_user.username or str(u_id), now,
            )
            await state.update_data(ticket_id=ticket_id)
        else:
            await conn.execute(
                "UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id
            )
 
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,FALSE,$3,$4)",
            ticket_id, u_id, message.text, now,
        )
 
    await message.answer(
        "✅ Ваше сообщение отправлено в поддержку. Ожидайте ответа.",
        reply_markup=support_user_kb(),
    )
 
    # Уведомляем всех админов (у кого нет DND)
    uname = f"@{message.from_user.username}" if message.from_user.username else f"ID:{u_id}"
    notif = (
        f"📨 <b>Новое сообщение в поддержку</b>\n\n"
        f"От: {uname}\n"
        f"Тикет: #{ticket_id}\n\n"
        f"<b>Сообщение:</b>\n{message.text}"
    )
    for admin_id in ADMIN_IDS:
        if not await is_admin_dnd(admin_id):
            try:
                await bot.send_message(
                    admin_id, notif,
                    parse_mode="HTML",
                    reply_markup=support_ticket_kb(ticket_id, u_id),
                )
            except Exception:
                pass
 
# ─────────────────────────────────────────────
#  ПОДДЕРЖКА — админ отвечает
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("sreply_"))
async def admin_reply_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    _, ticket_id_str, user_id_str = cb.data.split("_", 2)
    ticket_id = int(ticket_id_str)
    user_id   = int(user_id_str)
    await state.set_state(SupportState.admin_reply)
    await state.update_data(ticket_id=ticket_id, reply_to_user=user_id)
    await cb.message.answer(
        f"✍️ Введите ответ для тикета #{ticket_id}:\n/cancel — отмена",
    )
 
@router.message(Command("cancel"), SupportState.admin_reply)
async def admin_reply_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")
 
@router.message(SupportState.admin_reply)
async def admin_reply_send(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data      = await state.get_data()
    ticket_id = data["ticket_id"]
    user_id   = data["reply_to_user"]
    await state.clear()
 
    now = int(time.time())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,TRUE,$3,$4)",
            ticket_id, message.from_user.id, message.text, now,
        )
        await conn.execute(
            "UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id,
        )
 
    # Отправляем ответ пользователю
    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ поддержки</b> (тикет #{ticket_id}):\n\n{message.text}",
            parse_mode="HTML",
            reply_markup=support_user_kb(),
        )
    except Exception as e:
        await message.answer(f"Не удалось доставить ответ пользователю: {e}")
        return
 
    await message.answer(f"✅ Ответ на тикет #{ticket_id} отправлен.")
 
@router.callback_query(F.data.startswith("sclose_"))
async def admin_close_ticket(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    ticket_id = int(cb.data.removeprefix("sclose_"))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM support_tickets WHERE id=$1", ticket_id
        )
        await conn.execute(
            "UPDATE support_tickets SET status='closed', updated_at=$1 WHERE id=$2",
            int(time.time()), ticket_id,
        )
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"✅ Тикет #{ticket_id} закрыт.")
    if row:
        try:
            await bot.send_message(
                row["user_id"],
                f"✅ Ваше обращение #{ticket_id} закрыто. Если остались вопросы — напишите снова.",
            )
        except Exception:
            pass
 
# ─────────────────────────────────────────────
#  ШАБЛОНЫ — выбор при ответе
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("stmpl_"))
async def admin_show_templates(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    parts     = cb.data.split("_")
    ticket_id = int(parts[1])
    user_id   = int(parts[2])
 
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM templates ORDER BY id")
 
    if not rows:
        await cb.answer("Шаблонов нет. Создайте через /add_template", show_alert=True)
        return
 
    btns = []
    for r in rows:
        btns.append([InlineKeyboardButton(
            text=r["name"],
            callback_data=f"useTmpl_{r['id']}_{ticket_id}_{user_id}"
        )])
    await cb.message.answer(
        "📋 Выберите шаблон:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )
 
@router.callback_query(F.data.startswith("useTmpl_"))
async def admin_use_template(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        return
    parts     = cb.data.split("_")
    tmpl_id   = int(parts[1])
    ticket_id = int(parts[2])
    user_id   = int(parts[3])
 
    async with pool.acquire() as conn:
        tmpl = await conn.fetchrow("SELECT text FROM templates WHERE id=$1", tmpl_id)
        if not tmpl:
            await cb.answer("Шаблон не найден.", show_alert=True)
            return
        now = int(time.time())
        await conn.execute(
            "INSERT INTO support_messages (ticket_id, user_id, is_admin, text, sent_at) VALUES ($1,$2,TRUE,$3,$4)",
            ticket_id, cb.from_user.id, tmpl["text"], now,
        )
        await conn.execute(
            "UPDATE support_tickets SET updated_at=$1 WHERE id=$2", now, ticket_id,
        )
 
    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ поддержки</b> (тикет #{ticket_id}):\n\n{tmpl['text']}",
            parse_mode="HTML",
            reply_markup=support_user_kb(),
        )
    except Exception:
        pass
 
    await cb.message.edit_text(f"✅ Шаблон отправлен в тикет #{ticket_id}.")
 
# ─────────────────────────────────────────────
#  ШАБЛОНЫ — управление
# ─────────────────────────────────────────────
@router.message(Command("add_template"))
async def add_template_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(TemplateState.waiting_name)
    await message.answer("📋 Введите название шаблона (короткое, например «Как подключиться»):")
 
@router.message(TemplateState.waiting_name)
async def template_name(message: types.Message, state: FSMContext):
    await state.update_data(template_name=message.text.strip())
    await state.set_state(TemplateState.waiting_text)
    await message.answer("✍️ Теперь введите текст шаблона (то что будет отправлено пользователю):")
 
@router.message(TemplateState.waiting_text)
async def template_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO templates (name, text, admin_id) VALUES ($1,$2,$3)",
            data["template_name"], message.text, message.from_user.id,
        )
    await message.answer(f"✅ Шаблон «{data['template_name']}» создан.")
 
@router.message(Command("list_templates"))
async def list_templates(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, text FROM templates ORDER BY id")
    if not rows:
        await message.answer("Шаблонов нет. Создайте через /add_template")
        return
    lines = ["📋 <b>Шаблоны:</b>\n"]
    for r in rows:
        preview = r["text"][:50] + "..." if len(r["text"]) > 50 else r["text"]
        lines.append(f"<b>#{r['id']}</b> {r['name']}\n<i>{preview}</i>")
    await message.answer("\n\n".join(lines), parse_mode="HTML")
 
@router.message(Command("del_template"))
async def del_template(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args or not command.args.isdigit():
        await message.answer("Формат: <code>/del_template ID</code>", parse_mode="HTML")
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM templates WHERE id=$1", int(command.args))
    await message.answer(f"✅ Шаблон #{command.args} удалён.")
 
# ─────────────────────────────────────────────
#  DND — не беспокоить
# ─────────────────────────────────────────────
@router.message(Command("dnd"))
async def toggle_dnd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    admin_id = message.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dnd FROM admin_settings WHERE admin_id=$1", admin_id)
        if row:
            new_dnd = not row["dnd"]
            await conn.execute("UPDATE admin_settings SET dnd=$1 WHERE admin_id=$2", new_dnd, admin_id)
        else:
            new_dnd = True
            await conn.execute("INSERT INTO admin_settings (admin_id, dnd) VALUES ($1,$2)", admin_id, new_dnd)
    status = "🔕 Режим «Не беспокоить» включён" if new_dnd else "🔔 Уведомления поддержки включены"
    await message.answer(status)
 
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
#  ЕЖЕДНЕВНЫЙ ОТЧЁТ
# ─────────────────────────────────────────────
async def send_daily_report():
    now  = int(time.time())
    date = datetime.now().strftime("%d.%m.%Y")
    today_str = datetime.now().strftime("%Y-%m-%d")
 
    async with pool.acquire() as conn:
        # Статистика за день
        day_row = await conn.fetchrow(
            "SELECT * FROM daily_stats WHERE date=$1", today_str
        )
        new_users  = day_row["new_users"]  if day_row else 0
        new_trials = day_row["new_trials"] if day_row else 0
        new_paid   = day_row["new_paid"]   if day_row else 0
        revenue    = float(day_row["revenue"] if day_row else 0)
 
        # Конверсия
        conversion = round(new_paid / new_trials * 100, 1) if new_trials > 0 else 0
 
        # Активные подписки из Marzban
        all_users = await marzban_get_all_users()
        active_paid   = sum(1 for u in all_users if u.get("expire", 0) > now)
        active_trials = 0  # упрощённо
 
        # Финансы за день
        pay_rows = await conn.fetch(
            "SELECT COUNT(*) as cnt, SUM(amount) as total FROM payments WHERE created_at>$1 AND NOT is_trial",
            now - 86400,
        )
        pay_cnt   = pay_rows[0]["cnt"]   if pay_rows else 0
        pay_total = float(pay_rows[0]["total"] or 0) if pay_rows else 0
 
        # Поддержка
        new_tickets = await conn.fetchval(
            "SELECT COUNT(*) FROM support_tickets WHERE created_at>$1", now - 86400
        )
        open_tickets = await conn.fetchval(
            "SELECT COUNT(*) FROM support_tickets WHERE status='open'"
        )
 
        # Пользователи с активной платной подпиской
        paid_active = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE has_paid=1"
        )
 
        # Топ рефералы за день
        top_refs = await conn.fetch(
            """SELECT referrer_id, COUNT(*) as cnt FROM users
               WHERE referrer_id IS NOT NULL AND created_at>$1
               GROUP BY referrer_id ORDER BY cnt DESC LIMIT 3""",
            now - 86400,
        )
 
    report = (
        f"📊 <b>Отчёт за {date}</b>\n\n"
        f"⏱ <b>Итог по периоду</b>\n"
        f"• Новых пользователей: <b>{new_users}</b>\n"
        f"• Новых триалов: <b>{new_trials}</b>\n"
        f"• Конверсий триал → платная: <b>{new_paid} ({conversion}%)</b>\n"
        f"• Новых платных (всего): <b>{new_paid}</b>\n"
        f"• Поступлений: <b>{revenue:.2f} ₽</b>\n\n"
        f"💎 <b>Подписки</b>\n"
        f"• Активных сейчас: <b>{active_paid}</b>\n\n"
        f"💰 <b>Финансы</b>\n"
        f"• Оплаты подписок: <b>{pay_cnt} на сумму {pay_total:.2f} ₽</b>\n\n"
        f"🎫 <b>Поддержка</b>\n"
        f"• Новых тикетов: <b>{new_tickets}</b>\n"
        f"• Активных тикетов: <b>{open_tickets}</b>\n\n"
        f"👤 <b>Активность</b>\n"
        f"• Пользователей с активной подпиской: <b>{paid_active}</b>\n"
    )
 
    if top_refs:
        report += "\n🏆 <b>Топ по рефералам (за период)</b>\n"
        for i, r in enumerate(top_refs, 1):
            async with pool.acquire() as conn:
                ref_user = await conn.fetchrow(
                    "SELECT username FROM users WHERE user_id=$1", r["referrer_id"]
                )
            uname = f"@{ref_user['username']}" if ref_user and ref_user["username"] else f"ID:{r['referrer_id']}"
            report += f"{i}. {uname}: <b>{r['cnt']} приглашений</b>\n"
 
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report, parse_mode="HTML")
        except Exception:
            pass
 
async def daily_report_scheduler():
    """Отправляет отчёт каждый день в 23:00."""
    while True:
        now   = datetime.now()
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        await send_daily_report()
 
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
            "Формат: <code>/give username дни [устройств]</code>", parse_mode="HTML"
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
        await state.clear(); await cb.message.edit_text("Отменено."); return
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
        await state.clear(); await cb.answer("Сессия истекла.", show_alert=True); return
    await state.clear()
    user      = await activate_subscription(data["target_id"], data["days"], limit_ip)
    dev_label = DEVICE_OPTIONS.get(limit_ip, f"{limit_ip} уст.")
    if not user:
        await cb.message.edit_text("Ошибка активации в Marzban."); return
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
    await cb.answer(); await state.clear(); await cb.message.edit_text("Отменено.")
 
# ─────────────────────────────────────────────
#  ADMIN — промокоды
# ─────────────────────────────────────────────
async def _save_promo(message: types.Message, parts: list):
    code = parts[0].upper(); days = int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
    free_arg = next((p for p in parts if p.startswith("free:")), None)
    promo_type = "days"; tariff_key = None
    if free_arg:
        value = free_arg.removeprefix("free:")
        if value == "choice":
            promo_type = "free_choice"
        elif value in TARIFFS:
            promo_type = "free_tariff"; tariff_key = value
        else:
            await message.answer(f"Тариф <code>{value}</code> не найден.", parse_mode="HTML"); return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promos (code,days,uses,promo_type,tariff_key) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (code) DO UPDATE SET days=$2,uses=$3,promo_type=$4,tariff_key=$5",
            code, days, uses, promo_type, tariff_key,
        )
    type_label = {"days": "добавляет дни",
                  "free_tariff": f"бесплатный тариф «{TARIFFS[tariff_key]['name']}»" if tariff_key else "",
                  "free_choice": "бесплатный тариф на выбор"}.get(promo_type, promo_type)
    await message.answer(
        f"✅ Промокод <code>{code}</code> создан.\nТип: {type_label}\n"
        f"Дней: <b>{days}</b> · Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )
 
@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Форматы:\n<code>/add_promo КОД ДНИ [исп.]</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:1_dev</code>\n"
            "<code>/add_promo КОД ДНИ [исп.] free:choice</code>",
            parse_mode="HTML",
        ); return
    await _save_promo(message, parts)
 
@router.message(Command("genpromo"))
async def admin_genpromo(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminPromoState.waiting_input)
    tariff_list = "\n".join(f"  <code>{k}</code> — {v['name']}" for k, v in TARIFFS.items() if not v.get("trial"))
    await message.answer(
        "✦ <b>Генерация промокода</b>\n\n"
        "<code>КОД ДНИ [исп.]</code> — добавляет дни\n"
        "<code>КОД ДНИ [исп.] free:ТАРИФ</code> — бесплатный тариф\n"
        "<code>КОД ДНИ [исп.] free:choice</code> — на выбор\n\n"
        f"<b>Тарифы:</b>\n{tariff_list}\n\nТолько число → код авто\n/cancel — отмена",
        parse_mode="HTML",
    )
 
@router.message(Command("cancel"), AdminPromoState.waiting_input)
async def admin_genpromo_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Отменено.")
 
@router.message(AdminPromoState.waiting_input)
async def admin_genpromo_handle(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.strip().split()
    if parts[0].isdigit():
        parts = [uuid.uuid4().hex[:8].upper()] + parts
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Неверный формат."); return
    await _save_promo(message, parts)
 
@router.message(Command("list_promos"))
async def admin_list_promos(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code,days,uses,promo_type,tariff_key FROM promos ORDER BY promo_type,days DESC"
        )
    if not rows:
        await message.answer("Активных промокодов нет."); return
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
            [InlineKeyboardButton(text="→ Разослать всем", callback_data="bc_confirm")],
            [InlineKeyboardButton(text="← Отмена",         callback_data="bc_cancel")],
        ]),
    )
 
@router.callback_query(F.data == "bc_confirm")
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True); return
    data = await state.get_data(); text_body = data.get("broadcast_text", "")
    await state.clear()
    if not text_body:
        await cb.answer("Текст не найден.", show_alert=True); return
    await cb.message.edit_text("Рассылка запущена...")
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    ok = fail = 0
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
    await cb.answer(); await state.clear(); await cb.message.edit_text("Рассылка отменена.")
 
# ─────────────────────────────────────────────
#  ADMIN — /stats
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
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{MARZBAN_URL}/api/users?status=active",
                headers=await marz_headers(), timeout=15,
            )
            active = r.json().get("total", "?")
    except Exception:
        active = "?"
    dnd = await is_admin_dnd(message.from_user.id)
    dnd_status = "🔕 ВКЛ" if dnd else "🔔 ВЫКЛ"
    await message.answer(
        f"◎ <b>Статистика TrubaVPN</b>\n\n"
        f"Всего пользователей: <b>{total}</b>\n"
        f"Активных подписок:   <b>{active}</b>\n"
        f"Платили хоть раз:    <b>{paid}</b>\n"
        f"Активных промокодов: <b>{promos}</b>\n"
        f"Открытых тикетов:    <b>{open_t}</b>\n\n"
        f"DND режим: {dnd_status} (переключить: /dnd)",
        parse_mode="HTML",
    )
 
# ─────────────────────────────────────────────
#  ADMIN — /tickets
# ─────────────────────────────────────────────
@router.message(Command("tickets"))
async def admin_tickets(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_id, username, created_at FROM support_tickets "
            "WHERE status='open' ORDER BY created_at DESC LIMIT 10"
        )
    if not rows:
        await message.answer("Открытых тикетов нет. 🎉"); return
    lines = ["🎫 <b>Открытые тикеты:</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        date  = time.strftime("%d.%m %H:%M", time.localtime(r["created_at"]))
        lines.append(f"#{r['id']} · {uname} · {date}")
    await message.answer("\n".join(lines), parse_mode="HTML")
 
# ─────────────────────────────────────────────
#  ADMIN — /report (ручной запрос отчёта)
# ─────────────────────────────────────────────
@router.message(Command("report"))
async def admin_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("⏳ Формирую отчёт...")
    await send_daily_report()
 
# ─────────────────────────────────────────────
#  ADMIN — /admin
# ─────────────────────────────────────────────
@router.message(Command("admin"))
async def admin_help(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer(
        "◎ <b>Команды администратора:</b>\n\n"
        "👤 <b>Подписки:</b>\n"
        "<code>/give username дни [устройств]</code>\n"
        "<code>/genkey</code> — интерактивно\n\n"
        "🎟 <b>Промокоды:</b>\n"
        "<code>/add_promo КОД ДНИ [исп.]</code>\n"
        "<code>/add_promo КОД ДНИ [исп.] free:ТАРИФ</code>\n"
        "<code>/add_promo КОД ДНИ [исп.] free:choice</code>\n"
        "<code>/genpromo</code> · <code>/list_promos</code>\n\n"
        "💬 <b>Поддержка:</b>\n"
        "<code>/tickets</code> — открытые тикеты\n"
        "<code>/dnd</code> — режим «Не беспокоить»\n"
        "<code>/add_template</code> — создать шаблон\n"
        "<code>/list_templates</code> — список шаблонов\n"
        "<code>/del_template ID</code> — удалить шаблон\n\n"
        "📊 <b>Статистика:</b>\n"
        "<code>/stats</code> — текущая статистика\n"
        "<code>/report</code> — отчёт за день\n"
        "<code>/broadcast</code> — рассылка",
        parse_mode="HTML",
    )
 
# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    await init_db()
    await get_marzban_token()
    dp.include_router(router)
    asyncio.create_task(daily_report_scheduler())
    log.info("TrubaVPN Bot starting (Marzban + Support + Reports)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
 
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
