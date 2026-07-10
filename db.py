"""
Слой доступа к PostgreSQL (та же база, что и у бота).
Использует пул соединений psycopg2 и возвращает строки как dict.
"""
import threading
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

_pool: ThreadedConnectionPool | None = None
_lock = threading.Lock()


def init_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    1, 10, dsn=config.DATABASE_URL
                )
                _ensure_web_tables()


def _get_conn():
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    # ВАЖНО: без autocommit пул держит открытую транзакцию на SELECT и видит
    # старый MVCC-снимок — новые платежи от бота не видны до перезапуска.
    # autocommit=True — каждый запрос видит актуальные данные сразу.
    if not conn.autocommit:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.autocommit = True
    return conn


def _put_conn(conn):
    if _pool is not None:
        _pool.putconn(conn)


def query(sql: str, params: tuple = (), *, one: bool = False, commit: bool = False):
    """Выполнить запрос. Возвращает list[dict] или dict (one=True)."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            result = None
            if cur.description:  # SELECT ... RETURNING
                if one:
                    row = cur.fetchone()
                    result = dict(row) if row else None
                else:
                    result = [dict(r) for r in cur.fetchall()]
            if commit:
                conn.commit()
            return result
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def execute(sql: str, params: tuple = ()):
    """INSERT/UPDATE/DELETE с коммитом."""
    return query(sql, params, commit=True)


def scalar(sql: str, params: tuple = ()):
    """Вернуть одно значение (первая колонка первой строки)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _put_conn(conn)


def _ensure_web_tables():
    """Собственная таблица веб-админки для кодов подтверждения.
    Работает между несколькими воркерами gunicorn."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_auth_codes (
                    tg_id      BIGINT PRIMARY KEY,
                    code       TEXT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    attempts   INTEGER DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_logs (
                    id         SERIAL PRIMARY KEY,
                    admin_id   BIGINT,
                    admin_name TEXT,
                    action     TEXT,
                    target_id  BIGINT,
                    details    TEXT,
                    created_at BIGINT DEFAULT 0
                )
                """
            )
            # Персональные коды личных кабинетов пользователей (16 симв.).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cabinet_tokens (
                    user_id    BIGINT PRIMARY KEY,
                    code       TEXT UNIQUE NOT NULL,
                    created_at BIGINT DEFAULT 0
                )
                """
            )
            # Незавершённые платежи из кабинета (для авто-подтверждения).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cabinet_pending (
                    payment_id TEXT PRIMARY KEY,
                    user_id    BIGINT,
                    created_at BIGINT DEFAULT 0
                )
                """
            )
            # Общая с ботом таблица уже обработанных платежей (идемпотентность).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_payments (
                    payment_id   TEXT PRIMARY KEY,
                    processed_at BIGINT
                )
                """
            )
            # Персональные настройки админов веб-панели.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    admin_id        BIGINT PRIMARY KEY,
                    sale_notify     BOOLEAN DEFAULT TRUE,
                    hidden_sections TEXT DEFAULT '',
                    dnd             BOOLEAN DEFAULT FALSE
                )
                """
            )
        conn.commit()
        # Доп. колонки в payments (могут отсутствовать в старой БД).
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'purchase'")
                cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS note TEXT")
                # Новые персональные настройки админов (для старой БД).
                cur.execute("ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS hidden_sections TEXT DEFAULT ''")
                cur.execute("ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS dnd BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE admin_settings ADD COLUMN IF NOT EXISTS notify_always TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        _put_conn(conn)
