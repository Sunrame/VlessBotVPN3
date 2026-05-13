mport os
import uuid
import logging
import time
import sqlite3
import asyncio
import urllib3
import aiohttp
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

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# ─────────────────────────────────────────────
#  ТАРИФЫ
#  price — цена за 1 месяц (30 дней)
#  devices — количество устройств (для отображения в профиле)
# ─────────────────────────────────────────────
TARIFFS: dict = {
    "trial": {
        "name":    "🆓 Пробный (1 день)",
        "price":   10,
        "days":    1,
        "desc":    "Тестовый доступ на 24 часа",
        "devices": 1,
        "months_choice": False,   # для пробного выбор месяцев недоступен
    },
    "1_dev": {
        "name":    "📱 1 устройство",
        "price":   99,
        "days":    30,
        "desc":    "99 ₽ / мес",
        "devices": 1,
        "months_choice": True,
    },
    "2_dev": {
        "name":    "📱📱 2 устройства",
        "price":   179,
        "days":    30,
        "desc":    "179 ₽ / мес",
        "devices": 2,
        "months_choice": True,
    },
    "5_dev": {
        "name":    "💻 5 устройств",
        "price":   349,
        "days":    30,
        "desc":    "349 ₽ / мес",
        "devices": 5,
        "months_choice": True,
    },
}

# Скидки за количество месяцев: {месяцы: процент_скидки}
MONTH_DISCOUNTS = {1: 0, 3: 10, 6: 15, 12: 20}

