# -*- coding: utf-8 -*-
"""Внедряет Platega (platega.io) как агрегатор приёма оплаты.

Запуск: положить рядом с megamozg.py и block_platega.txt, затем
    python3 patch_platega.py
Скрипт правит megamozg.py на месте; перед запуском сделайте копию.
Повторный запуск безопасен: если Platega уже внедрена, скрипт скажет об
этом и ничего не тронет.
"""

import os
import sys

PATH = "megamozg.py"
BLOCK = "block_platega.txt"

if not os.path.exists(PATH):
    sys.exit(f"Не найден {PATH} — положите скрипт рядом с файлом бота.")
if not os.path.exists(BLOCK):
    sys.exit(f"Не найден {BLOCK} — он должен лежать рядом со скриптом.")

with open(PATH, encoding="utf-8", newline="") as f:
    src = f.read()

if "PLATEGA_MERCHANT_ID" in src:
    sys.exit("Platega уже внедрена — правки не нужны.")

# Файл бота хранится с CRLF; определяем перевод строки по факту, чтобы
# не смешать окончания строк в одном файле.
NL = "\r\n" if "\r\n" in src else "\n"


def L(*lines):
    return "".join(l + NL for l in lines)


def sub(old, new, label):
    global src
    n = src.count(old)
    assert n == 1, f"{label}: якорь найден {n} раз(а)"
    src = src.replace(old, new)
    print("OK:", label)


# ── 1. Конфигурация ──────────────────────────────────────────────────────
sub(L("def payments_available() -> bool:",
      '    """Есть ли хоть один рабочий способ оплаты — карта или звёзды.',
      "    Пока доступен любой из них, экраны покупки открываются как обычно, а",
      '    заглушка «оплата временно недоступна» не показывается."""',
      "    return PAYMENTS_ENABLED or STARS_ENABLED"),
    L("# ─────────────────────────────────────────────",
      "#  ОПЛАТА КАРТОЙ И СБП ЧЕРЕЗ PLATEGA (platega.io)",
      "#",
      "#  Включается сама, как только заданы PLATEGA_MERCHANT_ID и",
      "#  PLATEGA_SECRET (выдаются менеджером и лежат в ЛК → «Настройки»).",
      "#  Пока их нет — ведёт себя так, будто агрегатора не существует, и на",
      "#  экране оплаты остаются только звёзды.",
      "#",
      "#  PLATEGA_METHOD — код способа оплаты: 2 — СБП (QR), 11 — карты,",
      "#  12 — международные карты, 13 — криптовалюта, 14 — SberPay, 3 — ЕРИП.",
      "#  Если оставить пустым, метод не передаётся и человек выбирает его сам",
      "#  на платёжной форме. Ставить конкретный код стоит только если у",
      "#  магазина подключён именно он: на неподключённый метод Platega",
      "#  отвечает ошибкой, и кнопка оплаты просто не появится (причина будет",
      "#  видна в логе).",
      "# ─────────────────────────────────────────────",
      'PLATEGA_API_URL     = os.environ.get("PLATEGA_API_URL", "https://app.platega.io").rstrip("/")',
      'PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "").strip()',
      'PLATEGA_SECRET      = os.environ.get("PLATEGA_SECRET", "").strip()',
      "",
      "# Куда платёжная форма вернёт человека после оплаты — обратно в бот.",
      'PLATEGA_RETURN_URL  = os.environ.get("PLATEGA_RETURN_URL", "").strip() \\',
      '    or "https://t.me/trubavpnbot"',
      "",
      '_platega_method = os.environ.get("PLATEGA_METHOD", "").strip()',
      "PLATEGA_METHOD  = int(_platega_method) if _platega_method.isdigit() \\",
      "    and int(_platega_method) > 0 else None",
      "",
      "# Как часто фоновый наблюдатель спрашивает статус неоплаченных счетов и",
      "# сколько секунд после создания счёт вообще стоит проверять.",
      'PLATEGA_POLL_INTERVAL = int(os.environ.get("PLATEGA_POLL_INTERVAL", "10"))',
      'PLATEGA_POLL_WINDOW   = int(os.environ.get("PLATEGA_POLL_WINDOW", "3600"))',
      "",
      'PLATEGA_ENABLED = os.environ.get("PLATEGA_ENABLED", "1").strip().lower() \\',
      '    in ("1", "true", "yes", "on")',
      "",
      "",
      "def platega_available() -> bool:",
      '    """Готова ли Platega принимать оплату: включена и ключи заданы."""',
      "    return PLATEGA_ENABLED and bool(PLATEGA_MERCHANT_ID and PLATEGA_SECRET)",
      "",
      "",
      "def payments_available() -> bool:",
      '    """Есть ли хоть один рабочий способ оплаты — карта, СБП или звёзды.',
      "    Пока доступен любой из них, экраны покупки открываются как обычно, а",
      '    заглушка «оплата временно недоступна» не показывается."""',
      "    return PAYMENTS_ENABLED or STARS_ENABLED or platega_available()"),
    "1. конфигурация Platega")

