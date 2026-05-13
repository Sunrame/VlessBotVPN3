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
PAYMENT_TOKEN  = os.environ.get("PAYMENT_TOKEN", "")

PANEL_URL      = os.environ["PANEL_URL"].rstrip("/")
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
#  AIOGRAM И БД
# ─────────────────────────────────────────────
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
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ JSON
# ─────────────────────────────────────────────
def _safe_json_or_dict(raw, label: str):
    """
    Универсальный парсер:
    - если строка → json.loads
    - если dict → вернуть как есть
    - иначе → None
    """
    if raw is None:
        log.error(f"❌ {label}: значение None")
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            log.error(f"❌ {label}: пустая строка")
            return None
        try:
            return json.loads(raw)
        except Exception as e:
            log.error(f"❌ {label}: ошибка json.loads: {e} | raw={raw!r}")
            return None
    log.error(f"❌ {label}: неизвестный тип {type(raw)}")
    return None

async def _safe_json_response(resp: aiohttp.ClientResponse, label: str):
    """
    Универсальный разбор ответа панели:
    - пытаемся прочитать text()
    - логируем
    - пытаемся json.loads
    """
    try:
        text = await resp.text()
    except Exception as e:
        log.error(f"❌ {label}: не удалось прочитать текст ответа: {e}")
        return None

    log.warning(f"{label} RAW RESPONSE: {text}")

    text_strip = text.strip()
    if not text_strip:
        log.error(f"❌ {label}: пустой ответ от панели")
        return None

    try:
        data = json.loads(text_strip)
    except Exception as e:
        log.error(f"❌ {label}: ответ не JSON: {e}")
        return None

    if not isinstance(data, dict):
        log.error(f"❌ {label}: JSON не объект: {data}")
        return None

    return data

# ─────────────────────────────────────────────
#  3X-UI ПАНЕЛЬ: УНИВЕРСАЛЬНАЯ ЛОГИКА
# ─────────────────────────────────────────────
async def panel_create_client(user_id: int, days: int) -> str | None:
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
            # Логин
            r = await s.post(f"{base}/login", data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD})
            login_data = await _safe_json_response(r, "LOGIN")
            if not login_data or not login_data.get("success"):
                log.error(f"❌ Ошибка логина в панель: {login_data}")
                return None

            # Список инбаундов
            r = await s.get(f"{base}/xui/inbound/list")
            data = await _safe_json_response(r, "INBOUND_LIST")
            if not data or not data.get("success") or not data.get("obj"):
                log.error(f"❌ Не удалось получить список инбаундов: {data}")
                return None

            # Автопоиск инбаунда: ID 2 или любой VLESS
            inbound = next((i for i in data["obj"] if i.get("id") == 2), None)
            if not inbound:
                inbound = next((i for i in data["obj"] if i.get("protocol") == "vless"), None)

            if not inbound:
                log.error("❌ VLESS инбаунд не найден")
                return None

            inbound_id = inbound.get("id")
            port = inbound.get("port")

            log.warning("===== INBOUND DEBUG START =====")
            log.warning(f"INBOUND RAW: {inbound}")
            log.warning(f"SETTINGS TYPE: {type(inbound.get('settings'))}")
            log.warning(f"SETTINGS RAW: {inbound.get('settings')}")
            log.warning(f"STREAM TYPE: {type(inbound.get('streamSettings'))}")
            log.warning(f"STREAM RAW: {inbound.get('streamSettings')}")
            log.warning("===== INBOUND DEBUG END =====")

            # Разбор settings
            current_settings = _safe_json_or_dict(inbound.get("settings"), "settings")
            if current_settings is None:
                current_settings = {}

            clients_list = current_settings.get("clients") or []

            client_data = {
                "id": client_id,
                "email": email,
                "expiryTime": expire_ms,
                "enable": True,
                "flow": ""
            }

            if clients_list:
                try:
                    client_data["flow"] = clients_list[0].get("flow", "")
                except Exception as e:
                    log.error(f"❌ Ошибка чтения flow из clients: {e}")

            new_clients = clients_list + [client_data]
            new_settings_payload = {
                **current_settings,
                "clients": new_clients
            }

            payload = {
                "id": inbound_id,
                "settings": json.dumps(new_settings_payload, ensure_ascii=False)
            }

            r = await s.post(f"{base}/xui/inbound/addClient", json=payload)
            add_resp = await _safe_json_response(r, "ADD_CLIENT")
            if not add_resp or not add_resp.get("success"):
                log.error(f"❌ Ошибка addClient: {add_resp}")
                return None

            # Разбор streamSettings
            stream_settings = _safe_json_or_dict(inbound.get("streamSettings"), "streamSettings")
            if stream_settings is None:
                log.error("❌ streamSettings не разобран, не могу собрать VLESS ссылку")
                return None

            host = os.environ.get("VPN_HOST") or base.split("//")[-1].split(":")[0]
            security = stream_settings.get("security", "none")
            network = stream_settings.get("network", "tcp")

            vless_link = f"vless://{client_id}@{host}:{port}?type={network}&security={security}"

            if security == "reality":
                reality = stream_settings.get("realitySettings", {}) or {}
                pbk = reality.get("publicKey", "")
                sni = ""
                sid = ""
                try:
                    sni = (reality.get("serverNames") or [""])[0]
                except Exception:
                    pass
                try:
                    sid = (reality.get("shortIds") or [""])[0]
                except Exception:
                    pass

                vless_link += (
                    f"&pbk={pbk}"
                    f"&sni={sni}"
                    f"&sid={sid}"
                    f"&fp=chrome"
                )

            if client_data.get("flow"):
                vless_link += f"&flow={client_data['flow']}"

            vless_link += f"#{email}"

            log.info(f"✅ Сформирована VLESS ссылка: {vless_link}")
            return vless_link

    except Exception as e:
        log.exception(f"❌ Исключение в panel_create_client: {e}")
        return None

