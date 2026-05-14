import os
import uuid
import logging
import time
import sqlite3
import asyncio
import urllib3
import requests as _requests
import json
from urllib.parse import quote, urlparse

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

PANEL_URL      = os.environ["PANEL_URL"].rstrip("/")   # https://ip:port/token
PANEL_LOGIN    = os.environ["PANEL_LOGIN"]
PANEL_PASSWORD = os.environ["PANEL_PASSWORD"]

ADMIN_IDS: list[int] = []
for _key in ("ADMIN_ID_1", "ADMIN_ID_2"):
    _val = os.environ.get(_key, "")
    if _val.isdigit():
        ADMIN_IDS.append(int(_val))

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")
CHANNEL_LINK    = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
INBOUND_ID      = int(os.environ.get("INBOUND_ID", "2"))  # ID inbound в 3x-ui

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

TARIFFS: dict = {
    "trial": {"name": "🆓 Пробный (1 день)",  "price": 10,  "days": 1,  "desc": "Тестовый доступ на 24 часа"},
    "1_dev": {"name": "📱 1 устройство",       "price": 99,  "days": 30, "desc": "99 ₽ / 30 дней"},
    "2_dev": {"name": "📱📱 2 устройства",     "price": 179, "days": 30, "desc": "179 ₽ / 30 дней"},
    "5_dev": {"name": "💻 5 устройств",        "price": 349, "days": 30, "desc": "349 ₽ / 30 дней"},
}

class PromoState(StatesGroup):
    waiting_code = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

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
                client_uuid TEXT,
                has_paid    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses INTEGER DEFAULT 1
            );
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN client_uuid TEXT")
        except Exception:
            pass

# ─────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────

def get_panel_base() -> tuple[str, str]:
    parsed = urlparse(PANEL_URL)
    host_port = parsed.netloc   # 1.2.3.4:54321
    return PANEL_URL, host_port


def build_subscription_url(email: str) -> str:
    return f"{PANEL_URL}/sub/{email}"


def build_vless_link(client_id: str, host_port: str, port: int,
                     stream: dict, email: str) -> str:
    security = stream.get("security", "none")
    network  = stream.get("network", "tcp")

    params: dict[str, str] = {
        "encryption": "none",
        "type":       network,
        "security":   security,
    }

    if security == "reality":
        rs = stream.get("realitySettings", {})
        params["pbk"]  = rs.get("publicKey", "")
        params["sid"]  = rs.get("shortIds", [""])[0] if rs.get("shortIds") else ""
        params["sni"]  = rs.get("serverNames", [""])[0] if rs.get("serverNames") else ""
        params["fp"]   = rs.get("fingerprint", "chrome")
        params["flow"] = "xtls-rprx-vision"
    elif security == "tls":
        ts  = stream.get("tlsSettings", {})
        sni = ts.get("serverName", "")
        if sni:
            params["sni"] = sni
        params["fp"] = ts.get("fingerprint", "chrome")

    if network == "ws":
        ws   = stream.get("wsSettings", {})
        path = ws.get("path", "/")
        host = ws.get("headers", {}).get("Host", "")
        params["path"] = quote(path, safe="/")
        if host:
            params["host"] = host
    elif network == "grpc":
        grpc = stream.get("grpcSettings", {})
        params["serviceName"] = grpc.get("serviceName", "")
        params["mode"]        = "multi" if grpc.get("multiMode") else "gun"

    query = "&".join(f"{k}={v}" for k, v in params.items() if v)
    tag   = quote(f"TrubaVPN-{email}", safe="")
    host  = host_port.split(":")[0]

    return f"vless://{client_id}@{host}:{port}?{query}#{tag}"


