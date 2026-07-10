"""
TrubaVPN — веб-админка.
Подключается к той же базе и панели Remnawave, что и Telegram-бот.
Авторизация: админ вводит свой Telegram ID → бот присылает код →
админ вводит код на сайте → сессия.
Синхронизировано с новой версией бота: тарифы VPN / VPN‑обход,
реферальная система и выплаты. Система тикетов / шаблонов / медиа-партнёров
убрана — её больше нет в боте.
"""
import os
import time
import random
import string
import secrets
import logging
import functools
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, abort, jsonify,
)

import config
import db
import remnawave as rw
import telegram
import payments as pay

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
# Важно: используем [[ ]] вместо   для вывода в шаблонах Jinja.
app.jinja_env.variable_start_string = "[["
app.jinja_env.variable_end_string = "]]"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

MSK = timezone(timedelta(hours=3))

# АДМИНСКИЙ САЙТ: полная панель. Режим зашит в код и не зависит от .env.
PUBLIC_MODE = False


@app.before_request
def _public_mode_guard():
    """В PUBLIC_MODE прячем админ-панель: пускаем только кабинеты и статику."""
    if not PUBLIC_MODE:
        return None
    ep = request.endpoint or ""
    path = request.path or ""
    if ep in ("cabinet", "cabinet_login", "cabinet_pay", "cabinet_notifications", "cabinet_notifications_seen", "cabinet_logout", "cabinet_withdraw", "cabinet_security", "cabinet_legacy", "static") or path.startswith("/cab") or path.startswith("/cabinet") or path.startswith("/static/"):
        return None
    abort(404)


# ──────────────────────── ФИЛЬТРЫ ШАБЛОНОВ ────────────────────
@app.template_filter("dt")
def _fmt_dt(ts, fmt="%d.%m.%Y %H:%M"):
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts), tz=MSK).strftime(fmt)
    except Exception:
        return "—"


@app.template_filter("money")
def _fmt_money(v):
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except Exception:
        return v


# Монохромный (ЧБ) значок-колокольчик для раздела «Активность» — рисуется цветом текста
# (currentColor), как и остальные геометрические иконки, вместо цветного emoji 🔔.
ACTIVITY_ICON = (
    "<svg viewBox='0 0 24 24' width='1em' height='1em' fill='none' stroke='currentColor' "
    "stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round' style='display:block;margin:0 auto'>"
    "<path d='M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9'/>"
    "<path d='M13.5 21a1.8 1.8 0 0 1-3 0'/></svg>"
)

# Разделы бокового меню (кроме «Настроек» — они всегда снизу и не прячутся).
# Каждый раздел можно скрыть индивидуально для каждого админа.
NAV_SECTIONS = [
    ("dashboard", "▣", "Статистика"),
    ("users", "◉", "Подписчики"),
    ("online", "●", "Кто онлайн"),
    ("servers", "▤", "Серверы"),
    ("give_key", "⦿", "Выдать ключ"),
    ("promos", "◈", "Промокоды"),
    ("payments", "₽", "Платежи"),
    ("referrals", "⑂", "Рефералы"),
    ("broadcast", "▶", "Рассылка"),
    ("survey", "★", "Опрос"),
    ("activity", ACTIVITY_ICON, "Активность"),
    ("logs", "≣", "Логи"),
]


def _admin_settings(aid):
    """Персональные настройки админа. Гарантирует наличие стро��и, возвращает dict."""
    if not aid:
        return {}
    try:
        row = db.query("SELECT * FROM admin_settings WHERE admin_id=%s", (aid,), one=True)
        if not row:
            db.execute(
                "INSERT INTO admin_settings (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING",
                (aid,),
            )
            row = db.query("SELECT * FROM admin_settings WHERE admin_id=%s", (aid,), one=True)
        return row or {}
    except Exception:
        return {}


def _hidden_sections(settings_row):
    raw = (settings_row or {}).get("hidden_sections") or ""
    return {s for s in raw.split(",") if s}


# Взаимодействия с пользователем, которые ПО УМОЛЧАНИЮ идут без уведомления.
# Для них в меню «Пользователь» есть ��алочка «Уведомить», а в настройках —
# персональный список «всегда уведомлять» (тогда галочку в меню снять нельзя).
NOTIFY_ACTIONS = [
    ("days", "Изменение срока подписки"),
    ("plan", "Смена тарифа"),
    ("hwid", "Смена лимита устройств"),
    ("access", "Включение / отключение подписки"),
    ("whitelist", "Изменение белых списков"),
    ("reissue", "Перевыдача подписки"),
]

# Сопоставление конкретного action → группа уведомления.
ACTION_NOTIFY_GROUP = {
    "add_days": "days", "sub_days": "days", "set_days": "days",
    "set_plan": "plan", "set_hwid": "hwid",
    "enable": "access", "disable": "access",
    "whitelist_on": "whitelist", "whitelist_off": "whitelist",
    "whitelist_add_gb": "whitelist", "whitelist_sub_gb": "whitelist",
    "reissue": "reissue",
}


def _notify_always(aid):
    raw = (_admin_settings(aid).get("notify_always") or "") if aid else ""
    return {s for s in raw.split(",") if s}


def _compose_user_notify(action, form, user_id):
    """Текст уведомления пользователю о выполненном действии (или None)."""
    try:
        if action == "add_days":
            d = int(form.get("days", 0) or 0)
            return f"➕ Администратор добавил вам {d} дн. подписки."
        if action == "sub_days":
            d = int(form.get("days", 0) or 0)
            return f"➖ Срок вашей подписки уменьшен на {d} дн."
        if action == "set_days":
            d = int(form.get("days", 0) or 0)
            return f"🗓 Срок вашей подписки обновлён: {d} дн. от сегодня."
        if action == "set_plan":
            pk = form.get("plan") or None
            if pk == "none":
                pk = None
            return f"🔁 Ваш тариф изменён: «{config.PLAN_NAMES.get(pk, 'без тарифа')}»."
        if action == "set_hwid":
            h = int(form.get("hwid", 1) or 1)
            return f"📱 Лимит устройств изменён: {config.HWID_LABELS.get(h, h)}."
        if action == "enable":
            return "✅ Ваша подписка активирована."
        if action == "disable":
            return "⛔️ Ваша подписка отключена."
        if action == "whitelist_off":
            return "📦 Белые списки для вас отключены."
        if action in ("whitelist_on", "whitelist_add_gb", "whitelist_sub_gb"):
            wl = db.query("SELECT gb_limit FROM whitelist_limits WHERE user_id=%s", (user_id,), one=True)
            gb = int((wl or {}).get("gb_limit") or 0)
            return f"📦 Лимит белых списков обновлён: {gb} ГБ."
        if action == "reissue":
            snap = rw.user_snapshot(user_id)
            sub = snap.get("sub_url") if snap.get("exists") else ""
            return ("🔄 <b>Ваша подписка перевыпущена</b>\n\n"
                    "Старая ссылка и конфигурация больше не работают. "
                    "Обновите приложение новой ссылкой:\n"
                    + (f"🌐 <code>{sub}</code>" if sub else ""))
    except Exception:
        return None
    return None


@app.context_processor
def _inject():
    aid = session.get("admin_id")
    st = _admin_settings(aid) if aid else {}
    dnd = bool(st.get("dnd"))
    return {
        "HWID_LABELS": config.HWID_LABELS,
        "HWID_OPTIONS": config.HWID_LABELS,
        "PLAN_NAMES": config.PLAN_NAMES,
        "PAY_LABELS": config.PAY_LABELS,
        "admin_id": aid,
        "now_ts": int(time.time()),
        "activity_unseen": (0 if dnd else (_activity_unseen() if aid else 0)),
        "nav_sections": NAV_SECTIONS,
        "hidden_sections": _hidden_sections(st),
        "admin_dnd": dnd,
        "notify_actions": NOTIFY_ACTIONS,
        "notify_always": {s for s in ((st.get("notify_always") or "").split(",")) if s},
    }