async def activate_subscription(user_id: int, days: int):
    """
    Универсальная активация:
    - если панель отдала ссылку → используем её
    - если панель упала → генерируем локальный токен-заглушку
    """
    now = int(time.time())
    delta = days * 86400
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        current_expiry = row["expiry_date"] if row else 0
        token = row["sub_token"] if row and row["sub_token"] else None
        new_expiry = max(current_expiry, now) + delta

        if not token:
            panel_token = await panel_create_client(user_id, days)
            if panel_token:
                token = panel_token
            else:
                token = f"truba_{uuid.uuid4().hex[:10]}"
                log.warning(f"⚠️ Панель недоступна, выдан локальный токен: {token}")

        conn.execute(
            "UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?",
            (new_expiry, token, user_id)
        )
    return new_expiry, token

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
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
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]]
    )

# ─────────────────────────────────────────────
#  ОСНОВНЫЕ ХЕНДЛЕРЫ
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
            "SELECT user_id FROM users WHERE user_id = ?",
            (u_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
                (u_id, message.from_user.username, r_id)
            )
        else:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (message.from_user.username, u_id)
            )
    await message.answer(
        f"🚀 Добро пожаловать в {hbold('TrubaVPN')}!",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )

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
            "description": f"TrubaVPN — {info['name']}",
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
    except Exception as e:
        log.error(f"❌ Ошибка создания платежа: {e}")
        await cb.answer("Ошибка создания платежа")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(cb: CallbackQuery):
    pay_id = cb.data.removeprefix("check_")
    try:
        payment = Payment.find_one(pay_id)
        if payment.status != "succeeded":
            await cb.answer("⏳ Платёж ещё не подтверждён", show_alert=True)
            return
        
        u_id, days = int(payment.metadata["user_id"]), int(payment.metadata["days"])
        expiry, token = await activate_subscription(u_id, days)
        
        with db_conn() as conn:
            row = conn.execute(
                "SELECT referrer_id, has_paid FROM users WHERE user_id = ?",
                (u_id,)
            ).fetchone()
            if row and row["referrer_id"] and row["has_paid"] == 0:
                await activate_subscription(row["referrer_id"], 7)
                try:
                    await bot.send_message(row["referrer_id"], "🎊 Друг оплатил VPN! Вам +7 дней.")
                except Exception:
                    pass
            conn.execute("UPDATE users SET has_paid = 1 WHERE user_id = ?", (u_id,))
            
        date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))
        await cb.message.edit_text(
            f"✅ Оплачено!\n📅 До: {date_str}\n🔑 Ключ: {hcode(token)}",
            parse_mode="HTML",
            reply_markup=back_kb()
        )
    except Exception as e:
        log.error(f"❌ Ошибка проверки платежа: {e}")
        await cb.answer("Ошибка проверки")

