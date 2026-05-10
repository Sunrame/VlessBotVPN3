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

# Отключаем ворнинги
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- АДМИНЫ (В КОДЕ) ---
ADMIN_1 = 939883122
ADMIN_2 = 1883819477  # <--- ВТОРОЙ ID
ADMINS = [ADMIN_1, ADMIN_2]

# --- НАСТРОЙКИ ИЗ VARIABLES ---
PANEL_URL = os.getenv('PANEL_URL')
PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
SUB_PORT = "2096"

Configuration.configure(os.getenv('SHOP_ID'), os.getenv('YOOKASSA_KEY'))

try:
    SERVER_IP = PANEL_URL.split('//')[1].split(':')[0]
except:
    SERVER_IP = "213.176.94.201"

# --- КОНФИГУРАЦИЯ ТАРИФОВ ---
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

logging.basicConfig(level=logging.INFO)
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()
router = Router()
session = requests.Session()

# --- ЛОГИКА ПАНЕЛИ ---
def login_to_panel():
    try:
        res = session.post(f"{PANEL_URL}/login", data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD}, timeout=10)
        return res.status_code == 200 and res.json().get("success")
    except: return False

def add_user_to_panel(user_id):
    if not login_to_panel(): return None
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"v2_{user_id}"))
    u_sub_id = str(uuid.uuid4().hex)[:12]
    payload = {"id": 2, "settings": json.dumps({"clients": [{"id": u_uuid, "email": f"u_{user_id}", "limitIp": 1, "totalGB": 0, "expiryTime": 0, "enable": True, "tgId": str(user_id), "subId": u_sub_id}]})}
    try:
        session.post(f"{PANEL_URL}/panel/api/inbounds/addClient", json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        return u_sub_id
    except: return None

def get_vpn_link(token):
    return f"http://{SERVER_IP}:{SUB_PORT}/sub/{token}"

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, 
                    friends_count INTEGER DEFAULT 0, expiry_date INTEGER DEFAULT 0, sub_token TEXT)''')
    conn.commit(); conn.close()

async def activate_sub(user_id, days):
    conn = sqlite3.connect('users.db')
    token = add_user_to_panel(user_id)
    expiry = int(time.time()) + (int(days) * 86400)
    conn.execute('UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?', (expiry, token, user_id))
    conn.commit(); conn.close()
    return expiry, token

# --- КЛАВИАТУРЫ ---
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы", callback_data="ref"), InlineKeyboardButton(text="📖 Инфо", callback_data="info")]
    ])

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    ref_id = int(command.args) if command.args and command.args.isdigit() else None
    u_name = (message.from_user.username or "none").lower()
    
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT OR IGNORE INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)', (message.from_user.id, u_name, ref_id))
    conn.commit(); conn.close()
    
    await message.answer(f"🚀 {hbold('TrubaVPN')} готов к работе!", reply_markup=main_kb(), parse_mode="HTML")

# Команда GIVE
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args or len(command.args.split()) < 2: return
    
    target_name, days = command.args.split()
    target_name = target_name.replace("@", "").lower()
    
    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target_name,)).fetchone()
    if user:
        expiry, token = await activate_sub(user[0], days)
        await message.answer(f"✅ Выдано до {time.strftime('%d.%m.%Y', time.localtime(expiry))}")
        try: await bot.send_message(user[0], f"🎁 Доступ выдан!\n{hcode(get_vpn_link(token))}", parse_mode="HTML")
        except: pass
    conn.close()

# Меню тарифов
@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    btns = [[InlineKeyboardButton(text=f"🔹 {v['name']}", callback_data=f"type_{k}")] for k, v in TARIFFS_CONFIG.items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await callback.message.edit_text("💎 <b>Выберите тариф:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("type_"))
async def choose_duration(callback: CallbackQuery):
    t_key = callback.data.replace("type_", "")
    info = TARIFFS_CONFIG[t_key]
    btns = [[InlineKeyboardButton(text=f"{m} мес. — {p}₽", callback_data=f"buy_{t_key}_{m}")] for m, p in info['prices'].items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")])
    await callback.message.edit_text(f"💳 <b>{info['name']}</b>\n{info['desc']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

# Оплата
@router.callback_query(F.data.startswith("buy_"))
async def create_payment(callback: CallbackQuery):
    _, t_key, months = callback.data.split("_")
    price = TARIFFS_CONFIG[t_key]['prices'][months]
    payment = Payment.create({"amount": {"value": f"{price}.00", "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"}, "capture": True, "metadata": {"user_id": callback.from_user.id, "days": int(months)*30}}, str(uuid.uuid4()))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_{payment.id}")]
    ])
    await callback.message.edit_text(f"К оплате {price}₽", reply_markup=kb)

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    pay_id = callback.data.split("_")[1]
    payment = Payment.find_one(pay_id)
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        expiry, token = await activate_sub(u_id, payment.metadata['days'])
        
        # Реферальная логика
        conn = sqlite3.connect('users.db')
        user = conn.execute('SELECT referrer_id FROM users WHERE user_id = ?', (u_id,)).fetchone()
        if user and user[0]:
            conn.execute('UPDATE users SET friends_count = friends_count + 1 WHERE user_id = ?', (user[0],))
            # Если 5 друзей — можно добавить бонус (например +30 дней)
        conn.commit(); conn.close()
        
        await callback.message.edit_text(f"✅ Оплачено!\n{hcode(get_vpn_link(token))}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Оплата не найдена.", show_alert=True)

# Профиль
@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    active = d and d[0] > time.time()
    text = f"👤 <b>Профиль</b>\n\n📅 До: {time.strftime('%d.%m.%Y', time.localtime(d[0])) if active else '❌ Нет подписки'}"
    if active: text += f"\n🔗 Ссылка:\n{hcode(get_vpn_link(d[1]))}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]), parse_mode="HTML")

# Рефералка
@router.callback_query(F.data == "ref")
async def show_ref(callback: CallbackQuery):
    bot_info = await bot.get_me()
    conn = sqlite3.connect('users.db')
    f_count = conn.execute('SELECT friends_count FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()[0]
    conn.close()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    await callback.message.edit_text(f"🤝 <b>Рефералы</b>\n\nПригласи 5 друзей и получи месяц бесплатно!\n\n👥 Приглашено: {f_count}\n🔗 Ссылка:\n{hcode(link)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]), parse_mode="HTML")

@router.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_kb(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