def format_key_message(expiry: int, vless_link: str, sub_url: str | None) -> str:
    date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry))

    if vless_link.startswith("PANEL_ERROR_"):
        return (
            f"📅 Подписка до: <b>{date_str}</b>\n\n"
            "⚠️ Ключ временно недоступен — панель не ответила.\n"
            f"Напишите в поддержку: {SUPPORT_CONTACT}"
        )

    lines = [f"📅 Подписка до: <b>{date_str}</b>", "", "━━━━━━━━━━━━━━━━━━━━"]

    if sub_url:
        lines += [
            "📲 <b>Ссылка на подписку</b> (рекомендуется):",
            "<i>Импортируйте в Happ / v2rayNG — конфиг обновится автоматически.</i>",
            hcode(sub_url),
            "",
        ]

    lines += [
        "🔑 <b>VLESS-ключ</b> (вручную):",
        hcode(vless_link),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📖 Как подключиться: {CHANNEL_LINK}",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  3X-UI PANEL API  (sync requests inside executor)
# ─────────────────────────────────────────────

def _sync_login(s: _requests.Session) -> bool:
    try:
        res = s.post(
            f"{PANEL_URL}/login",
            data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD},
            timeout=15,
            verify=False,
        )
        ok = res.status_code == 200 and res.json().get("success")
        log.info("[Panel] Login → %s", ok)
        return ok
    except Exception as e:
        log.error("[Panel] Login error: %s", e)
        return False


def _sync_get_inbound(s: _requests.Session) -> tuple[dict | None, int, dict]:
    """Получает нужный inbound через /panel/api/inbounds/list."""
    try:
        res = s.get(
            f"{PANEL_URL}/panel/api/inbounds/list",
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
            verify=False,
        )
        log.info("[Panel] GET inbounds → %d | %s", res.status_code, res.text[:200])
        data = res.json()

        inbounds = None
        if isinstance(data, list):
            inbounds = data
        elif isinstance(data, dict):
            for key in ("obj", "inbounds", "data"):
                if isinstance(data.get(key), list):
                    inbounds = data[key]
                    break

        if not inbounds:
            log.error("[Panel] inbounds not found in response: %s", data)
            return None, 443, {}

        ib = next((x for x in inbounds if x.get("id") == INBOUND_ID), None)
        if not ib:
            log.warning("[Panel] inbound id=%d not found, using first. ids=%s",
                        INBOUND_ID, [x.get("id") for x in inbounds])
            ib = inbounds[0]

        port       = ib.get("port", 443)
        raw_stream = ib.get("streamSettings", "{}")
        stream     = json.loads(raw_stream) if isinstance(raw_stream, str) else (raw_stream or {})

        log.info("[Panel] inbound id=%s port=%s security=%s network=%s",
                 ib.get("id"), port, stream.get("security"), stream.get("network"))
        return ib, port, stream

    except Exception as e:
        log.error("[Panel] _sync_get_inbound error: %s", e)
        return None, 443, {}


def _sync_create_client(user_id: int, days: int) -> tuple[str | None, str | None, str | None]:
    s = _requests.Session()
    if not _sync_login(s):
        return None, None, None

    ib, port, stream = _sync_get_inbound(s)
    if not ib:
        return None, None, None

    _, host_port = get_panel_base()
    email     = f"truba_{user_id}"
    client_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_{user_id}"))  # стабильный UUID
    expire_ms = int((time.time() + days * 86400) * 1000)
    flow      = "xtls-rprx-vision" if stream.get("security") == "reality" else ""

    client_obj = {
        "id":         client_id,
        "email":      email,
        "expiryTime": expire_ms,
        "enable":     True,
        "flow":       flow,
        "limitIp":    0,
        "totalGB":    0,
        "tgId":       str(user_id),
        "subId":      uuid.uuid4().hex[:12],
    }
    payload = {
        "id":       ib["id"],
        "settings": json.dumps({"clients": [client_obj]}),
    }

    try:
        res = s.post(
            f"{PANEL_URL}/panel/api/inbounds/addClient",
            json=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
            verify=False,
        )
        log.info("[Panel] addClient → %d | %s", res.status_code, res.text[:200])
        resp = res.json()
        if not resp.get("success") and "already exists" not in res.text:
            log.error("[Panel] addClient failed: %s", resp)
            return None, None, None
    except Exception as e:
        log.error("[Panel] addClient error: %s", e)
        return None, None, None

    vless_link = build_vless_link(client_id, host_port, port, stream, email)
    sub_url    = build_subscription_url(email)
    log.info("[Panel] Client created | sub=%s", sub_url)
    return vless_link, sub_url, client_id


