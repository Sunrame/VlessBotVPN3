import os
import uuid
import requests
import logging
import time
import sqlite3
import asyncio
import json
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.utils.markdown import hcode, hbold
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from yookassa import Configuration, Payment

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv('BOT_TOKEN')
YOOKASSA_KEY = os.getenv('YOOKASSA_KEY', 'live_Vgr2Ea4LpPVScKOVQK5_QZW8fkGCAT9oPPHQH_z9R2c')

# Настройки панели (из переменных окружения)
PANEL_URL = os.getenv('PANEL_URL', 'http://127.0.0.1:2053').rstrip('/')
PANEL_LOGIN = os.getenv('PANEL_LOGIN', 'admin')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD', 'admin')
INBOUND_ID = int(os.getenv('INBOUND_ID', 1)) # ID подключения в 3x-ui

# Список ID администраторов
ADMINS = [int(os.getenv('ADMIN_ID_1', 0)), int(os.getenv('ADMIN_ID_2', 0)), 5906233405]

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

TARIFFS_CONFIG = {
    "standart": {
        "name": "Стандарт",
        "prices": {"1": 100, "3": 270, "6": 480, "12": 840},
        "desc": "— Трафик: <b>50 ГБ</b>\n— Устройств: <b>1</b>\n— Локация: DE"
    },
    "standart_plus": {
        "name": "Стандарт +",
        "prices": {"1": 150, "3": 405, "6": 720, "12": 1260},
        "desc": "— Трафик: <b>БЕЗЛИМИТ</b>\n— Устройств: <b>1</b>\n— Локация: DE"
    },
    "premium": {
        "name": "Премиум",
        "prices": {"1": 300, "3": 810, "6": 1440, "12": 2520},
        "desc": "— Трафик: <b>БЕЗЛИМИТ</b>\n— Устройств: <b>до 3-х</b>\n— Приоритетная поддержка"
    }
}

SUPPORT_CONTACT = "@vvvvvpppnn"
CHANNEL_ID = "@Truba_VPN"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()

# --- API ПАНЕЛИ ---
def get_panel_cookie():
    try:
        res = requests.post(f"{PANEL_URL}/login", data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD}, timeout=5)
        return res.cookies
    except Exception as e:
        logging.error(f"Panel login error: {e}")
        return None

def add_user_to_panel(user_uuid, user_id):
    cookies = get_panel_cookie()
    if not cookies: return False
    payload = {
        "id": INBOUND_ID,
        "settings": json.dumps({
            "clients": [{
                "id": user_uuid,
                "alterId": 0,
                "email": f"user_{user_id}",
                "limitIp": 1,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": True,
                "tgId": user_id,
                "subId": ""
            }]
        })
    }
    try:
        res = requests.post(f"{PANEL_URL}/panel/api/inbounds/addClient", json=payload, cookies=cookies, timeout=5)
        return res.json().get("success", False)
    except Exception as e:
        logging.error(f"Panel addClient error: {e}")
        return False

def get_vpn_link(user_id):
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    ip = "51.250.121.176" # Твой IP в Германии
    params = "type=tcp&encryption=none&security=reality&pbk=B-1Qi4UHnODvnBfJ4IEaT5hcO6xYngxhJRU1M8FmSQg&fp=chrome&sni=x5media.ru&sid=b3f386&spx=%2F"
    return f"vless://{u_uuid}@{ip}:443?{params}#TrubaVPN_{user_id}"

# --- БАЗЫ ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, 
                       bought_friends INTEGER DEFAULT 0, expiry_date INTEGER DEFAULT 0,
                       is_active INTEGER DEFAULT 0, current_plan TEXT DEFAULT 'none',
                       last_notified INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT expiry_date, is_active, username, current_plan, referrer_id, bought_friends FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

async def activate_user_in_db(user_id, plan, amount, is_days=False):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(amount) * (24 * 60 * 60 if is_days else 30 * 24 * 60 * 60)
    
    cursor.execute('SELECT expiry_date, referrer_id, is_active FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    expiry = (row[0] + added_time) if row and row[0] > now else (now + added_time)
    
    cursor.execute('UPDATE users SET is_active = 1, expiry_date = ?, current_plan = ? WHERE user_id = ?', 
                   (expiry, plan, user_id))
    conn.commit()
    conn.close()

    # Регистрация UUID в панели 3x-ui
    user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    add_user_to_panel(user_uuid, user_id)
    
    return expiry

# --- ОБРАБОТЧИКИ ---

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', 
                   (message.from_user.id, message.from_user.username, r_id))
    conn.commit()
    conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

def main_panel():
    btns = [[InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🤝 Реф. программа", callback_data="ref_program"), InlineKeyboardButton(text="📖 Инфо", callback_data="about_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=btns)

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    btns = [[InlineKeyboardButton(text=f"🔹 {v['name']}", callback_data=f"type_{k}")] for k, v in TARIFFS_CONFIG.items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")])
    await callback.message.edit_text("💎 <b>Выберите тариф:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("type_"))
async def choose_duration(callback: CallbackQuery):
    t_type = callback.data.replace("type_", "")
    info = TARIFFS_CONFIG[t_type]
    btns = [[InlineKeyboardButton(text=f"{m} мес. — {p}₽", callback_data=f"buy_{t_type}_{m}")] for m, p in info['prices'].items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")])
    await callback.message.edit_text(f"💳 <b>{info['name']}</b>\n{info['desc']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("buy_"))
async def create_payment_link(callback: CallbackQuery):
    parts = callback.data.split("_")
    t_type, months = ("_".join(parts[1:-1]), parts[-1])
    price = TARIFFS_CONFIG[t_type]['prices'][months]
    payment = Payment.create({
        "amount": {"value": f"{price}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
        "capture": True,
        "metadata": {"user_id": callback.from_user.id, "t_type": t_type, "months": months}
    }, str(uuid.uuid4()))
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
                                                   [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")]])
    await callback.message.edit_text(f"💳 <b>Оплата</b>\nСумма: {price}₽", reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    payment_id = callback.data.replace("check_", "")
    payment = Payment.find_one(payment_id)
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        expiry_ts = await activate_user_in_db(u_id, payment.metadata['t_type'], payment.metadata['months'])
        await callback.message.edit_text(f"✅ Успешно! До {time.strftime('%d.%m.%Y', time.localtime(expiry_ts))}\n\n{hcode(get_vpn_link(u_id))}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Оплата не найдена.", show_alert=True)

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    d = get_user_data(callback.from_user.id)
    if not d: return
    is_active = d[0] > int(time.time())
    text = f"👤 <b>Ваш профиль</b>\n\n📅 Подписка до: <b>{time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else '❌ Нет подписки'}</b>"
    if is_active: 
        text += f"\n\n🔗 <b>Ваша ссылка:</b>\n{hcode(get_vpn_link(callback.from_user.id))}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

@router.message(Command("give"))
async def admin_give_sub(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    args = command.args.split()
    if len(args) < 2: return
    target_username, days = args[0].replace("@", ""), args[1]
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE username = ?', (target_username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        expiry = await activate_user_in_db(row[0], "Admin Grant", days, is_days=True)
        await message.answer(f"✅ Выдано до {time.strftime('%d.%m.%Y', time.localtime(expiry))}")
        try: await bot.send_message(row[0], f"🎁 Доступ выдан!\n\n{hcode(get_vpn_link(row[0]))}", parse_mode="HTML")
        except: pass
    else: await message.answer("❌ Юзер не найден")

async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try: asyncio.run(main())
    except: pass
