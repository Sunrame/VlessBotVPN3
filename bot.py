import os
import uuid
import logging
import time
import sqlite3
import asyncio
import urllib3
import aiohttp
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

API_TOKEN      = os.environ["BOT_TOKEN"]
SHOP_ID        = os.environ["SHOP_ID"]
YOOKASSA_KEY   = os.environ["YOOKASSA_KEY"]
PAYMENT_TOKEN  = os.environ.get("PAYMENT_TOKEN", "")

PANEL_URL      = os.environ["PANEL_URL"].rstrip("/")
PANEL_LOGIN    = os.environ["PANEL_LOGIN"]
PANEL_PASSWORD = os.environ["PANEL_PASSWORD"]
SUB_PORT       = os.environ.get("SUB_PORT", "2096")

ADMIN_IDS = []
for key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    val = os.environ.get(key, "")
    if val.isdigit():
        ADMIN_IDS.append(int(val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/yourchannel")

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

TARIFFS = {
    "trial": {"name": "🆓 Пробный (1 день)", "price": 10, "days": 1},
    "1_dev": {"name": "📱 1 устройство", "price": 99, "days": 30},
    "2_dev": {"name": "📱📱 2 устройства", "price": 179, "days": 30},
    "5_dev": {"name": "💻 5 устройств", "price": 349, "days": 30},
}

class PromoState(StatesGroup):
    waiting_code = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vpn")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
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
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                referrer_id INTEGER,
                expiry_date INTEGER DEFAULT 0,
                sub_token TEXT,
                has_paid INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses INTEGER DEFAULT 1
            );
        """)

def safe_json_or_dict(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except:
            return None
    return None

async def safe_json_response(resp):
    try:
        text = await resp.text()
    except:
        return None
    try:
        return json.loads(text)
    except:
        return None

async def panel_create_client(user_id: int, days: int):
    base = PANEL_URL
    email = f"user_{user_id}"
    expire_ms = int((time.time() + days * 86400) * 1000)
    client_id = str(uuid.uuid4())

    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=20),
            connector=aiohttp.TCPConnector(ssl=False)
        ) as s:

            r = await s.post(f"{base}/login", data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD})
            login = await safe_json_response(r)
            if not login or not login.get("success"):
                return None

            r = await s.get(f"{base}/xui/inbound/list")
            data = await safe_json_response(r)
            if not data or not data.get("success"):
                return None

            inbounds = data.get("obj") or []
            inbound = next((i for i in inbounds if i.get("id") == 2), None)
            if not inbound:
                inbound = next((i for i in inbounds if i.get("protocol") == "vless"), None)
            if not inbound:
                return None

            inbound_id = inbound.get("id")
            port = inbound.get("port")

            settings = safe_json_or_dict(inbound.get("settings")) or {}
            clients = settings.get("clients") or []

            client_data = {
                "id": client_id,
                "email": email,
                "expiryTime": expire_ms,
                "enable": True,
                "flow": clients[0].get("flow", "") if clients else ""
            }

            new_settings = {**settings, "clients": clients + [client_data]}

            payload = {
                "id": inbound_id,
                "settings": json.dumps(new_settings, ensure_ascii=False)
            }

            r = await s.post(f"{base}/xui/inbound/addClient", json=payload)
            add = await safe_json_response(r)
            if not add or not add.get("success"):
                return None

            stream = safe_json_or_dict(inbound.get("streamSettings"))
            if not stream:
                return None

            host = os.environ.get("VPN_HOST") or base.split("//")[-1].split(":")[0]
            security = stream.get("security", "none")
            network = stream.get("network", "tcp")

            link = f"vless://{client_id}@{host}:{port}?type={network}&security={security}"

            if security == "reality":
                rs = stream.get("realitySettings", {}) or {}
                pbk = rs.get("publicKey", "")
                sni = (rs.get("serverNames") or [""])[0]
                sid = (rs.get("shortIds") or [""])[0]
                link += f"&pbk={pbk}&sni={sni}&sid={sid}&fp=chrome"

            if client_data["flow"]:
                link += f"&flow={client_data['flow']}"

            link += f"#{email}"
            return link

    except:
        return None

async def activate_subscription(user_id: int, days: int):
    now = int(time.time())
    delta = days * 86400

    with db_conn() as conn:
        row = conn.execute("SELECT expiry_date, sub_token FROM users WHERE user_id = ?", (user_id,)).fetchone()
        current_expiry = row["expiry_date"] if row else 0
        token = row["sub_token"]

        new_expiry = max(current_expiry, now) + delta

        if not token:
            token = await panel_create_client(user_id, days)
            if not token:
                token = f"local_{uuid.uuid4().hex[:10]}"

        conn.execute("UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?", (new_expiry, token, user_id))

    return new_expiry, token

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить VPN", callback_data="tariffs"),
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы", callback_data="ref_program"),
         InlineKeyboardButton(text="🎟 Промокод", callback_data="promo_enter")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support_tab"),
         InlineKeyboardButton(text="📖 Инфо", callback_data="info_tab")],
    ])

def back_kb(target="back"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]])

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    u_id = message.from_user.id
    r_id = None

    if command.args and command.args.isdigit():
        if int(command.args) != u_id:
            r_id = int(command.args)

    with db_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (u_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
                         (u_id, message.from_user.username, r_id))
        else:
            conn.execute("UPDATE users SET username = ? WHERE user_id = ?",
                         (message.from_user.username, u_id))

    await message.answer(f"🚀 Добро пожаловать в {hbold('VPN')}!", reply_markup=main_kb(), parse_mode="HTML")
@router.callback_query(F.data == "tariffs")
async def show_tariffs(cb: CallbackQuery):
    btns = [
        [InlineKeyboardButton(text=f"{v['name']} — {v['price']} ₽", callback_data=f"buy_{k}")]
        for k, v in TARIFFS.items()
    ]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await cb.message.edit_text(
        "🛒 <b>Выберите тариф:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("buy_"))
async def process_buy(cb: CallbackQuery):
    t_key = cb.data.removeprefix("buy_")
    if t_key not in TARIFFS:
        return
    info = TARIFFS[t_key]
    try:
        payment = Payment.create({
            "amount": {"value": f"{info['price']}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture": True,
            "description": f"VPN — {info['name']}",
            "metadata": {"user_id": str(cb.from_user.id), "days": str(info["days"])},
        }, str(uuid.uuid4()))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")],
        ])
        await cb.message.edit_text(
            f"💳 <b>{info['name']}</b>\nК оплате: <b>{info['price']} ₽</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except:
        await cb.answer("Ошибка создания платежа")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(cb: CallbackQuery):
    pay_id = cb.data.removeprefix("check_")
    try:
        payment = Payment.find_one(pay_id)
        if payment.status != "succeeded":
            await cb.answer("⏳ Платёж ещё не подтверждён", show_alert=True)
            return
        u_id = int(payment.metadata["user_id"])
        days = int(payment.metadata["days"])
        expiry, token = await activate_subscription(u_id, days)
        with db_conn() as conn:
            row = conn.execute("SELECT referrer_id, has_paid FROM users WHERE user_id = ?", (u_id,)).fetchone()
            if row and row["referrer_id"] and row["has_paid"] == 0:
                await activate_subscription(row["referrer_id"], 7)
                try:
                    await bot.send_message(row["referrer_id"], "🎊 Ваш друг оплатил VPN! Вам +7 дней.")
                except:
                    pass
            conn.execute("UPDATE users SET has_paid = 1 WHERE user_id = ?", (u_id,))
        date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))
        await cb.message.edit_text(
            f"✅ Оплачено!\n📅 До: {date_str}\n🔑 Ключ: <code>{token}</code>",
            parse_mode="HTML",
            reply_markup=back_kb()
        )
    except:
        await cb.answer("Ошибка проверки")

@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text("🎟 Введите промокод:", reply_markup=back_kb("promo_cancel"))

@router.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(cb)

@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    with db_conn() as conn:
        row = conn.execute("SELECT days, uses FROM promos WHERE code = ?", (code,)).fetchone()
        if not row:
            await message.answer("❌ Неверный промокод")
            return
        expiry, _ = await activate_subscription(message.from_user.id, row["days"])
        if row["uses"] <= 1:
            conn.execute("DELETE FROM promos WHERE code = ?", (code,))
        else:
            conn.execute("UPDATE promos SET uses = uses - 1 WHERE code = ?", (code,))
    await state.clear()
    await message.answer(
        f"✅ Промокод активирован! До {time.strftime('%d.%m.%Y', time.localtime(expiry))}",
        reply_markup=main_kb()
    )

@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    with db_conn() as conn:
        row = conn.execute("SELECT expiry_date, sub_token FROM users WHERE user_id = ?", (cb.from_user.id,)).fetchone()
    now = int(time.time())
    if row and row["expiry_date"] > now:
        text = (
            f"👤 <b>Профиль</b>\n"
            f"📅 До: {time.strftime('%d.%m.%Y', time.localtime(row['expiry_date']))}\n"
            f"🔑 Ключ: <code>{row['sub_token']}</code>"
        )
    else:
        text = "👤 <b>Профиль</b>\n❌ Подписка отсутствует"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 Приглашай друзей и получай +7 дней!\n🔗 Твоя ссылка: <code>{link}</code>",
        parse_mode="HTML",
        reply_markup=back_kb()
    )

@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Поддержка", url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await cb.message.edit_text("🆘 Поддержка:", reply_markup=kb)

@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await cb.message.edit_text("📖 Информация:", reply_markup=kb)

@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery):
    await cb.message.edit_text("🚀 Главное меню", reply_markup=main_kb())

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        await message.answer("Использование: /give @username 30")
        return
    parts = command.args.split()
    if len(parts) < 2:
        await message.answer("Укажите пользователя и дни")
        return
    target = parts[0]
    days = parts[1]
    if not days.isdigit():
        await message.answer("Дни должны быть числом")
        return
    days = int(days)
    with db_conn() as conn:
        if target.startswith("@"):
            username = target.lstrip("@")
            row = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        else:
            if not target.isdigit():
                await message.answer("Укажите корректный username или user_id")
                return
            row = conn.execute("SELECT user_id
