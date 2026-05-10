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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. АДМИНЫ (В ПЕРЕМЕННЫХ ИЛИ ТУТ) ---
ADMINS = [939883122, 1883819477] # Замени второй ID на нужный

# --- 2. НАСТРОЙКИ ---
PANEL_URL = os.getenv('PANEL_URL')
PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
SUB_PORT = "2096"

Configuration.configure(os.getenv('SHOP_ID'), os.getenv('YOOKASSA_KEY'))

try:
    SERVER_IP = PANEL_URL.split('//')[1].split(':')[0]
except:
    SERVER_IP = "213.176.94.201"

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
    # Настройки клиента для 3X-UI (Inbound ID = 2)
    payload = {"id": 2, "settings": json.dumps({"clients": [{"id": u_uuid, "email": f"u_{user_id}", "limitIp": 1, "totalGB": 0, "expiryTime": 0, "enable": True, "tgId": str(user_id), "subId": u_sub_id}]})}
    try:
        res = session.post(f"{PANEL_URL}/panel/api/inbounds/addClient", json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        if res.json().get("success") or "already exists" in res.text:
            return u_sub_id
    except: pass
    return None

def update_user_status(user_id, enable=True):
    if not login_to_panel(): return False
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"v2_{user_id}"))
    # Находим клиента и меняем enable
    payload = {"id": 2, "settings": json.dumps({"clients": [{"id": u_uuid, "email": f"u_{user_id}", "enable": enable}]})}
    try:
        session.post(f"{PANEL_URL}/panel/api/inbounds/updateClient", json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        return True
    except: return False

def get_vpn_link(token):
    return f"http://{SERVER_IP}:{SUB_PORT}/sub/{token}"

# --- БД ---
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, expiry_date INTEGER DEFAULT 0, sub_token TEXT)''')
    conn.commit(); conn.close()

# --- АДМИН-КОМАНДЫ ---

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS:
        return await message.answer(f"⛔ Нет прав. Ваш ID: {message.from_user.id}")

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("📝 Пример: `/give @username 30`", parse_mode="Markdown")

    target_name, days = command.args.split()
    target_name = target_name.replace("@", "").lower().strip()

    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target_name,)).fetchone()
    
    if user:
        u_id = user[0]
        token = add_user_to_panel(u_id)
        if token:
            expiry = int(time.time()) + (int(days) * 86400)
            conn.execute('UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?', (expiry, token, u_id))
            conn.commit()
            await message.answer(f"✅ Выдано @{target_name} на {days} дн.")
            try:
                await bot.send_message(u_id, f"🎁 Подписка активирована до {time.strftime('%d.%m.%Y', time.localtime(expiry))}!\n\n{hcode(get_vpn_link(token))}", parse_mode="HTML")
            except: pass
        else:
            await message.answer("❌ Панель 3X-UI вернула ошибку.")
    else:
        await message.answer(f"❌ Юзер @{target_name} не найден в базе. Он нажимал /start?")
    conn.close()

@router.message(Command("take"))
async def admin_take(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args: return await message.answer("📝 Пример: `/take @username`", parse_mode="Markdown")

    target_name = command.args.replace("@", "").lower().strip()
    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target_name,)).fetchone()
    
    if user:
        update_user_status(user[0], enable=False)
        conn.execute('UPDATE users SET expiry_date = 0 WHERE user_id = ?', (user[0],))
        conn.commit()
        await message.answer(f"⛔ Подписка у @{target_name} отозвана.")
        try: await bot.send_message(user[0], "⚠️ Ваша подписка аннулирована.")
        except: pass
    else:
        await message.answer("❌ Юзер не найден.")
    conn.close()

# --- ОСНОВНОЕ ---

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    init_db()
    u_name = (message.from_user.username or "none").lower()
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', (message.from_user.id, u_name))
    conn.commit(); conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💎 Купить VPN", callback_data="buy")]
    ])
    await message.answer(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    active = d and d[0] > time.time()
    if active:
        txt = f"👤 До: {time.strftime('%d.%m.%Y', time.localtime(d[0]))}\n🔗 Ссылка:\n{hcode(get_vpn_link(d[1]))}"
    else:
        txt = "👤 Подписка не активна."
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]), parse_mode="HTML")

@router.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Профиль", callback_data="profile")], [InlineKeyboardButton(text="💎 Купить VPN", callback_data="buy")]])
    await callback.message.edit_text("🚀 Меню:", reply_markup=kb)

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