def _sync_extend_client(client_uuid: str, extra_days: int) -> bool:
    s = _requests.Session()
    if not _sync_login(s):
        return False

    ib, _, _ = _sync_get_inbound(s)
    if not ib:
        return False

    raw_settings = ib.get("settings", "{}")
    settings = json.loads(raw_settings) if isinstance(raw_settings, str) else raw_settings

    for client in settings.get("clients", []):
        if client.get("id") == client_uuid:
            now_ms               = int(time.time() * 1000)
            current_exp          = client.get("expiryTime", now_ms)
            client["expiryTime"] = max(current_exp, now_ms) + extra_days * 86_400_000

            payload = {
                "id":       ib["id"],
                "settings": json.dumps({"clients": [client]}),
            }
            try:
                res = s.post(
                    f"{PANEL_URL}/panel/api/inbounds/updateClient/{client_uuid}",
                    json=payload,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    timeout=15,
                    verify=False,
                )
                log.info("[Panel] updateClient → %d | %s", res.status_code, res.text[:200])
                if res.json().get("success"):
                    return True
            except Exception as e:
                log.error("[Panel] updateClient error: %s", e)

    log.error("[Panel] client %s not found in inbound settings", client_uuid)
    return False


# Async-обёртки (запускают sync-функции в threadpool)
async def panel_create_client(user_id: int, days: int) -> tuple[str | None, str | None, str | None]:
    return await asyncio.get_event_loop().run_in_executor(
        None, _sync_create_client, user_id, days
    )


async def panel_extend_client(client_uuid: str, extra_days: int) -> bool:
    return await asyncio.get_event_loop().run_in_executor(
        None, _sync_extend_client, client_uuid, extra_days
    )


    pass  # replaced by sync versions below


# ─────────────────────────────────────────────
#  SUBSCRIPTION
# ─────────────────────────────────────────────

async def activate_subscription(user_id: int, days: int):
    now   = int(time.time())
    delta = days * 86400

    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token, client_uuid FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        current_expiry = row["expiry_date"] if row else 0
        token          = row["sub_token"]   if row and row["sub_token"]   else None
        client_uuid    = row["client_uuid"] if row and row["client_uuid"] else None
        new_expiry     = max(current_expiry, now) + delta
        sub_url        = None

        if client_uuid:
            ok = await panel_extend_client(client_uuid, days)
            if ok:
                email   = f"truba_{user_id}"
                sub_url = build_subscription_url(email)
            else:
                log.warning("[Sub] Не удалось продлить клиента %s, пересоздаём", client_uuid)
                client_uuid = None

        if not client_uuid:
            vless_link, sub_url, client_uuid = await panel_create_client(user_id, days)
            if not vless_link:
                vless_link  = f"PANEL_ERROR_{uuid.uuid4().hex[:8]}"
                sub_url     = None
                client_uuid = None
            token = vless_link

        conn.execute(
            "UPDATE users SET expiry_date=?, sub_token=?, client_uuid=? WHERE user_id=?",
            (new_expiry, token, client_uuid, user_id),
        )

    return new_expiry, token, sub_url


# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить VPN",  callback_data="tariffs"),
         InlineKeyboardButton(text="👤 Профиль",     callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы",    callback_data="ref_program"),
         InlineKeyboardButton(text="🎟 Промокод",    callback_data="promo_enter")],
        [InlineKeyboardButton(text="🆘 Поддержка",   callback_data="support_tab"),
         InlineKeyboardButton(text="📖 Инфо",        callback_data="info_tab")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])


