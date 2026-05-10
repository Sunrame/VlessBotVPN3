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

# Отключаем предупреждения SSL для работы с панелью по IP
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv('BOT_TOKEN')
SHOP_ID = os.getenv('SHOP_ID', '1350293') 
YOOKASSA_KEY = os.getenv('YOOKASSA_KEY', 'live_Vgr2Ea4LpPVScKOVQK5_QZW8fkGCAT9oPPHQH_z9R2c')

PANEL_URL = os.getenv('PANEL_URL', '').rstrip('/')
PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
INBOUND_ID = int(os.getenv('INBOUND_ID', 2)) 

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

# Глобальная сессия для работы с API панели
session = requests.Session()
session.verify = False 

# --- API ПАНЕЛИ ---
def login_to_panel():
    try:
        login_url = f"{PANEL_URL}/login"
        payload = {"username": PANEL_LOGIN, "password": PANEL_PASSWORD}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = session.post(login_url, data=payload, headers=headers, timeout=10)
        if response.status_code == 200 and response.json().get("success"):
            return True
        return False
    except Exception as e:
        logging.error(f"Ошибка логина в панель: {e}")
        return False

def add_user_to_panel(user_uuid, user_id):
    if not login_to_panel():
        logging.error("Не удалось войти в панель для создания клиента.")
        return False
    
    add_url = f"{PANEL_URL}/panel/api/inbounds/addClient"
    client_data = {
        "id": user_uuid,
        "alterId": 0,
        "email": f"user_{user_id}",
        "limitIp": 1,
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "tgId": str(user_id),
        "subId": str(uuid.uuid4())[:8]
    }
    payload = {
        "id": INBOUND_ID,
        "settings": json.dumps({"clients": [client_data]})
    }
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}

    try:
        res = session.post(add_url, json=payload, headers=headers, timeout=10)
        return res.json().get("success", False)
    except Exception as e:
        logging.error(f"Ошибка вызова addClient: {e}")
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

async def activate_user_in_db(user_id, plan, amount, is_days=False):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(amount) * (24*60*60 if is_days else 30*24*60*60)
    cursor.execute('SELECT expiry_date, referrer_id, is_active FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    
    expiry = (max(row[0], now) + added_time) if row and row[0] > 0 else (now + added_time)
    ref_id = row[1] if row else None
    already_active = row[2] if row else 0
    
    cursor.execute('UPDATE users SET is_active = 1, expiry_date = ?, current_plan = ? WHERE user_id = ?', (expiry, plan, user_id))
    
    # Рефералка
    if not is_days and not already_active and ref_id:
        cursor.execute('UPDATE users SET bought_friends = bought_friends + 1 WHERE user_id = ?', (ref_id,))
        cursor.execute('SELECT bought_friends FROM users WHERE user_id = ?', (ref_id,))
        ref_data = cursor.fetchone()
        if ref_data and ref_data[0] >= 5:
            forever = now + (100 * 365 * 24 * 60 * 60)
            cursor.execute('UPDATE users SET expiry_date = ?, is_active = 1, current_plan = "Вечный Премиум" WHERE user_id = ?', (forever, ref_id))
            try: await bot.send_message(ref_id, "🎉 Ура! Вы пригласили 5 друзей и получили Вечный Премиум!")
            except: pass
            
    conn.commit()
    conn.close()
    
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    add_user_to_panel(u_uuid, user_id)
    return expiry

def get_vpn_link(user_id):
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    ip = "213.176.94.201"
    params = "type=tcp&encryption=none&security=reality&pbk=B-1Qi4UHnODvnBfJ4IEaT5hcO6xYngxhJRU1M8FmSQg&fp=chrome&sni=x5media.ru&sid=b3f386&spx=%2F"
    return f"vless://{u_uuid}@{ip}:443?{params}#TrubaVPN_{user_id}"

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', 
                   (message.from_user.id, message.from_user.username, r_id))
    conn.commit(); conn.close()
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
    btns = []
    for m, p in info['prices'].items():
        btns.append([InlineKeyboardButton(text=f"{m} мес. — {p}₽", callback_data=f"buy_{t_type}_{m}")])
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
        expiry = await activate_user_in_db(u_id, payment.metadata['t_type'], payment.metadata['months'])
        await callback.message.edit_text(f"✅ Готово! До {time.strftime('%d.%m.%Y', time.localtime(expiry))}\n\n{hcode(get_vpn_link(u_id))}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Оплата не найдена.", show_alert=True)

# --- АДМИН КОМАНДЫ ---
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Формат: `/give @username дни`")
    target, days = command.args.split()
    target = target.replace("@", "")
    conn = sqlite3.connect('users.db'); cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE username = ?', (target,))
    row = cursor.fetchone(); conn.close()
    if row:
        expiry = await activate_user_in_db(row[0], "Админ-доступ", days, is_days=True)
        await message.answer(f"✅ Выдано @{target} до {time.strftime('%d.%m.%Y', time.localtime(expiry))}")
        try: await bot.send_message(row[0], f"🎁 Вам выдан доступ!\n{hcode(get_vpn_link(row[0]))}", parse_mode="HTML")
        except: pass
    else: await message.answer("❌ Юзер не найден в базе.")

@router.message(Command("take"))
async def admin_take(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args: return await message.answer("Формат: `/take @username`")
    target = command.args.replace("@", "").strip()
    conn = sqlite3.connect('users.db'); cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_active = 0, expiry_date = 0 WHERE username = ?', (target,))
    success = cursor.rowcount; conn.commit(); conn.close()
    await message.answer(f"⛔ Подписка @{target} аннулирована" if success else "❌ Юзер не найден")

# --- МЕНЮ ИНФО ---
@router.callback_query(F.data == "about_menu")
async def about_menu(callback: CallbackQuery):
    btns = [
        [InlineKeyboardButton(text="📢 Наш Telegram-канал", url=f"https://t.me/{CHANNEL_ID.replace('@','')}")],
        [InlineKeyboardButton(text="📜 Пользовательское Соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
        [InlineKeyboardButton(text="🛡 Политика Конфиденциальности", url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_CONTACT.replace('@','')}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]
    ]
    await callback.message.edit_text("📖 <b>Информация и поддержка:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data == "ref_program")
async def show_ref(callback: CallbackQuery):
    me = await bot.get_me()
    d = get_user_data(callback.from_user.id)
    friends = d[5] if d else 0
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    await callback.message.edit_text(f"🤝 <b>Реферальная программа</b>\n\nПригласи 5 друзей и получи Вечный Премиум!\n👥 Друзей: <b>{friends}/5</b>\n🔗 Ссылка:\n{hcode(link)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    d = get_user_data(callback.from_user.id)
    is_active = d and d[0] > int(time.time())
    text = f"👤 <b>Ваш профиль</b>\n\n📅 До: <b>{time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else '❌ Нет подписки'}</b>"
    if is_active: text += f"\n\n🔗 <b>Ваша ссылка:</b>\n{hcode(get_vpn_link(callback.from_user.id))}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