# ──────────────────────── АВТОРИЗАЦИЯ ────────────────────
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("login"))
        if session["admin_id"] not in config.ADMIN_IDS:
            session.clear()
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id") in config.ADMIN_IDS:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        raw = (request.form.get("tg_id") or "").strip()
        if not raw.isdigit():
            flash("Введите корректный числовой Telegram ID.", "error")
            return render_template("login.html")
        tg_id = int(raw)
        if tg_id not in config.ADMIN_IDS:
            # Не раскрываем, кто админ: общее сообщение
            flash("Если этот ID — администратор, код отправлен в Telegram.", "info")
            return render_template("login.html")

        code = f"{random.randint(0, 999999):06d}"
        expires = int(time.time()) + config.AUTH_CODE_TTL_MIN * 60
        db.execute(
            """INSERT INTO web_auth_codes (tg_id, code, expires_at, attempts)
               VALUES (%s, %s, %s, 0)
               ON CONFLICT (tg_id) DO UPDATE
               SET code=EXCLUDED.code, expires_at=EXCLUDED.expires_at, attempts=0""",
            (tg_id, code, expires),
        )
        sent = telegram.send_code(tg_id, code, config.AUTH_CODE_TTL_MIN)
        if not sent:
            flash("Не удалось отправить код. Откройте бота и нажмите /start, затем повторите.", "error")
            return render_template("login.html")
        session["pending_id"] = tg_id
        return redirect(url_for("verify"))
    return render_template("login.html")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    pending = session.get("pending_id")
    if not pending:
        return redirect(url_for("login"))
    if request.method == "POST":
        entered = (request.form.get("code") or "").strip()
        row = db.query("SELECT * FROM web_auth_codes WHERE tg_id=%s", (pending,), one=True)
        if not row:
            flash("Код не найден, запросите заново.", "error")
            return redirect(url_for("login"))
        if row["attempts"] >= 5:
            db.execute("DELETE FROM web_auth_codes WHERE tg_id=%s", (pending,))
            session.pop("pending_id", None)
            flash("Слишком много попыток. Запросите код заново.", "error")
            return redirect(url_for("login"))
        if int(time.time()) > row["expires_at"]:
            db.execute("DELETE FROM web_auth_codes WHERE tg_id=%s", (pending,))
            session.pop("pending_id", None)
            flash("Код истёк. Запросите новый.", "error")
            return redirect(url_for("login"))
        if entered != row["code"]:
            db.execute("UPDATE web_auth_codes SET attempts=attempts+1 WHERE tg_id=%s", (pending,))
            flash("Неверный код.", "error")
            return render_template("verify.html")
        # Успех
        db.execute("DELETE FROM web_auth_codes WHERE tg_id=%s", (pending,))
        session.pop("pending_id", None)
        session.permanent = True
        session["admin_id"] = pending
        flash("Добро пожаловать!", "info")
        return redirect(url_for("dashboard"))
    return render_template("verify.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ──────────────────────── ДАШБОРД / СТАТИСТИКА ─────────────
@app.route("/")
@login_required
def dashboard():
    now = int(time.time())
    day_start = int(datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    total = db.scalar("SELECT COUNT(*) FROM users") or 0
    paid = db.scalar("SELECT COUNT(*) FROM users WHERE has_paid=1") or 0
    promos = db.scalar("SELECT COUNT(*) FROM promos") or 0
    new_today = db.scalar("SELECT COUNT(*) FROM users WHERE created_at>=%s", (day_start,)) or 0

    pay_today = db.query(
        "SELECT amount, is_trial FROM payments WHERE created_at>=%s", (day_start,)
    ) or []
    # Доход = реально полученные рубли (amount). Выдачи админом записываются с
    # amount=0, поэтому в доход не попадают. Покупка пробной подписки (10 ₽)
    # записывается с реальной суммой и в доход попадает.
    revenue_today = sum(float(p["amount"] or 0) for p in pay_today)
    # Продажей считаем любой платёж с ненулевой суммой (выдачи админа — нет).
    sales_today = sum(1 for p in pay_today if float(p["amount"] or 0) > 0)

    revenue_total = db.scalar("SELECT COALESCE(SUM(amount),0) FROM payments") or 0
    ref_balance_total = db.scalar("SELECT COALESCE(SUM(referral_balance),0) FROM users") or 0
    payouts_total = db.scalar("SELECT COALESCE(SUM(amount),0) FROM referral_payouts") or 0

    all_users = rw.get_all_users()
    our = [u for u in all_users if str(u.get("username", "")).startswith("truba_")]
    active = sum(1 for u in our if rw.parse_dt(u.get("expireAt")) > now and u.get("status") != "DISABLED")
    online = sum(1 for u in our if rw.parse_dt((u.get("userTraffic") or {}).get("onlineAt")) > now - 180)

    recent_pays = db.query(
        """SELECT p.*, u.username FROM payments p
           LEFT JOIN users u ON u.user_id = p.user_id
           ORDER BY p.created_at DESC LIMIT 8"""
    ) or []

    stats = {
        "total": total, "paid": paid, "active": active, "online": online,
        "promos": promos, "new_today": new_today, "revenue_today": revenue_today,
        "sales_today": sales_today, "revenue_total": float(revenue_total),
        "ref_balance_total": float(ref_balance_total),
        "payouts_total": float(payouts_total),
        "panel_total": len(our),
    }
    return render_template("dashboard.html", s=stats, recent=recent_pays)


# ──────────────────────── ПОДПИСЧИКИ ────────────────────
@app.route("/users")
@login_required
def users():
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        if q.isdigit():
            rows = db.query(
                "SELECT * FROM users WHERE user_id=%s OR username ILIKE %s ORDER BY created_at DESC LIMIT 100",
                (int(q), like),
            )
        else:
            rows = db.query(
                "SELECT * FROM users WHERE username ILIKE %s ORDER BY created_at DESC LIMIT 100",
                (like,),
            )
    else:
        rows = db.query("SELECT * FROM users ORDER BY created_at DESC LIMIT 100")
    return render_template("users.html", users=rows or [], q=q)


@app.route("/api/search-users")
@login_required
def api_search_users():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    like = f"%{q}%"
    if q.isdigit():
        rows = db.query(
            "SELECT user_id, username, has_paid FROM users "
            "WHERE CAST(user_id AS TEXT) LIKE %s OR username ILIKE %s "
            "ORDER BY has_paid DESC, created_at DESC LIMIT 8",
            (like, like),
        )
    else:
        rows = db.query(
            "SELECT user_id, username, has_paid FROM users "
            "WHERE username ILIKE %s ORDER BY has_paid DESC, created_at DESC LIMIT 8",
            (like,),
        )
    return jsonify([
        {"id": r["user_id"], "username": r["username"], "paid": bool(r["has_paid"])}
        for r in (rows or [])
    ])


def _log(action, details, target_id=None):
    """Записать де��ствие администратора в журнал (раздел «Логи»)."""
    try:
        aid = session.get("admin_id")
        name = db.scalar("SELECT username FROM users WHERE user_id=%s", (aid,)) if aid else None
        db.execute(
            "INSERT INTO admin_logs (admin_id, admin_name, action, target_id, details, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (aid, name, action, target_id, details, int(time.time())),
        )
        if target_id:
            try:
                db.execute(
                    "INSERT INTO user_activity (user_id, kind, text, created_at, seen) VALUES (%s,%s,%s,%s,FALSE)",
                    (target_id, action, details, int(time.time())),
                )
            except Exception:
                pass
    except Exception:
        log.exception("admin log failed")


def _norm_btn_url(u):
    """Разрешаем в кнопках не только https: любые схемы (http, tg, mailto, tel),
    @username → t.me, «голый» домен → https://."""
    u = (u or "").strip()
    if not u:
        return ""
    if "://" in u or u.lower().startswith(("mailto:", "tel:", "tg:")):
        return u
    if u.startswith("@"):
        return "https://t.me/" + u.lstrip("@")
    return "https://" + u


_BACK_LABELS = [
    ("/payments", "К платежам"),
    ("/referrals", "К рефералам"),
    ("/online", "К онлайну"),
    ("/users", "К подписчикам"),
    ("/", "К статистике"),
]


def _resolve_back():
    """Ссылка и подпись кнопки «назад»: сначала явные back/bl из query,
    иначе оп��еделяем по HTTP Referer (работает и на ПК, и на телефоне)."""
    back = request.args.get("back")
    bl = request.args.get("bl")
    if not back and request.referrer:
        p = urlparse(request.referrer)
        here = urlparse(request.url)
        path = p.path or ""
        if p.netloc == here.netloc and path != request.path and "/action" not in path:
            back = path + (("?" + p.query) if p.query else "")
            for prefix, label in _BACK_LABELS:
                if path.startswith(prefix):
                    bl = bl or label
                    break
            bl = bl or "Назад"
    return back, bl


@app.route("/users/<int:user_id>")
@login_required
def user_detail(user_id):
    db_row = db.query("SELECT * FROM users WHERE user_id=%s", (user_id,), one=True)
    if not db_row:
        flash("Пользователь не найден в базе.", "error")
        return redirect(url_for("users"))
    snap = rw.user_snapshot(user_id)
    payments = db.query(
        "SELECT * FROM payments WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (user_id,)
    ) or []
    ref_count = db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=%s", (user_id,)) or 0
    wl_row = db.query("SELECT * FROM whitelist_limits WHERE user_id=%s", (user_id,), one=True)
    devices = rw.get_user_hwid(snap["uuid"]) if snap.get("exists") else []
    user_logs = db.query(
        "SELECT * FROM admin_logs WHERE target_id=%s ORDER BY created_at DESC LIMIT 50",
        (user_id,),
    ) or []
    cab_url = f"{_site_base()}/cab"
    cab_open_url = url_for("cabinet_admin_open", user_id=user_id)
    back, bl = _resolve_back()
    return render_template(
        "user_detail.html", u=db_row, snap=snap, payments=payments,
        ref_count=ref_count, wl=wl_row, devices=devices, logs=user_logs,
        cab_url=cab_url, cab_open_url=cab_open_url,
        back_url=(back or url_for("users")), back_label=(bl or "К списку"),
        back_param=back, bl_param=bl,
    )


@app.route("/users/<int:user_id>/action", methods=["POST"])
@login_required
def user_action(user_id):
    action = request.form.get("action")
    snap = rw.user_snapshot(user_id)

    if action == "add_days":
        days = int(request.form.get("days", 0) or 0)
        rw.extend_user(user_id, days)
        flash(f"Добавлено {days} дн.", "info")
        _log("Срок", f"Добавил {days} дн. подписки", user_id)
    elif action == "sub_days":
        days = int(request.form.get("days", 0) or 0)
        rw.extend_user(user_id, -days)
        flash(f"Снято {days} дн.", "info")
        _log("Срок", f"Снял {days} дн. подписки", user_id)
    elif action == "set_days":
        days = int(request.form.get("days", 0) or 0)
        rw.set_expire(user_id, days)
        flash(f"Срок установлен: через {days} дн.", "info")
        _log("Срок", f"Установил срок: через {days} дн.", user_id)
    elif action == "set_hwid":
        hwid = int(request.form.get("hwid", 1) or 1)
        if snap.get("exists"):
            rw.update_user(snap["uuid"], {"hwidDeviceLimit": hwid})
            # Синхронизируем extra_devices в БД (база — 1 устройство).
            extra = max(0, hwid - 1) if hwid else 0
            db.execute("UPDATE users SET extra_devices=%s WHERE user_id=%s", (extra, user_id))
            flash(f"Лимит устройств: {config.HWID_LABELS.get(hwid, hwid)}", "info")
            _log("Устройства", f"Лимит устройств: {config.HWID_LABELS.get(hwid, hwid)}", user_id)
        else:
            flash("Нет подписки в Remnawave.", "error")
    elif action == "set_plan":
        plan_key = request.form.get("plan") or None
        if plan_key == "none":
            plan_key = None
        if snap.get("exists") and plan_key in config.PLANS:
            squad = config.PLANS[plan_key]["squad"]
            rw.update_user(snap["uuid"], {"activeInternalSquads": rw.expand_squads(squad)})
            wl_gb = config.PLANS[plan_key]["whitelist_gb"]
            if wl_gb:
                db.execute(
                    """INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off)
                       VALUES (%s,%s,%s,FALSE)
                       ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE""",
                    (user_id, wl_gb, int(time.time()), wl_gb, int(time.time())),
                )
        db.execute("UPDATE users SET plan=%s WHERE user_id=%s", (plan_key, user_id))
        flash(f"Тариф: {config.PLAN_NAMES.get(plan_key, 'без тарифа')}", "info")
        _log("Тариф", f"Сменил тариф на «{config.PLAN_NAMES.get(plan_key, 'без тарифа')}»", user_id)
    elif action == "disable":
        if snap.get("exists"):
            rw.disable_user(snap["uuid"])
            flash("Подписка отключена.", "info")
            _log("Подписка", "Отключил подписку", user_id)
    elif action == "enable":
        if snap.get("exists"):
            rw.enable_user(snap["uuid"])
            flash("Подписка включена.", "info")
            _log("Подписка", "Включил подписку", user_id)
    elif action == "reissue":
        if snap.get("exists"):
            res = rw.revoke_subscription(snap["uuid"])
            if res:
                flash("Подписка перевыпущена: сгенерирована новая ссылка, старая больше не работает.", "info")
                _log("Подписка", "Перевыпустил подписку (revoke)", user_id)
            else:
                flash("Не удалось перевыпустить подписку.", "error")
        else:
            flash("Нет подписки в Remnawave.", "error")
    elif action == "whitelist_on":
        gb = int(request.form.get("gb", 0) or 0)
        if snap.get("exists"):
            rw.update_user(snap["uuid"], {"activeInternalSquads": rw.expand_squads(config.SQUAD_UUID_WHITELIST)})
            db.execute(
                """INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off)
                   VALUES (%s,%s,%s,FALSE)
                   ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE""",
                (user_id, gb, int(time.time()), gb, int(time.time())),
            )
            flash(f"Белые списки включены ({gb} GB).", "info")
            _log("Белые списки", f"Включил белые списки ({gb} ГБ)", user_id)
    elif action == "whitelist_off":
        if snap.get("exists"):
            rw.update_user(snap["uuid"], {"activeInternalSquads": [config.SQUAD_UUID_BASIC]})
        db.execute("DELETE FROM whitelist_limits WHERE user_id=%s", (user_id,))
        flash("Белые списки отключены.", "info")
        _log("Белые списки", "Отключил белые списки", user_id)
    elif action == "whitelist_add_gb":
        gb = int(request.form.get("gb", 0) or 0)
        wl = db.query("SELECT * FROM whitelist_limits WHERE user_id=%s", (user_id,), one=True)
        current = int((wl or {}).get("gb_limit") or 0)
        new_limit = max(0, current + gb)
        period_start = int((wl or {}).get("period_start") or time.time())
        if snap.get("exists"):
            rw.update_user(snap["uuid"], {"activeInternalSquads": rw.expand_squads(config.SQUAD_UUID_WHITELIST)})
        db.execute(
            """INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off)
               VALUES (%s,%s,%s,FALSE)
               ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, cut_off=FALSE""",
            (user_id, new_limit, period_start, new_limit),
        )
        flash(f"Белые списки: +{gb} ГБ (стало {new_limit} ГБ).", "info")
        _log("Белые списки", f"Добавил {gb} ГБ (стало {new_limit} ГБ)", user_id)
    elif action == "whitelist_sub_gb":
        gb = int(request.form.get("gb", 0) or 0)
        wl = db.query("SELECT * FROM whitelist_limits WHERE user_id=%s", (user_id,), one=True)
        current = int((wl or {}).get("gb_limit") or 0)
        new_limit = max(0, current - gb)
        db.execute(
            "UPDATE whitelist_limits SET gb_limit=%s, cut_off=FALSE WHERE user_id=%s",
            (new_limit, user_id),
        )
        flash(f"Белые списки: −{gb} ГБ (стало {new_limit} ГБ).", "info")
        _log("Белые списки", f"Снял {gb} ГБ (стало {new_limit} ГБ)", user_id)
    elif action == "payout":
        row = db.query("SELECT referral_balance FROM users WHERE user_id=%s", (user_id,), one=True)
        balance = float((row or {}).get("referral_balance") or 0)
        if balance <= 0:
            flash("Реферальный баланс уже нулевой.", "error")
        else:
            db.execute("UPDATE users SET referral_balance=0 WHERE user_id=%s", (user_id,))
            db.execute(
                "INSERT INTO referral_payouts (user_id, amount, created_at) VALUES (%s,%s,%s)",
                (user_id, balance, int(time.time())),
            )
            telegram.send_message(user_id, f"Ваш реферальный баланс {balance:.2f} руб. выплачен.")
            flash(f"Выплата {balance:.2f} руб. зафиксирована, баланс обнулён.", "info")
            _log("Выплата", f"Выплатил реферальный баланс {balance:.2f} ₽", user_id)
    elif action == "message":
        text = (request.form.get("text") or "").strip()
        btn_texts = request.form.getlist("btn_text")
        btn_urls = request.form.getlist("btn_url")
        _kb = []
        for bt, bu in zip(btn_texts, btn_urls):
            bt, bu = bt.strip(), _norm_btn_url(bu)
            if bt and bu:
                _kb.append([{"text": bt, "url": bu}])
        reply_markup = {"inline_keyboard": _kb} if _kb else None
        if text:
            ok = telegram.send_message(user_id, text, reply_markup=reply_markup)
            flash("Сообщение отправлено." if ok else "Не удалось отправить.", "info" if ok else "error")
            if ok:
                _log("Сообщение", f"Отправил личное сообщение: {text[:120]}", user_id)

    # Уведомление пользователя: по галочке в меню либо если включено «всегда» в настройках.
    group = ACTION_NOTIFY_GROUP.get(action)
    if group:
        want = (group in _notify_always(session.get("admin_id"))) or (request.form.get("notify") == "on")
        if want:
            msg = _compose_user_notify(action, request.form, user_id)
            if msg:
                try:
                    telegram.send_message(user_id, msg)
                except Exception:
                    pass

    back = request.args.get("back")
    bl = request.args.get("bl")
    return redirect(url_for("user_detail", user_id=user_id, back=back, bl=bl))


# ──────────────────────── ОНЛАЙН ────────────────────
@app.route("/online")
@login_required
def online():
    now = int(time.time())
    all_users = rw.get_all_users()
    our = [u for u in all_users if str(u.get("username", "")).startswith("truba_")]
    umap = {r["user_id"]: r.get("username")
            for r in (db.query("SELECT user_id, username FROM users") or [])}
    online_users = []
    for u in our:
        online_at = rw.parse_dt((u.get("userTraffic") or {}).get("onlineAt"))
        if online_at > now - 180:
            uname = str(u.get("username", ""))
            uid = uname.replace("truba_", "")
            try:
                real = umap.get(int(uid))
            except (TypeError, ValueError):
                real = None
            online_users.append({
                "user_id": uid,
                "username": ("@" + real) if real else uname,
                "online_at": online_at,
                "expire": rw.parse_dt(u.get("expireAt")),
            })
    online_users.sort(key=lambda x: x["online_at"], reverse=True)
    return render_template("online.html", users=online_users)


# ──────────────────────── ПРОМОКОДЫ ───────────────────
@app.route("/promos", methods=["GET", "POST"])
@login_required
def promos():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        ptype = request.form.get("promo_type", "days")
        uses = int(request.form.get("uses", 1) or 1)
        if not code:
            flash("Укажите код.", "error")
            return redirect(url_for("promos"))
        days = int(request.form.get("days", 0) or 0)
        discount = int(request.form.get("discount_percent", 0) or 0)
        tariff_key = request.form.get("tariff_key") or None
        db.execute(
            """INSERT INTO promos (code, days, uses, promo_type, tariff_key, discount_percent)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code) DO UPDATE SET days=EXCLUDED.days, uses=EXCLUDED.uses,
               promo_type=EXCLUDED.promo_type, tariff_key=EXCLUDED.tariff_key,
               discount_percent=EXCLUDED.discount_percent""",
            (code, days, uses, ptype, tariff_key, discount),
        )
        flash(f"Промокод {code} сохранён.", "info")
        _log("Промокод", f"Создал/изменил промокод {code} (тип {ptype}, дней {days}, исп. {uses})")
        return redirect(url_for("promos"))
    rows = db.query("SELECT * FROM promos ORDER BY code") or []
    return render_template("promos.html", promos=rows, plans=config.PLANS)


@app.route("/promos/<code>/delete", methods=["POST"])
@login_required
def promo_delete(code):
    db.execute("DELETE FROM promos WHERE code=%s", (code,))
    flash("Промокод удалён.", "info")
    _log("Промокод", f"Удалил промокод {code}")
    return redirect(url_for("promos"))


# ──────────────────────── ВЫДАТЬ КЛЮЧ ─────────────────
def _issue_subscription(uid, plan_key, days, extra_devices):
    """Выдать/про��лить подписку с учётом новой модели тарифов."""
    hwid = 1 + max(0, extra_devices)
    if plan_key == "trial":
        squad = config.TRIAL["squad"]
        wl_gb = config.TRIAL["whitelist_gb"]
        hwid = config.TRIAL["hwid"]
        plan_db = "trial"
    elif plan_key in config.PLANS:
        squad = config.PLANS[plan_key]["squad"]
        wl_gb = config.PLANS[plan_key]["whitelist_gb"]
        plan_db = plan_key
    else:  # custom
        squad = config.SQUAD_UUID_BASIC
        wl_gb = 0
        plan_db = None
    result = rw.extend_user(uid, days, hwid, squad)
    if not result:
        return None
    db.execute(
        "INSERT INTO users (user_id, created_at) VALUES (%s,%s) ON CONFLICT (user_id) DO NOTHING",
        (uid, int(time.time())),
    )
    if plan_key == "trial":
        db.execute(
            "UPDATE users SET remna_uuid=%s, plan='trial', trial_used=TRUE WHERE user_id=%s",
            (result.get("uuid"), uid),
        )
    else:
        db.execute(
            "UPDATE users SET remna_uuid=%s, plan=%s, extra_devices=%s, has_paid=1 WHERE user_id=%s",
            (result.get("uuid"), plan_db, max(0, extra_devices), uid),
        )
    if wl_gb:
        db.execute(
            """INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off)
               VALUES (%s,%s,%s,FALSE)
               ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE""",
            (uid, wl_gb, int(time.time()), wl_gb, int(time.time())),
        )
    # Подписка ВЫДАНА админом: в «Операции» остаётся сам тариф, факт выдачи —
    # в «Сумме» (source='gift', сумма 0 — в доход не идёт).
    op_key = "trial" if plan_key == "trial" else (plan_key if plan_key in config.PLANS else "extend")
    db.execute(
        "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, source, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (uid, 0, op_key, days, False, "gift", int(time.time())),
    )
    return result


@app.route("/give", methods=["GET", "POST"])
@login_required
def give_key():
    if request.method == "POST":
        raw = (request.form.get("user_id") or "").strip()
        if not raw.isdigit():
            flash("Введите числовой Telegram ID.", "error")
            return redirect(url_for("give_key"))
        uid = int(raw)
        plan_key = request.form.get("plan", "vpn")
        extra_devices = int(request.form.get("extra_devices", 0) or 0)
        if plan_key == "trial":
            days = config.TRIAL["days"]
        elif plan_key in config.PLANS:
            months = int(request.form.get("months", 1) or 1)
            days = months * 30
        else:
            days = int(request.form.get("days", 30) or 30)
        result = _issue_subscription(uid, plan_key, days, extra_devices)
        if result:
            sub = rw.format_sub_url(result)
            pname = config.PLAN_NAMES.get(plan_key, "подписка")
            telegram.send_message(
                uid,
                f"🎁 Вам выдана подписка <b>{pname}</b> на <b>{days}</b> дн.\n\n"
                + (f"🌐 <code>{sub}</code>" if sub else ""),
            )
            flash(f"Ключ выдан пользователю {uid}: {pname}, {days} дн.", "info")
            _log("Выдача", f"Выдал подписку «{pname}» на {days} дн.", uid)
            return redirect(url_for("user_detail", user_id=uid))
        flash("Ошибка выдачи ключа (Remnawave).", "error")
    return render_template("give.html", plans=config.PLANS, months=config.MONTH_CHOICES)


# ──────────────────────── ПЛАТЕЖИ ────────────────────
@app.route("/payments")
@login_required
def payments():
    rows = db.query(
        """SELECT p.*, u.username FROM payments p
           LEFT JOIN users u ON u.user_id = p.user_id
           ORDER BY p.created_at DESC LIMIT 300"""
    ) or []
    # Итог = сумма реальных руб��ей. Выда��и админа (amount=0) не влияют,
    # оплаченный триал (10 ₽) учитывается.
    total = db.scalar("SELECT COALESCE(SUM(amount),0) FROM payments") or 0
    return render_template("payments.html", payments=rows, total=float(total))


# ──────────────────────── РЕФЕРАЛЫ / ВЫПЛАТЫ ─────────────
@app.route("/referrals")
@login_required
def referrals():
    rows = db.query(
        """SELECT u.user_id, u.username, u.referral_balance,
                  (SELECT COUNT(*) FROM users r WHERE r.referrer_id = u.user_id) AS ref_count
           FROM users u
           WHERE u.referral_balance > 0
              OR EXISTS (SELECT 1 FROM users r WHERE r.referrer_id = u.user_id)
           ORDER BY u.referral_balance DESC, ref_count DESC LIMIT 300"""
    ) or []
    payouts = db.query(
        """SELECT p.*, u.username FROM referral_payouts p
           LEFT JOIN users u ON u.user_id = p.user_id
           ORDER BY p.created_at DESC LIMIT 100"""
    ) or []
    total_balance = db.scalar("SELECT COALESCE(SUM(referral_balance),0) FROM users") or 0
    total_paid = db.scalar("SELECT COALESCE(SUM(amount),0) FROM referral_payouts") or 0
    return render_template(
        "referrals.html", refs=rows, payouts=payouts,
        total_balance=float(total_balance), total_paid=float(total_paid),
        min_withdraw=config.REFERRAL_MIN_WITHDRAW, percent=config.REFERRAL_PERCENT,
    )


def _build_activity_feed(limit=300):
    """Общая лента активности (события + покупки) — для страницы и для звоночка."""
    acts = db.query(
        "SELECT user_id, kind, text, created_at, seen FROM user_activity ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ) or []
    pays = db.query(
        "SELECT user_id, amount, tariff_key, created_at FROM payments WHERE amount > 0 ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ) or []
    feed = []
    for a in acts:
        feed.append({"user_id": a["user_id"], "kind": a["kind"] or "Событие",
                     "text": a["text"], "created_at": a["created_at"] or 0,
                     "seen": bool(a["seen"])})
    for p in pays:
        nm = config.PLAN_NAMES.get(p["tariff_key"], p["tariff_key"] or "—")
        feed.append({"user_id": p["user_id"], "kind": "Покупка",
                     "text": f"Покупка ({nm}) — {float(p['amount'] or 0):.0f} ₽",
                     "created_at": p["created_at"] or 0, "seen": True})
    feed.sort(key=lambda x: x["created_at"], reverse=True)
    feed = feed[:limit]
    uids = list({f["user_id"] for f in feed if f["user_id"]})
    names = {}
    if uids:
        rows = db.query("SELECT user_id, username FROM users WHERE user_id = ANY(%s)", (uids,)) or []
        names = {r["user_id"]: r["username"] for r in rows}
    for f in feed:
        f["username"] = names.get(f["user_id"])
    return feed


@app.route("/activity")
@login_required
def activity():
    feed = _build_activity_feed(300)
    try:
        db.execute("UPDATE user_activity SET seen=TRUE WHERE seen=FALSE")
    except Exception:
        pass
    return render_template("activity.html", items=feed)


@app.route("/api/activity-feed")
@login_required
def api_activity_feed():
    """JSON-лента для выпадающего окна звоночка (как в личном кабинете)."""
    dnd = bool(_admin_settings(session["admin_id"]).get("dnd"))
    feed = _build_activity_feed(30)
    items = [
        {"kind": f["kind"], "text": f["text"], "ts": int(f["created_at"] or 0),
         "user_id": f["user_id"], "username": f.get("username"), "seen": bool(f.get("seen", True))}
        for f in feed
    ]
    unseen = 0 if dnd else (_activity_unseen() or 0)
    return jsonify({"ok": True, "unseen": unseen, "items": items})


@app.route("/api/activity-feed/seen", methods=["POST"])
@login_required
def api_activity_feed_seen():
    try:
        db.execute("UPDATE user_activity SET seen=TRUE WHERE seen=FALSE")
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/referrals/<int:user_id>/payout", methods=["POST"])
@login_required
def referral_payout(user_id):
    row = db.query("SELECT referral_balance FROM users WHERE user_id=%s", (user_id,), one=True)
    balance = float((row or {}).get("referral_balance") or 0)
    if balance <= 0:
        flash("Баланс уже нулевой.", "error")
        return redirect(url_for("referrals"))
    db.execute("UPDATE users SET referral_balance=0 WHERE user_id=%s", (user_id,))
    db.execute(
        "INSERT INTO referral_payouts (user_id, amount, created_at) VALUES (%s,%s,%s)",
        (user_id, balance, int(time.time())),
    )
    telegram.send_message(user_id, f"Ваш реферальный баланс {balance:.2f} руб. выплачен.")
    flash(f"Выплата {balance:.2f} руб. зафиксиро��ана, баланс обнулён.", "info")
    _log("Выплата", f"Выплатил реферальный баланс {balance:.2f} ₽", user_id)
    return redirect(url_for("referrals"))


# ──────────────────────── ОПРОС ────────────────────
@app.route("/survey")
@login_required
def survey():
    rows = db.query("SELECT * FROM survey_responses ORDER BY created_at DESC LIMIT 300") or []
    avg = db.scalar("SELECT AVG(rating) FROM survey_responses")
    avg = round(float(avg), 2) if avg is not None else None
    return render_template("survey.html", responses=rows, avg=avg, count=len(rows))


# ────────────────��─────── РАССЫЛКА ────────────────────
@app.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast():
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        audience = request.form.get("audience", "all")
        image_url = (request.form.get("image_url") or "").strip()
        btn_texts = request.form.getlist("btn_text")
        btn_urls = request.form.getlist("btn_url")
        buttons = []
        for bt, bu in zip(btn_texts, btn_urls):
            bt, bu = bt.strip(), _norm_btn_url(bu)
            if bt and bu:
                buttons.append([{"text": bt, "url": bu}])
        reply_markup = {"inline_keyboard": buttons} if buttons else None
        if not text and not image_url:
            flash("Введите текст рассылки или добавьте изображение.", "error")
            return redirect(url_for("broadcast"))
        if audience == "paid":
            rows = db.query("SELECT user_id FROM users WHERE has_paid=1") or []
        else:
            rows = db.query("SELECT user_id FROM users") or []
        sent = 0
        failed = 0
        for r in rows:
            if image_url:
                ok = telegram.send_photo(r["user_id"], image_url, caption=text, reply_markup=reply_markup)
            else:
                ok = telegram.send_message(r["user_id"], text, reply_markup=reply_markup)
            if ok:
                sent += 1
            else:
                failed += 1
            time.sleep(0.05)
        flash(f"Рассылка завершена: доставлено {sent}, ошибок {failed}.", "info")
        _log("Рассылка", f"Рассылка ({'подписчикам' if audience == 'paid' else 'всем'}): доставлено {sent}, ошибок {failed}")
        return redirect(url_for("broadcast"))
    total = db.scalar("SELECT COUNT(*) FROM users") or 0
    paid = db.scalar("SELECT COUNT(*) FROM users WHERE has_paid=1") or 0
    return render_template("broadcast.html", total=total, paid=paid)


# ──────────────────────── НАСТРОЙКИ ───────────────────
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    aid = session["admin_id"]
    db.execute(
        """INSERT INTO admin_settings (admin_id) VALUES (%s)
           ON CONFLICT (admin_id) DO NOTHING""",
        (aid,),
    )
    if request.method == "POST":
        sale = request.form.get("sale_notify") == "on"
        dnd = request.form.get("dnd") == "on"
        shown = set(request.form.getlist("section"))
        hidden = [ep for ep, _, _ in NAV_SECTIONS if ep not in shown]
        valid_groups = {g for g, _ in NOTIFY_ACTIONS}
        notify_always = [g for g in request.form.getlist("notify_group") if g in valid_groups]
        db.execute(
            "UPDATE admin_settings SET sale_notify=%s, dnd=%s, hidden_sections=%s, notify_always=%s WHERE admin_id=%s",
            (sale, dnd, ",".join(hidden), ",".join(notify_always), aid),
        )
        flash("Настройки сохранены.", "info")
        _log("Настройки",
             f"Продажи: {'вкл' if sale else 'выкл'}; Не беспокоить: {'вкл' if dnd else 'выкл'}; скрыто разделов: {len(hidden)}; всегда уведомлять: {len(notify_always)}")
        return redirect(url_for("settings"))
    row = db.query("SELECT * FROM admin_settings WHERE admin_id=%s", (aid,), one=True)
    return render_template("settings.html", cfg=row or {}, support_username=config.SUPPORT_USERNAME)


# ──────────────────────── ЛОГИ ДЕЙСТВИЙ АДМИНОВ ────────────────
@app.route("/logs")
@login_required
def logs():
    rows = db.query("SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT 500") or []
    return render_template("logs.html", logs=rows)


# ─────────────────────── СОСТОЯНИЕ СЕРВЕРОВ ───────────
@app.route("/servers")
@login_required
def servers():
    nodes_raw = rw.get_nodes()
    nodes = []
    for n in nodes_raw:
        online = (bool(n.get("isConnected"))
                  and not n.get("isDisabled")
                  and n.get("isNodeOnline", True))
        users_online = n.get("usersOnline")
        if users_online is None:
            users_online = n.get("onlineUsers")
        nodes.append({
            "name": n.get("name") or n.get("uuid") or "—",
            "address": n.get("address") or "",
            "online": bool(online),
            "disabled": bool(n.get("isDisabled")),
            "connected": bool(n.get("isConnected")),
            "xray": n.get("xrayVersion") or "",
            "users_online": users_online if users_online is not None else "—",
        })
    return render_template("servers.html", nodes=nodes, api_ok=(len(nodes_raw) > 0))


# ───────────────────��── ЛИЧНЫЙ КАБИНЕТ ПОЛЬЗОВАТЕЛЯ ─────────
def _site_base() -> str:
    return config.SITE_URL or request.url_root.rstrip("/")


def _activity(user_id, kind, text):
    """Записать событие в ленту активности (звоночек)."""
    try:
        db.execute(
            "INSERT INTO user_activity (user_id, kind, text, created_at, seen) VALUES (%s,%s,%s,%s,FALSE)",
            (user_id, kind, text, int(time.time())),
        )
    except Exception:
        log.exception("activity log failed")


def _activity_unseen():
    try:
        return db.scalar("SELECT COUNT(*) FROM user_activity WHERE seen=FALSE") or 0
    except Exception:
        return 0


def _user_activity(user_id, limit=30):
    """Лента активности конкретного пользователя (для звоночк�� в личном кабинете)."""
    try:
        return db.query(
            "SELECT id, kind, text, created_at, seen FROM user_activity "
            "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        ) or []
    except Exception:
        return []


def _user_activity_unseen(user_id):
    try:
        return db.scalar(
            "SELECT COUNT(*) FROM user_activity WHERE user_id=%s AND seen=FALSE", (user_id,)
        ) or 0
    except Exception:
        return 0


def _set_cab_session(user_id):
    session.permanent = True
    session["cab"] = {"uid": int(user_id), "exp": int(time.time()) + config.CABINET_SESSION_HOURS * 3600}


def _cab_user():
    """Пользователь активной сессии кабинета (вход по 9-значному коду из бота)."""
    sess = session.get("cab")
    if not isinstance(sess, dict):
        return None
    uid = sess.get("uid")
    if not uid or int(time.time()) > int(sess.get("exp", 0) or 0):
        return None
    return db.query("SELECT * FROM users WHERE user_id=%s", (uid,), one=True)


def _calc_plan_price(plan_key: str, months: int) -> int:
    plan = config.PLANS.get(plan_key)
    if not plan:
        return 0
    return plan["price_month"] * max(1, months)


def _calc_upgrade_price(extra_devices: int) -> int:
    vpn = config.PLANS["vpn"]
    bypass = config.PLANS["vpn_bypass"]
    plan_diff = bypass["price_month"] - vpn["price_month"]
    device_diff = max(0, bypass["device_price"] - vpn["device_price"]) * max(extra_devices or 0, 0)
    return plan_diff + device_diff


def _cab_nodes():
    nodes = []
    for n in rw.get_nodes():
        online = (bool(n.get("isConnected")) and not n.get("isDisabled")
                  and n.get("isNodeOnline", True))
        nodes.append({"name": n.get("name") or n.get("uuid") or "—", "online": bool(online)})
    return nodes


def _apply_paid_purchase(payment_id: str, md: dict) -> bool:
    """Применяет успешный платёж идемпотентно. True — если применён именно сейчас."""
    try:
        inserted = db.query(
            "INSERT INTO processed_payments (payment_id, processed_at) VALUES (%s,%s) "
            "ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
            (payment_id, int(time.time())), one=True,
        )
    except Exception as e:
        log.error("processed_payments insert: %s", e)
        return False
    if inserted is None:
        return False  # уже обработан (ботом или повторно)

    u_id = int(md.get("user_id", 0) or 0)
    kind = md.get("kind", "plan")
    days = int(md.get("days", 0) or 0)
    squad = md.get("squad") or None
    whitelist_gb = int(md.get("whitelist_gb", 0) or 0)
    price = float(md.get("price", 0) or 0)
    item_name = md.get("item_name", "Покупка")
    qty = int(md.get("qty", 0) or 0)
    now = int(time.time())

    urow = db.query("SELECT referrer_id FROM users WHERE user_id=%s", (u_id,), one=True) or {}
    referrer_id = urow.get("referrer_id")

    plan_key = md.get("plan_key") or None

    if kind == "plan":
        rw.extend_user(u_id, days, None, squad)
        if whitelist_gb > 0:
            db.execute(
                "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                "VALUES (%s,%s,%s,FALSE) "
                "ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE",
                (u_id, whitelist_gb, now, whitelist_gb, now),
            )
        if plan_key:
            db.execute("UPDATE users SET has_paid=1, plan=%s WHERE user_id=%s", (plan_key, u_id))
        else:
            db.execute("UPDATE users SET has_paid=1 WHERE user_id=%s", (u_id,))
    elif kind == "trial":
        t = config.TRIAL
        rw.extend_user(u_id, days, t.get("hwid"), squad)
        if whitelist_gb > 0:
            db.execute(
                "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
                "VALUES (%s,%s,%s,FALSE) "
                "ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE",
                (u_id, whitelist_gb, now, whitelist_gb, now),
            )
        db.execute("UPDATE users SET has_paid=1, trial_used=TRUE, plan='trial' WHERE user_id=%s", (u_id,))
    elif kind == "device":
        remna = rw.get_user(u_id)
        if remna:
            add = qty if qty > 0 else 1
            new_hwid = (remna.get("hwidDeviceLimit", 1) or 1) + add
            rw.update_user(remna["uuid"], {"hwidDeviceLimit": new_hwid})
            db.execute("UPDATE users SET extra_devices = COALESCE(extra_devices,0) + %s WHERE user_id=%s", (add, u_id))
    elif kind == "upgrade":
        remna = rw.get_user(u_id)
        if remna:
            rw.update_user(remna["uuid"], {"activeInternalSquads": rw.expand_squads(config.SQUAD_UUID_WHITELIST)})
        db.execute("UPDATE users SET plan='vpn_bypass' WHERE user_id=%s", (u_id,))
        wl_gb = config.PLANS["vpn_bypass"]["whitelist_gb"]
        db.execute(
            "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) "
            "VALUES (%s,%s,%s,FALSE) "
            "ON CONFLICT (user_id) DO UPDATE SET gb_limit=%s, period_start=%s, cut_off=FALSE",
            (u_id, wl_gb, now, wl_gb, now),
        )
    elif kind == "wl_topup":
        add_gb = whitelist_gb if whitelist_gb > 0 else 1
        existing = db.query("SELECT gb_limit FROM whitelist_limits WHERE user_id=%s", (u_id,), one=True)
        if existing:
            db.execute("UPDATE whitelist_limits SET gb_limit = gb_limit + %s, cut_off=FALSE WHERE user_id=%s", (add_gb, u_id))
        else:
            db.execute(
                "INSERT INTO whitelist_limits (user_id, gb_limit, period_start, cut_off) VALUES (%s,%s,%s,FALSE)",
                (u_id, add_gb, now),
            )
        remna = rw.get_user(u_id)
        if remna:
            squads = rw._squad_uuids(remna.get("activeInternalSquads"))
            if config.SQUAD_UUID_WHITELIST and config.SQUAD_UUID_WHITELIST not in squads:
                squads.append(config.SQUAD_UUID_WHITELIST)
                rw.update_user(remna["uuid"], {"activeInternalSquads": squads})

    try:
        db.execute(
            "INSERT INTO payments (user_id, amount, tariff_key, days, is_trial, source, created_at) "
            "VALUES (%s,%s,%s,%s,FALSE,'purchase',%s)",
            (u_id, price, kind, days, now),
        )
    except Exception as e:
        log.error("payments insert: %s", e)

    if referrer_id and price > 0:
        earned = round(price * config.REFERRAL_PERCENT / 100, 2)
        db.execute("UPDATE users SET referral_balance = COALESCE(referral_balance,0) + %s WHERE user_id=%s", (earned, referrer_id))

    try:
        _activity(u_id, "Покупка", f"{item_name} — {price:.0f} ₽")
    except Exception:
        pass
    try:
        telegram.send_message(u_id, f"Оплата прошла успешно: {item_name}.\nСпасибо за покупку!")
    except Exception:
        pass
    for aid in config.ADMIN_IDS:
        try:
            telegram.send_message(aid, f"Новая покупка через сайт\n\nПользователь ID: {u_id}\nПозиция: {item_name}\nСумма: {price:.0f} руб.")
        except Exception:
            pass
    return True


def _check_pending(user_id: int) -> bool:
    """Проверяет незавершённые платежи пользователя и применяет успешные."""
    applied = False
    # Убираем «зависшие» платежи старше 30 мин, чтобы не опра��ивать их бесконечно
    # и не подвешивать кабинет после возврата с оплаты.
    try:
        db.execute("DELETE FROM cabinet_pending WHERE created_at < %s", (int(time.time()) - 1800,))
    except Exception:
        pass
    rows = db.query(
        "SELECT payment_id FROM cabinet_pending WHERE user_id=%s AND created_at > %s",
        (user_id, int(time.time()) - 1800),
    ) or []
    for r in rows:
        pid = r["payment_id"]
        data = pay.get_payment(pid)
        if not data:
            continue
        status = data.get("status")
        if status == "succeeded":
            if _apply_paid_purchase(pid, data.get("metadata") or {}):
                applied = True
            db.execute("DELETE FROM cabinet_pending WHERE payment_id=%s", (pid,))
        elif status in ("canceled",):
            db.execute("DELETE FROM cabinet_pending WHERE payment_id=%s", (pid,))
    return applied


@app.route("/cabinet/check-pending")
def cabinet_check_pending():
    """Фоновая проверка незавершённых платежей (вызывается из JS кабинета).
    Не блокирует загрузку страницы: get_payment падает быстро (1 попытка)."""
    u = _cab_user()
    if not u:
        return jsonify({"ok": False, "auth": False, "applied": False}), 401
    applied = False
    try:
        applied = _check_pending(u["user_id"])
    except Exception as e:
        log.error("cabinet_check_pending: %s", e)
    if applied:
        flash("Оплата прошла успешно — покупка активирована.", "info")
    return jsonify({"ok": True, "applied": applied})


@app.route("/cabinet")
def cabinet():
    u = _cab_user()
    if not u:
        return redirect(url_for("cabinet_login"))
    user_id = u["user_id"]
    # ВАЖНО: НЕ проверяем платёж синхронно здесь — иначе страница
    # виснет, пока YooKassa не ответит. Проверка идёт фоном через
    # /cabinet/check-pending (JS ниже на странице).
    snap = rw.user_snapshot(user_id)
    wl = db.query("SELECT * FROM whitelist_limits WHERE user_id=%s", (user_id,), one=True)
    devices = rw.get_user_hwid(snap["uuid"]) if snap.get("exists") else []
    plan_key = u.get("plan")
    plan = config.PLANS.get(plan_key)
    nodes = _cab_nodes()
    device_price = plan["device_price"] if plan else None
    upgrade_price = _calc_upgrade_price(u.get("extra_devices") or 0) if plan_key == "vpn" else None
    can_wl_topup = (plan_key == "vpn_bypass") or bool(snap.get("has_whitelist"))
    months = config.MONTH_CHOICES
    extend_prices = {m: _calc_plan_price(plan_key, m) for m in months} if plan else {}
    buy_prices = {pk: {m: p["price_month"] * m for m in months} for pk, p in config.PLANS.items()}
    ref_username = telegram.get_bot_username() or config.BOT_USERNAME
    ref_link = f"https://t.me/{ref_username}?start={user_id}" if ref_username else ""
    ref_count = db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=%s", (user_id,)) or 0
    ref_balance = float(u.get("referral_balance") or 0)
    noti_items = _user_activity(user_id, 30)
    noti_unseen = _user_activity_unseen(user_id)
    return render_template(
        "cabinet.html", u=u, snap=snap, wl=wl, devices=devices,
        plan=plan, plan_key=plan_key, nodes=nodes,
        device_price=device_price, upgrade_price=upgrade_price,
        can_wl_topup=can_wl_topup, wl_price=config.WHITELIST_PRICE_PER_GB,
        months=months, extend_prices=extend_prices,
        plans=config.PLANS, buy_prices=buy_prices, trial=config.TRIAL,
        support_url=config.SUPPORT_URL, support_username=config.SUPPORT_USERNAME,
        ref_link=ref_link, ref_count=ref_count, ref_balance=ref_balance,
        ref_percent=config.REFERRAL_PERCENT, ref_min=config.REFERRAL_MIN_WITHDRAW,
        noti_items=noti_items, noti_unseen=noti_unseen,
    )


@app.route("/cab", methods=["GET", "POST"])
def cabinet_login():
    # Вход в личный кабинет только по 9-значному коду из бота.
    if _cab_user():
        return redirect(url_for("cabinet"))
    if request.method == "POST":
        entered = (request.form.get("code") or "").strip().upper().replace(" ", "").replace("-", "")
        fails = int(session.get("cab_fails", 0) or 0)
        if fails >= 10:
            flash("Слишком много попыток. Подождите и запросите новый код в боте.", "error")
            return render_template("cab_login.html", support_url=config.SUPPORT_URL)
        row = None
        if entered:
            row = db.query(
                "SELECT * FROM cabinet_login_codes WHERE REPLACE(UPPER(code),'-','')=%s",
                (entered,), one=True,
            )
        err = None
        if not row:
            err = "Неверный код. Запросите новый в боте кнопкой «Личный кабинет»."
        elif int(time.time()) > row["expires_at"]:
            db.execute("DELETE FROM cabinet_login_codes WHERE user_id=%s", (row["user_id"],))
            err = "Код истёк (действует 5 минут). Запросите новый в боте."
        if err:
            session["cab_fails"] = fails + 1
            flash(err, "error")
            return render_template("cab_login.html", support_url=config.SUPPORT_URL)
        user_id = row["user_id"]
        db.execute("DELETE FROM cabinet_login_codes WHERE user_id=%s", (user_id,))
        session.pop("cab_fails", None)
        _set_cab_session(user_id)
        return redirect(url_for("cabinet"))
    return render_template("cab_login.html", support_url=config.SUPPORT_URL)


@app.route("/cab/as/<int:user_id>")
@login_required
def cabinet_admin_open(user_id):
    """Админ открывает кабинет пользователя (просмотр/поддержка)."""
    _set_cab_session(user_id)
    return redirect(url_for("cabinet"))


@app.route("/cab/<legacy>")
def cabinet_legacy(legacy):
    # Старые ссылки вида /cab/<UID> больше не используются — ведём на вход по коду.
    return redirect(url_for("cabinet_login"))


@app.route("/cabinet/logout")
def cabinet_logout():
    session.pop("cab", None)
    flash("Вы вышли из кабинета.", "info")
    return redirect(url_for("cabinet_login"))


@app.route("/cabinet/notifications")
def cabinet_notifications():
    """JSON-лента уведомлений пользователя — для живого обновления без F5."""
    u = _cab_user()
    if not u:
        return jsonify({"ok": False, "auth": False}), 403
    user_id = u["user_id"]
    items = [
        {"kind": r["kind"], "text": r["text"], "ts": int(r["created_at"] or 0), "seen": bool(r["seen"])}
        for r in _user_activity(user_id, 30)
    ]
    return jsonify({"ok": True, "unseen": _user_activity_unseen(user_id), "items": items})


@app.route("/cabinet/notifications/seen", methods=["POST"])
def cabinet_notifications_seen():
    u = _cab_user()
    if not u:
        return jsonify({"ok": False}), 403
    try:
        db.execute("UPDATE user_activity SET seen=TRUE WHERE user_id=%s AND seen=FALSE", (u["user_id"],))
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/cabinet/security")
def cabinet_security():
    u = _cab_user()
    if not u:
        return redirect(url_for("cabinet_login"))
    ident = ("@" + u["username"]) if u.get("username") else ("ID " + str(u["user_id"]))
    copy_text = (
        "Привет. Моим аккаунтом Truba VPN пользуются 3-и лица. "
        "Мой Telegram: " + ident + ". Помогите мне."
    )
    return render_template(
        "cab_security.html", u=u, copy_text=copy_text,
        support_url=config.SUPPORT_URL, support_username=config.SUPPORT_USERNAME,
    )


@app.route("/cabinet/pay", methods=["POST"])
def cabinet_pay():
    u = _cab_user()
    if not u:
        return redirect(url_for("cabinet_login"))
    # Диагностика: фиксируем попытку оплаты сразу, чтобы было видно в логах.
    log.info("cabinet_pay HIT: user=%s action=%r form=%s",
             u["user_id"], request.form.get("action"), dict(request.form))
    if not pay.configured():
        log.error("cabinet_pay: YooKassa не настроена (SHOP_ID/YOOKASSA_KEY)")
        flash("Оплата временно недоступна. Обратитесь в поддержку.", "error")
        return redirect(url_for("cabinet"))
    user_id = u["user_id"]
    action = request.form.get("action")
    plan_key = u.get("plan")
    plan = config.PLANS.get(plan_key)
    snap = rw.user_snapshot(user_id)

    kind = None; price = 0; days = 0; qty = 0; whitelist_gb = 0
    squad = None; item_name = ""

    if action == "extend":
        if not plan:
            flash("Сначала оформите подписку.", "error")
            return redirect(url_for("cabinet"))
        try:
            months = int(request.form.get("months", 1) or 1)
        except ValueError:
            months = 1
        if months not in config.MONTH_CHOICES:
            months = 1
        kind = "plan"; days = months * 30; squad = plan["squad"]
        whitelist_gb = plan["whitelist_gb"]; price = _calc_plan_price(plan_key, months)
        item_name = f"Продление {plan['name']} · {months} мес."
    elif action == "buy":
        sel = request.form.get("plan_key", "")
        sel_plan = config.PLANS.get(sel)
        if not sel_plan:
            flash("Неизвестный тариф.", "error")
            return redirect(url_for("cabinet"))
        try:
            months = int(request.form.get("months", 1) or 1)
        except ValueError:
            months = 1
        if months not in config.MONTH_CHOICES:
            months = 1
        plan_key = sel
        kind = "plan"; days = months * 30; squad = sel_plan["squad"]
        whitelist_gb = sel_plan["whitelist_gb"]; price = _calc_plan_price(sel, months)
        item_name = f"Подписка {sel_plan['name']} · {months} мес."
    elif action == "trial":
        if u.get("trial_used"):
            flash("Пробная подписка уже использована.", "error")
            return redirect(url_for("cabinet"))
        t = config.TRIAL
        plan_key = "trial"
        kind = "trial"; days = t["days"]; squad = t["squad"]
        whitelist_gb = t["whitelist_gb"]; price = t["price"]
        item_name = t.get("name", "Пробная подписка")
    elif action == "device":
        if not plan:
            flash("Сначала оф��рмите подписку.", "error")
            return redirect(url_for("cabinet"))
        try:
            qty = int(request.form.get("qty", 1) or 1)
        except ValueError:
            qty = 1
        if qty <= 0:
            qty = 1
        kind = "device"; price = plan["device_price"] * qty
        item_name = f"+{qty} устр. ({plan['name']})"
    elif action == "wl_topup":
        if not (plan_key == "vpn_bypass" or snap.get("has_whitelist")):
            flash("Докупка ГБ доступна при активном обходе белых списков.", "error")
            return redirect(url_for("cabinet"))
        try:
            gb = int(request.form.get("gb", 1) or 1)
        except ValueError:
            gb = 1
        if gb <= 0:
            gb = 1
        kind = "wl_topup"; whitelist_gb = gb; price = config.WHITELIST_PRICE_PER_GB * gb
        item_name = f"Докупка трафика +{gb} ГБ"
    elif action == "upgrade":
        if plan_key != "vpn":
            flash("Обход белых списков можно добавить только к тарифу VPN.", "error")
            return redirect(url_for("cabinet"))
        kind = "upgrade"; price = _calc_upgrade_price(u.get("extra_devices") or 0)
        item_name = "Добавление обхода белых списков"
    else:
        flash("Неизвестное действие.", "error")
        return redirect(url_for("cabinet"))

    if price <= 0:
        log.error("cabinet_pay: некорректная сумма action=%s price=%s user=%s", action, price, user_id)
        flash("Некорректная сумма оплаты.", "error")
        return redirect(url_for("cabinet"))

    return_url = f"{_site_base()}/cabinet"
    metadata = {
        "user_id": str(user_id), "kind": kind, "days": str(days),
        "hwid": "", "squad": squad or "", "whitelist_gb": str(whitelist_gb),
        "plan_key": plan_key or "", "price": str(int(price)), "is_trial": "0",
        "item_name": item_name, "qty": str(qty), "src": "site",
    }
    try:
        pmt = pay.create_payment(int(price), f"TrubaVPN — {item_name}", metadata, return_url)
    except Exception as e:
        log.exception("cabinet_pay: исключение при create_payment action=%s: %s", action, e)
        pmt = None
    if not pmt or not pmt.get("confirmation_url"):
        log.error("cabinet_pay: платёж НЕ создан action=%s price=%s user=%s pmt=%r",
                  action, price, user_id, pmt)
        flash("Не удалось создать платёж. Попробуйте позже.", "error")
        return redirect(url_for("cabinet"))
    log.info("cabinet_pay: редирект на YooKassa action=%s user=%s pmt_id=%s",
             action, user_id, pmt.get("id"))
    try:
        db.execute(
            "INSERT INTO cabinet_pending (payment_id, user_id, created_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (payment_id) DO NOTHING",
            (pmt["id"], user_id, int(time.time())),
        )
    except Exception as e:
        log.error("cabinet_pending insert: %s", e)
    return redirect(pmt["confirmation_url"])


@app.route("/cabinet/withdraw", methods=["POST"])
def cabinet_withdraw():
    u = _cab_user()
    if not u:
        return redirect(url_for("cabinet_login"))
    user_id = u["user_id"]
    balance = float(u.get("referral_balance") or 0)
    if balance < config.REFERRAL_MIN_WITHDRAW:
        flash(f"Вывод доступен от {config.REFERRAL_MIN_WITHDRAW} ₽. Ваш баланс: {balance:.2f} ₽.", "error")
        return redirect(url_for("cabinet"))
    uname = ("@" + u["username"]) if u.get("username") else f"ID {user_id}"
    text = (
        "💸 <b>Запрос на вывод реферального баланса</b>\n\n"
        f"Пользователь: {uname}\n"
        f"ID: <code>{user_id}</code>\n"
        f"Сумма к выводу: <b>{balance:.2f} ₽</b>\n\n"
        "Заявка отправлена из личного кабинета на сайте."
    )
    sent = 0
    for admin_id in config.ADMIN_IDS:
        try:
            if telegram.send_message(admin_id, text):
                sent += 1
        except Exception as e:
            log.error("withdraw notify %s: %s", admin_id, e)
    if sent:
        flash("Заявка на вывод отправлена в поддержку. С вами свяжутся в Telegram.", "info")
    else:
        flash("Не удалось отправить заявку. Напишите в поддержку вручную.", "error")
    return redirect(url_for("cabinet"))


@app.errorhandler(404)
def _404(e):
    return render_template("error.html", code=404, msg="Страница не найдена"), 404


@app.errorhandler(500)
def _500(e):
    return render_template("error.html", code=500, msg="Внутренняя ошибка"), 500


with app.app_context():
    try:
        db.init_pool()
    except Exception as e:
        log.error("DB init failed: %s", e)
    # Диагностика оплаты при старте — сразу видно в консоли, читаются ли ключи.
    try:
        if pay.configured():
            log.info("Оплата YooKassa: НАСТРОЕНА (SHOP_ID=%s)", config.SHOP_ID)
        else:
            log.warning("Оплата YooKassa: НЕ НАСТРОЕНА — проверьте SHOP_ID/YOOKASSA_KEY в .env этой папки")
    except Exception as e:
        log.error("payments check failed: %s", e)


if __name__ == "__main__":
    # Админский сайт всегда на порту 5000.
    _port = 5000
    app.run(host="0.0.0.0", port=_port, debug=True, threaded=True)