# ─────────────────────────────────────────────
#  HANDLERS
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

    await message.answer(
        f"🚀 Добро пожаловать в {hbold('TrubaVPN')}!\n\n"
        "Высокоскоростной VPN с простой настройкой.\n"
        "Выберите действие ниже 👇",
        reply_markup=main_kb(), parse_mode="HTML",
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
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("buy_"))
async def process_buy(cb: CallbackQuery):
    t_key = cb.data.removeprefix("buy_")
    if t_key not in TARIFFS:
        await cb.answer("Тариф не найден.", show_alert=True)
        return

    info = TARIFFS[t_key]
    try:
        payment = Payment.create({
            "amount":       {"value": f"{info['price']}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
            "capture":      True,
            "description":  f"TrubaVPN — {info['name']}",
            "metadata":     {"user_id": str(cb.from_user.id), "days": str(info["days"])},
        }, str(uuid.uuid4()))
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
        await cb.answer("⏳ Платёж ещё не подтверждён. Попробуйте через минуту.", show_alert=True)
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
                    "🎊 Ваш друг оплатил подписку!\n"
                    "Вам и ему начислено по <b>+7 дней</b> бонуса.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        conn.execute("UPDATE users SET has_paid=1 WHERE user_id=?", (u_id,))

    key_msg = format_key_message(expiry, token, sub_url)
    await cb.message.edit_text(
        f"✅ <b>Оплата прошла успешно!</b>\n\n{key_msg}",
        parse_mode="HTML", reply_markup=back_kb(),
    )


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
        reply_markup=main_kb(), parse_mode="HTML",
    )


@router.message(PromoState.waiting_code)
async def handle_promo(message: types.Message, state: FSMContext):
    code = message.text.upper().strip()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT days, uses FROM promos WHERE code=?", (code,)
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
        expiry, token, sub_url = await activate_subscription(message.from_user.id, days)

        if uses <= 1:
            conn.execute("DELETE FROM promos WHERE code=?", (code,))
        else:
            conn.execute("UPDATE promos SET uses=uses-1 WHERE code=?", (code,))

    await state.clear()
    key_msg = format_key_message(expiry, token, sub_url)
    await message.answer(
        f"✅ Промокод <b>{code}</b> активирован! Добавлено <b>{days}</b> дн.\n\n{key_msg}",
        parse_mode="HTML", reply_markup=main_kb(),
    )


@router.callback_query(F.data == "profile")
async def profile_tab(cb: CallbackQuery):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT expiry_date, sub_token, client_uuid FROM users WHERE user_id=?",
            (cb.from_user.id,)
        ).fetchone()

    now = int(time.time())
    if row and row["expiry_date"] > now:
        days_left = (row["expiry_date"] - now) // 86400
        date_str  = time.strftime("%d.%m.%Y", time.localtime(row["expiry_date"]))
        token     = row["sub_token"] or ""

        if row["client_uuid"]:
            email    = f"truba_{cb.from_user.id}"
            sub_url  = build_subscription_url(email)
            sub_line = f"\n\n📲 <b>Ссылка на подписку</b> (для Happ):\n{hcode(sub_url)}"
        else:
            sub_line = ""

        if token.startswith("vless://"):
            key_line = f"\n\n🔑 <b>VLESS-ключ</b>:\n{hcode(token)}"
        elif token.startswith("PANEL_ERROR_"):
            key_line = "\n\n⚠️ Ключ не был выдан, обратитесь в поддержку."
        else:
            key_line = f"\n\n🔑 Ключ:\n{hcode(token)}" if token else ""

        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"✅ Подписка активна\n"
            f"📅 До: <b>{date_str}</b> (осталось {days_left} дн.)"
            f"{sub_line}"
            f"{key_line}"
        )
    else:
        text = (
            "👤 <b>Профиль</b>\n\n"
            "❌ Подписка не активна.\n"
            "Нажмите «💎 Купить VPN» для оформления."
        )

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())