# ─────────────────────────────────────────────
#  ПРОМОКОДЫ, ПРОФИЛЬ, РЕФЕРАЛЫ, ИНФО
# ─────────────────────────────────────────────
@router.callback_query(F.data == "promo_enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    await cb.message.edit_text(
        "🎟 Введите промокод:",
        reply_markup=back_kb("promo_cancel")
    )

@router.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(cb)

@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT days, uses FROM promos WHERE code = ?",
            (code,)
        ).fetchone()
        if not row:
            await message.answer("❌ Неверный код")
            return
        expiry, _ = await activate_subscription(message.from_user.id, row["days"])
        if row["uses"] <= 1:
            conn.execute("DELETE FROM promos WHERE code = ?", (code,))
        else:
            conn.execute("UPDATE promos SET uses = uses - 1 WHERE code = ?", (code,))
    await state.clear()
    await message.answer(
        f"✅ Активирован! До {time.strftime('%d.%m.%Y', time.localtime(expiry))}",
        reply_markup=main_kb()
    )

@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?",
            (cb.from_user.id,)
        ).fetchone()
    now = int(time.time())
    if row and row["expiry_date"] > now:
        text = (
            f"👤 <b>Профиль</b>\n"
            f"✅ До: {time.strftime('%d.%m.%Y', time.localtime(row['expiry_date']))}\n"
            f"🔑 Ключ: {hcode(row['sub_token'])}"
        )
    else:
        text = "👤 <b>Профиль</b>\n❌ Нет подписки"
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_kb()
    )

@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 Пригласи друга и получи +7 дней!\n🔗 Ссылка: {hcode(link)}",
        parse_mode="HTML",
        reply_markup=back_kb()
    )

@router.callback_query(F.data == "support_tab")
async def support_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Менеджер", url=f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await cb.message.edit_text("🆘 Поддержка на связи!", reply_markup=kb)

@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await cb.message.edit_text("📖 Информация о сервисе", reply_markup=kb)

@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery):
    await cb.message.edit_text(
        f"🚀 {hbold('TrubaVPN')} готов!",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ
# ─────────────────────────────────────────────
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        return
    parts = command.args.split()
    if len(parts) < 2:
        return
    target_username, days = parts[0].lstrip("@"), int(parts[1])
    with db_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ?",
            (target_username,)
        ).fetchone()
    if row:
        await activate_subscription(row["user_id"], days)
        await message.answer(f"✅ Выдано {days} дн. для {target_username}")

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        return
    parts = command.args.split()
    if len(parts) < 2:
        return
    code, days = parts[0].upper(), int(parts[1])
    uses = int(parts[2]) if len(parts) >= 3 else 1
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promos (code, days, uses) VALUES (?, ?, ?)",
            (code, days, uses)
        )
    await message.answer(f"✅ Промо {code} создан")

@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("📢 Введите текст рассылки:")

@router.message(BroadcastState.waiting_text)
async def broadcast_send(message: types.Message, state: FSMContext):
    await state.clear()
    with db_conn() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()
    ok, fail = 0, 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], message.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await message.answer(f"✅ Готово! Успешно: {ok}, Ошибок: {fail}")

@router.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    with db_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE expiry_date > ?",
            (int(time.time()),)
        ).fetchone()[0]
    await message.answer(f"📊 Всего: {total}\n✅ Активных: {active}")

async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
