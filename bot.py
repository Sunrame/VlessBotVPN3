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
#  КОНФИГУРАЦИЯ (только из переменных окружения)
# ─────────────────────────────────────────────
API_TOKEN      = os.environ["BOT_TOKEN"]
SHOP_ID        = os.environ["SHOP_ID"]
YOOKASSA_KEY   = os.environ["YOOKASSA_KEY"]
PAYMENT_TOKEN  = os.environ.get("PAYMENT_TOKEN", "")   # резервный токен, если нужен

PANEL_URL      = os.environ["PANEL_URL"].rstrip("/")    # напр. https://1.2.3.4:2053
PANEL_LOGIN    = os.environ["PANEL_LOGIN"]
PANEL_PASSWORD = os.environ["PANEL_PASSWORD"]
SUB_PORT       = os.environ.get("SUB_PORT", "2096")

ADMIN_IDS: list[int] = []
for key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    val = os.environ.get(key, "")
    if val.isdigit():
        ADMIN_IDS.append(int(val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@vvvvvpppnn")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# ─────────────────────────────────────────────
#  ТАРИФЫ
# ─────────────────────────────────────────────
TARIFFS: dict = {
    "trial": {"name": "🆓 Пробный (1 день)",    "price": 10,  "days": 1,  "desc": "Тестовый доступ на 24 часа"},
    "1_dev": {"name": "📱 1 устройство",         "price": 99,  "days": 30, "desc": "99 ₽ / 30 дней"},
    "2_dev": {"name": "📱📱 2 устройства",       "price": 179, "days": 30, "desc": "179 ₽ / 30 дней"},
    "5_dev": {"name": "💻 5 устройств",          "price": 349, "days": 30, "desc": "349 ₽ / 30 дней"},
}

# ─────────────────────────────────────────────
#  FSM СОСТОЯНИЯ
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
                sub_token    TEXT,
                has_paid    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses INTEGER DEFAULT 1
            );
        """)

# ─────────────────────────────────────────────
#  3X-UI ПАНЕЛЬ: выдача ключа
# ─────────────────────────────────────────────

async def panel_create_client(user_id: int, days: int) -> str | None:
    """
    Создаёт клиента в 3x-ui и возвращает корректную VLESS ссылку.
    Поддержка: TCP + REALITY + flow
    """
    base = PANEL_URL
    email = f"user_{user_id}"
    expire_ms = int((time.time() + days * 86400) * 1000)
    client_id = str(uuid.uuid4())

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=False)
        ) as s:

            # 1. Логин
            r = await s.post(
                f"{base}/login",
                data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD}
            )
            resp = await r.json(content_type=None)
            if not resp.get("success"):
                log.error("Panel login failed: %s", resp)
                return None

            # 2. Получаем inbound
            r = await s.get(f"{base}/xui/inbound/list")
            data = await r.json(content_type=None)

            if not data.get("success") or not data.get("obj"):
                log.error("Inbound list error: %s", data)
                return None
            
            inbound = next((i for i in data["obj"] if i["id"] == 2), None)

            if not inbound:
                log.error("Inbound with ID=2 not found")
                return None

            if inbound["protocol"] != "vless":
                log.error("Inbound is NOT VLESS: %s", inbound["protocol"])
                return None

            inbound_id = inbound["id"]
            port = inbound["port"]
            settings_json = json.loads(inbound["settings"])
            stream_json = json.loads(inbound["streamSettings"])

            # 3. Добавляем клиента
            flow = ""
            if settings_json.get("clients"):
                flow = settings_json["clients"][0].get("flow", "")

            payload = {
                "id": inbound_id,
                "settings": json.dumps({
                    "clients": [{
                        "id": client_id,
                        "email": email,
                        "expiryTime": expire_ms,
                        "enable": True,
                        "flow": flow
                    }]
                })
            }

            r = await s.post(f"{base}/xui/inbound/addClient", json=payload)
            add_resp = await r.json(content_type=None)

            if not add_resp.get("success"):
                log.error("Add client error: %s", add_resp)
                return None

            # 4. Формируем VLESS ссылку
            host = os.environ.get("VPN_HOST")
            if not host:
                host = base.replace("https://", "").replace("http://", "").split(":")[0]

            security = stream_json.get("security", "none")

            # === REALITY ===
            if security == "reality":
                reality = stream_json.get("realitySettings", {})
                public_key = reality.get("publicKey", "")
                short_id = reality.get("shortIds", [""])[0]
                server_name = reality.get("serverNames", [""])[0]

                vless_link = (
                    f"vless://{client_id}@{host}:{port}"
                    f"?type=tcp"
                    f"&security=reality"
                    f"&pbk={public_key}"
                    f"&sni={server_name}"
                    f"&sid={short_id}"
                    f"&fp=chrome"
                )
            # === ОБЫЧНЫЙ TCP ===
            else:
                vless_link = (
                    f"vless://{client_id}@{host}:{port}"
                    f"?type=tcp&security=none"
                )

            if flow:
                vless_link += f"&flow={flow}"
            
            vless_link += f"#{email}"
            return vless_link

    except Exception as e:
        log.exception("Panel error: %s", e)
        return None

async def activate_subscription(user_id: int, days: int):
    now = int(time.time())
    delta = days * 86400
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        current_expiry = row["expiry_date"] if row else 0
        token = row["sub_token"] if row and row["sub_token"] else None

        new_expiry = max(current_expiry, now) + delta

        # Если токена ещё нет — создаём клиента в панели
        if not token:
            token = await panel_create_client(user_id, days) or f"truba_{uuid.uuid4().hex[:10]}"

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
            InlineKeyboardButton(text="💎 Купить VPN",   callback_data="tariffs"),
            InlineKeyboardButton(text="👤 Профиль",      callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="🤝 Рефералы",     callback_data="ref_program"),
            InlineKeyboardButton(text="🎟 Промокод",     callback_data="promo_enter"),
        ],
        [
            InlineKeyboardButton(text="🆘 Поддержка",   callback_data="support_tab"),
            InlineKeyboardButton(text="📖 Инфо",        callback_data="info_tab"),
        ],
    ])

def back_kb(target="back"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
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
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (u_id,)
        ).fetchone()
        if not existing:
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
            callback_data=f"buy_{k}"
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
        await cb.answer("Тариф не найден", show_alert=True)
        return

    info = TARIFFS[t_key]
    try:
        payment = Payment.create(
            {
                "amount":       {"value": f"{info['price']}.00", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
                "capture":      True,
                "description":  f"TrubaVPN — {info['name']}",
                "metadata":     {"user_id": str(cb.from_user.id), "days": str(info["days"])},
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

    u_id = int(payment.metadata["user_id"])
    days = int(payment.metadata["days"])
    expiry, token = await activate_subscription(u_id, days)

    # Реферальный бонус (только при первой покупке)
    with db_conn() as conn:
        row = conn.execute(
            "SELECT referrer_id, has_paid FROM users WHERE user_id = ?", (u_id,)
        ).fetchone()

        if row and row["referrer_id"] and row["has_paid"] == 0:
            ref_id = row["referrer_id"]
            await activate_subscription(u_id, 7)
            await activate_subscription(ref_id, 7)
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
    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"📅 Подписка до: <b>{date_str}</b>\n"
        f"🔑 Ключ / ссылка подписки:\n{hcode(token)}\n\n"
        f"📖 Инструкции по подключению — в нашем канале: {CHANNEL_LINK}",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )

# ─────────────────────────────────────────────
#  ПРОМОКОДЫ (FSM)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
    ])
    await cb.message.edit_text(
        "🎟 Введите промокод:",
        reply_markup=kb,
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
                "❌ Неверный или уже использованный промокод.\n"
                "Попробуйте ещё раз или нажмите «Отмена».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
                ])
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
    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован!\n"
        f"Добавлено дней: <b>{days}</b>\n"
        f"Подписка до: <b>{date_str}</b>",
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
        date_str = time.strftime("%d.%m.%Y", time.localtime(row["expiry_date"]))
        token_line = f"\n🔑 Ключ:\n{hcode(row['sub_token'])}" if row["sub_token"] else ""
        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна\n"
            f"📅 До: <b>{date_str}</b> (осталось {days_left} дн.)"
            f"{token_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "❌ Подписка не активна.\n"
            "Нажмите «💎 Купить VPN» для оформления."
        )

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

# ─────────────────────────────────────────────
#  РЕФЕРАЛЬНАЯ ПРОГРАММА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    text = (
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей — при первой оплате вы оба получите <b>+7 дней</b>!\n\n"
        f"🔗 Ваша реферальная ссылка:\n{hcode(link)}"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

# ─────────────────────────────────────────────
#  ПОДДЕРЖКА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✍️ Написать менеджеру",
            url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])
    await cb.message.edit_text(
        "🆘 <b>Поддержка</b>\n\nЕсть вопросы? Мы на связи!",
        reply_markup=kb,
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ИНФО
# ─────────────────────────────────────────────
@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал с инструкциями",        url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
        [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-obrabotki-04-27")],
        [InlineKeyboardButton(text="⬅️ Назад",                       callback_data="back")],
    ])
    await cb.message.edit_text(
        "📖 <b>Информация и документы:</b>",
        reply_markup=kb,
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
#  АДМИН КОМАНДЫ
# ─────────────────────────────────────────────
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    if not command.args or len(command.args.split()) < 2:
        await message.answer("⚠️ Формат: <code>/give username дни</code>", parse_mode="HTML")
        return

    parts = command.args.split()
    target_username = parts[0].lstrip("@")
    days = int(parts[1]) if parts[1].lstrip("-").isdigit() else 0

    with db_conn() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE username = ?", (target_username,)).fetchone()

    if not row:
        await message.answer(f"❌ Пользователь @{target_username} не найден.")
        return

    expiry, _ = await activate_subscription(row["user_id"], days)
    date_str = time.strftime("%d.%m.%Y", time.localtime(expiry))
    await message.answer(f"✅ @{target_username} выдано {days} дней. До {date_str}")

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS: return
    parts = (command.args or "").split()
    if len(parts) < 2: return
    code, days = parts[0].upper(), int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 else 1
    with db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO promos (code, days, uses) VALUES (?, ?, ?)", (code, days, uses))
    await message.answer(f"✅ Промокод {code} на {days} дн. создан.")

@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("📢 Введите текст рассылки. /cancel — отмена.")

@router.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    now = int(time.time())
    with db_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE expiry_date > ?", (now,)).fetchone()[0]
    await message.answer(f"📊 Всего: {total}\n✅ Активно: {active}")

# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    init_db()
    dp.include_router(router)
    log.info("TrubaVPN Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
