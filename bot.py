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

# --- КОНФИГУРАЦИЯ ПАНЕЛИ ---
SERVER_IP = "213.176.94.201"
PANEL_PORT = "21524"
SUB_PORT = "2096"  # Порт подписки из твоих настроек
SECRET_PATH = "rNsOideTnxjP1005fX" 

BASE_URL = f"http://{SERVER_IP}:{PANEL_PORT}/{SECRET_PATH}"

API_TOKEN = os.getenv('BOT_TOKEN')
SHOP_ID = os.getenv('SHOP_ID', '1350293') 
YOOKASSA_KEY = os.getenv('YOOKASSA_KEY', 'live_Vgr2Ea4LpPVScKOVQK5_QZW8fkGCAT9oPPHQH_z9R2c')

PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
INBOUND_ID = int(os.getenv('INBOUND_ID', 2)) 

ADMINS = [int(os.getenv('ADMIN_ID_1', 0)), int(os.getenv('ADMIN_ID_2', 0)), 5906233405]

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
session.verify = False 

# --- API ПАНЕЛИ ---
def login_to_panel():
    try:
        login_url = f"{BASE_URL}/login"
        payload = {"username": PANEL_LOGIN, "password": PANEL_PASSWORD}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        response = session.post(login_url, data=payload, headers=headers, timeout=10)
        if response.status_code == 200 and response.json().get("success"):
            return True
        return False
    except Exception as e:
        logging.error(f"Login error: {e}")
        return False

def add_user_to_panel(user_id):
    if not login_to_panel():
        return False
    
    user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    # Для подписки генерируем стабильный subId на основе ID пользователя
    user_sub_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"sub_{user_id}"))[:16]
    
    add_url = f"{BASE_URL}/panel/api/inbounds/addClient"
    
    client_data = {
        "id": user_uuid,
        "alterId": 0,
        "email": f"user_{user_id}",
        "limitIp": 1,
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "tgId": str(user_id),
        "subId": user_sub_id
    }
    
    payload = {
        "id": INBOUND_ID,
        "settings": json.dumps({"clients": [client_data]})
    }
    
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}

    try:
        res = session.post(add_url, json=payload, headers=headers, timeout=10)
        result = res.json()
        return result.get("success") or "already exists" in result.get("msg", "")
    except Exception as e:
        logging.error(f"AddClient error: {e}")
        return False

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users 
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

async def activate_user_logic(user_id, plan, amount, is_days=False):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(amount) * (86400 if is_days else 2592000)
    
    cursor.execute('SELECT expiry_date FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    expiry = (max(row[0] or 0, now) + added_time)
    
    cursor.execute('UPDATE users SET is_active = 1, expiry_date = ?, current_plan = ? WHERE user_id = ?', (expiry, plan, user_id))
    conn.commit()
    conn.close()
    
    add_user_to_panel(user_id)
    return expiry

def get_vpn_link(user_id):
    # Генерация ссылки на подписку
    # В 3x-ui по умолчанию путь /sub/, а в качестве токена используется subId
    user_sub_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"sub_{user_id}"))[:16]
    return f"http://{SERVER_IP}:{SUB_PORT}/sub/{user_sub_id}"

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
async def create_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    t_type, months = ("_".join(parts[1:-1]), parts[-1])
    price = TARIFFS_CONFIG[t_type]['prices'][months]
    payment = Payment.create({
        "amount": {"value": f"{price}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
        "capture": True,
        "metadata": {"user_id": callback.from_user.id, "t_type": t_type, "months": months}
    }, str(uuid.uuid4()))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"type_{t_type}")]
    ])
    await callback.message.edit_text(f"💳 Сумма к оплате: {price}₽", reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    payment_id = callback.data.replace("check_", "")
    payment = Payment.find_one(payment_id)
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        expiry = await activate_user_logic(u_id, payment.metadata['t_type'], payment.metadata['months'])
        await callback.message.edit_text(f"✅ Готово! Твоя ссылка на подписку:\n\n{hcode(get_vpn_link(u_id))}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Оплата не найдена.", show_alert=True)

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args or len(command.args.split()) < 2: return await message.answer("Формат: `/give @username дни`")
    target, days = command.args.split()
    target = target.replace("@", "")
    conn = sqlite3.connect('users.db'); cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE username = ?', (target,))
    row = cursor.fetchone(); conn.close()
    if row:
        expiry = await activate_user_logic(row[0], "Admin Grant", days, is_days=True)
        await message.answer(f"✅ Выдано @{target}. Ссылка обновлена в профиле.")
        try: await bot.send_message(row[0], f"🎁 Доступ выдан! Ссылка на подписку:\n{hcode(get_vpn_link(row[0]))}", parse_mode="HTML")
        except: pass
    else: await message.answer("❌ Юзер не найден")

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    d = get_user_data(callback.from_user.id)
    is_active = d and d[0] > int(time.time())
    text = f"👤 <b>Ваш профиль</b>\n\n📅 Срок до: {time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else '❌ Нет подписки'}"
    if is_active:
        text += f"\n\n🔗 <b>Ссылка на подписку:</b>\n{hcode(get_vpn_link(callback.from_user.id))}\n\n<i>Добавьте эту ссылку в v2rayNG, Shadowrocket или Streisand.</i>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
