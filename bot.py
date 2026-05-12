import os, uuid, requests, logging, time, sqlite3, asyncio, json, urllib3
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

# Твой ID и список админов
ADMINS = [5906233405]
SUPPORT_CONTACT = "@vvvvvpppnn"
CHANNEL_ID = "@Truba_VPN"

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# Тарифы согласно image_829c9b.png
TARIFFS_CONFIG = {
    "trial": {
        "name": "Пробный (1 день)", 
        "price": 10, "days": 1, 
        "desc": "— Тестовый доступ на 24 часа"
    },
    "1_dev": {
        "name": "1 устройство", 
        "price": 99, "days": 30, 
        "desc": "— Оптимально для одного телефона"
    },
    "2_dev": {
        "name": "2 устройства", 
        "price": 179, "days": 30, 
        "desc": "— Можно использовать на телефоне и ПК"
    },
    "5_dev": {
        "name": "5 устройств", 
        "price": 349, "days": 30, 
        "desc": "— Для всей семьи или группы друзей"
    }
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
session = requests.Session()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, 
                    expiry_date INTEGER DEFAULT 0, sub_token TEXT)''')
    # Таблица промокодов
    cursor.execute('''CREATE TABLE IF NOT EXISTS promos 
                    (code TEXT PRIMARY KEY, days INTEGER)''')
    conn.commit(); conn.close()

async def activate_subscription(user_id, days):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    now = int(time.time())
    added_sec = days * 24 * 60 * 60
    
    cursor.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    
    current_expiry = row[0] if row else 0
    token = row[1] if row else None
    
    # Продлеваем от текущей даты или от даты окончания (если она еще не прошла)
    new_expiry = max(current_expiry, now) + added_sec
    
    # Если токена нет, здесь должна быть логика обращения к панели (add_user_to_panel)
    if not token:
        token = f"sub_{uuid.uuid4().hex[:10]}" # Заглушка, замени на вызов панели
    
    cursor.execute('UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?', (new_expiry, token, user_id))
    conn.commit(); conn.close()
    return new_expiry, token

# --- КЛАВИАТУРЫ ---
def main_panel():
    btns = [
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Реф. программа", callback_data="ref_program"), InlineKeyboardButton(text="🎟 Промокод", callback_data="promo_enter")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support_tab"), InlineKeyboardButton(text="📖 Инфо", callback_data="about_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    r_id = int(command.args) if command.args and command.args.isdigit() else None
    u_id = message.from_user.id
    
    conn = sqlite3.connect('users.db')
    user = conn.execute('SELECT user_id FROM users WHERE user_id = ?', (u_id,)).fetchone()
    
    if not user:
        conn.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)', 
                     (u_id, message.from_user.username, r_id))
        conn.commit()
        # Рефка: 7 дней обоим согласно image_829c9b.png
        if r_id and r_id != u_id:
            await activate_subscription(u_id, 7)
            await activate_subscription(r_id, 7)
            try: await bot.send_message(r_id, "🎁 По вашей ссылке пришел друг! Вам и ему начислено по 7 дней подписки.")
            except: pass
            
    conn.close()
    await message.answer(f"🚀 {hbold('TrubaVPN')} готов к работе!", reply_markup=main_panel(), parse_mode="HTML")

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    btns = []
    for k, v in TARIFFS_CONFIG.items():
        # Расчет выгоды для 30-дневных тарифов (просто для наглядности)
        btn_text = f"🔹 {v['name']} — {v['price']}₽"
        btns.append([InlineKeyboardButton(text=btn_text, callback_data=f"buy_{k}")])
    
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")])
    await callback.message.edit_text("💎 <b>Выберите подходящий тариф:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@router.callback_query(F.data.startswith("buy_"))
async def create_pay(callback: CallbackQuery):
    t_key = callback.data.replace("buy_", "")
    info = TARIFFS_CONFIG[t_key]
    
    payment = Payment.create({
        "amount": {"value": f"{info['price']}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
        "capture": True,
        "metadata": {"user_id": callback.from_user.id, "days": info['days']}
    }, str(uuid.uuid4()))
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="tariffs")]
    ])
    await callback.message.edit_text(f"💳 <b>{info['name']}</b>\n{info['desc']}\n\nК оплате: {info['price']}₽", reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    pay_id = callback.data.replace("check_", "")
    payment = Payment.find_one(pay_id)
    if payment.status == 'succeeded':
        days = int(payment.metadata['days'])
        expiry, token = await activate_subscription(callback.from_user.id, days)
        await callback.message.edit_text(f"✅ Оплата прошла! Подписка активна до {time.strftime('%d.%m.%Y', time.localtime(expiry))}\n\nВаш ключ: {hcode(token)}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Платеж еще не обработан.", show_alert=True)

# --- ВКЛАДКА ПОДДЕРЖКИ И ИНФО ---
@router.callback_query(F.data == "support_tab")
async def support_tab(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Написать менеджеру", url=f"https://t.me/{SUPPORT_CONTACT.replace('@','')}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]
    ])
    await callback.message.edit_text("🆘 <b>Служба поддержки</b>\n\nВозникли вопросы или проблемы? Напишите нам, и мы поможем!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "about_menu")
async def about_menu(callback: CallbackQuery):
    btns = [
        [InlineKeyboardButton(text="📢 Канал с инструкциями", url=f"https://t.me/{CHANNEL_ID.replace('@','')}")],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-materialov-i-servisov-internet-sajta-04-27")],
        [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-servisa-TrubaVPN-04-27")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]
    ]
    await callback.message.edit_text("📖 <b>Информация:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

# --- АДМИН-ФУНКЦИИ ---
@router.message(Command("broadcast"))
async def admin_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    if not command.args: return await message.answer("Использование: `/broadcast Текст рассылки`")
    
    conn = sqlite3.connect('users.db')
    users = conn.execute('SELECT user_id FROM users').fetchall()
    conn.close()
    
    sent = 0
    for u in users:
        try:
            await bot.send_message(u[0], f"📢 <b>Рассылка:</b>\n\n{command.args}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except: continue
    await message.answer(f"✅ Сообщение получили {sent} пользователей.")

@router.message(Command("add_promo"))
async def admin_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    try:
        code, days = command.args.split()
        conn = sqlite3.connect('users.db')
        conn.execute('INSERT INTO promos (code, days) VALUES (?, ?)', (code.upper(), int(days)))
        conn.commit(); conn.close()
        await message.answer(f"✅ Промокод `{code.upper()}` на {days} дней создан.")
    except:
        await message.answer("Ошибка. Формат: `/add_promo СЕКРЕТ 30`")

# --- ОСТАЛЬНОЕ ---
@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    
    now = int(time.time())
    is_active = d and d[0] > now
    days_left = (d[0] - now) // 86400 if is_active else 0
    
    text = f"👤 <b>Ваш профиль</b>\n\n"
    text += f"📅 Осталось дней: <b>{days_left}</b>\n"
    if is_active:
        text += f"🔗 Ключ: {hcode(d[1])}"
    else:
        text += "❌ Подписка не активна"
        
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "ref_program")
async def ref_program(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    text = (f"🤝 <b>Приглашай друзей — получай бонусы!</b>\n\n"
            f"За каждого друга, который перейдет по твоей ссылке, "
            f"и ты, и он получите <b>+7 дней</b> к подписке!\n\n"
            f"🔗 Твоя ссылка:\n{hcode(link)}")
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "to_main")
async def to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} готов к работе!", reply_markup=main_panel(), parse_mode="HTML")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
