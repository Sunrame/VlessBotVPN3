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

# Отключаем ворнинги безопасности для запросов к IP
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- НАСТРОЙКИ АДМИНОВ ---
ADMIN_1 = 939883122
ADMIN_2 = 1883819477  # <--- ВСТАВЬ ТУТ ID ВТОРОГО АДМИНА
ADMINS = [ADMIN_1, ADMIN_2]

# --- ПАРАМЕТРЫ ПАНЕЛИ ---
PANEL_URL = os.getenv('PANEL_URL')
PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
SUB_PORT = "2096" # Порт из твоих настроек подписки

# Вычисляем IP сервера из ссылки
try:
    SERVER_IP = PANEL_URL.split('//')[1].split(':')[0]
except:
    SERVER_IP = "213.176.94.201"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()
router = Router()
session = requests.Session()

# --- ФУНКЦИИ ПАНЕЛИ ---
def login_to_panel():
    try:
        res = session.post(f"{PANEL_URL}/login", 
                          data={"username": PANEL_LOGIN, "password": PANEL_PASSWORD}, 
                          timeout=10)
        return res.status_code == 200 and res.json().get("success")
    except:
        return False

def add_user_to_panel(user_id):
    if not login_to_panel():
        return None
    
    # Генерируем уникальные данные для клиента
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"v2_{user_id}"))
    u_sub_id = str(uuid.uuid4().hex)[:12]
    inbound_id = 2  # ID твоего подключения
    
    client_data = {
        "id": u_uuid,
        "email": f"u_{user_id}",
        "limitIp": 1,
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "tgId": str(user_id),
        "subId": u_sub_id
    }
    
    payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_data]})}
    try:
        res = session.post(f"{PANEL_URL}/panel/api/inbounds/addClient", 
                          json=payload, 
                          headers={"X-Requested-With": "XMLHttpRequest"})
        if res.json().get("success") or "already exists" in res.text:
            return u_sub_id
    except:
        pass
    return None

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, sub_token TEXT)''')
    conn.commit(); conn.close()

# --- КОМАНДЫ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message):
    init_db()
    u_name = (message.from_user.username or "none").lower()
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', 
                 (message.from_user.id, u_name))
    conn.commit(); conn.close()
    await message.answer(f"👋 Привет! Я бот {hbold('TrubaVPN')}.\n\nДождись, пока админ выдаст тебе доступ.", parse_mode="HTML")

@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    # Проверка на админа
    if message.from_user.id not in ADMINS:
        return await message.answer(f"🚫 Нет прав. Твой ID: {message.from_user.id}")

    # Проверка аргументов
    if not command.args:
        return await message.answer("⚠️ Пиши: `/give @username`", parse_mode="Markdown")

    target_name = command.args.replace("@", "").lower().strip()

    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target_name,)).fetchone()
    
    if user:
        u_id = user[0]
        token = add_user_to_panel(u_id) # Создаем в панели
        
        if token:
            conn.execute('UPDATE users SET sub_token = ? WHERE user_id = ?', (token, u_id))
            conn.commit()
            
            link = f"http://{SERVER_IP}:{SUB_PORT}/sub/{token}"
            await message.answer(f"✅ Доступ для @{target_name} создан!")
            
            try:
                await bot.send_message(u_id, f"🎉 Админ выдал тебе доступ к VPN!\n\nТвоя ссылка для подключения:\n{hcode(link)}", parse_mode="HTML")
            except:
                await message.answer("⚠️ Ссылка создана, но не смог отправить сообщение пользователю (бот заблокирован?).")
        else:
            await message.answer("❌ Ошибка панели 3X-UI. Проверь PANEL_URL и логины в Amvera.")
    else:
        await message.answer("❌ Пользователь не найден. Он должен хотя бы раз нажать /start")
    conn.close()

async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
