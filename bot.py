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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from yookassa import Configuration, Payment

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv('BOT_TOKEN')
SHOP_ID = '1350293' 
YOOKASSA_KEY = 'live_Vgr2Ea4LpPVScKOVQK5_QZW8fkGCAT9oPPHQH_z9R2c'
ADMINS = [int(os.getenv('ADMIN_ID_1', 0)), int(os.getenv('ADMIN_ID_2', 0))]

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# Обновленные данные тарифов
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

# Очищаем URL от лишних слэшей в конце для корректной работы ссылок
PANEL_URL = os.getenv('PANEL_URL', '').rstrip('/')
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
        r = s.post(f"{PANEL_URL}/login", data={'username': LOGIN, 'password': PASSWORD}, timeout=10)
        return s if r.status_code == 200 else None
    except: return None

def get_vpn_link(user_id, username, expiry_ts, plan):
    """
    Генерирует ссылку на подписку. 
    Пользователь вставляет её в приложение и получает список всех серверов.
    """
    # UUID генерируется на основе ID пользователя для постоянства
    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"truba_v2_{user_id}"))
    
    # Извлекаем чистый хост (IP или домен) из PANEL_URL
    # Например из http://31.44.9.47:52790 получаем 31.44.9.47
    host = PANEL_URL.split('://')[-1].split(':')[0]
    
    # Формируем стандартную ссылку подписки для 3X-UI
    # Формат: http://IP:ПОРТ_ПОДПИСКИ/sub/UUID
    return f"http://{host}:{SUB_PORT}/sub/{u_uuid}"

# --- ОБРАБОТЧИКИ ---

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    text = "💎 <b>Выберите тарифный план:</b>\n\nВсе наши серверы используют современный протокол VLESS + REALITY для максимальной маскировки."
    btns = [[InlineKeyboardButton(text=f"🔹 {v['name']}", callback_data=f"type_{k}")] for k, v in TARIFFS_CONFIG.items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("type_"))
async def choose_duration(callback: CallbackQuery):
    t_type = callback.data.replace("type_", "")
    info = TARIFFS_CONFIG[t_type]
    
    text = (
        f"💳 <b>Тариф: {info['name']}</b>\n\n"
        f"{info['desc']}\n\n"
        f"—————\n"
        f"⏳ <b>Выберите срок подписки:</b>\n"
        f"<i>При покупке на долгий срок цена ниже!</i>"
    )
    
    btns = []
    for months, price in info['prices'].items():
        m_int = int(months)
        price_per_month = price // m_int
        btn_text = f"{months} мес. — {price}₽ ({price_per_month}₽/мес)"
        btns.append([InlineKeyboardButton(text=btn_text, callback_data=f"buy_{t_type}_{months}")])
        
    btns.append([InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="tariffs")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("buy_"))
async def create_payment_link(callback: CallbackQuery):
    parts = callback.data.split("_")
    t_type, months = ("_".join(parts[1:-1]), parts[-1])
    
    info = TARIFFS_CONFIG[t_type]
    price = info['prices'][months]
    plan_display = f"{info['name']} ({months} мес.)"

    idempotency_key = str(uuid.uuid4())
    payment = Payment.create({
        "amount": {"value": f"{price}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
        "capture": True,
        "description": f"Подписка TrubaVPN: {plan_display}",
        "metadata": {"user_id": callback.from_user.id, "t_type": t_type, "months": months}
    }, idempotency_key)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить (СБП / Карты)", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="❌ Отменить покупку", callback_data=f"type_{t_type}")]
    ])

    await callback.message.edit_text(
        f"💳 <b>Оформление подписки</b>\n\n"
        f"Тариф: <b>{plan_display}</b>\n"
        f"К оплате: <b>{price}₽</b>\n\n"
        f"1. Нажмите кнопку «Оплатить»\n"
        f"2. После завершения платежа нажмите «Проверить оплату».\n\n"
        f"🆘 <i>Если возникли вопросы или нужен чек, пишите в поддержку: {SUPPORT_CONTACT}</i>\n\n"
        f"<i>Ссылка на оплату действительна 15 минут.</i>",
        reply_markup=markup, 
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    payment_id = callback.data.replace("check_", "")
    payment = Payment.find_one(payment_id)

    if payment.status == 'succeeded':
        user_id = int(payment.metadata['user_id'])
        t_type, months = payment.metadata['t_type'], payment.metadata['months']
        plan_name = f"{TARIFFS_CONFIG[t_type]['name']} ({months} мес.)"
        
        expiry_ts = await activate_user_in_db(user_id, plan_name, months)
        lnk = get_vpn_link(user_id, callback.from_user.username, expiry_ts, plan_name)

        await callback.message.edit_text(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф <b>{plan_name}</b> активирован.\n"
            f"📅 До: <b>{time.strftime('%d.%m.%Y', time.localtime(expiry_ts))}</b>\n\n"
            f"🔗 <b>Твоя подписка (нажми, чтобы скопировать):</b>\n{hcode(lnk)}\n\n"
            f"Добавь эту ссылку в приложение (Nekobox, v2rayNG или Streisand), чтобы увидеть все доступные серверы.", parse_mode="HTML"
        )
        
        admin_txt = f"💰 <b>Успешная оплата!</b>\nСумма: {payment.amount.value}₽\nЮзер: @{callback.from_user.username}\nID: <code>{user_id}</code>"
        for admin in ADMINS:
            try: await bot.send_message(admin, admin_txt, parse_mode="HTML")
            except: pass
    else:
        await callback.answer("⏳ Оплата ещё не поступила. Попробуйте через минуту.", show_alert=True)

# --- ГЛАВНОЕ МЕНЮ И СТАРТ ---

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username', (message.from_user.id, message.from_user.username, r_id))
    conn.commit()
    conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} — быстрый и анонимный доступ в сеть!", reply_markup=main_panel(), parse_mode="HTML")

def main_panel():
    btns = [
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Реф. программа", callback_data="ref_program")],
        [InlineKeyboardButton(text="📖 О сервисе", callback_data="about_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    d = get_user_data(callback.from_user.id)
    if not d: return
    now = int(time.time())
    
    is_active = d[0] > now
    expiry_text = time.strftime('%d.%m.%Y', time.localtime(d[0])) if is_active else "❌ Не активна"
    plan_text = d[3] if is_active else "Отсутствует"

    text = (
        f"👤 <b>Ваш профиль</b>\n"
        f"—————\n"
        f"🆔 ID: <code>{callback.from_user.id}</code>\n"
        f"💎 Тариф: <b>{plan_text}</b>\n"
        f"📅 Срок до: <b>{expiry_text}</b>"
    )

    if is_active:
        lnk = get_vpn_link(callback.from_user.id, d[2], d[0], d[3])
        text += f"\n\n🔗 <b>Ваша подписка:</b>\n{hcode(lnk)}"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} Главное меню:", reply_markup=main_panel(), parse_mode="HTML")

@router.callback_query(F.data == "ref_program")
async def show_ref(callback: CallbackQuery):
    d = get_user_data(callback.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    text = (
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Пригласите 5 друзей, которые купят любую подписку, и получите тариф <b>ПРЕМИУМ НАВСЕГДА!</b>\n\n"
        f"👥 Приглашено друзей: <b>{d[5] if d else 0} / 5</b>\n\n"
        f"🔗 Ваша ссылка для приглашения:\n{hcode(link)}"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

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

async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
