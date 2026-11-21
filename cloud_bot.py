"""Cloud Networks VPN — Telegram-бот на HTML-виджетах (Bot API 10.4).

Весь интерфейс — виджеты внутри сообщений:
  • главный виджет (MAIN_HTML): статус подписки, покупка, друзья, промокод;
  • инструкция (GUIDE_HTML): выбор устройства и шаги подключения;
  • карточка (CARD_HTML): универсальный экран, который целиком рисует сервер, —
    им сделаны админ-панель, уведомления, рассылки и ответы на промокоды.

Под главным виджетом всегда есть обычные кнопки и подпись с советом обновить
Telegram: в старых версиях приложения виджет не показывается.

Бот работает сам по себе, без aiogram: long polling getUpdates, все вызовы —
прямые запросы к Bot API. Так новые поля (widget_query) не зависят от того,
успела ли библиотека их поддержать.

Таблицы — с префиксом cn_, поэтому базу можно делить с другими ботами.
"""
import asyncio
import base64
import html
import json
import logging
import math
import os
import re
import secrets
import time
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cloud")
logging.getLogger("httpx").setLevel(logging.WARNING)   # иначе в логах будет URL с токеном


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


# ─────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────
BOT_TOKEN = env("BOT_TOKEN")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
BRAND = env("CLOUD_BRAND", "Cloud Networks VPN")
DATABASE_URL = env("DATABASE_URL").replace("postgres://", "postgresql://", 1)

REMNAWAVE_URL = env("REMNAWAVE_URL").rstrip("/")
REMNAWAVE_TOKEN = env("REMNAWAVE_TOKEN")
REMNAWAVE_COOKIE = env("REMNAWAVE_COOKIE")
SUB_BASE_URL = env("SUB_BASE_URL").rstrip("/")
REMNA_PREFIX = env("CLOUD_REMNA_PREFIX", "cloud").rstrip("_")
SQUAD_BASIC = env("SQUAD_UUID_BASIC")
SQUAD_WL = env("SQUAD_UUID_WHITELIST", SQUAD_BASIC)
WL_NODES = [x.strip() for x in env("WHITELIST_NODE_UUID").split(",") if x.strip()]

SUPPORT = env("CLOUD_SUPPORT", env("SUPPORT_USERNAME", "cloudnetworks_support")).lstrip("@")
BOT_USERNAME = ""  # заполняется при старте из getMe

ADMIN_IDS: set[int] = set()
for _part in (env("ADMIN_IDS") + "," + env("ADMIN_ID_1") + "," + env("ADMIN_ID_2")).split(","):
    if _part.strip().isdigit():
        ADMIN_IDS.add(int(_part.strip()))

# Тариф: одна подписка на 2 устройства. Цены по умолчанию — правятся в админке.
DEFAULTS = {
    "price": env_int("PLAN_PRICE", 200),        # ₽ в месяц
    "bd": env_int("PLAN_DEVICES", 2),           # устройств в подписке
    "dp": env_int("DEVICE_PRICE", env_int("PLAN_BYPASS_DEVICE_PRICE", 50)),          # ₽ за доп. устройство (навсегда)
    "gp": env_int("GB_PRICE", env_int("WHITELIST_PRICE_PER_GB", 3)),               # ₽ за 1 ГБ 📶
    "pg": env_int("PLAN_GB", env_int("PLAN_BYPASS_WHITELIST_GB", 20)),               # ГБ 📶 в месяц в подписке
    "d3": 0, "d6": 0, "d12": 0,                 # скидки за срок, %
    "wk_on": 1, "wk_pct": 20,                   # скидка выходного дня
    "wk_code": env("WEEKEND_CODE", "CLUD20").upper(),
    "wk_last": "",                              # когда последний раз разослали
}
MONTHS = (1, 3, 6, 12)
MAX_TOTAL_DISCOUNT = 50

TRIAL_DAYS = env_int("TRIAL_DAYS", 1)
TRIAL_DEVICES = env_int("TRIAL_DEVICES", 1)
TRIAL_GB = env_int("TRIAL_GB", env_int("TRIAL_WHITELIST_GB", 1))
TRIAL_ON = env("TRIAL_ENABLED", "1") in ("1", "true", "yes", "on")
REF_DAYS = env_int("REFERRAL_DAYS", 30)

PLATEGA_API = env("PLATEGA_API_URL", "https://app.platega.io").rstrip("/")
PLATEGA_ID = env("PLATEGA_MERCHANT_ID")
PLATEGA_SECRET = env("PLATEGA_SECRET")
PLATEGA_METHODS = [int(x) for x in env("PLATEGA_METHODS", env("PLATEGA_METHOD", "2,11,14")).split(",")
                   if x.strip().isdigit()]
PLATEGA_ON = env("PLATEGA_ENABLED", "1") in ("1", "true", "yes", "on") and bool(PLATEGA_ID and PLATEGA_SECRET)

TON_WALLET = env("TON_WALLET")
TONCENTER_KEY = env("TONCENTER_API_KEY")
TON_RATE_FALLBACK = float(env("TON_RUB_RATE", "0") or 0)

MSK = timezone(timedelta(hours=3))
MONTHS_RU = "янв фев мар апр мая июн июл авг сен окт ноя дек".split()

HTTP: httpx.AsyncClient | None = None
pool: asyncpg.Pool | None = None


def now() -> int:
    return int(time.time())


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def fmt_date(ts: int) -> str:
    d = datetime.fromtimestamp(int(ts), MSK)
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ─────────────────────────────────────────────
#  BOT API
# ─────────────────────────────────────────────
class TgError(Exception):
    pass


async def tg(method: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    for attempt in range(3):
        try:
            r = await HTTP.post(API + method, json=params,
                                timeout=70 if method == "getUpdates" else 30)
            data = r.json()
        except Exception as e:
            if attempt == 2:
                raise TgError(f"network: {e!r}")
            await asyncio.sleep(1 + attempt)
            continue
        if data.get("ok"):
            return data.get("result")
        retry = (data.get("parameters") or {}).get("retry_after")
        if data.get("error_code") == 429 and retry and attempt < 2:
            await asyncio.sleep(min(int(retry), 30) + 0.5)
            continue
        raise TgError(data.get("description") or str(data))
    raise TgError("unreachable")


def ikb(rows) -> dict:
    """[[("Текст", "callback"|"https://…")]] → InlineKeyboardMarkup."""
    out = []
    for row in rows:
        line = []
        for text, act in row:
            key = "url" if act.startswith("https://") else "callback_data"
            line.append({"text": text, key: act})
        out.append(line)
    return {"inline_keyboard": out}


async def send_text(chat_id: int, text: str, kb=None) -> dict | None:
    try:
        return await tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        reply_markup=kb, link_preview_options={"is_disabled": True})
    except TgError as e:
        await note_send_error(chat_id, e)
        return None


async def note_send_error(chat_id: int, e: Exception) -> None:
    msg = str(e).lower()
    if "blocked" in msg or "deactivated" in msg or "chat not found" in msg:
        await db_exec("UPDATE cn_users SET blocked=TRUE WHERE user_id=$1", chat_id)
    else:
        log.warning("send to %s: %s", chat_id, e)


async def notify_admins(text: str) -> None:
    for aid in ADMIN_IDS:
        await send_text(aid, text)


