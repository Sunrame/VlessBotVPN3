import os
import uuid
import requests
import logging
import time
import sqlite3
import asyncio
import json
import urllib3
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.utils.markdown import hcode, hbold
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from yookassa import Configuration, Payment

# Отключаем предупреждения SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- КОНФИГУРАЦИЯ ---
SERVER_IP = "213.176.94.201"
PANEL_PORT = "21524"
SUB_PORT = "2096"
SECRET_PATH = "rNsOideTrxjP1O05fX" 

BASE_URL = f"http://{SERVER_IP}:{PANEL_PORT}/{SECRET_PATH}"

API_TOKEN = os.getenv('BOT_TOKEN')
SHOP_ID = os.getenv('SHOP_ID', '1350293') 
YOOKASSA_KEY = os.getenv('YOOKASSA_KEY', 'live_Vgr2Ea4LpPVScKOVQK5_QZW8fkGCAT9oPPHQH_z9R2c')

PANEL_LOGIN = os.getenv('PANEL_LOGIN', 'Infobiznes240305082009')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD', 'Infobiznes')
INBOUND_ID = 2 

# Список админов (добавлен твой ID)
ADMINS = [5906233405]

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# --- ТАРИФЫ ---
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
session = requests.Session()

# --- API ПАНЕЛИ ---
def login_to_panel():
    try:
        login_url = f"{BASE_URL}/login"
        payload = {"username": PANEL_LOGIN, "password": PANEL_PASSWORD}
        res = session.post(login_url, data=payload, timeout=10)
        return res.status_code == 200 and res.json().get("success")
    except Exception as e:
        return False

def add_user_to_panel(user_id):
    if not login_to_panel(): return False
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"v2_{user_id}"))
    u_sub_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"sub_{user_id}"))[:12]
    add_url = f"{BASE_URL}/panel/api/inbounds/addClient"
    client_data = {"id": u_uuid, "email": f"u_{user_id}", "totalGB": 0, "expiryTime": 0, "enable": True, "tgId": str(user_id), "subId": u_sub_id}
    payload = {"id": INBOUND_ID, "settings": json.dumps({"clients": [client_data]})}
    try:
        res = session.post(add_url, json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        return res.json().get("success") or "already exists" in res.text
    except: return False

# --- БД ---
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, bought_friends INTEGER DEFAULT 0, expiry_date INTEGER DEFAULT 0, is_active INTEGER DEFAULT 0, current_plan TEXT DEFAULT 'none')''')
    conn.commit(); conn.close()

async def activate_user_logic(user_id, plan, amount, is_days=False):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(amount) * (86400 if is_days else 2592000)
    cursor.execute('SELECT expiry_date, referrer_id, is_active FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    expiry = (max(row[0] or 0, now) + added_time)
    cursor.execute('UPDATE users SET is_active = 1, expiry_date = ?, current_plan = ? WHERE user_id = ?', (expiry, plan, user_id))
    conn.commit(); conn.close()
    add_user_to_panel(user_id)
    return expiry

def get_vpn_link(user_id):
    u_sub_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"sub_{user_id}"))[:12]
    return f"http://{SERVER_IP}:{SUB_PORT}/sub/{u_sub_id}"

# --- КОМАНДЫ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    u_name = (message.from_user.username or "none").lower()
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', (message.from_user.id, u_name, r_id))
    conn.commit(); conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

# АДМИН КОМАНДА GIVE
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS:
        return # Бот просто промолчит, если ты не админ
    
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Ошибка! Пиши: `/give @username 30`", parse_mode="Markdown")

    target_username, days = command.args.split()
    target_username = target_username.replace("@", "").lower()

    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target_username,)).fetchone()
    conn.close()

    if user:
        expiry = await activate_user_logic(user[0], "Admin Grant", days, is_days=True)
        date_str = time.strftime('%d.%m.%Y', time.localtime(expiry))
        await message.answer(f"✅ Выдано @{target_username} до {date_str}")
        try:
            await bot.send_message(user[0], f"🎁 Тебе выдан доступ до {date_str}!\nСсылка:\n{hcode(get_vpn_link(user[0]))}", parse_mode="HTML")
        except: pass
    else:
        await message.answer("❌ Пользователь не найден в базе. Он должен нажать /start")

@router.message(Command("take"))
async def admin_take(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    target = command.args.replace("@", "").lower().strip()
    conn = sqlite3.connect('users.db')
    conn.execute('UPDATE users SET is_active = 0, expiry_date = 0 WHERE username = ?', (target,))
    conn.commit(); conn.close()
    await message.answer(f"⛔ Доступ у @{target} отозван.")

# --- ОСТАЛЬНОЕ МЕНЮ ---
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
async def create_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    t_type, months = ("_".join(parts[1:-1]), parts[-1])
    price = TARIFFS_CONFIG[t_type]['prices'][months]
    payment = Payment.create({"amount": {"value": f"{price}.00", "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"}, "capture": True, "metadata": {"user_id": callback.from_user.id, "t_type": t_type, "months": months}}, str(uuid.uuid4()))
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)], [InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_{payment.id}")], [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"type_{t_type}")]])
    await callback.message.edit_text(f"💳 Сумма: {price}₽", reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    payment = Payment.find_one(callback.data.replace("check_", ""))
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        expiry = await activate_user_logic(u_id, payment.metadata['t_type'], payment.metadata['months'])
        await callback.message.edit_text(f"✅ Оплачено до {time.strftime('%d.%m.%Y', time.localtime(expiry))}\n{hcode(get_vpn_link(u_id))}", parse_mode="HTML")
    else: await callback.answer("⏳ Не оплачено", show_alert=True)

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, is_active FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    is_active = d and d[0] > int(time.time())
    text = f"👤 Профиль\n📅 До: {time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else '❌ Нет подписки'}"
    if is_active: text += f"\n🔗 Ссылка:\n{hcode(get_vpn_link(callback.from_user.id))}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