@router.callback_query(F.data == "ref_program")
async def ref_program(cb: CallbackQuery):
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей — при первой оплате вы оба получите <b>+7 дней</b>!\n\n"
        f"🔗 Ваша ссылка:\n{hcode(link)}",
        parse_mode="HTML", reply_markup=back_kb(),
    )


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


@router.callback_query(F.data == "info_tab")
async def info_tab(cb: CallbackQuery):
    await cb.message.edit_text(
        "📖 <b>Информация и документы:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Канал с инструкциями",        url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="📜 Пользовательское соглашение",
                                  url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
            [InlineKeyboardButton(text="🛡 Политика конфиденциальности",
                                  url="https://telegra.ph/Politika-obrabotki-04-27")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "back")
async def back_to_main(cb: CallbackQuery):
    await cb.message.edit_text(
        f"🚀 {hbold('TrubaVPN')} готов к работе!",
        reply_markup=main_kb(), parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  ADMIN КОМАНДЫ
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
            "SELECT user_id FROM users WHERE username=?", (target_username,)
        ).fetchone()

    if not row:
        await message.answer(f"❌ Пользователь @{target_username} не найден.", parse_mode="HTML")
        return

    target_id              = row["user_id"]
    expiry, token, sub_url = await activate_subscription(target_id, days)
    date_str               = time.strftime("%d.%m.%Y", time.localtime(expiry))

    await message.answer(
        f"✅ @{target_username} выдано <b>{days}</b> дней.\nДо: <b>{date_str}</b>",
        parse_mode="HTML",
    )
    try:
        key_msg = format_key_message(expiry, token, sub_url)
        await bot.send_message(
            target_id,
            f"🎁 Администратор выдал вам <b>{days}</b> дней подписки!\n\n{key_msg}",
            parse_mode="HTML",
        )
    except Exception:
        pass


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
            "INSERT OR REPLACE INTO promos (code, days, uses) VALUES (?,?,?)",
            (code, days, uses),
        )

    await message.answer(
        f"✅ Промокод <code>{code}</code> создан.\n"
        f"Даёт: <b>{days}</b> дней | Использований: <b>{uses}</b>",
        parse_mode="HTML",
    )


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


@router.message(Command("panel_check"))
async def admin_panel_check(message: types.Message):
    """Диагностика: перебирает все возможные пути панели и показывает результат."""
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("🔍 Проверяю пути панели, подождите...")

    test_paths = [
        "/panel/api/inbounds",
        "/panel/inbounds",
        "/panel/API/inbounds",
        "/panel/inbound/list",
        "/xui/API/inbounds",
        "/xui/inbounds",
        "/api/inbounds",
    ]

    def _check():
        lines = []
        s = _requests.Session()
        ok = _sync_login(s)
        lines.append(f"{'✅' if ok else '❌'} Login: {'OK' if ok else 'FAILED'}")
        for path in test_paths:
            try:
                r       = s.get(f"{PANEL_URL}{path}",
                                headers={"X-Requested-With": "XMLHttpRequest"},
                                timeout=10, verify=False)
                snippet = r.text.strip()[:100].replace("\n", " ")
                is_html = snippet.lstrip().startswith("<")
                icon    = "🟢" if r.status_code == 200 and not is_html else (
                          "🟡" if r.status_code == 200 else "🔴")
                lines.append(f"{icon} {path}\n   → {r.status_code} | {snippet}")
            except Exception as e:
                lines.append(f"💥 {path}\n   → {type(e).__name__}: {e}")
        return lines

    lines = await asyncio.get_event_loop().run_in_executor(None, _check)
    await message.answer(
        "<b>Panel diagnostics:</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )



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
        f"💳 Когда-либо платили: <b>{paid}</b>\n"
        f"🎟 Активных промокодов: <b>{promos}</b>",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def main():
    init_db()
    dp.include_router(router)
    log.info("TrubaVPN Bot starting... INBOUND_ID=%d", INBOUND_ID)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
