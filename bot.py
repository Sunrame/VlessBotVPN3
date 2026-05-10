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

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv('BOT_TOKEN')
SHOP_ID = os.getenv('SHOP_ID', '1350293')
YOOKASSA_KEY = os.getenv('YOOKASSA_KEY')

PANEL_URL = os.getenv('PANEL_URL', 'http://127.0.0.1:21524/SECRET').rstrip('/')
PANEL_LOGIN = os.getenv('PANEL_LOGIN')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD')
SUB_PORT = os.getenv('SUB_PORT', '2096')

# Список админов (включая твой статический ID)
ADMINS = [939883122, 1883819477]
if os.getenv('ADMIN_ID_1'): ADMINS.append(int(os.getenv('ADMIN_ID_1')))
if os.getenv('ADMIN_ID_2'): ADMINS.append(int(os.getenv('ADMIN_ID_2')))

SUPPORT_CONTACT = "@vvvvvpppnn"
CHANNEL_ID = "@Truba_VPN"

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

try:
    SERVER_IP = PANEL_URL.split('//')[1].split(':')[0]
except:
    SERVER_IP = "213.176.94.201"

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
bot = Bot(token=API_TOKEN)
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
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    u_sub_id = str(uuid.uuid4().hex)[:12]
    payload = {"id": 2, "settings": json.dumps({"clients": [{"id": u_uuid, "email": f"u_{user_id}", "limitIp": 1, "totalGB": 0, "expiryTime": 0, "enable": True, "tgId": str(user_id), "subId": u_sub_id}]})}
    try:
        session.post(f"{PANEL_URL}/panel/api/inbounds/addClient", json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        return u_sub_id
    except: return None

def update_user_status(user_id, enable=True):
    if not login_to_panel(): return False
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    payload = {"id": 2, "settings": json.dumps({"clients": [{"id": u_uuid, "email": f"u_{user_id}", "enable": enable}]})}
    try:
        session.post(f"{PANEL_URL}/panel/api/inbounds/updateClient", json=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        return True
    except: return False

def get_vpn_link(sub_token):
    return f"http://{SERVER_IP}:{SUB_PORT}/sub/{sub_token}"

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, 
                    bought_friends INTEGER DEFAULT 0, expiry_date INTEGER DEFAULT 0, sub_token TEXT)''')
    conn.commit(); conn.close()

async def activate_user_in_db(user_id, plan_name, amount, is_days=False):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(amount) * (24*60*60 if is_days else 30*24*60*60)
    
    cursor.execute('SELECT expiry_date, referrer_id, bought_friends, sub_token FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    
    current_expiry = row[0] if row else 0
    ref_id = row[1] if row else None
    current_friends = row[2] if row else 0
    existing_token = row[3] if row else None
    
    new_expiry = (current_expiry + added_time) if current_expiry > now else (now + added_time)
    
    # Если токена еще нет — создаем в панели
    token = existing_token
    if not token:
        token = add_user_to_panel(user_id)
    else:
        update_user_status(user_id, enable=True)
    
    cursor.execute('UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?', (new_expiry, token, user_id))
    
    # Реферальный бонус пригласившему (только при первой покупке новичка)
    if not is_days and current_expiry <= now and ref_id:
        cursor.execute('UPDATE users SET bought_friends = bought_friends + 1 WHERE user_id = ?', (ref_id,))
        cursor.execute('SELECT bought_friends FROM users WHERE user_id = ?', (ref_id,))
        ref_data = cursor.fetchone()
        if ref_data and ref_data[0] >= 5:
            forever = now + (10 * 365 * 24 * 60 * 60) # 10 лет
            cursor.execute('UPDATE users SET expiry_date = ? WHERE user_id = ?', (forever, ref_id))
            try: await bot.send_message(ref_id, "🎁 Поздравляем! 5 ваших друзей купили VPN. Вам выдан Вечный Премиум!")
            except: pass
            
    conn.commit(); conn.close()
    return new_expiry, token

# --- КЛАВИАТУРЫ ---
def main_panel():
    btns = [[InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🤝 Реф. программа", callback_data="ref_program"), InlineKeyboardButton(text="📖 Инфо", callback_data="about_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=btns)

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    u_name = (message.from_user.username or "none").lower()
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', 
                 (message.from_user.id, u_name, r_id))
    conn.commit(); conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

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
        text = f"{m} мес. — {p}₽"
        btns.append([InlineKeyboardButton(text=text, callback_data=f"buy_{t_type}_{m}")])
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
    pay_id = callback.data.replace("check_", "")
    payment = Payment.find_one(pay_id)
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        expiry, token = await activate_user_in_db(u_id, payment.metadata['t_type'], payment.metadata['months'])
        await callback.message.edit_text(f"✅ Успешно! Подписка до {time.strftime('%d.%m.%Y', time.localtime(expiry))}\n\n{hcode(get_vpn_link(token))}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Оплата не найдена.", show_alert=True)

# --- АДМИН КОМАНДЫ ---
@router.message(Command("give"))
async def admin_give(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Формат: `/give @username дни`")
    target, days = command.args.split()
    target = target.replace("@", "").lower().strip()
    conn = sqlite3.connect('users.db')
    row = conn.execute('SELECT user_id FROM users WHERE username = ?', (target,)).fetchone()
    conn.close()
    if row:
        expiry, token = await activate_user_in_db(row[0], "Admin", days, is_days=True)
        await message.answer(f"✅ Выдано до {time.strftime('%d.%m.%Y', time.localtime(expiry))}")
        try: await bot.send_message(row[0], f"🎁 Доступ выдан!\n{hcode(get_vpn_link(token))}", parse_mode="HTML")
        except: pass
    else: await message.answer("❌ Юзер не найден")

@router.message(Command("take"))
async def admin_take(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    target = (command.args or "").replace("@", "").strip().lower()
    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE username = ?', (target,)).fetchone()
    if user:
        update_user_status(user[0], enable=False)
        conn.execute('UPDATE users SET expiry_date = 0 WHERE user_id = ?', (user[0],))
        conn.commit(); conn.close()
        await message.answer(f"⛔ Подписка @{target} аннулирована")
    else:
        conn.close()
        await message.answer("❌ Юзер не найден")

# --- ДОПОЛНИТЕЛЬНЫЕ МЕНЮ ---
@router.callback_query(F.data == "ref_program")
async def show_ref(callback: CallbackQuery):
    me = await bot.get_me()
    conn = sqlite3.connect('users.db')
    row = conn.execute('SELECT bought_friends FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    friends = row[0] if row else 0
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    await callback.message.edit_text(f"🤝 <b>5 покупок друзей = Вечный Премиум!</b>\n\nБонус засчитывается, когда друг совершает первую покупку.\n\n👥 Друзей купило: {friends}/5\n🔗 Ссылка:\n{hcode(link)}", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "about_menu")
async def about_menu(callback: CallbackQuery):
    btns = [[InlineKeyboardButton(text="📢 Канал", url=f"https://t.me/{CHANNEL_ID.replace('@','')}")],
            [InlineKeyboardButton(text="📜 Соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
            [InlineKeyboardButton(text="🛡 Политика", url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
            [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_CONTACT.replace('@','')}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]
    await callback.message.edit_text("📖 <b>Информация и поддержка:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    is_active = d and d[0] > int(time.time())
    text = f"👤 <b>Ваш профиль</b>\n\n📅 До: {time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else '❌ Нет подписки'}"
    if is_active: text += f"\n🔗 Ваша ссылка подписки:\n{hcode(get_vpn_link(d[1]))}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} активен!", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