# ── 2. Таблица счетов ────────────────────────────────────────────────────
sub(L("        # Отправленные напоминания об окончании подписки (по срокам 3д/1д/1ч)."),
    L("        # Счета Platega. Устроены так же, как счета на звёзды: наружу в",
      "        # платёж уходит только сумма, а состав покупки лежит здесь и",
      "        # достаётся по transactionId уже после подтверждения оплаты.",
      '        await conn.execute("""',
      "            CREATE TABLE IF NOT EXISTS platega_invoices (",
      "                tx_id        TEXT PRIMARY KEY,",
      "                user_id      BIGINT NOT NULL,",
      "                kind         TEXT NOT NULL,",
      "                days         INTEGER DEFAULT 0,",
      "                hwid         INTEGER,",
      "                squad        TEXT,",
      "                whitelist_gb INTEGER DEFAULT 0,",
      "                plan_key     TEXT,",
      "                price        INTEGER DEFAULT 0,",
      "                is_trial     BOOLEAN DEFAULT FALSE,",
      "                item_name    TEXT,",
      "                qty          INTEGER DEFAULT 0,",
      "                status       TEXT DEFAULT 'PENDING',",
      "                created_at   BIGINT DEFAULT 0,",
      "                paid_at      BIGINT",
      "            )",
      '        """)',
      "        # Отправленные напоминания об окончании подписки (по срокам 3д/1д/1ч)."),
    "2. таблица platega_invoices")

# ── 3. Кнопка оплаты на общем экране ─────────────────────────────────────
sub(L("    payment = None",
      "    if PAYMENTS_ENABLED and Payment is not None:"),
    L("    # Platega — основной способ оплаты картой и СБП.",
      '    platega_tx = ""',
      "    if platega_available():",
      "        platega_tx, platega_url = await _platega_invoice_create(",
      "            user_id, None, kind=kind, item_name=item_name, price=price, days=days,",
      "            hwid=hwid, squad=squad, whitelist_gb=whitelist_gb, plan_key=plan_key,",
      "            is_trial=is_trial, qty=qty,",
      "        )",
      "        if platega_url:",
      '            rows.append([btn("Оплатить", emoji_id=BTN_ICON_PAY, style="success",',
      "                             url=platega_url)])",
      '            price_parts.append(f"{price} руб.")',
      '            hints.append("Оплата зачислится автоматически, обычно за несколько секунд.")',
      "        else:",
      '            platega_tx = ""',
      "",
      "    payment = None",
      "    if PAYMENTS_ENABLED and Payment is not None:"),
    "3. кнопка оплаты Platega")

