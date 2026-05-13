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

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
TARIFFS: dict = {
    "trial": {"name": "🆓 Пробный (1 день)",  "price": 10,  "days": 1,  "desc": "Тестовый доступ на 24 часа"},
    "1_dev": {"name": "📱 1 устройство",       "price": 99,  "days": 30, "desc": "99 ₽ / 30 дней"},
    "2_dev": {"name": "📱📱 2 устройства",     "price": 179, "days": 30, "desc": "179 ₽ / 30 дней"},
    "5_dev": {"name": "💻 5 устройств",        "price": 349, "days": 30, "desc": "349 ₽ / 30 дней"},
}

# ─────────────────────────────────────────────
#  FSM
# ─────────────────────────────────────────────
class PromoState(StatesGroup):
    waiting_code = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("trubavpn")

# ─────────────────────────────────────────────
#  AIOGRAM
# ─────────────────────────────────────────────
bot    = Bot(token=API_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()

# ─────────────────────────────────────────────
#  БАЗА ДАННЫХ
# ─────────────────────────────────────────────
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
                has_paid    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses INTEGER DEFAULT 1
            );
        """)

# ─────────────────────────────────────────────
#  3X-UI ПАНЕЛЬ
# ─────────────────────────────────────────────
async def panel_create_client(user_id: int, days: int) -> str | None:
    """
    Создаёт клиента в 3x-ui и возвращает VLESS-ссылку.
    Логирует ВСЕ ответы панели для отладки.
    """
    email      = f"tuba_{user_id}"
    client_id  = str(uuid.uuid4())
    expire_ms  = int((time.time() + days * 86400) * 1000)

    jar     = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=False),
        ) as s:

            # 1. ЛОГИН
            log.info("[Panel] === Creating client for user %d ===", user_id)
            r = await s.post(
                f"{PANEL_URL}/login",
                data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD},
            )
            raw = await r.text()
            log.info("[Panel] Login: status=%d, response=%s", r.status, raw[:1000])
            
            try:
                resp = json.loads(raw)
            except:
                log.error("[Panel] Login: Failed to parse JSON")
                return None
                
            if not resp.get("success"):
                log.error("[Panel] Login failed: %s", resp)
                return None
            log.info("[Panel] Login: OK")

            # 2. ПОЛУЧИТЬ INBOUNDS
            inbound_paths = [
                "/xui/inbounds",
                "/xui/API/inbounds",
                "/api/inbounds",
            ]
            
            inbound_id = None
            found_path = None
            
            for path in inbound_paths:
                log.info("[Panel] Trying path: %s", path)
                try:
                    r = await s.get(f"{PANEL_URL}{path}")
                    raw = await r.text()
                    log.info("[Panel] %s: status=%d, response_len=%d, body=%s", 
                             path, r.status, len(raw), raw[:1000])
                    
                    if r.status == 200:
                        data = json.loads(raw)
                        
                        # Проверяем структуру ответа
                        if isinstance(data, dict):
                            # Вариант 1: {success: true, obj: [...]}
                            if data.get("success") and data.get("obj"):
                                if isinstance(data["obj"], list) and data["obj"]:
                                    inbound_id = data["obj"][0].get("id")
                                    if inbound_id:
                                        found_path = path
                                        log.info("[Panel] %s: Found inbound_id=%s", path, inbound_id)
                                        break
                            
                            # Вариант 2: {obj: [...]} (без success)
                            elif data.get("obj"):
                                if isinstance(data["obj"], list) and data["obj"]:
                                    inbound_id = data["obj"][0].get("id")
                                    if inbound_id:
                                        found_path = path
                                        log.info("[Panel] %s: Found inbound_id=%s (no success flag)", path, inbound_id)
                                        break
                            
                            # Вариант 3: direct list [{id: ..., ...}]
                            elif isinstance(data, list) and data:
                                inbound_id = data[0].get("id")
                                if inbound_id:
                                    found_path = path
                                    log.info("[Panel] %s: Found inbound_id=%s (direct list)", path, inbound_id)
                                    break
                        
                        log.info("[Panel] %s: No valid inbound found in response", path)
                        
                except json.JSONDecodeError as e:
                    log.warning("[Panel] %s: JSON decode error: %s", path, e)
                except Exception as e:
                    log.warning("[Panel] %s: Error: %s", path, e)
            
            if not inbound_id:
                log.error("[Panel] Could not find inbound_id on any path")
                return None

            log.info("[Panel] Using inbound_id=%s from path=%s", inbound_id, found_path)

            # 3. ДОБАВИТЬ КЛИЕНТА
            client_obj = {
                "id":         client_id,
                "email":      email,
                "expiryTime": expire_ms,
                "enable":     True,
                "flow":       "",
                "limitIp":    0,
                "totalGB":    0,
            }
            
            payload = {
                "id":       inbound_id,
                "settings": json.dumps({"clients": [client_obj]}),
            }
            
            add_paths = [
                f"/xui/inbound/addClient",
                f"/xui/API/inbounds/{inbound_id}/addClient",
                f"/api/inbounds/{inbound_id}/addClient",
            ]
            
            add_ok = False
            for add_path in add_paths:
                log.info("[Panel] Trying addClient: %s", add_path)
                try:
                    r = await s.post(f"{PANEL_URL}{add_path}", json=payload)
                    raw = await r.text()
                    log.info("[Panel] %s: status=%d, response=%s", add_path, r.status, raw[:1000])
                    
                    resp = json.loads(raw)
                    if resp.get("success"):
                        add_ok = True
                        log.info("[Panel] addClient OK via %s", add_path)
                        break
                    else:
                        log.warning("[Panel] addClient failed: %s", resp)
                except Exception as e:
                    log.warning("[Panel] %s: Error: %s", add_path, e)
                    continue
            
            if not add_ok:
                log.error("[Panel] Could not add client on any path")
                return None

            # 4. ПОСТРОИТЬ VLESS ССЫЛКУ
            host_port  = PANEL_URL.split("://")[-1]
            panel_host = host_port.split(":")[0]
            port       = 443
            
            vless_link = f"vless://{client_id}@{panel_host}:{port}?encryption=none&type=tcp#TrubaVPN"
            
            log.info("[Panel] Success! Created VLESS link: %s", vless_link[:80])
            return vless_link

    except Exception as e:
        log.exception("[Panel] Unexpected error: %s", e)
        return None


async def panel_extend_client(token: str, extra_days: int) -> bool:
    """Продлевает подписку клиента."""
    try:
        client_id = token.split("vless://")[1].split("@")[0]
    except:
        return False

    jar     = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=False),
        ) as s:

            r = await s.post(
                f"{PANEL_URL}/login",
                data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD},
            )
            if not (await r.json(content_type=None)).get("success"):
                return False

            paths = ["/xui/inbounds", "/xui/API/inbounds", "/api/inbounds"]
            for path in paths:
                try:
                    r    = await s.get(f"{PANEL_URL}{path}")
                    data = json.loads(await r.text())
                    
                    obj_list = data.get("obj", data if isinstance(data, list) else [])
                    for ib in obj_list:
                        settings = json.loads(ib.get("settings", "{}"))
                        for client in settings.get("clients", []):
                            if client.get("id") == client_id:
                                now_ms      = int(time.time() * 1000)
                                current_exp = client.get("expiryTime", now_ms)
                                new_exp     = max(current_exp, now_ms) + extra_days * 86400 * 1000

                                client["expiryTime"] = new_exp
                                payload = {
                                    "id":       ib["id"],
                                    "settings": json.dumps({"clients": [client]}),
                                }
                                r = await s.post(
                                    f"{PANEL_URL}/xui/inbound/updateClient/{client_id}",
                                    json=payload,
                                )
                                return (await r.json(content_type=None)).get("success", False)
                except:
                    continue
    except:
        pass

    return False

# ─────────────────────────────────────────────
#  АКТИВАЦИЯ ПОДПИСКИ
# ─────────────────────────────────────────────
async def activate_subscription(user_id: int, days: int):
    now   = int(time.time())
    delta = days * 86400

    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        current_expiry = row["expiry_date"] if row else 0
        token          = row["sub_token"]   if row and row["sub_token"] else None
        new_expiry     = max(current_expiry, now) + delta

        if token and token.startswith("vless://"):
            ok = await panel_extend_client(token, days)
            if not ok:
                log.warning("Could not extend client for user %s", user_id)
        else:
            token = await panel_create_client(user_id, days)
            if not token:
                token = f"PANEL_ERROR_{uuid.uuid4().hex[:8]}"
                log.error("Panel unavailable for user %s", user_id)

        conn.execute(
            "UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?",
            (new_expiry, token, user_id),
        )

    return new_expiry, token

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 Купить VPN", callback_data="tariffs"),
            InlineKeyboardButton(text="👤 Профиль",    callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="🤝 Рефералы",   callback_data="ref_program"),
            InlineKeyboardButton(text="🎟 Промокод",   callback_data="promo_enter"),
        ],
        [
            InlineKeyboardButton(text="🆘 Поддержка",  callback_data="support_tab"),
            InlineKeyboardButton(text="📖 Инфо",       callback_data="info_tab"),
        ],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])

# ─────────────────────────────────────────────
#  /start
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
        exists = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (u_id,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
                (u_id, message.from_user.username, r_id),
            )
        else:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (message.from_user.username, u_id),
            )

    await message.answer(
        f"🚀 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\n"
        "Выберите действие ниже 👇",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "tariffs")
async def show_tariffs(cb: CallbackQuery):
    btns = [
        [InlineKeyboardButton(
            text=f"{v['name']} — {v['price']} ₽",
            callback_data=f"buy_{k}",
        )]
        for k, v in TARIFFS.items()
    ]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await cb.message.edit_text(
        "🛒 <b>Выберите тариф:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ПЛАТЁЖ: создание
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("buy_"))
async def process_buy(cb: CallbackQuery):
    t_key = cb.data.removeprefix("buy_")
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return

    info = TARIFFS[t_key]
    try:
        payment = Payment.create(
            {
                "amount":       {"value": f"{info['price']}.00", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
                "capture":      True,
                "description":  f"TrubaVPN — {info['name']}",
                "metadata":     {
                    "user_id": str(cb.from_user.id),
                    "days":    str(info["days"]),
                },
            },
            str(uuid.uuid4()),
        )
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа, попробуйте позже.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",         url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="⬅️ Назад",            callback_data="tariffs")],
    ])
    await cb.message.edit_text(
        f"💳 <b>{info['name']}</b>\n"
        f"ℹ️ {info['desc']}\n\n"
        f"К оплате: <b>{info['price']} ₽</b>\n\n"
        "После оплаты нажмите «✅ Проверить оплату».",
        reply_markup=kb,
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ПЛАТЁЖ: проверка
# ─────────────────────────────────────────────
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
        await cb.answer("⏳ Платёж ещё не подтверждён. Попробуйте через минуту.", show_alert=True)
        return

    u_id   = int(payment.metadata["user_id"])
    days   = int(payment.metadata["days"])
    expiry, token = await activate_subscription(u_id, days)

    with db_conn() as conn:
        row = conn.execute(
            "SELECT referrer_id, has_paid FROM users WHERE user_id = ?", (u_id,)
        ).fetchone()

        if row and row["referrer_id"] and row["has_paid"] == 0:
            ref_id = row["referrer_id"]
            await activate_subscription(u_id,    7)
            await activate_subscription(ref_id,  7)
            try:
                await bot.send_message(
                    ref_id,
                    "🎊 Ваш друг оплатил подписку!\n"
                    "Вам и ему начислено по <b>+7 дней</b> бонуса.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        conn.execute("UPDATE users SET has_paid = 1 WHERE user_id = ?", (u_id,))

    date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))

    if token.startswith("PANEL_ERROR_"):
        await cb.message.edit_text(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"📅 Подписка до: <b>{date_str}</b>\n\n"
            "⚠️ Ключ временно недоступен — панель не ответила.\n"
            f"Напишите в поддержку: {SUPPORT_CONTACT}",
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
        return

    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"📅 Подписка до: <b>{date_str}</b>\n\n"
        f"🔑 Ваш VLESS-ключ (скопируйте целиком):\n"
        f"{hcode(token)}\n\n"
        f"📖 Как подключиться: {CHANNEL_LINK}",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  ПРОМОКОДЫ (FSM)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(
        "🎟 Введите промокод:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
        ]),
    )

@router.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        f"🚀 {hbold('TrubaVPN')} готов к работе!",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )

@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT days, uses FROM promos WHERE code = ?", (code,)
        ).fetchone()

        if not row:
            await message.answer(
                "❌ Неверный или уже использованный промокод.\nПопробуйте ещё раз:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
                ]),
            )
            return

        days = row["days"]
        uses = row["uses"]
        expiry, token = await activate_subscription(message.from_user.id, days)

        if uses <= 1:
            conn.execute("DELETE FROM promos WHERE code = ?", (code,))
        else:
            conn.execute("UPDATE promos SET uses = uses - 1 WHERE code = ?", (code,))

    await state.clear()
    date_str = time.strftime("%d.%m.%Y", time.localtime(expiry))

    if token.startswith("PANEL_ERROR_"):
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован! Добавлено <b>{days}</b> дней.\n"
            "⚠️ Ключ временно недоступен, обратитесь в поддержку.",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        return

    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован!\n"
        f"Добавлено: <b>{days}</b> дней | До: <b>{date_str}</b>\n\n"
        f"🔑 Ваш VLESS-ключ:\n{hcode(token)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?",
            (cb.from_user.id,),
        ).fetchone()

    now = int(time.time())
    if row and row["expiry_date"] > now:
        days_left = (row["expiry_date"] - now) // 86400
        date_str  = time.strftime("%d.%m.%Y", time.localtime(row["expiry_date"]))
        token     = row["sub_token"] or ""

        if token.startswith("vless://"):
            key_line = f"\n\n🔑 VLESS-ключ (скопируйте целиком):\n{hcode(token)}"
        elif token.startswith("PANEL_ERROR_"):
            key_line = "\n\n⚠️ Ключ не был выдан, обратитесь в поддержку."
        else:
            key_line = f"\n\n🔑 Ключ:\n{hcode(token)}" if token else ""

        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна\n"
            f"📅 До: <b>{date_str}</b> (осталось {days_left} дн.)"
            f"{key_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "❌ Подписка не активна.\n"
            "Нажмите «💎 Купить VPN» для оформления."
        )

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

# ─────────────────────────────────────────────
#  РЕФЕРАЛЫ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей — при первой оплате вы оба получите <b>+7 дней</b>!\n\n"
        f"🔗 Ваша ссылка:\n{hcode(link)}",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  ПОДДЕРЖКА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    await cb.message.edit_text(
        "🆘 <b>Поддержка</b>\n\nЕсть вопросы? Мы на связи!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✍️ Написать менеджеру",
                url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}",
            )],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ИНФО
# ─────────────────────────────────────────────
@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    await cb.message.edit_text(
        "📖 <b>Информация и документы:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Канал с инструкциями",        url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="📜 Пользовательское соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
            [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-obrabotki-04-27")],
            [InlineKeyboardButton(text="⬅️ Назад",                       callback_data="back")],
        ]),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  НАЗАД
# ─────────────────────────────────────────────
@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery):
    await cb.message.edit_text(
        f"🚀 {hbold('TrubaVPN')} готов к работе!",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  АДМИН: /give
# ─────────────────────────────────────────────
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("⚠️ Формат: <code>/give username дни</code>", parse_mode="HTML")
        return

    target_username = parts[0].lstrip("@")
    days = int(parts[1])

    with db_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (target_username,)
        ).fetchone()

    if not row:
        await message.answer(f"❌ Пользователь @{target_username} не найден.", parse_mode="HTML")
        return

    target_id      = row["user_id"]
    expiry, token  = await activate_subscription(target_id, days)
    date_str       = time.strftime("%d.%m.%Y", time.localtime(expiry))

    await message.answer(
        f"✅ @{target_username} выдано <b>{days}</b> дней.\nДо: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        if token.startswith("vless://"):
            key_text = f"\n\n🔑 Ваш VLESS-ключ:\n{hcode(token)}"
        else:
            key_text = ""
        
        await bot.send_message(
            target_id,
            f"🎁 Администратор выдал вам <b>{days}</b> дней подписки!\n"
            f"До: <b>{date_str}</b>{key_text}",
            parse_mode="HTML",
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
#  АДМИН: /add_promo
# ─────────────────────────────────────────────
@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "⚠️ Формат: <code>/add_promo КОД ДНИ [использований]</code>\n"
            "Пример: <code>/add_promo SUMMER30 30 100</code>",
            parse_mode="HTML",
        )
        return

    code = parts[0].upper()
    days = int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1

    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promos (code, days, uses) VALUES (?, ?, ?)",
            (code, days, uses),
        )

    await message.answer(
        f"✅ Промокод <code>{code}</code> создан.\n"
        f"Даёт: <b>{days}</b> дней | Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  АДМИН: /broadcast
# ─────────────────────────────────────────────
@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("📢 Введите текст рассылки (HTML поддерживается).\n/cancel — отмена.")

@router.message(Command("cancel"), BroadcastState.waiting_text)
async def broadcast_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Рассылка отменена.")

@router.message(BroadcastState.waiting_text)
async def broadcast_send(message: types.Message, state: FSMContext):
    await state.clear()
    text = f"📢 <b>Рассылка от TrubaVPN:</b>\n\n{message.text}"

    with db_conn() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()

    ok, fail = 0, 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], text, parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Рассылка завершена.\nОтправлено: {ok} | Ошибок: {fail}")

# ─────────────────────────────────────────────
#  АДМИН: /stats
# ─────────────────────────────────────────────
@router.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    now = int(time.time())
    with db_conn() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE expiry_date > ?", (now,)).fetchone()[0]
        paid   = conn.execute("SELECT COUNT(*) FROM users WHERE has_paid = 1").fetchone()[0]
        promos = conn.execute("SELECT COUNT(*) FROM promos").fetchone()[0]

    await message.answer(
        f"📊 <b>Статистика TrubaVPN</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"✅ Активных подписок: <b>{active}</b>\n"
        f"💳 Когда-либо платили: <b>{paid}</b>\n"
        f"🎟 Активных промокодов: <b>{promos}</b>",
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    init_db()
    dp.include_router(router)
    log.info("TrubaVPN Bot starting...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
