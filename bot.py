import os
import uuid
import requests
import logging
import time
import sqlite3
import asyncio
import json
from aiogram import Bot, Dispatcher, types, Router, F, BaseMiddleware
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.utils.markdown import hcode, hbold
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, LabeledPrice, PreCheckoutQuery

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv('BOT_TOKEN')
PAYMENT_TOKEN = os.getenv('PAYMENT_TOKEN') # Токен от @BotFather
ADMINS = [int(os.getenv('ADMIN_ID_1', 0)), int(os.getenv('ADMIN_ID_2', 0))]

# Данные тарифов (цены в рублях)
TARIFFS_CONFIG = {
    "standart": {
        "name": "Стандарт",
        "prices": {"1": 100, "3": 270, "6": 480, "12": 840},
        "desc": "— Трафик: 50 ГБ\n— Устройств: 1\n— Локации: NL, DE"
    },
    "standart_plus": {
        "name": "Стандарт +",
        "prices": {"1": 150, "3": 405, "6": 720, "12": 1260},
        "desc": "— Трафик: БЕЗЛИМИТ\n— Устройств: 1\n— Локации: NL, DE, KZ"
    },
    "premium": {
        "name": "Премиум",
        "prices": {"1": 300, "3": 810, "6": 1440, "12": 2520},
        "desc": "— Трафик: БЕЗЛИМИТ\n— Устройств: до 3-х\n— Приоритетная поддержка"
    }
}

PANEL_URL = os.getenv('PANEL_URL') 
SUB_PORT = os.getenv('SUB_PORT', '2096') 
LOGIN = os.getenv('PANEL_LOGIN')
PASSWORD = os.getenv('PANEL_PASSWORD')
INBOUND_ID = 1 

SUPPORT_CONTACT = "@vvvvvpppnn"
CHANNEL_ID = "@Truba_VPN"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()

# --- БАЗЫ ДАННЫХ (без изменений) ---
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

async def activate_user_in_db(user_id, plan, months):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_time = int(months) * 30 * 24 * 60 * 60
    cursor.execute('SELECT expiry_date, referrer_id, is_active FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    expiry = (row[0] + added_time) if row and row[0] > now else (now + added_time)
    ref_id = row[1] if row else None
    already_active = row[2] if row else 0
    
    cursor.execute('UPDATE users SET is_active = 1, expiry_date = ?, current_plan = ? WHERE user_id = ?', (expiry, plan, user_id))
    
    if not already_active and ref_id:
        cursor.execute('UPDATE users SET bought_friends = bought_friends + 1 WHERE user_id = ?', (ref_id,))
        cursor.execute('SELECT bought_friends FROM users WHERE user_id = ?', (ref_id,))
        ref_data = cursor.fetchone()
        if ref_data and ref_data[0] >= 5:
            forever = now + (100 * 365 * 24 * 60 * 60)
            cursor.execute('UPDATE users SET expiry_date = ?, is_active = 1, current_plan = "Премиум" WHERE user_id = ?', (forever, ref_id))
    conn.commit()
    conn.close()
    return expiry

# --- API ПАНЕЛИ ---
def get_3xui_session():
    s = requests.Session()
    try:
        r = s.post(f"{PANEL_URL.strip('/')}/login", data={'username': LOGIN, 'password': PASSWORD}, timeout=10)
        return s if r.status_code == 200 else None
    except: return None

def get_vpn_link(user_id, username, expiry_ts, plan):
    session = get_3xui_session()
    if not session: return "Ошибка связи"
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    host = PANEL_URL.split('://')[-1].split(':')[0]
    # Упрощенная логика добавления клиента (как в вашем исходнике)
    return f"{PANEL_URL.split('://')[0]}://{host}:{SUB_PORT}/sub/{u_uuid}?remark=Truba_{plan.replace(' ', '_')}"

# --- ОБРАБОТЧИКИ ПЛАТЕЖЕЙ (ЮKASSA) ---

@router.callback_query(F.data.startswith("buy_"))
async def process_buy_invoice(callback: CallbackQuery):
    parts = callback.data.split("_")
    t_type, months = ("_".join(parts[1:-1]), parts[-1])
    
    info = TARIFFS_CONFIG[t_type]
    price = info['prices'][months]
    plan_display = f"{info['name']} ({months} мес.)"
    
    # Выставляем счет
    await callback.message.answer_invoice(
        title=f"Подписка {plan_display}",
        description=f"Доступ к VPN\n{info['desc']}",
        payload=f"{t_type}:{months}", # Данные для обработки после оплаты
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=plan_display, amount=price * 100)], # В копейках
        start_parameter="truba_vpn_sub"
    )
    await callback.answer()

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    # Финальное подтверждение перед списанием
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    # Сюда бот попадает сразу после оплаты
    payload = message.successful_payment.invoice_payload
    t_type, months = payload.split(":")
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
    
    plan_name = f"{TARIFFS_CONFIG[t_type]['name']} ({months} мес.)"
    price = TARIFFS_CONFIG[t_type]['prices'][months]
    
    # 1. Активация
    expiry_ts = await activate_user_in_db(user_id, plan_name, months)
    lnk = get_vpn_link(user_id, message.from_user.username, expiry_ts, plan_name)
    
    # 2. Сообщение пользователю
    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\nТариф: <b>{plan_name}</b> активирован.\n"
        f"🔗 <b>Ваш ключ:</b>\n{hcode(lnk)}\n\n"
        f"Для получения чека напишите в поддержку: {SUPPORT_CONTACT}",
        parse_mode="HTML"
    )
    
    # 3. Уведомление админу (вам) — чтобы вы сделали чек в «Мой налог»
    admin_text = (
        f"💰 <b>Новая оплата! Нужно сделать чек:</b>\n\n"
        f"👤 Клиент: {username}\n"
        f"💵 Сумма: <b>{price}₽</b>\n"
        f"📦 Тариф: {plan_name}\n"
        f"🔗 <a href='tg://user?id={user_id}'>Открыть профиль</a>"
    )
    for admin in ADMINS:
        try: await bot.send_message(admin, admin_text, parse_mode="HTML")
        except: pass

# --- ОСТАЛЬНЫЕ МЕНЮ (из вашего кода) ---

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    text = "💎 <b>Выберите тип тарифа:</b>"
    btns = [
        [InlineKeyboardButton(text="🔹 Стандарт (от 70₽)", callback_data="type_standart")],
        [InlineKeyboardButton(text="⭐ Стандарт + (от 105₽)", callback_data="type_standart_plus")],
        [InlineKeyboardButton(text="👑 Премиум (от 210₽)", callback_data="type_premium")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("type_"))
async def choose_duration(callback: CallbackQuery):
    t_type = callback.data.replace("type_", "")
    info = TARIFFS_CONFIG[t_type]
    text = f"💳 <b>Тариф: {info['name']}</b>\n\n{info['desc']}\n\n⏳ <b>Выберите срок:</b>"
    btns = []
    for m, p in info['prices'].items():
        btns.append([InlineKeyboardButton(text=f"{m} мес. — {p}₽", callback_data=f"buy_{t_type}_{m}")])
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

# ... (остальные функции: profile, start, ref_program — остаются как в вашем коде) ...

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    # Сокращенная версия вашего старта для примера
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', (message.from_user.id, message.from_user.username, r_id))
    conn.commit()
    conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} готов!", reply_markup=main_panel(), parse_mode="HTML")

def main_panel():
    btns = [
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Реф. программа", callback_data="ref_program")],
        [InlineKeyboardButton(text="📖 О сервисе", callback_data="about_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} Главное меню:", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