# ── 4. Не дублируем рублёвую цену, если включены оба агрегатора ──────────
sub(L("    if payment:",
      '        rows.append([btn("Оплатить картой", emoji_id=BTN_ICON_PAY, style="success",',
      "                         url=payment.confirmation.confirmation_url)])",
      '        price_parts.append(f"{price} руб.")',
      '        hints.append("После оплаты картой нажмите «Проверить оплату».")'),
    L("    if payment:",
      "        # ЮKassa (если её всё-таки включат рядом с Platega) идёт вторым",
      "        # способом: рублёвая цена в строке «К оплате» уже есть, повторять",
      "        # её не нужно.",
      '        rows.append([btn("Оплатить картой (ЮKassa)", emoji_id=BTN_ICON_PAY,',
      '                         style=None if price_parts else "success",',
      "                         url=payment.confirmation.confirmation_url)])",
      "        if not price_parts:",
      '            price_parts.append(f"{price} руб.")',
      '        hints.append("После оплаты через ЮKassa нажмите «Проверить оплату».")'),
    "4. цена не дублируется")

# ── 4b. Кнопки «Проверить оплату» — в конце, после всех способов ─────────
sub(L("    if payment:",
      '        rows.append([btn("Проверить оплату", emoji_id=BTN_ICON_CHECK_SUB,',
      '                         callback_data=f"paycheck_{payment.id}")])',
      '    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])'),
    L("    # Ручная проверка — на случай, если человек не хочет ждать пары секунд",
      "    # до автоматического зачисления.",
      "    if platega_tx:",
      '        rows.append([btn("Проверить оплату", emoji_id=BTN_ICON_CHECK_SUB,',
      '                         callback_data=f"plcheck_{platega_tx}")])',
      "    if payment:",
      '        rows.append([btn("Проверить оплату (ЮKassa)", emoji_id=BTN_ICON_CHECK_SUB,',
      '                         callback_data=f"paycheck_{payment.id}")])',
      '    rows.append([InlineKeyboardButton(text="Назад", callback_data="back")])'),
    "4b. кнопки «Проверить оплату»")

# ── 5. Стиль кнопки звёзд: «главной» она остаётся только без карты ───────
sub(L('            rows.append([btn(f"Оплатить звёздами — {stars}", emoji_id=BTN_ICON_STARS,',
      '                             style=None if payment else "success",',
      '                             callback_data=f"starspay_{token}")])'),
    L('            rows.append([btn(f"Оплатить звёздами — {stars}", emoji_id=BTN_ICON_STARS,',
      '                             style=None if rows else "success",',
      '                             callback_data=f"starspay_{token}")])'),
    "5. стиль кнопки звёзд")

sub(L("            if payment or stars == int(price):",
      '                price_parts.append(f"{stars} {EMOJI_STARS}")'),
    L("            if price_parts or stars == int(price):",
      '                price_parts.append(f"{stars} {EMOJI_STARS}")'),
    "5. приписка про курс звёзд")

# ── 6. Сам блок Platega ──────────────────────────────────────────────────
with open(BLOCK, encoding="utf-8") as f:
    block = f.read().replace("\r\n", "\n").replace("\n", NL)

sub(L("async def _create_payment_core(user_id: int, *, kind: str, item_name: str,"),
    block + NL + L("async def _create_payment_core(user_id: int, *, kind: str, item_name: str,"),
    "6. блок Platega")

# ── 7. Запуск наблюдателя и лог ──────────────────────────────────────────
sub(L("    asyncio.create_task(naloggo_queue_scheduler())"),
    L("    asyncio.create_task(naloggo_queue_scheduler())",
      "    if platega_available():",
      "        asyncio.create_task(platega_watcher())"),
    "7. фоновый наблюдатель")

sub(L('    log.info("Оплата картой (ЮKassa): %s",',
      '             "включена" if PAYMENTS_ENABLED else "ОТКЛЮЧЕНА")'),
    L('    log.info("Оплата через Platega: %s",',
      '             ("включена, метод " + (str(PLATEGA_METHOD) if PLATEGA_METHOD else "выбирает плательщик"))',
      '             if platega_available() else "ОТКЛЮЧЕНА (не заданы PLATEGA_MERCHANT_ID/PLATEGA_SECRET)")',
      '    log.info("Оплата картой (ЮKassa): %s",',
      '             "включена" if PAYMENTS_ENABLED else "ОТКЛЮЧЕНА")'),
    "7. лог в main()")

with open(PATH, "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("Готово: Platega внедрена в", PATH)
