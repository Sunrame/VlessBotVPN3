"""
Конфигурация веб-админки. Читает те же переменные окружения, что и бот.
Админы “прописаны в коде бота” — берём их из ADMIN_ID_1 / ADMIN_ID_2.
Зеркалит модель нового бота: тарифы VPN / VPN‑обход, докупка устройств и ГБ,
реферальная система.
"""
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()


def _clean(url: str) -> str:
    return (url or "").rstrip("/")


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)

REMNAWAVE_URL = _clean(os.environ.get("REMNAWAVE_URL", ""))
REMNAWAVE_TOKEN = os.environ.get("REMNAWAVE_TOKEN", "")
REMNAWAVE_COOKIE = os.environ.get("REMNAWAVE_COOKIE", "")
SUB_BASE_URL = _clean(os.environ.get("SUB_BASE_URL", ""))

# Сквады / нода белых списков — точно как в боте (без жёсткого дефолта).
SQUAD_UUID = os.environ.get("SQUAD_UUID_BASIC", "")
SQUAD_UUID_BASIC = SQUAD_UUID
SQUAD_UUID_WHITELIST = os.environ.get("SQUAD_UUID_WHITELIST", SQUAD_UUID)
WHITELIST_NODE_UUID = os.environ.get("WHITELIST_NODE_UUID", "")

# Админы — сколько угодно.
# Поддерживаются два способа задать администраторов (можно смешивать):
#   1) ADMIN_IDS=111,222,333  — список ID через запятую (или пробел/;)
#   2) ADMIN_ID_1, ADMIN_ID_2, ADMIN_ID_3, ...  — по одному ID на переменную,
#      без ограничения по количеству.
ADMIN_IDS: list[int] = []


def _add_admin(_raw: str) -> None:
    _raw = (_raw or "").strip()
    if _raw.isdigit():
        _id = int(_raw)
        if _id not in ADMIN_IDS:
            ADMIN_IDS.append(_id)


# 1) Список через запятую/пробел/точку с запятой.
import re as _re
for _part in _re.split(r"[\s,;]+", os.environ.get("ADMIN_IDS", "")):
    _add_admin(_part)

# 2) Пронумерованные переменные ADMIN_ID_1, ADMIN_ID_2, ... (любое количество).
for _key, _val in os.environ.items():
    if _re.fullmatch(r"ADMIN_ID_\d+", _key):
        _add_admin(_val)

# Поддержка — теперь это просто юзернейм (кнопка ведёт в личку).
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "vvvvvpppnn")
SUPPORT_URL = "https://t.me/" + SUPPORT_USERNAME
# Юзернейм основного бота — для реферальных ссылок в личном кабинете.
# Определяется автоматически через getMe; это — запасной вариант.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "trubavpnbot").lstrip("@")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/Truba_VPN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Truba_VPN")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
AUTH_CODE_TTL_MIN = int(os.environ.get("AUTH_CODE_TTL_MIN", "5"))
# Вход в личный кабинет по одноразовому коду из бота.
LOGIN_CODE_TTL_MIN = int(os.environ.get("LOGIN_CODE_TTL_MIN", "5"))       # сколько минут действует код на активацию
CABINET_SESSION_HOURS = int(os.environ.get("CABINET_SESSION_HOURS", "1"))  # сколько часов живёт сессия после входа

# ЮKassa — те же ключи, что у бота (для оплат из личного кабинета на сайте).
SHOP_ID = os.environ.get("SHOP_ID", "")
YOOKASSA_KEY = os.environ.get("YOOKASSA_KEY", "")
# Публичный адрес сайта — для ссылок личного кабинета и return_url ЮKassa.
# Если пусто — берётся автоматически из адреса запроса.
SITE_URL = _clean(os.environ.get("SITE_URL", ""))

# ────────────────────────────────────
#  ТАРИФЫ — ЗЕРКАЛО НОВОГО БОТА
# ──────────────────────────────────
TRIAL = {
    "name": "Пробная подписка",
    "price": 10,
    "days": 1,
    "hwid": 1,
    "squad": SQUAD_UUID_WHITELIST,
    "whitelist_gb": 3,
}

PLANS = {
    "vpn": {
        "key": "vpn",
        "name": "VPN",
        "price_month": 99,
        "device_price": 50,
        "squad": SQUAD_UUID_BASIC,
        "whitelist_gb": 0,
        "desc": "Более трёх локаций, 1 устройство, трафик не ограничен.",
    },
    "vpn_bypass": {
        "key": "vpn_bypass",
        "name": "VPN с обходом белых списков",
        "price_month": 149,
        "device_price": 70,
        "squad": SQUAD_UUID_WHITELIST,
        "whitelist_gb": 20,
        "desc": "Более трёх локаций, 1 устройство, трафик не ограничен. Трафик на обход — 20 ГБ.",
    },
}

MONTH_CHOICES = [1, 3, 6, 12]

# Докупка трафика на белых списках: цена за 1 ГБ.
WHITELIST_PRICE_PER_GB = int(os.environ.get("WHITELIST_PRICE_PER_GB", "3"))

# Реферальная система: процент с оплаты друга и порог вывода.
REFERRAL_PERCENT = 50
REFERRAL_MIN_WITHDRAW = 1000

# Лимит устройств: 1..20 + 0 = без лимита (как в боте).
HWID_LABELS = {i: f"{i} устр." for i in range(1, 21)}
HWID_LABELS[0] = "Без лимита"
# Совместимость со старыми шаблонами.
HWID_OPTIONS = HWID_LABELS

# Человеческие названия тарифа пользователя (users.plan).
PLAN_NAMES = {
    "trial": "Пробный",
    "vpn": "VPN",
    "vpn_bypass": "VPN + обход",
}

# Расшифровка payments.tariff_key. В новом боте туда пишется вид
# покупки (kind), а не ключ тарифа. Старые ключи тоже учтены.
PAY_LABELS = {
    # новые kind
    "gift": "Выдано админом",
    "extend": "Продление",
    "trial": "Пробная подписка",
    "plan": "Тариф VPN",
    "device": "Доп. устройство",
    "upgrade": "Обход белых списков",
    "wl_topup": "Докупка трафика",
    # легаси
    "vpn": "VPN",
    "vpn_bypass": "VPN + обход",
    "1_dev": "1 устройство",
    "2_dev": "2 устройства",
    "5_dev": "5 устройств",
    "family": "Семейный",
}

MSK = timezone(timedelta(hours=3))