# ─────────────────────────────────────────────
#  БАЗА
# ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS cn_users (
    user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
    created_at BIGINT DEFAULT 0, referrer_id BIGINT,
    plan TEXT DEFAULT '', trial_used BOOLEAN DEFAULT FALSE,
    expire_ts BIGINT DEFAULT 0, extra_dev INTEGER DEFAULT 0,
    blocked BOOLEAN DEFAULT FALSE, banned BOOLEAN DEFAULT FALSE,
    promo TEXT, promo_pct INTEGER DEFAULT 0, promo_until BIGINT DEFAULT 0,
    wk_used TEXT DEFAULT '', rem_exp BIGINT DEFAULT 0, rem_stage INTEGER DEFAULT 0,
    last_main BIGINT DEFAULT 0, amode TEXT DEFAULT '', aw BIGINT DEFAULT 0,
    paid_total INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cn_wl (
    user_id BIGINT PRIMARY KEY, gb_limit INTEGER DEFAULT 0, base_gb INTEGER DEFAULT 0,
    period_start BIGINT DEFAULT 0, cut_off BOOLEAN DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS cn_invoices (
    id TEXT PRIMARY KEY, user_id BIGINT, kind TEXT, qty INTEGER, amount INTEGER,
    method TEXT, ext_id TEXT, url TEXT, ton_nano BIGINT DEFAULT 0, msg_hash TEXT,
    promo TEXT, wk TEXT, status TEXT DEFAULT 'PENDING', confirmed BOOLEAN DEFAULT FALSE,
    tries INTEGER DEFAULT 0, created_at BIGINT DEFAULT 0, paid_at BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cn_invoices_status ON cn_invoices (status, created_at);
CREATE INDEX IF NOT EXISTS cn_invoices_user ON cn_invoices (user_id, created_at);
CREATE TABLE IF NOT EXISTS cn_promos (
    code TEXT PRIMARY KEY, kind TEXT, value INTEGER, uses_left INTEGER,
    used INTEGER DEFAULT 0, created_at BIGINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cn_promo_uses (
    code TEXT, user_id BIGINT, at BIGINT, PRIMARY KEY (code, user_id)
);
CREATE TABLE IF NOT EXISTS cn_ref (
    friend_id BIGINT PRIMARY KEY, referrer_id BIGINT, at BIGINT
);
CREATE TABLE IF NOT EXISTS cn_settings (key TEXT PRIMARY KEY, value TEXT);
"""


async def db_exec(sql: str, *args):
    async with pool.acquire() as c:
        return await c.execute(sql, *args)


async def db_row(sql: str, *args):
    async with pool.acquire() as c:
        return await c.fetchrow(sql, *args)


async def db_rows(sql: str, *args):
    async with pool.acquire() as c:
        return await c.fetch(sql, *args)


async def db_val(sql: str, *args):
    async with pool.acquire() as c:
        return await c.fetchval(sql, *args)


async def touch_user(u: dict, referrer: int | None = None) -> dict:
    """Создаёт/обновляет пользователя. Реферал записывается только новому."""
    uid = u["id"]
    if referrer == uid:
        referrer = None
    row = await db_row(
        "INSERT INTO cn_users (user_id, username, first_name, created_at, referrer_id) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (user_id) DO UPDATE SET "
        "username=EXCLUDED.username, first_name=EXCLUDED.first_name, blocked=FALSE "
        "RETURNING *, (xmax = 0) AS is_new",
        uid, u.get("username"), (u.get("first_name") or "")[:64], now(), referrer)
    return dict(row)


async def get_user(uid: int) -> dict | None:
    row = await db_row("SELECT * FROM cn_users WHERE user_id=$1", uid)
    return dict(row) if row else None


_settings_cache = {"ts": 0.0, "data": None}


async def settings() -> dict:
    if _settings_cache["data"] and time.time() - _settings_cache["ts"] < 15:
        return _settings_cache["data"]
    data = dict(DEFAULTS)
    for r in await db_rows("SELECT key, value FROM cn_settings"):
        k, v = r["key"], r["value"]
        if k in data and isinstance(DEFAULTS[k], int):
            try:
                data[k] = int(v)
            except ValueError:
                pass
        else:
            data[k] = v
    _settings_cache.update(ts=time.time(), data=data)
    return data


async def save_setting(key: str, value) -> None:
    await db_exec("INSERT INTO cn_settings (key, value) VALUES ($1,$2) "
                  "ON CONFLICT (key) DO UPDATE SET value=$2", key, str(value))
    _settings_cache["data"] = None


# ─────────────────────────────────────────────
#  REMNAWAVE
# ─────────────────────────────────────────────
def remna_name(uid: int) -> str:
    return f"{REMNA_PREFIX}_{uid}"


def _rh() -> dict:
    h = {"Authorization": f"Bearer {REMNAWAVE_TOKEN}", "Content-Type": "application/json"}
    if REMNAWAVE_COOKIE:
        h["Cookie"] = REMNAWAVE_COOKIE
    return h


LAST_REMNA_ERROR = ""


def _remna_err(where: str, detail) -> None:
    global LAST_REMNA_ERROR
    LAST_REMNA_ERROR = f"{where}: {detail}"[:400]
    log.error("[remna] %s", LAST_REMNA_ERROR)


_remna_cache: dict[int, tuple[float, dict | None]] = {}


async def remna_get(uid: int, cached: bool = False) -> dict | None:
    if cached:
        hit = _remna_cache.get(uid)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
    try:
        r = await HTTP.get(f"{REMNAWAVE_URL}/api/users/by-username/{remna_name(uid)}",
                           headers=_rh(), timeout=15)
    except Exception as e:
        _remna_err("GET", repr(e))
        raise
    if r.status_code == 404:
        user = None
    elif r.status_code >= 400:
        _remna_err(f"GET {r.status_code}", r.text[:300])
        raise RuntimeError("remna get failed")
    else:
        user = r.json().get("response")
    _remna_cache[uid] = (time.time(), user)
    return user


async def remna_patch(uid: int, payload: dict) -> dict | None:
    """PATCH /api/users. Панель 3.x узнаёт пользователя по username."""
    _remna_cache.pop(uid, None)
    body = dict(payload, username=remna_name(uid))
    try:
        r = await HTTP.patch(f"{REMNAWAVE_URL}/api/users", json=body, headers=_rh(), timeout=15)
    except Exception as e:
        _remna_err("PATCH", repr(e))
        return None
    if r.status_code >= 400:
        _remna_err(f"PATCH {r.status_code}", r.text[:300])
        return None
    return r.json().get("response")


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ts(value) -> int:
    if not value:
        return 0
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return 0


def squads_of(user: dict) -> list[str]:
    out = []
    for s in (user or {}).get("activeInternalSquads") or []:
        if isinstance(s, dict) and s.get("uuid"):
            out.append(s["uuid"])
        elif isinstance(s, str):
            out.append(s)
    return out


def sub_url(user: dict | None) -> str:
    if not user:
        return ""
    url = user.get("subscriptionUrl") or ""
    if not url and user.get("shortUuid") and SUB_BASE_URL:
        url = f"{SUB_BASE_URL}/{user['shortUuid']}"
    return url


async def remna_extend(uid: int, days: int, hwid: int | None = None,
                       squads: list[str] | None = None) -> dict | None:
    """Продлевает на days дней (от конца текущей подписки или от сейчас).
    Нет пользователя в панели — создаёт. days может быть отрицательным."""
    try:
        user = await remna_get(uid)
    except Exception:
        return None
    t = now()
    if not user:
        if days <= 0:
            return None
        payload = {
            "username": remna_name(uid), "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET", "expireAt": iso(t + days * 86400),
            "hwidDeviceLimit": hwid or 1, "telegramId": uid,
            "activeInternalSquads": squads or [SQUAD_BASIC],
        }
        try:
            r = await HTTP.post(f"{REMNAWAVE_URL}/api/users", json=payload, headers=_rh(), timeout=15)
        except Exception as e:
            _remna_err("POST", repr(e))
            return None
        if r.status_code >= 400:
            _remna_err(f"POST {r.status_code}", r.text[:300])
            return None
        _remna_cache.pop(uid, None)
        res = r.json().get("response")
    else:
        cur = parse_ts(user.get("expireAt"))
        new_exp = max(cur, t) + days * 86400 if days > 0 else max(t + 60, cur + days * 86400)
        payload = {"expireAt": iso(new_exp), "status": "ACTIVE"}
        if hwid is not None:
            payload["hwidDeviceLimit"] = hwid
        if squads is not None:
            payload["activeInternalSquads"] = squads
        res = await remna_patch(uid, payload)
    if res:
        await db_exec("UPDATE cn_users SET expire_ts=$2, rem_stage=0 WHERE user_id=$1",
                      uid, parse_ts(res.get("expireAt")))
    return res


# ── Трафик 📶 ─────────────────────────────────────────────────────────
_wl_cache: dict[str, tuple[float, dict]] = {}


def utc_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


async def wl_usage_since(start: str) -> dict:
    """{username: байты} на нодах 📶 с даты start по сегодня. Кэш 60 с."""
    hit = _wl_cache.get(start)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    total: dict = {}
    today = utc_date(now())
    for node in WL_NODES:
        try:
            r = await HTTP.get(f"{REMNAWAVE_URL}/api/bandwidth-stats/nodes/{node}/users",
                               params={"start": start, "end": today, "topUsersLimit": 100000},
                               headers=_rh(), timeout=30)
            r.raise_for_status()
            for u in ((r.json() or {}).get("response") or {}).get("topUsers") or []:
                name = u.get("username") or ""
                total[name] = total.get(name, 0) + int(u.get("total") or 0)
        except Exception as e:
            log.warning("[wl] usage %s: %s", node, e)
    _wl_cache[start] = (time.time(), total)
    return total


async def wl_used_gb(uid: int, period_start: int) -> float:
    if not WL_NODES:
        return 0.0
    start = utc_date(max(period_start, now() - 60 * 86400))
    used = (await wl_usage_since(start)).get(remna_name(uid), 0)
    return round(used / 1024 ** 3, 1)


# ─────────────────────────────────────────────
#  ВИДЖЕТЫ (HTML уходит в sendWidget как есть; лимит — 4096 байт после zlib)
# ─────────────────────────────────────────────
MAIN_HTML = r'''<style>
*{box-sizing:border-box}
body{margin:0;padding:12px;background:#050a17;color:#fff;font:15px/1.35 system-ui,sans-serif}
.hd{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px}
.hd small{display:block;font-weight:400;font-size:12px;color:#7d90c4}
.c{background:#0c1530;border:1px solid #19295a;border-radius:14px;padding:11px 12px;margin-top:10px}
.mt{color:#7d90c4;font-size:13px}
button{display:block;width:100%;margin-top:8px;padding:11px 6px;border:0;border-radius:11px;background:#132250;color:#fff;font:600 14px system-ui,sans-serif}
button.p{background:#2b63ff}
button[disabled]{color:#5f70a3}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.g button,.r button{margin:0}
.r{display:flex;gap:6px;margin-top:8px}
.r button{padding:9px 0}
.bar{height:6px;border-radius:3px;background:#19295a;margin-top:6px}
.bar i{display:block;height:6px;border-radius:3px;background:#3d7bff}
.w{display:flex;justify-content:space-between;align-items:center;gap:8px}
.w button{width:auto;margin:0;padding:8px 12px}
s{color:#7d90c4}
.big{font-size:22px;font-weight:700}
.sel{user-select:text;-webkit-user-select:text;word-break:break-all;font-size:13px}
.e{margin-top:8px;font-size:13px;color:#9fb6ff;text-align:center}
</style>
<div id=a></div>
<script>
const W=window.TelegramWidget||{},A=document.getElementById('a');
let S={},V='h',T={k:'p',q:1},E='';
function ld(x){try{const o=JSON.parse(x);if(o&&o.p)S=o}catch(e){}}
ld(W.chatData);if(S.v)V=S.v;
const MN='янв фев мар апр мая июн июл авг сен окт ноя дек'.split(' ');
function dt(t){const d=new Date(t*1e3);return d.getDate()+' '+MN[d.getMonth()]+' '+d.getFullYear()}
function dl(){return Math.max(0,Math.ceil((S.e-Date.now()/1e3)/86400))}
function B(t,a,c,x){return`<button data-a="${a}" class="${c||''}"${x?' disabled':''}>${t}</button>`}
function pr(f,dm){const D=Math.min(5000,10000-(100-dm)*(100-(S.pp||0)));return[f,Math.floor((f*(10000-D)+5000)/10000)]}
function cost(){const k=T.k,q=T.q;return k=='p'?pr(S.p*q,(S.dm||{})[q]||0):pr((k=='d'?S.dp:S.gp)*q,0)}
function act(){return S.s==1||S.s==3}
const HD=`<div class=hd><svg width=34 height=34 viewBox="0 0 34 34"><rect width=34 height=34 rx=10 fill="#2b63ff"/><path d="M11 23h12a4.5 4.5 0 0 0 .6-9 6.5 6.5 0 0 0-12.4 2A3.6 3.6 0 0 0 11 23z" fill="#fff"/></svg><div>Cloud Networks<small>VPN</small></div></div>`;
function home(){
 let h=HD,s=S.s;
 if(act())h+=`<div class=c><div class=w><b>${s==3?'Пробный доступ':'Подписка активна'}</b><span class=mt>${dl()} дн.</span></div><div class=mt>до ${dt(S.e)} · устройств: ${S.d}</div>`+(S.g?`<div class=w style="margin-top:8px"><span class=mt>📶</span><span class=mt>${S.g[0]} из ${S.g[1]} ГБ</span></div><div class=bar><i style="width:${Math.min(100,S.g[0]/S.g[1]*100||0)}%"></i></div>`:'')+'</div>';
 else if(s==2)h+=`<div class=c><b>Подписка закончилась</b><div class=mt>${dt(S.e)}. Продлите, чтобы снова подключиться</div></div>`;
 else h+=`<div class=c><b>${S.p} ₽ в месяц</b><div class=mt>${S.bd} устройства · все локации · 📶 ${S.pg} ГБ в месяц</div></div>`;
 if(S.pay&&S.pay.st=='ok')h+=`<div class=c style="border-color:#2b63ff"><b>Оплата прошла ✓</b><div class=mt>Инструкция по подключению — в сообщении ниже</div></div>`;
 if(S.wk)h+=`<div class=c style="border-color:#2b63ff"><div class=w><div><b>−${S.wk}% на выходных</b><div class=mt>промокод ${S.wc}</div></div>${B('Применить','wk','p')}</div></div>`;
 if(S.pp)h+=`<div class=e>Промокод ${S.pc}: скидка ${S.pp}% на следующую покупку</div>`;
 if(S.t)h+=B(`Попробовать бесплатно · ${S.td} дн.`,'trial','p');
 h+=B(s?'Продлить подписку':'Купить подписку','v:b',S.t?'':'p');
 h+=`<div class=g>${B('Подключить','guide','',!act())}${B('Друзья','v:r')}${B('Промокод','v:c')}${B('Поддержка','sp')}</div>`;
 return h}
function buy(){
 let h=`<div class=w><b style="font-size:17px">Покупка</b>${B('Назад','v:h')}</div>`;
 const p=S.pay;
 if(p&&p.st=='w'){
  h+=`<div class=c><div class=mt>К оплате</div><div class=big>${p.a} ${p.m=='t'?'TON':'₽'}</div><div class=mt>${p.m=='t'?'Ждём подтверждение платежа в сети, обычно до минуты':'После оплаты всё включится само, кнопки нажимать не нужно'}</div></div>`;
  if(p.u)h+=B('Перейти к оплате','go','p');
  return h+B('Выбрать другое','x')}
 if(S.s==1)h+=`<div class=r>${[['p','Подписка'],['d','Устройства'],['g','📶 ГБ']].map(x=>B(x[1],'k:'+x[0],T.k==x[0]?'p':'')).join('')}</div>`;
 const k=T.k,c=cost();
 let o=k=='p'?[1,3,6,12]:k=='d'?[1,2,3,5]:[10,25,50,100];
 h+=`<div class=r>${o.map(q=>B(q+(k=='p'?' мес':k=='g'?' ГБ':''),'q:'+q,T.q==q?'p':'')).join('')}</div>`;
 const dm=k=='p'?(S.dm||{})[T.q]||0:0;
 h+=`<div class=c><div class=mt>${k=='p'?`${S.bd+(S.xd||0)} устройства · 📶 ${S.pg*T.q} ГБ`+(dm?` · −${dm}% за срок`:''):k=='d'?`+${T.q} к лимиту, навсегда · ${S.dp} ₽ за шт.`:`${S.gp} ₽ за 1 ГБ`}</div><div class=big>${c[1]} ₽ ${c[1]<c[0]?`<s>${c[0]} ₽</s>`:''}</div>${S.pp?`<div class=mt>Промокод ${S.pc}: −${S.pp}%</div>`:''}</div>`;
 h+=B('Картой или СБП','pay:p','p');
 if(S.ton&&W.wallet)h+=B('TON-кошелёк','pay:t');
 return h}
function ref(){
 return`<div class=w><b style="font-size:17px">Друзья</b>${B('Назад','v:h')}</div><div class=c>Приглашайте друзей: за каждого, кто оформит подписку, вы получите <b>${S.rd} дней</b> бесплатно.<div class=mt style="margin-top:6px">Приглашено: ${S.r[0]} · оплатили: ${S.r[1]}</div></div><div class="c sel">${S.rl}</div><div class=g>${B('Скопировать','cp','p')}${B('Поделиться','sh')}</div>`}
function code(){
 return`<div class=w><b style="font-size:17px">Промокод</b>${B('Назад','v:h')}</div><div class=c>Отправьте промокод обычным сообщением в этот чат — он применится сам.</div>`+(S.pp?`<div class=c>Сейчас действует: <b>${S.pc}</b>, скидка ${S.pp}%</div>`:'')+`<div class=c><b>Каждые выходные −${S.wp}%</b><div class=mt>Промокод ${S.wc} работает в субботу и воскресенье</div></div>`}
function R(){A.innerHTML=({h:home,b:buy,r:ref,c:code}[V]||home)()+(E?`<div class=e>${E}</div>`:'')}
const ER={REJECTED:'Платёж отменён',TIMEOUT:'Бот не ответил, попробуйте ещё раз',RATE_LIMITED:'Слишком часто, подождите пару секунд',BUSY:'Подождите…',BAD_REQUEST:'Кошелёк отклонил платёж'};
function er(e){E=ER[e&&e.code]||'Ошибка, попробуйте ещё раз';R()}
function rq(d,f){E='…';R();W.request(d).then(a=>{let x={};try{x=JSON.parse(a.result)||{}}catch(e){}E=x.m||'';if(x.st)ld(JSON.stringify(x.st));if(f)f(x);R()},er)}
A.onclick=ev=>{
 const t=ev.target.closest('button');if(!t||t.disabled)return;
 const a=t.dataset.a,[n,v]=a.split(':');E='';
 if(W.HapticFeedback)W.HapticFeedback.selectionChanged();
 if(n=='v'){V=v;if(v=='b'){T={k:'p',q:1}}return R()}
 if(n=='k'){T={k:v,q:v=='p'?1:v=='d'?1:10};return R()}
 if(n=='q'){T.q=+v;return R()}
 if(a=='x'){S.pay=null;return R()}
 if(a=='go')return W.openLink(S.pay.u);
 if(a=='sp')return W.openTelegramLink('https://t.me/'+S.sp);
 if(a=='cp')return W.copyText(S.rl);
 if(a=='sh')return W.openTelegramLink('https://t.me/share/url?url='+encodeURIComponent(S.rl));
 if(a=='wk')return rq('wk',()=>{V='b';T={k:'p',q:1}});
 if(a=='pay:t'){E='Создаю счёт…';R();
  return W.request(`pay:${T.k}${T.q}:t`,{wallet:true}).then(r=>{const x=JSON.parse(r.result);if(!x.tx){E=x.m||'Ошибка';return R()}
   E='Подтвердите платёж в кошельке';R();return W.wallet.sendTransaction(x.tx,'ts:'+x.i).then(()=>{S.pay={st:'w',m:'t',a:x.a};E='';R()})}).catch(er)}
 if(n=='pay')return rq(`pay:${T.k}${T.q}:p`,x=>{if(x.pay)S.pay=x.pay});
 rq(a)};
W.onEvent&&W.onEvent('chatDataChanged',()=>{ld(W.chatData);R()});
function sy(){W.request&&W.request('s').then(a=>{ld(a.result);R()},()=>{})}
W.onEvent&&W.onEvent('activated',sy);
R();sy();
</script>
'''

GUIDE_HTML = r'''<style>
*{box-sizing:border-box}
body{margin:0;padding:12px;background:#050a17;color:#fff;font:15px/1.4 system-ui,sans-serif}
.c{background:#0c1530;border:1px solid #19295a;border-radius:14px;padding:11px 12px;margin-top:10px}
.mt{color:#7d90c4;font-size:13px}
button{display:block;width:100%;margin-top:8px;padding:11px 6px;border:0;border-radius:11px;background:#132250;color:#fff;font:600 14px system-ui,sans-serif}
button.p{background:#2b63ff}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.g button{margin:0}
.w{display:flex;justify-content:space-between;align-items:center}
.w button{width:auto;margin:0;padding:8px 12px}
ol{margin:0;padding-left:20px}
li{margin:4px 0}
</style>
<div id=a></div>
<script>
const W=window.TelegramWidget||{},A=document.getElementById('a');
let S={},V='';try{S=JSON.parse(W.chatData)||{}}catch(e){}
const G='https://github.com/Happ-proxy/',AS='https://apps.apple.com/',GP='https://play.google.com/store/apps/details?id=com.happproxy';
const P='Скопируйте ссылку подписки кнопкой ниже|Откройте Happ, нажмите «+» → «Вставить из буфера»|Нажмите большую кнопку подключения и разрешите добавить VPN';
const D={
i:['iPhone и iPad','Установите Happ из App Store',[['App Store (Россия)',AS+'ru/app/happ-proxy-utility-plus/id6746188973'],['App Store (другие страны)',AS+'app/happ-proxy-utility/id6504287215']]],
a:['Android','Установите Happ из Google Play или скачайте APK',[['Google Play',GP],['Скачать APK',G+'happ-android/releases/latest/download/Happ.apk']]],
w:['Windows','Скачайте и установите Happ для Windows',[['Скачать для Windows',G+'happ-desktop/releases/latest/download/setup-Happ.x64.exe']]],
m:['macOS','Установите Happ из App Store или скачайте DMG',[['App Store',AS+'ru/app/happ-proxy-utility-plus/id6746188973'],['Скачать DMG',G+'happ-desktop/releases/latest/download/Happ.macOS.universal.dmg']]],
t:['Телевизор','Установите Happ из Google Play на Android TV или из App Store на Apple TV',[['Google Play',GP],['Apple TV',AS+'app/happ-proxy-utility-for-tv/id6748297274']]]};
function B(t,a,c){return`<button data-a="${a}" class="${c||''}">${t}</button>`}
function R(){
 if(!V){A.innerHTML=`<b style="font-size:17px">Как подключиться</b><div class=mt>Выберите устройство — покажем, что сделать</div><div class=g>${Object.keys(D).map(k=>B(D[k][0],k,k=='t'?'':'')).join('')}${B('Поддержка','sp')}</div>`;return}
 const d=D[V],st=[d[1]].concat(P.split('|'));
 if(V=='t')st[2]='Откройте Happ на ТВ, нажмите «+» и добавьте подписку по ссылке — её удобно отправить себе в Telegram на телевизоре';
 A.innerHTML=`<div class=w><b style="font-size:17px">${d[0]}</b>${B('Назад','b')}</div><div class=c><ol>${st.map(x=>`<li>${x}</li>`).join('')}</ol></div>${d[2].map((x,i)=>B(x[0],'l'+i)).join('')}`+(S.su?B('Скопировать ссылку подписки','cp','p')+B('Открыть страницу подписки','op'):'<div class=mt style="margin-top:8px">Ссылка подписки появится после оплаты</div>')}
A.onclick=e=>{const t=e.target.closest('button');if(!t)return;const a=t.dataset.a;
 if(a=='b')V='';else if(a=='sp')return W.openTelegramLink('https://t.me/'+S.sp);
 else if(a=='cp')return W.copyText(S.su);
 else if(a=='op')return W.openLink(S.su);
 else if(a[0]=='l')return W.openLink(D[V][2][+a[1]][1]);
 else V=a;R()};
R();
</script>
'''

CARD_HTML = r'''<style>
*{box-sizing:border-box}
body{margin:0;padding:12px;background:#050a17;color:#fff;font:15px/1.4 system-ui,sans-serif}
.t{font-weight:700;font-size:17px}
.c{background:#0c1530;border:1px solid #19295a;border-radius:14px;padding:11px 12px;margin-top:10px}
.mt{color:#7d90c4}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:3px 0;border:0}
td+td{text-align:right;font-weight:600}
button{display:block;width:100%;padding:11px 6px;border:0;border-radius:11px;background:#132250;color:#fff;font:600 14px system-ui,sans-serif}
button.p{background:#2b63ff}
.g{display:grid;gap:8px;margin-top:10px}
.e{margin-top:8px;font-size:13px;color:#9fb6ff;text-align:center}
.sel{user-select:text;-webkit-user-select:text}
</style>
<div id=a></div>
<script>
const W=window.TelegramWidget||{},A=document.getElementById('a');
let S={},E='';
function ld(x){try{const o=JSON.parse(x);if(o&&(o.t||o.x))S=o}catch(e){}}
ld(W.chatData);
function R(){const b=S.b||[];
 A.innerHTML=(S.t?`<div class=t>${S.t}</div>`:'')+(S.x?`<div class="c sel">${S.x}</div>`:'')+(S.r?`<div class=c><table>${S.r.map(r=>`<tr><td class=mt>${r[0]}</td><td>${r[1]}</td></tr>`).join('')}</table></div>`:'')+
 (b.length?`<div class=g style="grid-template-columns:repeat(${S.c||1},1fr)">${b.map((x,i)=>`<button data-i=${i} class="${x[2]?'p':''}">${x[0]}</button>`).join('')}</div>`:'')+(E?`<div class=e>${E}</div>`:'')}
const ER={TIMEOUT:'Бот не ответил, попробуйте ещё раз',RATE_LIMITED:'Слишком часто, подождите',BUSY:'Подождите…',NOT_ALLOWED:'Откройте бота, чтобы продолжить'};
A.onclick=e=>{const t=e.target.closest('button');if(!t)return;const a=S.b[+t.dataset.i][1],v=a.slice(2);E='';
 if(W.HapticFeedback)W.HapticFeedback.selectionChanged();
 if(a[1]==':'){if(a[0]=='c')return W.copyText(v);if(a[0]=='o')return W.openLink(v);if(a[0]=='g')return W.openTelegramLink(v)}
 E='…';R();W.request(a).then(r=>{E='';let x={};try{x=JSON.parse(r.result)||{}}catch(e){}if(x.m)E=x.m;ld(r.result);R()},er=>{E=ER[er&&er.code]||'Ошибка';R()})};
W.onEvent&&W.onEvent('chatDataChanged',()=>{ld(W.chatData);E='';R()});
R();
</script>
'''

APP_HTML = r'''<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Cloud Networks VPN</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}html,body{background:#050a17}
body{margin:0;padding:14px 14px 28px;color:#fff;font:15px/1.4 system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent}
.hd{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px}.hd small{display:block;font-weight:400;font-size:12px;color:#7d90c4}
.c{background:#0c1530;border:1px solid #19295a;border-radius:16px;padding:12px 14px;margin-top:10px}
.mt{color:#7d90c4;font-size:13px}
button{display:block;width:100%;margin-top:8px;padding:13px 8px;border:0;border-radius:12px;background:#132250;color:#fff;font:600 15px system-ui,sans-serif;cursor:pointer}
button.p{background:#2b63ff}button:active{opacity:.8}button[disabled]{color:#5f70a3}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.g button,.r button{margin:0}
.r{display:flex;gap:6px;margin-top:8px}.r button{padding:10px 0}
.bar{height:6px;border-radius:3px;background:#19295a;margin-top:6px}.bar i{display:block;height:6px;border-radius:3px;background:#3d7bff}
.w{display:flex;justify-content:space-between;align-items:center;gap:8px}.w button{width:auto;margin:0;padding:8px 14px}
s{color:#7d90c4}.big{font-size:24px;font-weight:700}.t{font-size:18px;font-weight:700}
.sel{user-select:text;-webkit-user-select:text;word-break:break-all;font-size:13px}
.e{margin-top:10px;font-size:13px;color:#9fb6ff;text-align:center}
input,textarea{width:100%;margin-top:8px;padding:12px;border-radius:12px;border:1px solid #19295a;background:#0c1530;color:#fff;font:15px system-ui,sans-serif;outline:none}
textarea{min-height:90px}ol{margin:0;padding-left:20px}li{margin:4px 0}table{width:100%;border-collapse:collapse}td{padding:3px 0}td+td{text-align:right;font-weight:600}
</style></head><body><div id=a></div>
<script>
const T=window.Telegram&&Telegram.WebApp||{initData:'',ready(){},expand(){},openLink:u=>open(u),openTelegramLink:u=>open(u)},A=document.getElementById('a');
try{T.ready();T.expand();T.setHeaderColor('#050a17');T.setBackgroundColor('#050a17')}catch(e){}
let S=null,V='h',T2={k:'p',q:1},E='',C=null,DV='',PT=0;
const MN='янв фев мар апр мая июн июл авг сен окт ноя дек'.split(' ');
const dt=t=>{const d=new Date(t*1e3);return d.getDate()+' '+MN[d.getMonth()]+' '+d.getFullYear()};
const dl=()=>Math.max(0,Math.ceil((S.e-Date.now()/1e3)/86400));
const es=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const B=(t,a,c,x)=>`<button data-a="${es(a)}" class="${c||''}"${x?' disabled':''}>${t}</button>`;
function pr(f,dm){const D=Math.min(5000,10000-(100-dm)*(100-(S.pp||0)));return[f,Math.floor((f*(10000-D)+5000)/10000)]}
function cost(){const k=T2.k,q=T2.q;return k=='p'?pr(S.p*q,(S.dm||{})[q]||0):pr((k=='d'?S.dp:S.gp)*q,0)}
const act=()=>S.s==1||S.s==3,back=v=>`<div class=w><span class=t>${v}</span>${B('Назад','v:h')}</div>`;
const HD=`<div class=hd><svg width=36 height=36 viewBox="0 0 34 34"><rect width=34 height=34 rx=10 fill="#2b63ff"/><path d="M11 23h12a4.5 4.5 0 0 0 .6-9 6.5 6.5 0 0 0-12.4 2A3.6 3.6 0 0 0 11 23z" fill="#fff"/></svg><div>Cloud Networks<small>VPN</small></div></div>`;
function home(){let h=HD,s=S.s;
 if(act())h+=`<div class=c><div class=w><b>${s==3?'Пробный доступ':'Подписка активна'}</b><span class=mt>${dl()} дн.</span></div><div class=mt>до ${dt(S.e)} · устройств: ${S.d}</div>`+(S.g?`<div class=w style="margin-top:8px"><span class=mt>📶</span><span class=mt>${S.g[0]} из ${S.g[1]} ГБ</span></div><div class=bar><i style="width:${Math.min(100,S.g[0]/S.g[1]*100||0)}%"></i></div>`:'')+'</div>';
 else if(s==2)h+=`<div class=c><b>Подписка закончилась</b><div class=mt>${dt(S.e)}. Продлите — всё заработает сразу</div></div>`;
 else h+=`<div class=c><b>${S.p} ₽ в месяц</b><div class=mt>${S.bd} устройства · все локации · 📶 ${S.pg} ГБ в месяц</div></div>`;
 if(S.pay&&S.pay.st=='ok')h+=`<div class=c style="border-color:#2b63ff"><b>Оплата прошла ✓</b><div class=mt>Подключитесь по инструкции — кнопка «Подключить»</div></div>`;
 if(S.wk)h+=`<div class=c style="border-color:#2b63ff"><div class=w><div><b>−${S.wk}% на выходных</b><div class=mt>промокод ${es(S.wc)}</div></div>${B('Применить','wk','p')}</div></div>`;
 if(S.pp)h+=`<div class=e>Промокод ${es(S.pc)}: скидка ${S.pp}% на следующую покупку</div>`;
 if(S.t)h+=B(`Попробовать бесплатно · ${S.td} дн.`,'trial','p');
 h+=B(s?'Продлить подписку':'Купить подписку','v:b',S.t?'':'p');
 h+=`<div class=g>${B('Подключить','v:g','',!act())}${B('Друзья','v:r')}${B('Промокод','v:c')}${B('Поддержка','sp')}</div>`;
 if(S.adm)h+=B('Админ-панель','a:h');return h}
function buy(){let h=back('Покупка');const p=S.pay;
 if(p&&p.st=='w'){h+=`<div class=c><div class=mt>К оплате</div><div class=big>${p.a} ${p.m=='t'?'TON':'₽'}</div><div class=mt>${p.m=='t'?'Переведите из кошелька — сумма и комментарий подставятся. Подтверждение — до пары минут.':'После оплаты всё включится само, этот экран обновится.'}</div></div>`;
  if(p.u)h+=B('Перейти к оплате','go','p');return h+B('Выбрать другое','x')}
 if(S.s==1)h+=`<div class=r>${[['p','Подписка'],['d','Устройства'],['g','📶 ГБ']].map(x=>B(x[1],'k:'+x[0],T2.k==x[0]?'p':'')).join('')}</div>`;
 const k=T2.k,c=cost(),o=k=='p'?[1,3,6,12]:k=='d'?[1,2,3,5]:[10,25,50,100],dm=k=='p'?(S.dm||{})[T2.q]||0:0;
 h+=`<div class=r>${o.map(q=>B(q+(k=='p'?' мес':k=='g'?' ГБ':''),'q:'+q,T2.q==q?'p':'')).join('')}</div>`;
 h+=`<div class=c><div class=mt>${k=='p'?`${S.bd+(S.xd||0)} устройства · 📶 ${S.pg*T2.q} ГБ`+(dm?` · −${dm}% за срок`:''):k=='d'?`+${T2.q} к лимиту, навсегда · ${S.dp} ₽ за шт.`:`${S.gp} ₽ за 1 ГБ`}</div><div class=big>${c[1]} ₽ ${c[1]<c[0]?`<s>${c[0]} ₽</s>`:''}</div>${S.pp?`<div class=mt>Промокод ${es(S.pc)}: −${S.pp}%</div>`:''}</div>`;
 h+=B('Картой или СБП','pay:p','p');if(S.ton)h+=B('Оплатить TON','pay:t');return h}
function ref(){return back('Друзья')+`<div class=c>За каждого друга, который оформит подписку, — <b>${S.rd} дней</b> бесплатно.<div class=mt style="margin-top:6px">Приглашено: ${S.r[0]} · оплатили: ${S.r[1]}</div></div><div class="c sel">${es(S.rl)}</div><div class=g>${B('Скопировать','cp:'+S.rl,'p')}${B('Поделиться','sh')}</div>`}
function code(){return back('Промокод')+`<input id=pc placeholder="Введите промокод" autocapitalize=characters>${B('Применить','pc','p')}`+(S.pp?`<div class=c>Сейчас действует <b>${es(S.pc)}</b>: −${S.pp}%</div>`:'')+`<div class=c><b>Каждые выходные −${S.wp}%</b><div class=mt>Промокод ${es(S.wc)} работает в субботу и воскресенье</div></div>`}
const AS='https://apps.apple.com/',GH='https://github.com/Happ-proxy/',GP='https://play.google.com/store/apps/details?id=com.happproxy';
const DEV={i:['iPhone и iPad','Установите Happ из App Store',[['App Store (Россия)',AS+'ru/app/happ-proxy-utility-plus/id6746188973'],['App Store (другие страны)',AS+'app/happ-proxy-utility/id6504287215']]],a:['Android','Установите Happ из Google Play или скачайте APK',[['Google Play',GP],['Скачать APK',GH+'happ-android/releases/latest/download/Happ.apk']]],w:['Windows','Скачайте и установите Happ для Windows',[['Скачать для Windows',GH+'happ-desktop/releases/latest/download/setup-Happ.x64.exe']]],m:['macOS','Установите Happ из App Store или скачайте DMG',[['App Store',AS+'ru/app/happ-proxy-utility-plus/id6746188973'],['Скачать DMG',GH+'happ-desktop/releases/latest/download/Happ.macOS.universal.dmg']]],t:['Телевизор','Установите Happ из Google Play на Android TV или из App Store на Apple TV',[['Google Play',GP],['Apple TV',AS+'app/happ-proxy-utility-for-tv/id6748297274']]]};
function guide(){if(!DV)return back('Как подключиться')+`<div class=mt style="margin-top:6px">Выберите устройство</div><div class=g>${Object.keys(DEV).map(k=>B(DEV[k][0],'d:'+k)).join('')}${B('Поддержка','sp')}</div>`;
 const d=DEV[DV],st=[d[1],'Скопируйте ссылку подписки кнопкой ниже','Откройте Happ, нажмите «+» → «Вставить из буфера»','Нажмите кнопку подключения и разрешите добавить VPN'];
 if(DV=='t')st[2]='Откройте Happ на ТВ, нажмите «+» и добавьте подписку по ссылке — её удобно отправить себе в Telegram на ТВ';
 return`<div class=w><span class=t>${d[0]}</span>${B('Назад','d:')}</div><div class=c><ol>${st.map(x=>`<li>${x}</li>`).join('')}</ol></div>${d[2].map(x=>B(x[0],'o:'+x[1])).join('')}`+(S.su?B('Скопировать ссылку подписки','cp:'+S.su,'p')+B('Открыть страницу подписки','o:'+S.su):'<div class=e>Ссылка подписки появится после оплаты</div>')}
function card(){const c=C||{},b=c.b||[];return(c.t?`<div class=w><span class=t>${c.t}</span>${B('Закрыть','v:h')}</div>`:'')+(c.x?`<div class="c sel">${c.x}</div>`:'')+(c.r?`<div class=c><table>${c.r.map(r=>`<tr><td class=mt>${r[0]}</td><td>${r[1]}</td></tr>`).join('')}</table></div>`:'')+(c.in?(c.in=='bc'?'<textarea id=ai placeholder="Текст"></textarea>':'<input id=ai placeholder="Введите">')+B('Отправить','ai','p'):'')+(b.length?`<div class=g style="grid-template-columns:repeat(${c.c||1},1fr)">${b.map(x=>B(x[0],x[1],x[2]?'p':'')).join('')}</div>`:'')}
function R(){if(!S){A.innerHTML=HD+`<div class=e>${E||'Загрузка…'}</div>`;return}
 A.innerHTML=({h:home,b:buy,r:ref,c:code,g:guide,a:card}[V]||home)()+(E?`<div class=e>${E}</div>`:'')}
async function api(a,x){const r=await fetch('/api',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(Object.assign({initData:T.initData,a},x||{}))});if(!r.ok)throw 0;return r.json()}
async function run(a,x,f){E='…';R();try{const r=await api(a,x);E=r.m||'';if(r.st)S=r.st;if(r.card){C=r.card;V='a'}if(f)f(r)}catch(e){E='Нет связи с сервером. Если не открывается — включите VPN или пользуйтесь меню в чате'}R()}
function poll(){clearTimeout(PT);if(S&&S.pay&&S.pay.st=='w')PT=setTimeout(()=>api('s').then(r=>{S=r.st;if(S.pay&&S.pay.st=='ok')V='h';R();poll()}).catch(poll),5000)}
function link(u){u.startsWith('https://t.me/')?T.openTelegramLink(u):T.openLink(u)}
function copy(t){(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>{E='Скопировано ✓';R()},()=>{E=t;R()})}
A.onclick=ev=>{const t=ev.target.closest('button');if(!t||t.disabled)return;const a=t.dataset.a,i=a.indexOf(':'),n=i<0?a:a.slice(0,i),v=i<0?'':a.slice(i+1);E='';
 try{T.HapticFeedback.selectionChanged()}catch(e){}
 if(n=='v'){V=v;DV='';if(v=='b')T2={k:'p',q:1};return R()}
 if(n=='k'){T2={k:v,q:v=='g'?10:1};return R()}if(n=='q'){T2.q=+v;return R()}if(n=='d'){DV=v;return R()}
 if(a=='x'){S.pay=null;return R()}if(a=='go')return link(S.pay.u);if(a=='sp')return link('https://t.me/'+S.sp);
 if(n=='cp')return copy(v);if(n=='o')return link(v);if(n=='g')return link(v);if(n=='c')return copy(v);
 if(a=='sh')return link('https://t.me/share/url?url='+encodeURIComponent(S.rl));
 if(a=='pc')return run('promo',{code:document.getElementById('pc').value.trim()});
 if(a=='wk')return run('wk',0,r=>{if(!r.m){V='b';T2={k:'p',q:1}}});
 if(n=='pay')return run('pay',{k:T2.k,q:T2.q,mt:v},r=>{if(r.pay){S.pay=r.pay;if(r.pay.u)link(r.pay.u);poll()}});
 if(a=='ai')return run('ain',{text:document.getElementById('ai').value});
 if(a=='trial')return run('trial');
 run(a)};
api('s').then(r=>{S=r.st;R();poll()}).catch(()=>{E='Не удалось загрузить. Если не открывается — включите VPN или пользуйтесь меню в чате бота';R()});R();
</script></body></html>
'''


# ─────────────────────────────────────────────
#  ЦЕНЫ, ВЫХОДНЫЕ, ПРОМОКОДЫ
# ─────────────────────────────────────────────
def calc_price(st: dict, kind: str, qty: int, promo_pct: int) -> tuple[int, int]:
    """(полная цена, к оплате). Формула совпадает с виджетом (pr() в MAIN_HTML)."""
    if kind == "p":
        full, dm = st["price"] * qty, int(st.get(f"d{qty}", 0) or 0)
    elif kind == "d":
        full, dm = st["dp"] * qty, 0
    else:
        full, dm = st["gp"] * qty, 0
    d = min(MAX_TOTAL_DISCOUNT * 100, 10000 - (100 - dm) * (100 - promo_pct))
    return full, max(1, (full * (10000 - d) + 5000) // 10000)


def weekend_info(t: int | None = None) -> tuple[bool, str, int]:
    """(сейчас выходные?, id выходных = дата субботы, когда кончаются)."""
    d = datetime.fromtimestamp(t or now(), MSK)
    if d.weekday() not in (5, 6):
        return False, "", 0
    sat = (d - timedelta(days=d.weekday() - 5)).replace(hour=0, minute=0, second=0, microsecond=0)
    return True, sat.strftime("%Y-%m-%d"), int((sat + timedelta(days=2)).timestamp())


async def user_promo(u: dict) -> tuple[str | None, int]:
    """Действующий промокод пользователя (код, %)."""
    if u.get("promo") and u.get("promo_pct") and int(u.get("promo_until") or 0) > now():
        return u["promo"], int(u["promo_pct"])
    return None, 0


async def apply_weekend(uid: int) -> str:
    """Включает скидку выходного дня. Возвращает текст ошибки или ''."""
    st = await settings()
    on, wk_id, until = weekend_info()
    u = await get_user(uid)
    if not st["wk_on"]:
        return "Скидка выходного дня сейчас не действует"
    if not on:
        return f"Промокод {st['wk_code']} действует только в субботу и воскресенье"
    if u and u.get("wk_used") == wk_id:
        return "Скидку выходного дня можно использовать один раз за выходные"
    await db_exec("UPDATE cn_users SET promo=$2, promo_pct=$3, promo_until=$4 WHERE user_id=$1",
                  uid, st["wk_code"], int(st["wk_pct"]), until)
    return ""


class PromoAbort(Exception):
    pass


async def apply_promo_code(uid: int, code: str) -> tuple[bool, str]:
    """Промокод из сообщения. (успех, текст ответа)."""
    st = await settings()
    if code == st["wk_code"]:
        err = await apply_weekend(uid)
        if err:
            return False, err
        return True, f"Скидка {st['wk_pct']}% применена к следующей покупке до конца воскресенья."
    promo = await db_row("SELECT * FROM cn_promos WHERE code=$1", code)
    if not promo:
        return False, "Такого промокода нет. Проверьте написание."
    if promo["uses_left"] is not None and promo["uses_left"] <= 0:
        return False, "Этот промокод уже закончился."
    try:
        async with pool.acquire() as c:
            async with c.transaction():
                if not await c.fetchval(
                        "INSERT INTO cn_promo_uses (code, user_id, at) VALUES ($1,$2,$3) "
                        "ON CONFLICT DO NOTHING RETURNING user_id", code, uid, now()):
                    raise PromoAbort("Вы уже использовали этот промокод.")
                if not await c.fetchval(
                        "UPDATE cn_promos SET used=used+1, uses_left=uses_left-1 "
                        "WHERE code=$1 AND (uses_left IS NULL OR uses_left > 0) RETURNING code", code):
                    raise PromoAbort("Этот промокод уже закончился.")
    except PromoAbort as e:
        return False, str(e)
    if promo["kind"] == "pct":
        await db_exec("UPDATE cn_users SET promo=$2, promo_pct=$3, promo_until=$4 WHERE user_id=$1",
                      uid, code, int(promo["value"]), now() + 7 * 86400)
        return True, f"Скидка {promo['value']}% применена к следующей покупке. Действует 7 дней."
    days = int(promo["value"])
    res = await grant_days(uid, days)
    if not res:
        await db_exec("DELETE FROM cn_promo_uses WHERE code=$1 AND user_id=$2", code, uid)
        await db_exec("UPDATE cn_promos SET used=used-1, uses_left=uses_left+1 WHERE code=$1", code)
        return False, "Не получилось начислить дни, попробуйте позже или напишите в поддержку."
    return True, f"Начислено {days} {plural(days, 'день', 'дня', 'дней')} подписки."


async def grant_days(uid: int, days: int) -> dict | None:
    """Дни подписки в подарок (промокод, реферал, админ). Новым — полный тариф."""
    st = await settings()
    u = await get_user(uid) or {}
    try:
        exists = await remna_get(uid)
    except Exception:
        return None
    if exists:
        res = await remna_extend(uid, days)
    else:
        res = await remna_extend(uid, days, hwid=st["bd"] + int(u.get("extra_dev") or 0),
                                 squads=[SQUAD_BASIC, SQUAD_WL])
        if res:
            await wl_set(uid, st["pg"] * max(1, round(days / 30)), reset=True)
    if res and u.get("plan") != "paid":
        await db_exec("UPDATE cn_users SET plan='paid' WHERE user_id=$1", uid)
    if res:
        await push_state(uid)
    return res


async def wl_set(uid: int, base_gb: int, reset: bool) -> None:
    """Новый период 📶: base_gb из тарифа + докупленное сверх прошлого тарифа."""
    old = await db_row("SELECT * FROM cn_wl WHERE user_id=$1", uid)
    extra = max(0, int(old["gb_limit"]) - int(old["base_gb"])) if old else 0
    await db_exec(
        "INSERT INTO cn_wl (user_id, gb_limit, base_gb, period_start, cut_off) VALUES ($1,$2,$3,$4,FALSE) "
        "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, base_gb=$3, period_start=$4, cut_off=FALSE",
        uid, base_gb + extra, base_gb, now())


# ─────────────────────────────────────────────
#  СОСТОЯНИЕ ДЛЯ ВИДЖЕТА
# ─────────────────────────────────────────────
def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=r{uid}"


async def user_state(uid: int, cached: bool = True) -> dict:
    """Всё, что нужно главному виджету. Не больше 1024 байт в JSON."""
    st = await settings()
    u = await get_user(uid) or {}
    try:
        r = await remna_get(uid, cached=cached)
    except Exception:
        r = None
    t = now()
    exp = parse_ts(r.get("expireAt")) if r else 0
    active = bool(r) and exp > t and str(r.get("status") or "ACTIVE").upper() == "ACTIVE"
    s = (3 if u.get("plan") == "trial" else 1) if active else (2 if r else 0)
    if r and exp != int(u.get("expire_ts") or 0):
        await db_exec("UPDATE cn_users SET expire_ts=$2 WHERE user_id=$1", uid, exp)
    pc, pp = await user_promo(u)
    on, wk_id, _ = weekend_info()
    state = {
        "s": s, "e": exp, "d": int(r.get("hwidDeviceLimit") or 0) if r else 0,
        "p": st["price"], "bd": st["bd"], "dp": st["dp"], "gp": st["gp"], "pg": st["pg"],
        "dm": {str(m): int(st.get(f"d{m}", 0) or 0) for m in MONTHS if m > 1},
        "xd": int(u.get("extra_dev") or 0),
        "wp": st["wk_pct"], "wc": st["wk_code"],
        "wk": st["wk_pct"] if (st["wk_on"] and on and u.get("wk_used") != wk_id
                               and pc != st["wk_code"]) else 0,
        "t": 1 if (TRIAL_ON and not u.get("trial_used") and not r) else 0, "td": TRIAL_DAYS,
        "rd": REF_DAYS, "rl": ref_link(uid), "sp": SUPPORT,
        "ton": 1 if TON_WALLET else 0,
    }
    rr = await db_row("SELECT count(*) AS n, count(r.friend_id) AS p FROM cn_users u "
                      "LEFT JOIN cn_ref r ON r.friend_id=u.user_id WHERE u.referrer_id=$1", uid)
    state["r"] = [int(rr["n"]), int(rr["p"])]
    if pp:
        state["pc"], state["pp"] = pc, pp
    if s in (1, 3):
        wl = await db_row("SELECT * FROM cn_wl WHERE user_id=$1", uid)
        if wl and wl["gb_limit"] and SQUAD_WL != SQUAD_BASIC:
            state["g"] = [await wl_used_gb(uid, wl["period_start"]), int(wl["gb_limit"])]
    r_inv = await db_row(
        "SELECT * FROM cn_invoices WHERE user_id=$1 AND created_at>$2 ORDER BY created_at DESC LIMIT 1",
        uid, t - 3600)
    if r_inv:
        if r_inv["status"] in ("PENDING", "WORK", "ERROR") and (r_inv["method"] != "ton" or r_inv["msg_hash"]):
            state["pay"] = {"i": r_inv["id"], "a": ton_str(r_inv["ton_nano"]) if r_inv["method"] == "ton"
                            else r_inv["amount"], "m": "t" if r_inv["method"] == "ton" else "p",
                            "u": r_inv["url"] or "", "st": "w"}
        elif r_inv["status"] == "PAID" and t - int(r_inv["paid_at"] or 0) < 600:
            state["pay"] = {"i": r_inv["id"], "st": "ok"}
    return state


def main_caption(state: dict) -> str:
    if state["s"] in (1, 3):
        line = f"Подписка активна до {fmt_date(state['e'])}"
    elif state["s"] == 2:
        line = "Подписка закончилась — продлите в меню"
    else:
        line = f"Подписка {state['p']} ₽/мес · {state['bd']} устройства"
    return (f"<b>☁️ {esc(BRAND)}</b>\n{line}\n\n"
            "<i>Не видите меню над этим текстом? Обновите Telegram до последней версии "
            "или пользуйтесь кнопками ниже.</i>")


def main_kb() -> dict:
    return ikb([[("Купить", "fb:buy"), ("Подключить", "fb:conn")],
                [("Друзья", "fb:ref"), ("Поддержка", f"https://t.me/{SUPPORT}")]])


# ── Виджеты разрешены? ─────────────────────────────────────────────────
# С 13.11.2026 Telegram отвечает обычным ботам INPUT_MEDIA_WIDGET_NOT_ALLOWED.
# Тогда бот работает обычными сообщениями с кнопками (classic_*) и раз в
# полчаса пробует виджеты снова — как только их разрешат, переключится сам.
WIDGETS_MODE = env("WIDGETS", "auto")          # auto | off
_widget_block = {"until": 0}


def widgets_on() -> bool:
    return WIDGETS_MODE != "off" and now() >= _widget_block["until"]


async def send_widget(**params) -> dict | None:
    """sendWidget; None — виджеты сейчас недоступны (тогда шлём обычное сообщение)."""
    if not widgets_on():
        return None
    try:
        return await tg("sendWidget", **params)
    except TgError as e:
        if "WIDGET_NOT_ALLOWED" in str(e):
            if not _widget_block["until"] or now() - _widget_block["until"] > 3600:
                log.warning("Telegram не разрешает боту виджеты — работаю обычными сообщениями")
            _widget_block["until"] = now() + 1800
            return None
        raise


async def show_screen(uid: int, screen: dict, edit_mid: int | None = None) -> dict | None:
    """Экран (тот же формат, что у карточки) обычным сообщением: правка или новое."""
    text, kb = screen_to_msg(screen)
    if edit_mid:
        try:
            await tg("editMessageText", chat_id=uid, message_id=edit_mid, text=text, parse_mode="HTML",
                     reply_markup=kb, link_preview_options={"is_disabled": True})
            return {"message_id": edit_mid}
        except TgError as e:
            if "not modified" in str(e):
                return {"message_id": edit_mid}
    return await send_text(uid, text, kb)


async def send_main(uid: int, view: str | None = None) -> None:
    state = await user_state(uid, cached=False)
    data = dict(state, v=view) if view else state
    try:
        m = await send_widget(chat_id=uid, html=MAIN_HTML, chat_data=jdump(data),
                              caption=main_caption(state), parse_mode="HTML",
                              allow_queries=True, reply_markup=main_kb())
    except TgError as e:
        await note_send_error(uid, e)
        m = None
    if not m:
        m = await show_screen(uid, await main_screen(uid, view or "h", "p1", state))
    if m:
        await db_exec("UPDATE cn_users SET last_main=$2 WHERE user_id=$1", uid, m["message_id"])


async def push_state(uid: int) -> None:
    """Обновляет последнее меню пользователя без нового сообщения."""
    u = await get_user(uid)
    if not u or not u.get("last_main"):
        return
    state = await user_state(uid, cached=False)
    if widgets_on():
        try:
            await tg("editMessageWidgetData", chat_id=uid, message_id=u["last_main"],
                     chat_data=jdump(state))
            await tg("editMessageCaption", chat_id=uid, message_id=u["last_main"],
                     caption=main_caption(state), parse_mode="HTML", reply_markup=main_kb())
            return
        except TgError as e:
            if "not modified" in str(e):
                return
    try:
        text, kb = screen_to_msg(await main_screen(uid, "h", "p1", state))
        await tg("editMessageText", chat_id=uid, message_id=u["last_main"], text=text,
                 parse_mode="HTML", reply_markup=kb, link_preview_options={"is_disabled": True})
    except TgError:
        pass


async def send_guide(uid: int) -> None:
    try:
        r = await remna_get(uid)
    except Exception:
        r = None
    su = sub_url(r)
    data = {"su": su, "sp": SUPPORT} if su else {"sp": SUPPORT}
    caption = ("<b>Как подключиться</b>\n1. Установите приложение Happ.\n"
               "2. Скопируйте ссылку подписки:\n" + (f"<code>{esc(su)}</code>\n" if su else "") +
               "3. В Happ нажмите «+» → «Вставить из буфера» и подключитесь.")
    kb = ikb([[("App Store", HAPP["ios_ru"]), ("Google Play", HAPP["gp"])],
              [("Windows", HAPP["win"]), ("macOS", HAPP["dmg"])]])
    try:
        if await send_widget(chat_id=uid, html=GUIDE_HTML, chat_data=jdump(data),
                             caption=caption, parse_mode="HTML", reply_markup=kb):
            return
    except TgError as e:
        log.warning("sendWidget guide → %s: %s", uid, e)
    await show_screen(uid, guide_screen(su, ""))


def card_fit(card: dict) -> dict:
    """Карточка обязана влезть в 1024 байта: режем строки и текст."""
    card = {k: v for k, v in card.items() if v not in (None, "", [])}
    while len(jdump(card).encode()) > 1020:
        if card.get("r") and len(card["r"]) > 1:
            card["r"] = card["r"][:-1]
        elif card.get("x") and len(card["x"]) > 40:
            card["x"] = card["x"][: int(len(card["x"]) * 0.8)].rsplit(" ", 1)[0] + "…"
        elif card.get("b") and len(card["b"]) > 1:
            card["b"] = card["b"][:-1]
        else:
            break
    return card


def card_caption(card: dict) -> str:
    text = re.sub(r"<br\s*/?>", "\n", card.get("x") or "")
    text = re.sub(r"<(?!/?(b|i|code)>)[^>]+>", "", text)
    return (f"<b>{card['t']}</b>\n" if card.get("t") else "") + text[:900]


async def send_card(uid: int, card: dict, *, kb=None, caption: str | None = None,
                    queries: bool = True) -> dict | None:
    try:
        m = await send_widget(chat_id=uid, html=CARD_HTML, chat_data=jdump(card_fit(card)),
                              caption=(caption or card_caption(card))[:1024], parse_mode="HTML",
                              allow_queries=queries, reply_markup=kb)
        if m:
            return m
    except TgError as e:
        if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
            await note_send_error(uid, e)
            return None
        log.warning("sendWidget card → %s: %s", uid, e)
    return await show_screen(uid, card)


# ─────────────────────────────────────────────
#  ОПЛАТА: PLATEGA (карта, СБП)
# ─────────────────────────────────────────────
KIND_NAMES = {"p": "Подписка", "d": "Устройства", "g": "📶"}


def item_name(kind: str, qty: int) -> str:
    if kind == "p":
        return f"Подписка на {qty} {plural(qty, 'месяц', 'месяца', 'месяцев')}"
    if kind == "d":
        return f"+{qty} {plural(qty, 'устройство', 'устройства', 'устройств')}"
    return f"📶 +{qty} ГБ"


def _rub_word(n: int) -> str:
    return plural(n, "рубль", "рубля", "рублей")


async def platega_create(uid: int, username: str | None, amount: int) -> tuple[str, str, str]:
    """(transactionId, ссылка, ошибка). Перебирает способы до первого рабочего."""
    errors = []
    for method in PLATEGA_METHODS:
        payload = {
            "paymentMethod": method,
            "paymentDetails": {"amount": float(amount), "currency": "RUB"},
            "description": f"Интернет-Сервис. Пополнение баланса на {amount} {_rub_word(amount)}",
            "return": f"https://t.me/{BOT_USERNAME}", "failedUrl": f"https://t.me/{BOT_USERNAME}",
            "payload": f"tg{uid}",
            "metadata": {"userId": str(uid), "userName": f"@{username}" if username else str(uid)},
        }
        try:
            r = await HTTP.post(f"{PLATEGA_API}/transaction/process", json=payload, timeout=12,
                                headers={"X-MerchantId": PLATEGA_ID, "X-Secret": PLATEGA_SECRET})
            if r.status_code >= 400:
                errors.append(f"{method}: HTTP {r.status_code} {r.text[:150]}")
                continue
            data = r.json()
        except Exception as e:
            errors.append(f"{method}: {e!r}")
            continue
        tx = str(data.get("transactionId") or data.get("id") or "")
        url = next((data[k] for k in ("redirect", "url", "paymentUrl") if
                    isinstance(data.get(k), str) and data[k].startswith("http")), "")
        if tx and url:
            if PLATEGA_METHODS[0] != method:          # рабочий способ — первым
                PLATEGA_METHODS.remove(method)
                PLATEGA_METHODS.insert(0, method)
            return tx, url, ""
        errors.append(f"{method}: нет ссылки в ответе")
    return "", "", " | ".join(errors)[:500]


async def platega_status(tx: str) -> str:
    try:
        r = await HTTP.get(f"{PLATEGA_API}/transaction/{tx}", timeout=15,
                           headers={"X-MerchantId": PLATEGA_ID, "X-Secret": PLATEGA_SECRET})
        if r.status_code >= 400:
            return ""
        return str((r.json() or {}).get("status") or "").upper()
    except Exception:
        return ""


# ─────────────────────────────────────────────
#  ОПЛАТА: TON (кошелёк Telegram прямо в виджете)
# ─────────────────────────────────────────────
_ton_rate = {"ts": 0.0, "rub": 0.0}


async def ton_rub_rate() -> float:
    """Сколько рублей стоит 1 TON. Кэш 10 минут, запасной курс — TON_RUB_RATE."""
    if _ton_rate["rub"] and time.time() - _ton_rate["ts"] < 600:
        return _ton_rate["rub"]
    try:
        r = await HTTP.get("https://tonapi.io/v2/rates", params={"tokens": "ton", "currencies": "rub"},
                           timeout=8)
        rub = float(r.json()["rates"]["TON"]["prices"]["RUB"])
        if rub > 0:
            _ton_rate.update(ts=time.time(), rub=rub)
            return rub
    except Exception as e:
        log.warning("курс TON: %s", e)
    return _ton_rate["rub"] or TON_RATE_FALLBACK


def ton_str(nano) -> str:
    return f"{int(nano or 0) / 1e9:.2f}"


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def comment_boc(text: str) -> str:
    """BOC одной ячейки «комментарий к переводу» (op 0 + текст), base64."""
    body = b"\x00\x00\x00\x00" + text.encode()
    cell = bytes([0, len(body) * 2]) + body
    boc = b"\xb5\xee\x9c\x72" + bytes([0x41, 1, 1, 1, 0, len(cell), 0]) + cell
    return base64.b64encode(boc + _crc32c(boc).to_bytes(4, "little")).decode()


def boc_comment(b64: str) -> str:
    """Текст комментария из BOC тела сообщения (только простой случай)."""
    try:
        b = base64.b64decode(b64)
        if b[:4] != b"\xb5\xee\x9c\x72":
            return ""
        flags, off = b[4], b[5]
        size = flags & 7
        i = 6
        cells = int.from_bytes(b[i:i + size], "big"); i += size
        roots = int.from_bytes(b[i:i + size], "big"); i += size
        i += size + off + size * roots
        if flags & 0x80:
            i += off * cells
        d2 = b[i + 1]
        data = b[i + 2:i + 2 + (d2 + 1) // 2]
        if data[:4] != b"\x00\x00\x00\x00":
            return ""
        return data[4:].decode("utf-8", "ignore").strip()
    except Exception:
        return ""


async def ton_incoming() -> list[tuple[str, int, str]]:
    """Последние входящие переводы на TON_WALLET: (комментарий, нанотоны, hash)."""
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    try:
        r = await HTTP.get("https://toncenter.com/api/v3/transactions",
                           params={"account": TON_WALLET, "limit": 100, "sort": "desc"},
                           headers=headers, timeout=15)
        txs = (r.json() or {}).get("transactions") or []
    except Exception as e:
        log.warning("toncenter: %s", e)
        return []
    out = []
    for tx in txs:
        msg = tx.get("in_msg") or {}
        if not msg.get("source"):
            continue
        mc = msg.get("message_content") or {}
        com = ((mc.get("decoded") or {}).get("comment") or boc_comment(mc.get("body") or "")).strip()
        if com:
            out.append((com, int(msg.get("value") or 0), str(tx.get("hash") or "")))
    return out


# ─────────────────────────────────────────────
#  СЧЕТА И ВЫДАЧА
# ─────────────────────────────────────────────
VALID_QTY = {"p": set(MONTHS), "d": set(range(1, 11)), "g": {10, 25, 50, 100, 200}}


async def make_invoice(uid: int, kind: str, qty: int, method: str) -> tuple[dict | None, str]:
    """Счёт на покупку. Возвращает (строка счёта, ошибка)."""
    if kind not in VALID_QTY or qty not in VALID_QTY[kind]:
        return None, "Неверный выбор"
    u = await get_user(uid)
    if not u or u.get("banned"):
        return None, "Покупка недоступна, напишите в поддержку"
    if kind in ("d", "g"):
        st_now = await user_state(uid)
        if st_now["s"] != 1:
            return None, "Сначала оформите подписку"
    st = await settings()
    code, pct = await user_promo(u)
    full, amount = calc_price(st, kind, qty, pct)
    inv_id = "CN" + secrets.token_hex(4).upper()
    _, wk_id, _ = weekend_info()
    row = {"id": inv_id, "user_id": uid, "kind": kind, "qty": qty, "amount": amount,
           "method": method, "promo": code, "wk": wk_id if code == st["wk_code"] else None,
           "ext_id": None, "url": None, "ton_nano": 0}
    if method == "ton":
        if not TON_WALLET:
            return None, "Оплата TON пока не подключена"
        rate = await ton_rub_rate()
        if rate <= 0:
            return None, "Не удалось получить курс TON, попробуйте картой"
        row["ton_nano"] = math.ceil(amount / rate * 100) * 10_000_000   # вверх до 0.01 TON
    else:
        if not PLATEGA_ON:
            return None, "Оплата картой временно недоступна"
        tx, url, err = await platega_create(uid, u.get("username"), amount)
        if not tx:
            log.error("[platega] %s", err)
            await notify_admins(f"⚠️ Platega не создала платёж для {uid}: {esc(err)}")
            return None, "Платёжная система не ответила, попробуйте через минуту"
        row["ext_id"], row["url"] = tx, url
    await db_exec(
        "INSERT INTO cn_invoices (id, user_id, kind, qty, amount, method, ext_id, url, ton_nano, "
        "promo, wk, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
        inv_id, uid, kind, qty, amount, method, row["ext_id"], row["url"], row["ton_nano"],
        row["promo"], row["wk"], now())
    return row, ""


async def confirm_invoice(inv_id: str) -> None:
    """Платёж подтверждён: выдаём покупку. Идемпотентно."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "UPDATE cn_invoices SET status='WORK', confirmed=TRUE, tries=tries+1 "
            "WHERE id=$1 AND status IN ('PENDING','ERROR') RETURNING *", inv_id)
    if not row:
        return
    try:
        ok = await grant(dict(row))
    except Exception:
        log.exception("grant %s", inv_id)
        ok = False
    if not ok:
        await db_exec("UPDATE cn_invoices SET status='ERROR' WHERE id=$1", inv_id)
        if row["tries"] == 1:
            await notify_admins(
                f"🔴 Оплата {inv_id} прошла, но выдать не получилось (пользователь "
                f"<code>{row['user_id']}</code>, {esc(item_name(row['kind'], row['qty']))}). "
                f"Бот повторяет попытки.\n<code>{esc(LAST_REMNA_ERROR)}</code>")
        return
    await db_exec("UPDATE cn_invoices SET status='PAID', paid_at=$2 WHERE id=$1", inv_id, now())
    await after_paid(dict(row))


async def grant(inv: dict) -> bool:
    uid, kind, qty = inv["user_id"], inv["kind"], inv["qty"]
    st = await settings()
    u = await get_user(uid) or {}
    if kind == "p":
        res = await remna_extend(uid, 30 * qty, hwid=st["bd"] + int(u.get("extra_dev") or 0),
                                 squads=[SQUAD_BASIC, SQUAD_WL])
        if not res:
            return False
        await wl_set(uid, st["pg"] * qty, reset=True)
        await db_exec("UPDATE cn_users SET plan='paid' WHERE user_id=$1", uid)
        return True
    if kind == "d":
        extra = int(u.get("extra_dev") or 0) + qty
        res = await remna_patch(uid, {"hwidDeviceLimit": st["bd"] + extra})
        if not res:
            return False
        await db_exec("UPDATE cn_users SET extra_dev=$2 WHERE user_id=$1", uid, extra)
        return True
    # 📶: +qty ГБ к текущему периоду, отрезанный доступ возвращаем
    wl = await db_row("SELECT * FROM cn_wl WHERE user_id=$1", uid)
    if not wl:
        await db_exec("INSERT INTO cn_wl (user_id, gb_limit, base_gb, period_start) VALUES ($1,$2,0,$3)",
                      uid, qty, now())
    else:
        await db_exec("UPDATE cn_wl SET gb_limit=gb_limit+$2 WHERE user_id=$1", uid, qty)
    if wl and wl["cut_off"]:
        r = await remna_get(uid)
        sq = squads_of(r)
        if SQUAD_WL not in sq:
            if not await remna_patch(uid, {"activeInternalSquads": sq + [SQUAD_WL]}):
                return False
        await db_exec("UPDATE cn_wl SET cut_off=FALSE WHERE user_id=$1", uid)
    return True


async def after_paid(inv: dict) -> None:
    uid = inv["user_id"]
    st = await settings()
    if inv.get("promo"):
        if inv["promo"] == st["wk_code"]:
            await db_exec("UPDATE cn_users SET wk_used=$2 WHERE user_id=$1", uid, inv.get("wk") or "")
        await db_exec("UPDATE cn_users SET promo=NULL, promo_pct=0, promo_until=0 "
                      "WHERE user_id=$1 AND promo=$2", uid, inv["promo"])
    await db_exec("UPDATE cn_users SET paid_total=paid_total+$2 WHERE user_id=$1", uid, inv["amount"])
    u = await get_user(uid) or {}
    method = "TON" if inv["method"] == "ton" else "Platega"
    who = f"@{u['username']}" if u.get("username") else esc(u.get("first_name") or uid)
    await notify_admins(f"💳 {inv['amount']} ₽ · {esc(item_name(inv['kind'], inv['qty']))} · "
                        f"{who} (<code>{uid}</code>) · {method}")
    await push_state(uid)
    if inv["kind"] == "p":
        await send_guide(uid)
        if u.get("referrer_id"):
            await reward_referrer(u["referrer_id"], uid, u.get("username"))
    else:
        await send_card(uid, {"t": "Оплата прошла ✓",
                              "x": f"{esc(item_name(inv['kind'], inv['qty']))} — уже добавлено к подписке."})


async def reward_referrer(ref_id: int, friend_id: int, friend_username: str | None) -> None:
    """REF_DAYS дней пригласившему — один раз за друга, при его первой оплате."""
    if not REF_DAYS:
        return
    ins = await db_val("INSERT INTO cn_ref (friend_id, referrer_id, at) VALUES ($1,$2,$3) "
                       "ON CONFLICT DO NOTHING RETURNING friend_id", friend_id, ref_id, now())
    if not ins:
        return
    if not await grant_days(ref_id, REF_DAYS):
        await db_exec("DELETE FROM cn_ref WHERE friend_id=$1", friend_id)
        await notify_admins(f"⚠️ Не удалось начислить {REF_DAYS} дн. за друга: "
                            f"<code>{ref_id}</code> ← <code>{friend_id}</code>")
        return
    who = f"@{friend_username}" if friend_username else "Ваш друг"
    await send_card(ref_id, {"t": "+1 месяц за друга 🎉",
                             "x": f"{esc(who)} оформил подписку по вашей ссылке. "
                                  f"Мы добавили {REF_DAYS} дней к вашей подписке.",
                             "b": [["Открыть меню", "open:h", 1]]})


async def activate_trial(uid: int) -> str:
    u = await get_user(uid) or {}
    if not TRIAL_ON or u.get("trial_used"):
        return "Пробный период уже использован"
    try:
        if await remna_get(uid):
            return "Пробный период доступен только новым пользователям"
    except Exception:
        return "Сервис временно недоступен, попробуйте позже"
    # отметка заранее: двойное нажатие не выдаст второй пробный период
    if not await db_val("UPDATE cn_users SET trial_used=TRUE WHERE user_id=$1 AND trial_used=FALSE "
                        "RETURNING user_id", uid):
        return "Пробный период уже использован"
    res = await remna_extend(uid, TRIAL_DAYS, hwid=TRIAL_DEVICES, squads=[SQUAD_BASIC, SQUAD_WL])
    if not res:
        await db_exec("UPDATE cn_users SET trial_used=FALSE WHERE user_id=$1", uid)
        return "Не получилось включить, попробуйте позже"
    await db_exec("UPDATE cn_users SET plan='trial' WHERE user_id=$1", uid)
    await db_exec("INSERT INTO cn_wl (user_id, gb_limit, base_gb, period_start) VALUES ($1,$2,$2,$3) "
                  "ON CONFLICT (user_id) DO UPDATE SET gb_limit=$2, base_gb=$2, period_start=$3, cut_off=FALSE",
                  uid, TRIAL_GB, now())
    await send_guide(uid)
    return ""


# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ (карточки, которые рисует сервер)
# ─────────────────────────────────────────────
PRICE_KEYS = {
    "price": ("Подписка, ₽/мес", 10, 1), "dp": ("Доп. устройство, ₽", 5, 1),
    "gp": ("1 ГБ 📶, ₽", 1, 1), "pg": ("📶 в месяц, ГБ", 5, 0), "bd": ("Устройств в подписке", 1, 1),
    "d3": ("Скидка за 3 мес, %", 5, 0), "d6": ("Скидка за 6 мес, %", 5, 0), "d12": ("Скидка за 12 мес, %", 5, 0),
}


def back(to: str = "a:h") -> list:
    return ["Назад", to]


async def adm_home() -> dict:
    t = now()
    day0 = int(datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    r = await db_row(
        "SELECT count(*) AS n, count(*) FILTER (WHERE expire_ts>$1 AND plan='paid') AS a, "
        "count(*) FILTER (WHERE expire_ts>$1 AND plan='trial') AS tr, "
        "count(*) FILTER (WHERE created_at>$1-86400) AS nw, count(*) FILTER (WHERE blocked) AS bl "
        "FROM cn_users", t)
    inc = await db_row(
        "SELECT coalesce(sum(amount) FILTER (WHERE paid_at>=$2),0) AS d, "
        "coalesce(sum(amount) FILTER (WHERE paid_at>$1-7*86400),0) AS w, "
        "coalesce(sum(amount) FILTER (WHERE paid_at>$1-30*86400),0) AS m "
        "FROM cn_invoices WHERE status='PAID'", t, day0)
    pay = " · ".join(x for x in ("Platega ✓" if PLATEGA_ON else "Platega ✕",
                                 "TON ✓" if TON_WALLET else "TON ✕"))
    return {"t": "Админ-панель", "r": [
        ["Пользователей", f"{r['n']} (заблок. {r['bl']})"], ["Активных подписок", r["a"]],
        ["На пробном", r["tr"]], ["Новых за сутки", r["nw"]],
        ["Доход сегодня", f"{inc['d']} ₽"], ["За 7 дней", f"{inc['w']} ₽"], ["За 30 дней", f"{inc['m']} ₽"],
        ["Оплата", pay]],
        "b": [["Найти", "a:find"], ["Выдать", "a:give"], ["Промокоды", "a:pl"], ["Рассылка", "a:bc"],
              ["Цены", "a:pr"], ["Выходные", "a:wk"], ["Обновить", "a:h", 1]], "c": 2}


INPUT_HINTS = {
    "find": "Пришлите ID пользователя или @username.",
    "give": "Пришлите: <b>ID дни</b> — например <code>123456789 30</code>.",
    "promo": "Пришлите: <b>КОД 30д [кол-во]</b> — дни подписки, или <b>КОД 15% [кол-во]</b> — скидка.<br>"
             "Без количества — 1 использование, 0 — без ограничений.",
    "bc": "Пришлите сообщение для рассылки. Текст придёт людям карточкой-виджетом, "
          "фото и другое — обычным сообщением.",
}


async def adm_input(admin: int, mode: str, hint: str | None = None) -> dict:
    await db_exec("UPDATE cn_users SET amode=$2 WHERE user_id=$1", admin, mode)
    return {"t": "Жду сообщение", "x": hint or INPUT_HINTS[mode], "in": mode.split(":")[0],
            "b": [["Отмена", "a:x"]]}


async def adm_user(uid: int, note: str = "") -> dict:
    u = await get_user(uid)
    if not u:
        return {"t": "Не найден", "x": f"Пользователя {uid} нет в базе бота.", "b": [back()]}
    try:
        r = await remna_get(uid)
    except Exception:
        r = None
    exp = parse_ts(r.get("expireAt")) if r else 0
    if not r:
        status = "нет подписки"
    elif exp > now():
        status = ("пробная до " if u["plan"] == "trial" else "до ") + fmt_date(exp)
    else:
        status = "закончилась " + fmt_date(exp)
    if u.get("banned"):
        status = "заблокирован"
    wl = await db_row("SELECT * FROM cn_wl WHERE user_id=$1", uid)
    refs = await db_val("SELECT count(*) FROM cn_users WHERE referrer_id=$1", uid)
    name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "—")
    rows = [["ID", uid], ["Имя", esc(name)], ["Подписка", status],
            ["Устройства", f"{r.get('hwidDeviceLimit') if r else '—'} (доп. {u['extra_dev']})"],
            ["📶", f"{await wl_used_gb(uid, wl['period_start'])} из {wl['gb_limit']} ГБ" if wl else "—"],
            ["Оплачено", f"{u['paid_total']} ₽"], ["Пригласил", refs], ["С нами с", fmt_date(u["created_at"])]]
    p = f"a:uo:{uid}:"
    return {"t": "Пользователь", "r": rows, "x": note or None, "c": 3, "b": [
        ["+7 дн", p + "d7"], ["+30 дн", p + "d30"], ["−7 дн", p + "m7"],
        ["−1 устр", p + "v-"], ["+1 устр", p + "v+"], ["+10 ГБ 📶", p + "g10"],
        ["Написать", p + "msg"], ["Разблок." if u.get("banned") else "Заблок.", p + ("ub" if u.get("banned") else "bn")],
        back()]}


async def adm_user_op(admin: int, uid: int, op: str) -> dict:
    u = await get_user(uid)
    if not u:
        return await adm_user(uid)
    st = await settings()
    ok = True
    if op in ("d7", "d30"):
        ok = bool(await grant_days(uid, int(op[1:])))
    elif op == "m7":
        ok = bool(await remna_extend(uid, -7))
    elif op in ("v+", "v-"):
        extra = max(0, int(u["extra_dev"]) + (1 if op == "v+" else -1))
        base = TRIAL_DEVICES if u["plan"] == "trial" else st["bd"]
        ok = bool(await remna_patch(uid, {"hwidDeviceLimit": base + extra}))
        if ok:
            await db_exec("UPDATE cn_users SET extra_dev=$2 WHERE user_id=$1", uid, extra)
    elif op == "g10":
        ok = await grant({"user_id": uid, "kind": "g", "qty": 10})
    elif op in ("bn", "ub"):
        ok = bool(await remna_patch(uid, {"status": "DISABLED" if op == "bn" else "ACTIVE"}))
        if ok:
            await db_exec("UPDATE cn_users SET banned=$2 WHERE user_id=$1", uid, op == "bn")
    elif op == "msg":
        return await adm_input(admin, f"msg:{uid}", f"Пришлите текст — он придёт пользователю {uid} карточкой.")
    if ok:
        await push_state(uid)
    return await adm_user(uid, "Готово ✓" if ok else f"Не получилось: {esc(LAST_REMNA_ERROR[:200])}")


async def adm_promos(note: str = "") -> dict:
    rows = await db_rows("SELECT * FROM cn_promos ORDER BY created_at DESC LIMIT 8")
    st = await settings()
    lines = [f"<b>{esc(st['wk_code'])}</b> · −{st['wk_pct']}% по выходным · "
             f"{'вкл' if st['wk_on'] else 'выкл'}"]
    for p in rows:
        what = f"{p['value']} дн." if p["kind"] == "days" else f"−{p['value']}%"
        left = "∞" if p["uses_left"] is None else p["uses_left"]
        lines.append(f"<b>{esc(p['code'])}</b> · {what} · исп. {p['used']}, осталось {left}")
    b = [["Создать", "a:pn", 1]] + [[f"Удалить {p['code']}", f"a:pd:{p['code']}"] for p in rows[:6]] + [back()]
    return {"t": "Промокоды", "x": (note + "<br>" if note else "") + "<br>".join(lines), "b": b}


async def adm_prices(note: str = "") -> dict:
    st = await settings()
    return {"t": "Цены", "x": note or None, "c": 2,
            "r": [[v[0], st[k]] for k, v in PRICE_KEYS.items()],
            "b": [[v[0].split(",")[0], f"a:pe:{k}"] for k, v in PRICE_KEYS.items()] + [back()]}


async def adm_price_edit(key: str) -> dict:
    st = await settings()
    name, step, _ = PRICE_KEYS[key]
    p = f"a:pv:{key}:"
    return {"t": name, "x": f"Сейчас: <b>{st[key]}</b>", "c": 4,
            "b": [[f"−{step * 5}", f"{p}-{step * 5}"], [f"−{step}", f"{p}-{step}"],
                  [f"+{step}", f"{p}{step}"], [f"+{step * 5}", f"{p}{step * 5}"], back("a:pr")]}


async def adm_weekend(note: str = "") -> dict:
    st = await settings()
    return {"t": "Скидка выходного дня", "x": note or None, "r": [
        ["Статус", "включена" if st["wk_on"] else "выключена"], ["Скидка", f"{st['wk_pct']}%"],
        ["Промокод", esc(st["wk_code"])], ["Рассылка", "по субботам в 11:00 МСК"],
        ["Последняя", st["wk_last"] or "—"]], "c": 2,
        "b": [["Выключить" if st["wk_on"] else "Включить", "a:wt"], ["Тест мне", "a:wm"],
              ["−5%", "a:wp:-5"], ["+5%", "a:wp:5"], ["Разослать сейчас", "a:ws"], back()]}


def weekend_card(st: dict) -> dict:
    return {"t": f"−{st['wk_pct']}% на выходных ☁️",
            "x": f"Только в эту субботу и воскресенье — скидка {st['wk_pct']}% на подписку, "
                 f"устройства и 📶 по промокоду <b>{esc(st['wk_code'])}</b>.",
            "b": [[f"Скопировать {st['wk_code']}", f"c:{st['wk_code']}"],
                  ["Купить со скидкой", "open:wk", 1]]}


async def adm_bc_confirm() -> dict:
    st = await settings()
    n_all = await db_val("SELECT count(*) FROM cn_users WHERE NOT blocked")
    n_act = await db_val("SELECT count(*) FROM cn_users WHERE NOT blocked AND expire_ts>$1", now())
    preview = esc(st.get("bc_text") or "[фото / медиа]")[:300].replace("\n", "<br>")
    return {"t": "Рассылка", "x": preview, "c": 2,
            "b": [[f"Всем · {n_all}", "a:bs:all", 1], [f"С подпиской · {n_act}", "a:bs:act"],
                  ["Отмена", "a:h"]]}


async def adm_route(admin: int, data: str) -> dict:
    parts = data.split(":")
    cmd = parts[1] if len(parts) > 1 else "h"
    if cmd in ("h", "x"):
        await db_exec("UPDATE cn_users SET amode='' WHERE user_id=$1", admin)
        return await adm_home()
    if cmd in ("find", "give", "bc"):
        return await adm_input(admin, cmd)
    if cmd == "pn":
        return await adm_input(admin, "promo")
    if cmd == "pl":
        return await adm_promos()
    if cmd == "pd" and len(parts) > 2:
        await db_exec("DELETE FROM cn_promos WHERE code=$1", parts[2])
        return await adm_promos(f"Удалён {esc(parts[2])}")
    if cmd == "pr":
        return await adm_prices()
    if cmd == "pe" and parts[2] in PRICE_KEYS:
        return await adm_price_edit(parts[2])
    if cmd == "pv" and parts[2] in PRICE_KEYS:
        key = parts[2]
        st = await settings()
        lo = PRICE_KEYS[key][2]
        hi = 90 if key.startswith("d") and key != "dp" else 100000
        await save_setting(key, min(hi, max(lo, int(st[key]) + int(parts[3]))))
        return await adm_price_edit(key)
    if cmd == "wk":
        return await adm_weekend()
    if cmd == "wt":
        st = await settings()
        await save_setting("wk_on", 0 if st["wk_on"] else 1)
        return await adm_weekend()
    if cmd == "wp":
        st = await settings()
        await save_setting("wk_pct", min(50, max(5, int(st["wk_pct"]) + int(parts[2]))))
        return await adm_weekend()
    if cmd == "wm":
        await send_card(admin, weekend_card(await settings()))
        return await adm_weekend("Отправил вам пример ниже ↓")
    if cmd == "ws":
        st = await settings()
        _, wk_id, _ = weekend_info()
        await save_setting("wk_last", wk_id or datetime.now(MSK).strftime("%Y-%m-%d"))
        asyncio.create_task(broadcast(admin, False, card=weekend_card(st)))
        return await adm_weekend("Рассылка запущена, итог придёт сообщением")
    if cmd == "bs":
        asyncio.create_task(broadcast(admin, parts[2] == "act"))
        return {"t": "Рассылка запущена", "x": "Итог придёт отдельным сообщением.", "b": [back()]}
    if cmd == "u" and parts[2].isdigit():
        return await adm_user(int(parts[2]))
    if cmd == "uo" and parts[2].isdigit():
        return await adm_user_op(admin, int(parts[2]), parts[3])
    return await adm_home()


async def adm_show(admin: int, card: dict) -> None:
    """Показывает экран в админ-панели (правит прежнее сообщение или шлёт новое)."""
    u = await get_user(admin)
    aw = u.get("aw") if u else None
    if aw and widgets_on():
        try:
            await tg("editMessageWidgetData", chat_id=admin, message_id=aw, chat_data=jdump(card_fit(card)))
            return
        except TgError as e:
            if "not modified" in str(e):
                return
    # после сообщения админа — новый экран ниже, а не правка старого где-то выше
    m = await send_card(admin, card, caption="<b>Админ-панель</b>")
    if m:
        await db_exec("UPDATE cn_users SET aw=$2 WHERE user_id=$1", admin, m["message_id"])


async def adm_text(msg: dict, mode: str) -> None:
    admin = msg["from"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    await adm_show(admin, await adm_text_card(admin, mode, text, msg))


async def adm_text_card(admin: int, mode: str, text: str, msg: dict | None) -> dict:
    """Ввод админа (из чата или из мини-приложения) → следующий экран."""
    await db_exec("UPDATE cn_users SET amode='' WHERE user_id=$1", admin)
    if mode == "find":
        q = text.lstrip("@")
        row = (await db_row("SELECT user_id FROM cn_users WHERE user_id=$1", int(q)) if q.isdigit()
               else await db_row("SELECT user_id FROM cn_users WHERE lower(username)=lower($1)", q))
        card = await adm_user(row["user_id"]) if row else {"t": "Не найден", "x": esc(text), "b": [back()]}
    elif mode == "give":
        m = re.fullmatch(r"@?(\w+)\s+(-?\d+)", text)
        uid = None
        if m:
            uid = int(m.group(1)) if m.group(1).isdigit() else await db_val(
                "SELECT user_id FROM cn_users WHERE lower(username)=lower($1)", m.group(1))
        if not uid:
            card = {"t": "Не понял", "x": "Формат: <code>ID дни</code>", "b": [["Ещё раз", "a:give"], back()]}
        else:
            if not await get_user(uid):
                await db_exec("INSERT INTO cn_users (user_id, created_at) VALUES ($1,$2) "
                              "ON CONFLICT DO NOTHING", uid, now())
            days = int(m.group(2))
            ok = await grant_days(uid, days) if days > 0 else await remna_extend(uid, days)
            card = await adm_user(uid, f"{'Выдано' if days > 0 else 'Списано'} {abs(days)} дн. ✓" if ok
                                  else f"Не получилось: {esc(LAST_REMNA_ERROR[:200])}")
    elif mode == "promo":
        m = re.fullmatch(r"([A-Za-z0-9_-]{3,32})\s+(\d+)\s*(д|d|дн|дней|%)\s*(\d+)?", text, re.I)
        if not m:
            card = {"t": "Не понял", "x": INPUT_HINTS["promo"], "b": [["Ещё раз", "a:pn"], back()]}
        else:
            code, val, unit, uses = m.group(1).upper(), int(m.group(2)), m.group(3), m.group(4)
            kind = "pct" if unit == "%" else "days"
            if kind == "pct":
                val = min(90, val)
            uses_left = None if uses == "0" else int(uses or 1)
            await db_exec("INSERT INTO cn_promos (code, kind, value, uses_left, created_at) VALUES ($1,$2,$3,$4,$5) "
                          "ON CONFLICT (code) DO UPDATE SET kind=$2, value=$3, uses_left=$4",
                          code, kind, val, uses_left, now())
            card = await adm_promos(f"Создан {esc(code)} ✓")
    elif mode == "bc":
        await save_setting("bc_text", (msg.get("text") or "") if msg else text)
        await save_setting("bc_mid", msg["message_id"] if msg else 0)
        await save_setting("bc_chat", admin)
        card = await adm_bc_confirm()
    elif mode.startswith("msg:"):
        uid = int(mode[4:])
        sent = await send_card(uid, {"t": "Сообщение от поддержки", "x": esc(text).replace("\n", "<br>"),
                                     "b": [["Ответить", f"g:https://t.me/{SUPPORT}"]]})
        card = await adm_user(uid, "Отправлено ✓" if sent else "Не доставлено")
    else:
        card = await adm_home()
    return card


async def broadcast(admin: int, only_active: bool, card: dict | None = None) -> None:
    st = await settings()
    sql = "SELECT user_id FROM cn_users WHERE NOT blocked" + (" AND expire_ts>$1" if only_active else "")
    users = await db_rows(sql, *([now()] if only_active else []))
    text = st.get("bc_text") or ""
    if card is None and text:
        card = {"x": esc(text).replace("\n", "<br>"), "b": [["Открыть меню", "open:h", 1]]}
    ok = 0
    for r in users:
        uid = r["user_id"]
        try:
            if card:
                sent = await send_card(uid, card, caption=esc(text) if text and "t" not in card else None)
            else:
                sent = await tg("copyMessage", chat_id=uid, from_chat_id=int(st.get("bc_chat") or admin),
                                  message_id=int(st["bc_mid"]))
            ok += 1 if sent else 0
        except TgError as e:
            await note_send_error(uid, e)
        await asyncio.sleep(0.05)
    await send_text(admin, f"📣 Рассылка завершена: доставлено {ok} из {len(users)}.")


# ─────────────────────────────────────────────
#  ЗАПРОСЫ ВИДЖЕТОВ
# ─────────────────────────────────────────────
PAY_RE = re.compile(r"pay:([pdg])(\d{1,3}):([pt])")


async def widget_route(q: dict) -> dict | None:
    uid = q["from"]["id"]
    data = q.get("data") or ""
    qtype = (q.get("type") or {}).get("type") or "data"
    u = await get_user(uid)
    if not u:
        await touch_user(q["from"])
    if data.startswith("a:"):
        return await adm_route(uid, data) if is_admin(uid) else {"m": "Нет доступа"}
    if data == "s":
        return await user_state(uid)
    if data == "trial":
        err = await activate_trial(uid)
        return {"m": err} if err else {"st": await user_state(uid, cached=False),
                                       "m": "Пробный доступ включён — инструкция ниже ↓"}
    if data == "guide":
        await send_guide(uid)
        return {"m": "Инструкция — в сообщении ниже ↓"}
    if data == "wk":
        err = await apply_weekend(uid)
        return {"m": err} if err else {"st": await user_state(uid)}
    if data.startswith("open:"):
        view = data[5:]
        if view == "wk":
            err = await apply_weekend(uid)
            if err:
                return {"m": err}
            view = "b"
        await send_main(uid, view if view in ("h", "b", "r", "c") else None)
        return {"m": "Меню — ниже ↓"}
    m = PAY_RE.fullmatch(data)
    if m:
        kind, qty, method = m.group(1), int(m.group(2)), m.group(3)
        inv, err = await make_invoice(uid, kind, qty, "ton" if method == "t" else "platega")
        if not inv:
            return {"m": err}
        if method == "t":
            tx = {"valid_until": now() + 900,
                  "messages": [{"address": TON_WALLET, "amount": str(inv["ton_nano"]),
                                "payload": comment_boc(inv["id"])}]}
            return {"tx": tx, "i": inv["id"], "a": ton_str(inv["ton_nano"])}
        pay = {"i": inv["id"], "a": inv["amount"], "u": inv["url"], "m": "p", "st": "w"}
        if len(inv["url"]) > 250:
            pay["u"] = ""
            await send_text(uid, f"Счёт на {inv['amount']} ₽ — {esc(item_name(kind, qty))}",
                            ikb([[(f"Оплатить {inv['amount']} ₽", inv["url"])]]))
            return {"pay": pay, "m": "Ссылка на оплату — в сообщении ниже ↓"}
        return {"pay": pay}
    if data.startswith("ts:") and qtype == "sent_transaction":
        inv = await db_row("SELECT * FROM cn_invoices WHERE id=$1 AND user_id=$2 AND method='ton'",
                           data[3:], uid)
        if not inv:
            return {"m": "Счёт не найден"}
        await db_exec("UPDATE cn_invoices SET msg_hash=$2 WHERE id=$1",
                      inv["id"], str(q["type"].get("msg_hash") or "sent"))
        asyncio.create_task(ton_check_soon())
        return {"m": "Платёж отправлен, проверяем в сети"}
    return {"m": "Обновите меню: /start"}


async def on_widget_query(q: dict) -> None:
    try:
        ans = await asyncio.wait_for(widget_route(q), timeout=13)
    except asyncio.TimeoutError:
        ans = {"m": "Сервис отвечает долго, попробуйте ещё раз"}
    except Exception:
        log.exception("widget_query %s", q.get("data"))
        ans = {"m": "Ошибка, попробуйте ещё раз"}
    payload = jdump(ans) if ans else None
    if payload and len(payload.encode()) > 1024:
        log.error("ответ виджету больше 1024 байт: %s", payload[:200])
        payload = jdump({"m": "Ошибка ответа"})
    try:
        await tg("answerWidgetQuery", widget_query_id=q["id"], data=payload)
    except TgError as e:
        log.warning("answerWidgetQuery: %s", e)


# ─────────────────────────────────────────────
#  СООБЩЕНИЯ И ЗАПАСНЫЕ КНОПКИ
# ─────────────────────────────────────────────
async def on_message(msg: dict) -> None:
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or "from" not in msg:
        return
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        arg = text[6:].strip()
        ref = int(arg[1:]) if arg.startswith("r") and arg[1:].isdigit() else None
        await touch_user(msg["from"], ref)
        await send_main(uid)
        return
    u = await get_user(uid) or await touch_user(msg["from"])
    if text in ("/menu", "/help"):
        await send_main(uid)
        return
    if text == "/admin" and is_admin(uid):
        await db_exec("UPDATE cn_users SET amode='' WHERE user_id=$1", uid)
        m = await send_card(uid, await adm_home(), caption="<b>Админ-панель</b>")
        if m:
            await db_exec("UPDATE cn_users SET aw=$2 WHERE user_id=$1", uid, m["message_id"])
        return
    if is_admin(uid) and u.get("amode"):
        await adm_text(msg, u["amode"])
        return
    code = text.upper()
    if not re.fullmatch(r"[A-ZА-Я0-9_-]{3,32}", code):
        await send_text(uid, "Меню бота — по команде /start. Вопросы — в поддержку.",
                        ikb([[("Открыть меню", "fb:menu"), ("Поддержка", f"https://t.me/{SUPPORT}")]]))
        return
    ok, reply = await apply_promo_code(uid, code)
    card = {"t": "Промокод " + esc(code), "x": esc(reply)}
    if ok:
        card["b"] = [["К покупке", "open:b", 1]] if "Скидка" in reply else [["Открыть меню", "open:h", 1]]
        await push_state(uid)
    await send_card(uid, card)


FB_MAP = {"fb:menu": "open:h", "fb:buy": "m:b:p1", "fb:conn": "gd:", "fb:ref": "m:r"}


async def on_callback(cb: dict) -> None:
    """Кнопки под сообщениями. Старые «fb:…» под виджетом ведут в те же экраны."""
    if not await get_user(cb["from"]["id"]):
        await touch_user(cb["from"])
    data = cb.get("data") or ""
    if data in FB_MAP:
        cb = dict(cb, data=FB_MAP[data])
    elif data.startswith("fb:p:"):
        cb = dict(cb, data=f"pay:p{data[5:]}:p")
    await classic_callback(cb)


# ─────────────────────────────────────────────
#  ФОНОВЫЕ ЗАДАЧИ
# ─────────────────────────────────────────────
async def check_platega() -> None:
    rows = await db_rows("SELECT id, ext_id FROM cn_invoices WHERE method='platega' AND status='PENDING' "
                         "AND created_at>$1", now() - 3600)
    for r in rows:
        status = await platega_status(r["ext_id"])
        if status == "CONFIRMED":
            await confirm_invoice(r["id"])
        elif status in ("CANCELED", "CHARGEBACKED", "EXPIRED"):
            uid = await db_val("UPDATE cn_invoices SET status='CANCELED' WHERE id=$1 AND status='PENDING' "
                               "RETURNING user_id", r["id"])
            if uid:
                await push_state(uid)


_ton_lock = asyncio.Lock()


async def check_ton() -> None:
    if not TON_WALLET or _ton_lock.locked():
        return
    async with _ton_lock:
        rows = await db_rows("SELECT id, ton_nano FROM cn_invoices WHERE method='ton' AND status='PENDING' "
                             "AND created_at>$1", now() - 3 * 3600)
        if not rows:
            return
        incoming = {}
        for com, value, h in await ton_incoming():
            incoming[com.upper()] = max(incoming.get(com.upper(), 0), value)
        for r in rows:
            got = incoming.get(r["id"])
            if got and got >= int(r["ton_nano"]) * 0.99:
                await confirm_invoice(r["id"])
            elif got:
                await db_exec("UPDATE cn_invoices SET status='UNDERPAID' WHERE id=$1", r["id"])
                await notify_admins(f"⚠️ TON-счёт {r['id']} оплачен не полностью: "
                                    f"{ton_str(got)} из {ton_str(r['ton_nano'])} TON")


async def ton_check_soon() -> None:
    for delay in (8, 15, 25, 40):
        await asyncio.sleep(delay)
        await check_ton()


async def payments_loop() -> None:
    n = 0
    while True:
        n += 1
        try:
            if PLATEGA_ON:
                await check_platega()
            if n % 3 == 0:
                await check_ton()
            if n % 6 == 0:          # раз в минуту — повтор неудавшихся выдач
                for r in await db_rows("SELECT id FROM cn_invoices WHERE status='ERROR' AND confirmed "
                                       "AND tries<60 AND created_at>$1", now() - 86400):
                    await confirm_invoice(r["id"])
        except Exception:
            log.exception("payments_loop")
        await asyncio.sleep(10)


async def check_wl_limits() -> None:
    if not WL_NODES or SQUAD_WL == SQUAD_BASIC:
        return
    rows = await db_rows("SELECT w.* FROM cn_wl w JOIN cn_users u USING (user_id) "
                         "WHERE NOT w.cut_off AND w.gb_limit>0 AND u.expire_ts>$1", now())
    for w in rows:
        uid = w["user_id"]
        used = await wl_used_gb(uid, w["period_start"])
        if used < w["gb_limit"]:
            continue
        try:
            r = await remna_get(uid)
        except Exception:
            continue
        sq = [s for s in squads_of(r) if s != SQUAD_WL] or [SQUAD_BASIC]
        if r and await remna_patch(uid, {"activeInternalSquads": sq}):
            await db_exec("UPDATE cn_wl SET cut_off=TRUE WHERE user_id=$1", uid)
            await send_card(uid, {"t": "Трафик 📶 закончился",
                                  "x": f"Вы израсходовали {w['gb_limit']} ГБ 📶 за период. "
                                       "Остальные серверы работают как обычно. Можно докупить гигабайты.",
                                  "b": [["Докупить", "open:b", 1]]})


async def send_reminders() -> None:
    t = now()
    st = await settings()
    rows = await db_rows("SELECT * FROM cn_users WHERE NOT blocked AND NOT banned AND plan IN ('paid','trial') "
                         "AND expire_ts BETWEEN $1 AND $2", t - 2 * 86400, t + 3 * 86400)
    on, _, _ = weekend_info()
    for u in rows:
        exp, stage = int(u["expire_ts"]), int(u["rem_stage"])
        left = exp - t
        need = 3 if left <= 0 else 2 if left <= 86400 else (1 if u["plan"] == "paid" else 0)
        if need <= stage:
            continue
        try:
            r = await remna_get(u["user_id"])
        except Exception:
            continue
        real = parse_ts(r.get("expireAt")) if r else 0
        if real != exp:                       # продлили — напоминание уже не нужно
            await db_exec("UPDATE cn_users SET expire_ts=$2, rem_stage=0 WHERE user_id=$1", u["user_id"], real)
            continue
        await db_exec("UPDATE cn_users SET rem_stage=$2 WHERE user_id=$1", u["user_id"], need)
        trial = u["plan"] == "trial"
        if need == 3:
            title = "Пробный доступ закончился" if trial else "Подписка закончилась"
            text = "VPN отключён. Оформите подписку — всё заработает за минуту, настраивать заново не нужно."
        else:
            days = max(1, math.ceil(left / 86400))
            title = "Пробный доступ скоро закончится" if trial else "Подписка скоро закончится"
            text = (f"Осталось {days} {plural(days, 'день', 'дня', 'дней')} — до {fmt_date(exp)}. "
                    "Продлите заранее, чтобы VPN не отключился.")
        if on and st["wk_on"]:
            text += f"<br>Сейчас выходные: промокод <b>{esc(st['wk_code'])}</b> даёт −{st['wk_pct']}%."
        await send_card(u["user_id"], {"t": title, "x": text,
                                       "b": [["Купить" if trial else "Продлить", "open:b", 1]]})
        await asyncio.sleep(0.05)


async def weekend_tick() -> None:
    st = await settings()
    d = datetime.now(MSK)
    _, wk_id, _ = weekend_info()
    if st["wk_on"] and d.weekday() == 5 and d.hour >= 11 and st["wk_last"] != wk_id:
        await save_setting("wk_last", wk_id)
        admin = next(iter(ADMIN_IDS), 0)
        await broadcast(admin, False, card=weekend_card(st))


async def slow_loop() -> None:
    n = 0
    while True:
        await asyncio.sleep(60)
        n += 1
        for job, every in ((weekend_tick, 5), (send_reminders, 30), (check_wl_limits, 30)):
            if n % every == 0:
                try:
                    await job()
                except Exception:
                    log.exception("%s", job.__name__)


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def handle_update(up: dict) -> None:
    try:
        if "widget_query" in up:
            await on_widget_query(up["widget_query"])
        elif "message" in up:
            await on_message(up["message"])
        elif "callback_query" in up:
            await on_callback(up["callback_query"])
        elif "my_chat_member" in up:
            m = up["my_chat_member"]
            if (m.get("new_chat_member") or {}).get("status") == "kicked":
                await db_exec("UPDATE cn_users SET blocked=TRUE WHERE user_id=$1", m["from"]["id"])
    except Exception:
        log.exception("update %s", up.get("update_id"))


def check_widgets() -> None:
    for name, src in (("main", MAIN_HTML), ("guide", GUIDE_HTML), ("card", CARD_HTML)):
        size = len(zlib.compress(src.encode(), 9))
        (log.info if size <= 4096 else log.error)("виджет %s: %d байт в сжатом виде (лимит 4096)", name, size)


async def main() -> None:
    global HTTP, pool, BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    HTTP = httpx.AsyncClient(limits=httpx.Limits(max_connections=50))
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    me = await tg("getMe")
    BOT_USERNAME = me["username"]
    check_widgets()
    log.info("Бот @%s запущен. Platega: %s, TON: %s", BOT_USERNAME, PLATEGA_ON, bool(TON_WALLET))
    await tg("setMyCommands", commands=[{"command": "start", "description": "Меню"}])
    await tg("deleteWebhook")
    await start_http()
    asyncio.create_task(payments_loop())
    asyncio.create_task(slow_loop())
    offset = 0
    allowed = ["message", "callback_query", "widget_query", "my_chat_member"]
    while True:
        try:
            ups = await tg("getUpdates", offset=offset, timeout=50, allowed_updates=allowed)
        except TgError as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)
            continue
        for up in ups:
            offset = up["update_id"] + 1
            asyncio.create_task(handle_update(up))



# ─────────────────────────────────────────────
#  ОБЫЧНЫЕ СООБЩЕНИЯ С КНОПКАМИ (пока Telegram не пускает виджеты)
#
#  Экраны описываются тем же словарём, что карточка-виджет:
#  t — заголовок, x — текст, r — строки «название: значение»,
#  b — кнопки [текст, действие], c — колонки, kb — готовые ряды кнопок.
#  Действия: «c:текст» копирует, «o:/g:ссылка» открывает, остальное — callback.
# ─────────────────────────────────────────────
HAPP = {
    "ios_ru": "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973",
    "ios": "https://apps.apple.com/app/happ-proxy-utility/id6504287215",
    "gp": "https://play.google.com/store/apps/details?id=com.happproxy",
    "apk": "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk",
    "win": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
    "dmg": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg",
    "atv": "https://apps.apple.com/app/happ-proxy-utility-for-tv/id6748297274",
}
GUIDE_DEVICES = {
    "i": ("iPhone и iPad", "Установите Happ из App Store",
          [("App Store (Россия)", HAPP["ios_ru"]), ("App Store (другие страны)", HAPP["ios"])]),
    "a": ("Android", "Установите Happ из Google Play или скачайте APK",
          [("Google Play", HAPP["gp"]), ("Скачать APK", HAPP["apk"])]),
    "w": ("Windows", "Скачайте и установите Happ для Windows", [("Скачать для Windows", HAPP["win"])]),
    "m": ("macOS", "Установите Happ из App Store или скачайте DMG",
          [("App Store", HAPP["ios_ru"]), ("Скачать DMG", HAPP["dmg"])]),
    "t": ("Телевизор", "Установите Happ из Google Play на Android TV или из App Store на Apple TV",
          [("Google Play", HAPP["gp"]), ("Apple TV", HAPP["atv"])]),
}


def screen_to_msg(card: dict) -> tuple[str, dict]:
    parts = []
    if card.get("t"):
        parts.append(f"<b>{card['t']}</b>")
    if card.get("x"):
        parts.append(re.sub(r"<br\s*/?>", "\n", card["x"]))
    if card.get("r"):
        parts.append("\n".join(f"{k}: <b>{v}</b>" for k, v in card["r"]))
    text = "\n\n".join(parts) or "…"

    def button(label, act):
        if act.startswith("c:"):
            return {"text": label, "copy_text": {"text": act[2:][:256]}}
        if act[:2] in ("o:", "g:"):
            return {"text": label, "url": act[2:]}
        if act.startswith("w:"):
            return {"text": label, "web_app": {"url": act[2:]}}
        if act.startswith("https://"):
            return {"text": label, "url": act}
        return {"text": label, "callback_data": act[:64]}

    if card.get("kb"):
        rows = [[button(lbl, act) for lbl, act in row] for row in card["kb"]]
    else:
        btns = [button(b[0], b[1]) for b in card.get("b") or []]
        cols = max(1, int(card.get("c") or 1))
        rows = [btns[i:i + cols] for i in range(0, len(btns), cols)]
    return text, {"inline_keyboard": rows}


async def main_screen(uid: int, view: str = "h", sel: str = "p1", state: dict | None = None) -> dict:
    s = state or await user_state(uid)
    if view == "b":
        return buy_screen(s, sel)
    if view == "r":
        share = "https://t.me/share/url?url=" + quote(s["rl"])
        return {"t": "Друзья", "x": f"За каждого друга, который оформит подписку, — <b>{s['rd']} дней</b> "
                                    f"бесплатно.\nПриглашено: {s['r'][0]} · оплатили: {s['r'][1]}\n\n"
                                    f"Ваша ссылка:\n<code>{esc(s['rl'])}</code>",
                "kb": [[("Скопировать ссылку", "c:" + s["rl"]), ("Поделиться", "o:" + share)],
                       [("Назад", "m:h")]]}
    if view == "c":
        x = ("Отправьте промокод обычным сообщением в этот чат — он применится сам.\n\n"
             f"Каждые выходные −{s['wp']}% по промокоду <b>{esc(s['wc'])}</b> (суббота и воскресенье).")
        if s.get("pp"):
            x += f"\n\nСейчас действует: <b>{esc(s['pc'])}</b>, скидка {s['pp']}%."
        return {"t": "Промокод", "x": x, "kb": [[("Назад", "m:h")]]}
    # главный экран
    if s["s"] in (1, 3):
        left = max(0, math.ceil((s["e"] - now()) / 86400))
        x = (f"{'Пробный доступ' if s['s'] == 3 else 'Подписка активна'} до <b>{fmt_date(s['e'])}</b> "
             f"({left} {plural(left, 'день', 'дня', 'дней')})\nУстройств: {s['d']}")
        if s.get("g"):
            x += f"\n📶 {s['g'][0]} из {s['g'][1]} ГБ"
    elif s["s"] == 2:
        x = f"Подписка закончилась {fmt_date(s['e'])}. Продлите — всё заработает сразу, настраивать заново не нужно."
    else:
        x = (f"<b>{s['p']} ₽ в месяц</b>\n{s['bd']} устройства · все локации · "
             f"📶 {s['pg']} ГБ в месяц")
    pay = s.get("pay") or {}
    if pay.get("st") == "w":
        x += f"\n\n⏳ Ждём оплату {pay['a']} {'TON' if pay.get('m') == 't' else '₽'} — после оплаты всё включится само."
    elif pay.get("st") == "ok":
        x += "\n\n✅ Оплата прошла. Инструкция по подключению — в сообщении ниже."
    if s.get("wk"):
        x += f"\n\n🔥 Выходные: −{s['wk']}% по промокоду <b>{esc(s['wc'])}</b>"
    if s.get("pp"):
        x += f"\n\nПромокод {esc(s['pc'])}: −{s['pp']}% на следующую покупку"
    kb = [[("📱 Открыть приложение", "w:" + MINIAPP_URL)]] if MINIAPP_URL else []
    if s.get("t"):
        kb.append([(f"Попробовать бесплатно · {s['td']} дн.", "trial")])
    if s.get("wk"):
        kb.append([(f"Применить −{s['wk']}%", "wk")])
    kb.append([("Продлить подписку" if s["s"] else "Купить подписку", "m:b:p1")])
    kb.append([("Подключить", "gd:" if s["s"] in (1, 3) else "m:b:p1"), ("Друзья", "m:r")])
    kb.append([("Промокод", "m:c"), ("Поддержка", f"g:https://t.me/{s['sp']}")])
    return {"t": f"☁️ {esc(BRAND)}", "x": x, "kb": kb}


def buy_screen(s: dict, sel: str) -> dict:
    kind, qty = sel[:1], int(sel[1:] or 1) if sel[1:].isdigit() else 1
    if kind not in VALID_QTY or qty not in VALID_QTY[kind] or (kind != "p" and s["s"] != 1):
        kind, qty = "p", 1
    st = {"price": s["p"], "dp": s["dp"], "gp": s["gp"], **{f"d{k}": v for k, v in s["dm"].items()}}
    full, price = calc_price(st, kind, qty, s.get("pp") or 0)
    if kind == "p":
        dm = s["dm"].get(str(qty), 0)
        x = (f"Подписка на {qty} мес.\n{s['bd'] + s['xd']} устройства · все локации · 📶 {s['pg'] * qty} ГБ"
             + (f"\nСкидка за срок: {dm}%" if dm else ""))
        chips = [(f"{'• ' if q == qty else ''}{q} мес", f"m:b:p{q}") for q in MONTHS]
    elif kind == "d":
        x = f"+{qty} к лимиту устройств, навсегда\n{s['dp']} ₽ за устройство"
        chips = [(f"{'• ' if q == qty else ''}+{q}", f"m:b:d{q}") for q in (1, 2, 3, 5)]
    else:
        x = f"📶 +{qty} ГБ к текущему периоду\n{s['gp']} ₽ за 1 ГБ"
        chips = [(f"{'• ' if q == qty else ''}{q} ГБ", f"m:b:g{q}") for q in (10, 25, 50, 100)]
    x += f"\n\nК оплате: <b>{price} ₽</b>" + (f" <s>{full} ₽</s>" if price < full else "")
    if s.get("pp"):
        x += f"\nПромокод {esc(s['pc'])}: −{s['pp']}%"
    kb = []
    if s["s"] == 1:
        kb.append([(("✓ " if kind == k else "") + name, f"m:b:{k}{q}")
                   for k, name, q in (("p", "Подписка", 1), ("d", "Устройства", 1), ("g", "📶 ГБ", 10))])
    kb.append(chips)
    sel = f"{kind}{qty}"
    if PLATEGA_ON:
        kb.append([(f"💳 Картой или СБП · {price} ₽", f"pay:{sel}:p")])
    if TON_WALLET:
        kb.append([("💎 Оплатить TON", f"pay:{sel}:t")])
    kb.append([("Назад", "m:h")])
    return {"t": "Покупка", "x": x, "kb": kb}


def guide_screen(su: str, dev: str) -> dict:
    if dev not in GUIDE_DEVICES:
        x = "Выберите устройство — покажем, что сделать."
        if su:
            x += f"\n\nСсылка подписки:\n<code>{esc(su)}</code>"
        keys = list(GUIDE_DEVICES)
        kb = [[(GUIDE_DEVICES[k][0], f"gd:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
        kb.append([("Поддержка", f"g:https://t.me/{SUPPORT}")])
        return {"t": "Как подключиться", "x": x, "kb": kb}
    name, first, links = GUIDE_DEVICES[dev]
    steps = [first, "Скопируйте ссылку подписки кнопкой ниже",
             "Откройте Happ, нажмите «+» → «Вставить из буфера»",
             "Нажмите большую кнопку подключения и разрешите добавить VPN"]
    if dev == "t":
        steps[2] = ("Откройте Happ на ТВ, нажмите «+» и добавьте подписку по ссылке — "
                    "её удобно отправить себе в Telegram на телевизоре")
    x = "\n".join(f"{i}. {t}" for i, t in enumerate(steps, 1))
    kb = [[(lbl, "o:" + url)] for lbl, url in links]
    if su:
        kb += [[("Скопировать ссылку подписки", "c:" + su)], [("Открыть страницу подписки", "o:" + su)]]
    else:
        x += "\n\nСсылка подписки появится после оплаты."
    kb.append([("Назад", "gd:")])
    return {"t": name, "x": x, "kb": kb}


async def pay_screen(uid: int, kind: str, qty: int, method: str) -> dict:
    inv, err = await make_invoice(uid, kind, qty, "ton" if method == "t" else "platega")
    back_btn = ("Назад", f"m:b:{kind}{qty}")
    if not inv:
        return {"t": "Оплата", "x": esc(err), "kb": [[back_btn]]}
    name = esc(item_name(kind, qty))
    if method == "t":
        await db_exec("UPDATE cn_invoices SET msg_hash='link' WHERE id=$1", inv["id"])
        url = (f"https://app.tonkeeper.com/transfer/{TON_WALLET}?amount={inv['ton_nano']}"
               f"&text={inv['id']}")
        return {"t": "Оплата TON", "x": f"{name}\nК оплате: <b>{ton_str(inv['ton_nano'])} TON</b> "
                                         f"(≈ {inv['amount']} ₽)\n\nКнопка откроет кошелёк с уже заполненной "
                                         f"суммой и комментарием <code>{inv['id']}</code> — его не меняйте. "
                                         "После перевода всё включится само в течение пары минут.",
                "kb": [[("Оплатить в кошельке", "o:" + url)], [back_btn]]}
    return {"t": "Оплата", "x": f"{name}\nК оплате: <b>{inv['amount']} ₽</b>\n\n"
                                 "После оплаты всё включится само, нажимать ничего не нужно.",
            "kb": [[(f"Оплатить {inv['amount']} ₽", inv["url"])], [back_btn]]}


async def classic_callback(cb: dict) -> None:
    uid = cb["from"]["id"]
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    mid = msg.get("message_id")
    toast, screen, is_main, is_admin_screen = None, None, False, False
    try:
        if data.startswith("m:"):
            parts = data.split(":")
            screen = await main_screen(uid, parts[1], parts[2] if len(parts) > 2 else "p1")
            is_main = True
        elif data.startswith("gd:"):
            try:
                su = sub_url(await remna_get(uid, cached=True))
            except Exception:
                su = ""
            screen = guide_screen(su, data[3:])
        elif data.startswith("a:"):
            if is_admin(uid):
                screen, is_admin_screen = await adm_route(uid, data), True
            else:
                toast = "Нет доступа"
        elif PAY_RE.fullmatch(data):
            m = PAY_RE.fullmatch(data)
            screen = await pay_screen(uid, m.group(1), int(m.group(2)), m.group(3))
        elif data == "trial":
            toast = await activate_trial(uid) or "Пробный доступ включён — инструкция ниже"
            screen, is_main = await main_screen(uid, "h"), True
        elif data == "wk":
            err = await apply_weekend(uid)
            toast = err or "Скидка применена"
            screen, is_main = await main_screen(uid, "b" if not err else "h", "p1"), True
        else:
            ans = await widget_route({"from": cb["from"], "data": data, "type": {"type": "data"}})
            toast = (ans or {}).get("m")
            if toast and toast.endswith("↓"):
                toast = None
    except Exception:
        log.exception("callback %s", data)
        toast = "Ошибка, попробуйте ещё раз"
    try:
        await tg("answerCallbackQuery", callback_query_id=cb["id"], text=(toast or None) and toast[:190])
    except TgError:
        pass
    if screen:
        m = await show_screen(uid, screen, edit_mid=mid)
        if m and is_main:
            await db_exec("UPDATE cn_users SET last_main=$2 WHERE user_id=$1", uid, m["message_id"])
        if m and is_admin_screen:
            await db_exec("UPDATE cn_users SET aw=$2 WHERE user_id=$1", uid, m["message_id"])


# ─────────────────────────────────────────────
#  МИНИ-ПРИЛОЖЕНИЕ (Telegram Mini App)
#
#  Страница живёт на Cloudflare Worker (MINIAPP_URL). Worker отдаёт HTML и
#  пересылает POST /api сюда — на встроенный HTTP-сервер бота (порт PORT).
#  Каждый запрос подписан Telegram (initData) — проверяем подпись токеном бота.
# ─────────────────────────────────────────────
import hashlib
import hmac
from urllib.parse import parse_qsl

from aiohttp import web

MINIAPP_URL = env("MINIAPP_URL").rstrip("/")
API_PORT = env_int("PORT", 8080)


def check_init_data(raw: str) -> dict | None:
    """Пользователь из initData, если подпись верна и ей меньше суток."""
    try:
        pairs = dict(parse_qsl(raw or "", keep_blank_values=True))
        got = pairs.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got) or now() - int(pairs.get("auth_date") or 0) > 86400:
            return None
        user = json.loads(pairs.get("user") or "{}")
        return user if user.get("id") else None
    except Exception:
        return None


async def app_state(uid: int) -> dict:
    st = await user_state(uid, cached=False)
    if st["s"] in (1, 3):
        try:
            st["su"] = sub_url(await remna_get(uid, cached=True))
        except Exception:
            pass
    if is_admin(uid):
        st["adm"] = 1
    return st


async def app_api(body: dict, user: dict) -> dict:
    uid = user["id"]
    if not await get_user(uid):
        await touch_user(user)
    a = str(body.get("a") or "s")
    if a == "s":
        return {"st": await app_state(uid)}
    if a == "trial":
        err = await activate_trial(uid)
        return {"m": err or "Пробный доступ включён — откройте «Подключить»", "st": await app_state(uid)}
    if a == "wk":
        err = await apply_weekend(uid)
        return {"m": err, "st": await app_state(uid)} if err else {"st": await app_state(uid)}
    if a == "promo":
        code = str(body.get("code") or "").strip().upper()
        if not re.fullmatch(r"[A-ZА-Я0-9_-]{3,32}", code):
            return {"m": "Введите промокод"}
        ok, reply = await apply_promo_code(uid, code)
        if ok:
            await push_state(uid)
        return {"m": reply, "st": await app_state(uid)}
    if a == "pay":
        kind, method = str(body.get("k") or "p"), str(body.get("mt") or "p")
        try:
            qty = int(body.get("q") or 1)
        except (TypeError, ValueError):
            qty = 1
        inv, err = await make_invoice(uid, kind, qty, "ton" if method == "t" else "platega")
        if not inv:
            return {"m": err}
        if method == "t":
            await db_exec("UPDATE cn_invoices SET msg_hash='link' WHERE id=$1", inv["id"])
            url = f"https://app.tonkeeper.com/transfer/{TON_WALLET}?amount={inv['ton_nano']}&text={inv['id']}"
            return {"pay": {"i": inv["id"], "a": ton_str(inv["ton_nano"]), "u": url, "m": "t", "st": "w"}}
        return {"pay": {"i": inv["id"], "a": inv["amount"], "u": inv["url"], "m": "p", "st": "w"}}
    if a.startswith("a:") or a == "ain":
        if not is_admin(uid):
            return {"m": "Нет доступа"}
        if a == "ain":
            u = await get_user(uid) or {}
            mode = u.get("amode") or ""
            if not mode:
                return {"card": await adm_home()}
            text = str(body.get("text") or "").strip()
            if not text:
                return {"m": "Пустое сообщение"}
            return {"card": await adm_text_card(uid, mode, text, None)}
        return {"card": await adm_route(uid, a)}
    return {"m": "Обновите приложение"}


async def http_api(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"m": "bad request"}, status=400)
    user = check_init_data(str(body.get("initData") or ""))
    if not user:
        return web.json_response({"m": "Откройте приложение из Telegram"}, status=403)
    try:
        res = await asyncio.wait_for(app_api(body, user), timeout=25)
    except asyncio.TimeoutError:
        res = {"m": "Сервис отвечает долго, попробуйте ещё раз"}
    except Exception:
        log.exception("miniapp api %s", body.get("a"))
        res = {"m": "Ошибка, попробуйте ещё раз"}
    return web.json_response(res)


async def start_http() -> None:
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/api", http_api)
    app.router.add_get("/", lambda r: web.Response(text=APP_HTML, content_type="text/html",
                                                   headers={"Cache-Control": "no-cache"}))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    log.info("HTTP API мини-приложения слушает порт %s", API_PORT)
    if MINIAPP_URL:
        try:
            await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Открыть",
                                                       "web_app": {"url": MINIAPP_URL}})
        except TgError as e:
            log.warning("setChatMenuButton: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