def calc_price(base_price: int, months: int) -> int:
    """Итоговая цена за N месяцев с учётом скидки."""
    discount = MONTH_DISCOUNTS.get(months, 0)
    total = base_price * months
    return round(total * (1 - discount / 100))

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
                has_paid    INTEGER DEFAULT 0,
                tariff_key  TEXT DEFAULT '',
                devices     INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses INTEGER DEFAULT 1
            );
        """);
        # Миграция: добавляем столбцы если их нет
        try:
            conn.execute("ALTER TABLE users ADD COLUMN tariff_key TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN devices INTEGER DEFAULT 1")
        except Exception:
            pass

# ─────────────────────────────────────────────
#  3X-UI: создание клиента
# ─────────────────────────────────────────────
async def panel_create_client(user_id: int, days: int) -> str | None:
    """
    Логинимся в 3x-ui, создаём VLESS-клиента.
    Возвращает ссылку-подписку или None при ошибке.
    """
    base = PANEL_URL
    email = f"user_{user_id}"
    expire_ms = int((time.time() + days * 86400) * 1000)
    client_id = str(uuid.uuid4())

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=False)
        ) as s:
            # 1. Авторизация
            r = await s.post(f"{base}/login",
                             data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD})
            resp = await r.json(content_type=None)
            if not resp.get("success"):
                log.error("Panel login failed: %s", resp)
                return None

            # 2. Список inbound'ов
            r = await s.get(f"{base}/xui/inbound/list")
            data = await r.json(content_type=None)
            if not data.get("success") or not data.get("obj"):
                log.error("Panel inbound list failed: %s", data)
                return None
            inbound_id = data["obj"][0]["id"]

            # 3. Добавляем клиента
            payload = {
                "id": inbound_id,
                "settings": (
                    f'{{"clients":[{{"id":"{client_id}","email":"{email}",'
                    f'"expiryTime":{expire_ms},"enable":true,"flow":""}}]}}'
                ),
            }
            r = await s.post(f"{base}/xui/inbound/addClient", json=payload)
            add_resp = await r.json(content_type=None)
            if not add_resp.get("success"):
                log.error("Panel addClient failed: %s", add_resp)
                return None

            # 4. Генерируем ссылку подписки
            host = base.split("://", 1)[-1].split(":")[0]
            sub_link = f"http://{host}:{SUB_PORT}/{client_id}"
            return sub_link

    except Exception as e:
        log.exception("Panel error: %s", e)
        return None


async def panel_update_client_expiry(user_id: int, new_expiry_ts: int) -> bool:
    """
    Обновляем expiryTime существующего клиента в панели.
    Возвращает True при успехе.
    """
    base = PANEL_URL
    email = f"user_{user_id}"
    expire_ms = new_expiry_ts * 1000

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=False)
        ) as s:
            r = await s.post(f"{base}/login",
                             data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD})
            resp = await r.json(content_type=None)
            if not resp.get("success"):
                return False

            r = await s.get(f"{base}/xui/inbound/list")
            data = await r.json(content_type=None)
            if not data.get("success") or not data.get("obj"):
                return False

            for inbound in data["obj"]:
                import json
                try:
                    settings = json.loads(inbound.get("settings", "{}"))
                except Exception:
                    continue
                for client in settings.get("clients", []):
                    if client.get("email") == email:
                        client["expiryTime"] = expire_ms
                        payload = {
                            "id": inbound["id"],
                            "settings": json.dumps(settings),
                        }
                        r2 = await s.post(f"{base}/xui/inbound/{inbound['id']}/updateClient/{client['id']}", json=payload)
                        upd = await r2.json(content_type=None)
                        return bool(upd.get("success"))
            return False
    except Exception as e:
        log.exception("Panel update error: %s", e)
        return False

# ─────────────────────────────────────────────
#  ПОДПИСКА В БД
# ─────────────────────────────────────────────
async def activate_subscription(
    user_id: int, days: int,
    tariff_key: str = "", devices: int = 1
) -> tuple[int, str]:
    now = int(time.time())
    delta = days * 86400
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        current_expiry = row["expiry_date"] if row else 0
        token = row["sub_token"] if row and row["sub_token"] else None
        new_expiry = max(current_expiry, now) + delta

        if not token:
            # Создаём нового клиента в панели
            token = await panel_create_client(user_id, days)
            if not token:
                token = f"truba_{uuid.uuid4().hex[:10]}"
                log.warning("Panel unavailable, used fallback token for user %s", user_id)
        else:
            # Обновляем expiryTime в панели
            await panel_update_client_expiry(user_id, new_expiry)

        update_kwargs = {"expiry_date": new_expiry, "sub_token": token}
        if tariff_key:
            update_kwargs["tariff_key"] = tariff_key
            update_kwargs["devices"] = devices

        conn.execute(
            """UPDATE users
               SET expiry_date = :expiry_date,
                   sub_token   = :sub_token,
                   tariff_key  = CASE WHEN :tariff_key != '' THEN :tariff_key ELSE tariff_key END,
                   devices     = CASE WHEN :tariff_key != '' THEN :devices    ELSE devices    END
               WHERE user_id = :user_id""",
            {**update_kwargs, "user_id": user_id},
        )
    return new_expiry, token

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 Купить VPN",    callback_data="tariffs"),
            InlineKeyboardButton(text="👤 Профиль",       callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="🤝 Рефералы",      callback_data="ref_program"),
            InlineKeyboardButton(text="🎟 Промокод",      callback_data="promo_enter"),
        ],
        [
            InlineKeyboardButton(text="📖 Инструкции",   callback_data="instructions_tab"),
            InlineKeyboardButton(text="ℹ️ Инфо",         callback_data="info_tab"),
        ],
        [
            InlineKeyboardButton(text="🆘 Поддержка",    callback_data="support_tab"),
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
#  ТАРИФЫ (шаг 1)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "tariffs")
async def show_tariffs(cb: CallbackQuery):
    btns = [
        [InlineKeyboardButton(
            text=f"{v['name']} — от {v['price']} ₽/мес",
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
#  ВЫБОР МЕСЯЦЕВ (шаг 2)
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("buy_"))
async def process_buy(cb: CallbackQuery):
    t_key = cb.data.removeprefix("buy_")
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден", show_alert=True)
        return

    info = TARIFFS[t_key]

    # Для пробного — сразу к оплате
    if not info["months_choice"]:
        await _show_payment_confirm(cb, t_key, 1)
        return

    # Строим кнопки выбора месяцев
    btns = []
    for months, discount in MONTH_DISCOUNTS.items():
        total = calc_price(info["price"], months)
        label = (
            f"{months} мес — {total} ₽"
            + (f" (скидка {discount}%)" if discount else "")
        )
        btns.append([InlineKeyboardButton(
            text=label,
            callback_data=f"months_{t_key}_{months}"
        )])
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")])

    await cb.message.edit_text(
        f"📅 <b>{info['name']}</b>\n\nВыберите срок подписки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ПОДТВЕРЖДЕНИЕ И ОПЛАТА (шаг 3)
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("months_"))
async def select_months(cb: CallbackQuery):
    parts = cb.data.removeprefix("months_").rsplit("_", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await cb.answer("Ошибка", show_alert=True)
        return
    t_key, months = parts[0], int(parts[1])
    await _show_payment_confirm(cb, t_key, months)


async def _show_payment_confirm(cb: CallbackQuery, t_key: str, months: int):
    info = TARIFFS[t_key]
    days = info["days"] * months if months > 1 else info["days"]
    total_price = calc_price(info["price"], months)
    discount = MONTH_DISCOUNTS.get(months, 0)

    try:
        payment = Payment.create(
            {
                "amount":       {"value": f"{total_price}.00", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
                "capture":      True,
                "description":  f"TrubaVPN — {info['name']} x{months} мес",
                "metadata":     {
                    "user_id":   str(cb.from_user.id),
                    "days":      str(days),
                    "tariff_key": t_key,
                    "devices":   str(info["devices"]),
                },
            },
            str(uuid.uuid4()),
        )
    except Exception as e:
        log.exception("Payment create error: %s", e)
        await cb.answer("Ошибка создания платежа, попробуйте позже.", show_alert=True)
        return

    discount_line = f"\n🎁 Скидка: <b>{discount}%</b>" if discount else ""
    back_target = "tariffs" if not info["months_choice"] else f"buy_{t_key}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",         url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="⬅️ Назад",            callback_data=back_target)],
    ])

    period_label = f"{months} мес" if months > 1 else (
        "1 день" if info["days"] == 1 else "1 мес"
    )

    await cb.message.edit_text(
        f"💳 <b>{info['name']}</b> — {period_label}\n"
        f"ℹ️ {info['desc']}{discount_line}\n\n"
        f"К оплате: <b>{total_price} ₽</b>\n\n"
        "После оплаты нажмите «✅ Проверить оплату».",
        reply_markup=kb,
        parse_mode="HTML",
    )

# ─────────────────────────────────────────────
#  ПРОВЕРКА ПЛАТЕЖА
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

    u_id      = int(payment.metadata["user_id"])
    days      = int(payment.metadata["days"])
    t_key     = payment.metadata.get("tariff_key", "")
    devices   = int(payment.metadata.get("devices", 1))

    expiry, token = await activate_subscription(u_id, days, tariff_key=t_key, devices=devices)

    # Реферальный бонус (только при первой оплате)
    with db_conn() as conn:
        row = conn.execute(
            "SELECT referrer_id, has_paid FROM users WHERE user_id = ?", (u_id,)
        ).fetchone()
        if row and row["referrer_id"] and row["has_paid"] == 0:
            ref_id = row["referrer_id"]
            await activate_subscription(ref_id, 7)
            try:
                await bot.send_message(
                    ref_id,
                    "🎊 Ваш друг оплатил подписку!\n"
                    "Вам начислено <b>+7 дней</b> бонуса.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        conn.execute("UPDATE users SET has_paid = 1 WHERE user_id = ?", (u_id,))

    date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))
    tariff_name = TARIFFS.get(t_key, {}).get("name", "")

    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"📦 Тариф: <b>{tariff_name}</b>\n"
        f"📅 Подписка до: <b>{date_str}</b>\n\n"
        f"🔑 Ссылка подписки:\n{hcode(token)}\n\n"
        f"📖 Инструкции по подключению:\n{CHANNEL_LINK}",
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
    await cb.message.edit_text("🎟 Введите промокод:", reply_markup=kb)

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
            "SELECT expiry_date, sub_token, tariff_key, devices FROM users WHERE user_id = ?",
            (cb.from_user.id,),
        ).fetchone()

    now = int(time.time())
    if row and row["expiry_date"] > now:
        days_left   = (row["expiry_date"] - now) // 86400
        hours_left  = ((row["expiry_date"] - now) % 86400) // 3600
        date_str    = time.strftime("%d.%m.%Y", time.localtime(row["expiry_date"]))
        tariff_name = TARIFFS.get(row["tariff_key"], {}).get("name", "—")
        devices_str = f"{row['devices']} уст." if row["devices"] else "—"

        time_left = (
            f"{days_left} дн. {hours_left} ч." if days_left > 0 else f"{hours_left} ч."
        )

        if row["sub_token"]:
            key_line = f"\n\n🔑 <b>Ссылка подписки:</b>\n{hcode(row['sub_token'])}"
        else:
            key_line = "\n\n⚠️ Ключ не сгенерирован. Обратитесь в поддержку."

        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна\n"
            f"📦 Тариф: <b>{tariff_name}</b>\n"
            f"📱 Устройств: <b>{devices_str}</b>\n"
            f"📅 Действует до: <b>{date_str}</b>\n"
            f"⏳ Осталось: <b>{time_left}</b>"
            f"{key_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "❌ Подписка не активна.\n\n"
            "Нажмите «💎 Купить VPN» для оформления."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить / продлить VPN", callback_data="tariffs")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# ─────────────────────────────────────────────
#  РЕФЕРАЛЬНАЯ ПРОГРАММА
# ─────────────────────────────────────────────
@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    text = (
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей — при первой оплате друга вы оба получите <b>+7 дней</b>!\n\n"
        f"🔗 Ваша реферальная ссылка:\n{hcode(link)}"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())

# ─────────────────────────────────────────────
#  ИНСТРУКЦИИ (новая вкладка)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "instructions_tab")
async def instructions_tab(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все инструкции (канал)", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])
    await cb.message.edit_text(
        "📖 <b>Инструкции по подключению</b>\n\n"
        "Пошаговые гайды для всех устройств:\n"
        "• Android / iPhone\n"
        "• Windows / macOS\n"
        "• Роутеры\n\n"
        "Нажмите кнопку ниже 👇",
        reply_markup=kb,
        parse_mode="HTML",
    )

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
        [InlineKeyboardButton(text="📢 Канал",                       url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
        [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-obrabotki-04-27")],
        [InlineKeyboardButton(text="⬅️ Назад",                       callback_data="back")],
    ])
    await cb.message.edit_text(
        "ℹ️ <b>Информация и документы:</b>",
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
#  АДМИН: /give @username дни
# ─────────────────────────────────────────────
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args or len(command.args.split()) < 2:
        await message.answer("⚠️ Формат: <code>/give username дни</code> (без @)", parse_mode="HTML")
        return
    parts = command.args.split()
    target_username = parts[0].lstrip("@")
    if not parts[1].lstrip("-").isdigit():
        await message.answer("❌ Количество дней должно быть числом.")
        return
    days = int(parts[1])
    with db_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (target_username,)
        ).fetchone()
    if not row:
        await message.answer(f"❌ Пользователь <b>@{target_username}</b> не найден в базе.", parse_mode="HTML")
        return
    target_id = row["user_id"]
    expiry, _ = await activate_subscription(target_id, days)
    date_str = time.strftime("%d.%m.%Y", time.localtime(expiry))
    await message.answer(
        f"✅ <b>@{target_username}</b> выдано <b>{days}</b> дней.\n"
        f"Подписка до: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Администратор выдал вам <b>{days}</b> дней подписки!\n"
            f"Подписка действует до <b>{date_str}</b>.",
            parse_mode="HTML",
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
#  АДМИН: /genkey @username дни тариф_ключ
#  Генерация и выдача ключа без оплаты
# ─────────────────────────────────────────────
@router.message(Command("genkey"))
async def admin_genkey(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        tariff_keys = ", ".join(TARIFFS.keys())
        await message.answer(
            f"⚠️ Формат: <code>/genkey username дни [тариф]</code>\n"
            f"Доступные тарифы: {tariff_keys}\n"
            f"Пример: <code>/genkey ivan 30 1_dev</code>",
            parse_mode="HTML",
        )
        return

    target_username = parts[0].lstrip("@")
    if not parts[1].isdigit():
        await message.answer("❌ Количество дней должно быть числом.")
        return
    days = int(parts[1])
    t_key = parts[2] if len(parts) >= 3 and parts[2] in TARIFFS else "1_dev"
    devices = TARIFFS[t_key]["devices"]

    with db_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (target_username,)
        ).fetchone()

    if not row:
        await message.answer(
            f"❌ Пользователь <b>@{target_username}</b> не найден в базе.",
            parse_mode="HTML",
        )
        return

    target_id = row["user_id"]
    await message.answer(f"⏳ Генерирую ключ для @{target_username}...")

    expiry, token = await activate_subscription(
        target_id, days, tariff_key=t_key, devices=devices
    )
    date_str = time.strftime("%d.%m.%Y", time.localtime(expiry))
    tariff_name = TARIFFS[t_key]["name"]

    await message.answer(
        f"✅ Ключ выдан <b>@{target_username}</b>\n"
        f"📦 Тариф: <b>{tariff_name}</b>\n"
        f"📅 До: <b>{date_str}</b>\n"
        f"🔑 Ключ: {hcode(token)}",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Вам выдан доступ к TrubaVPN!\n\n"
            f"📦 Тариф: <b>{tariff_name}</b>\n"
            f"📅 Подписка до: <b>{date_str}</b>\n\n"
            f"🔑 Ссылка подписки:\n{hcode(token)}\n\n"
            f"📖 Инструкции: {CHANNEL_LINK}",
            parse_mode="HTML",
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
#  АДМИН: /add_promo КОД ДНИ [использований]
# ─────────────────────────────────────────────
@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.answer(
            "⚠️ Формат: <code>/add_promo КОД ДНИ [кол-во]</code>\n"
            "Пример: <code>/add_promo SUMMER30 30 100</code>",
            parse_mode="HTML",
        )
        return
    code = parts[0].upper()
    if not parts[1].isdigit():
        await message.answer("❌ Дни должны быть числом.")
        return
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
#  АДМИН: /broadcast (FSM)
# ─────────────────────────────────────────────
@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_text)
    await message.answer("📢 Введите текст рассылки (HTML). /cancel — отмена.")

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
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE expiry_date > ?", (now,)
        ).fetchone()[0]
        paid   = conn.execute(
            "SELECT COUNT(*) FROM users WHERE has_paid = 1"
        ).fetchone()[0]
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
