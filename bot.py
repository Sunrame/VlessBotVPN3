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

# Твой ID из User Summary
ADMINS = [1883819477, 939883122] 
SUPPORT_CONTACT = "@vvvvvpppnn"
CHANNEL_LINK = "https://t.me/Truba_VPN"

Configuration.configure(SHOP_ID, YOOKASSA_KEY)

# Тарифы согласно image_829c9b.png
TARIFFS_CONFIG = {
    "trial": {"name": "Пробный (1 день)", "price": 10, "days": 1, "desc": "— Тестовый доступ на 24 часа"},
    "1_dev": {"name": "1 устройство", "price": 99, "days": 30, "desc": "— 99 рублей за 30 дней"},
    "2_dev": {"name": "2 устройства", "price": 179, "days": 30, "desc": "— 179 рублей за 30 дней"},
    "5_dev": {"name": "5 устройств", "price": 349, "days": 30, "desc": "— 349 рублей за 30 дней"}
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                    (user_id INTEGER PRIMARY KEY, username TEXT, referrer_id INTEGER, 
                    expiry_date INTEGER DEFAULT 0, sub_token TEXT, has_paid INTEGER DEFAULT 0)''')
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
    token = row[1] if row else f"truba_{uuid.uuid4().hex[:8]}"
    
    new_expiry = max(current_expiry, now) + added_sec
    cursor.execute('UPDATE users SET expiry_date = ?, sub_token = ? WHERE user_id = ?', 
                   (new_expiry, token, user_id))
    conn.commit(); conn.close()
    return new_expiry, token

# --- КЛАВИАТУРЫ ---
def main_kb():
    btns = [
        [InlineKeyboardButton(text="💎 Купить VPN", callback_data="tariffs"), InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🤝 Рефералы", callback_data="ref_program"), InlineKeyboardButton(text="🎟 Промокод", callback_data="promo_enter")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support_tab"), InlineKeyboardButton(text="📖 Инфо", callback_data="info_tab")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    init_db()
    u_id = message.from_user.id
    r_id = int(command.args) if command.args and command.args.isdigit() and int(command.args) != u_id else None
    
    conn = sqlite3.connect('users.db')
    if not conn.execute('SELECT user_id FROM users WHERE user_id = ?', (u_id,)).fetchone():
        conn.execute('INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)', 
                     (u_id, message.from_user.username, r_id))
        conn.commit()
    conn.close()
    await message.answer(f"🚀 Добро пожаловать в {hbold('TrubaVPN')}!", reply_markup=main_kb(), parse_mode="HTML")

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    btns = [[InlineKeyboardButton(text=f"{v['name']} — {v['price']}₽", callback_data=f"buy_{k}")] for k, v in TARIFFS_CONFIG.items()]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await callback.message.edit_text("🛒 **Выберите тариф:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: CallbackQuery):
    t_key = callback.data.replace("buy_", "")
    info = TARIFFS_CONFIG[t_key]
    payment = Payment.create({
        "amount": {"value": f"{info['price']}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/trubavpnbot"},
        "capture": True,
        "metadata": {"user_id": callback.from_user.id, "days": info['days']}
    }, str(uuid.uuid4()))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment.id}")]
    ])
    await callback.message.edit_text(f"💳 {hbold(info['name'])}\n{info['desc']}\n\nК оплате: {info['price']}₽", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_"))
async def check_payment_status(callback: CallbackQuery):
    pay_id = callback.data.replace("check_", "")
    payment = Payment.find_one(pay_id)
    if payment.status == 'succeeded':
        u_id = int(payment.metadata['user_id'])
        days = int(payment.metadata['days'])
        expiry, token = await activate_subscription(u_id, days)
        
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT referrer_id, has_paid FROM users WHERE user_id = ?', (u_id,))
        row = cursor.fetchone()
        
        # Рефералка за ПЕРВУЮ покупку
        if row and row[0] and row[1] == 0:
            ref_id = row[0]
            await activate_subscription(u_id, 7)
            await activate_subscription(ref_id, 7)
            cursor.execute('UPDATE users SET has_paid = 1 WHERE user_id = ?', (u_id,))
            conn.commit()
            try:
                await bot.send_message(ref_id, "🎊 Ваш друг оплатил подписку! Вам и ему начислено по 7 дней бонуса.")
            except: pass
        else:
            cursor.execute('UPDATE users SET has_paid = 1 WHERE user_id = ?', (u_id,))
            conn.commit()
        conn.close()
        await callback.message.edit_text(f"✅ Оплата прошла!\nДо: {time.strftime('%d.%m.%Y', time.localtime(expiry))}\nКлюч: {hcode(token)}", parse_mode="HTML")
    else:
        await callback.answer("⏳ Платеж еще не подтвержден.", show_alert=True)

# --- ПРОМОКОДЫ ---
@router.callback_query(F.data == "promo_enter")
async def promo_input(callback: CallbackQuery):
    await callback.message.answer("⌨️ Введите промокод:")
    await callback.answer()

@router.message(lambda m: not m.text.startswith('/'))
async def handle_promo(message: types.Message):
    code = message.text.upper().strip()
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT days FROM promos WHERE code = ?', (code,))
    row = cursor.fetchone()
    if row:
        days = row[0]
        expiry, _ = await activate_subscription(message.from_user.id, days)
        cursor.execute('DELETE FROM promos WHERE code = ?', (code,))
        conn.commit()
        await message.answer(f"✅ Промокод активирован! Добавлено {days} дней.")
    else:
        await message.answer("❌ Неверный или использованный промокод.")
    conn.close()

# --- ИНФО И ПОДДЕРЖКА ---
@router.callback_query(F.data == "info_tab")
async def info_tab(callback: CallbackQuery):
    btns = [
        [InlineKeyboardButton(text="📢 Канал с инструкциями", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", url="https://telegra.ph/Soglashenie-ob-ispolzovanii-04-27")],
        [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-obrabotki-04-27")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ]
    await callback.message.edit_text("📖 **Информация и документы:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data == "support_tab")
async def support_tab(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Написать менеджеру", url=f"https://t.me/{SUPPORT_CONTACT.replace('@','')}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await callback.message.edit_text("🆘 **Поддержка**\nЕсть вопросы? Мы на связи!", reply_markup=kb)

@router.callback_query(F.data == "ref_program")
async def ref_program(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    await callback.message.edit_text(f"🤝 **Реферальная программа**\n\nЗа каждую ПЕРВУЮ покупку друга вы оба получите по **7 дней**!\n\n🔗 Твоя ссылка: {hcode(link)}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))

@router.callback_query(F.data == "profile")
async def profile_tab(callback: CallbackQuery):
    conn = sqlite3.connect('users.db')
    d = conn.execute('SELECT expiry_date, sub_token FROM users WHERE user_id = ?', (callback.from_user.id,)).fetchone()
    conn.close()
    now = int(time.time())
    days = (d[0] - now) // 86400 if d and d[0] > now else 0
    text = f"👤 **Профиль**\n\n📅 Осталось дней: `{max(0, days)}`"
    if d and d[1] and d[0] > now: text += f"\n🔑 Ключ: {hcode(d[1])}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))

@router.callback_query(F.data == "back")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(f"🚀 {hbold('TrubaVPN')} готов к работе!", reply_markup=main_kb(), parse_mode="HTML")

# --- АДМИН ПАНЕЛЬ ---
@router.message(Command("broadcast"))
async def admin_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    conn = sqlite3.connect('users.db'); users = conn.execute('SELECT user_id FROM users').fetchall(); conn.close()
    for u in users:
        try: await bot.send_message(u[0], f"📢 **Рассылка:**\n\n{command.args}", parse_mode="HTML")
        except: continue
    await message.answer("✅ Готово.")

@router.message(Command("add_promo"))
async def add_promo(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMINS: return
    code, days = command.args.split()
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT INTO promos (code, days) VALUES (?, ?)', (code.upper(), int(days)))
    conn.commit(); conn.close()
    await message.answer(f"✅ Промокод {code} на {days} дней создан.")

async def main():
    init_db(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
