# -*- coding: utf-8 -*-
"""
ХГУ Тест - Веб-приложение для подготовки к поступлению в Худжандский государственный университет
Python 3.12 + Flask
"""

import os
import json
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    send_file,
    Flask, render_template, request, redirect, url_for, flash,
    session, jsonify, send_from_directory, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# ==================== КОНФИГУРАЦИЯ ====================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hgu-test-secret-key-change-in-production-2026")
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif", "webp"}

# Railway / Render / любой reverse-proxy: корректные HTTPS, scheme, host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# За продакшеном (Railway) — cookie только по HTTPS
_IS_CLOUD = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("RENDER")
)
if _IS_CLOUD:
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PREFERRED_URL_SCHEME"] = "https"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)

# Реквизиты карты администратора (замените на реальные)
ADMIN_CARD = {
    "dc": "5058 XXXX XXXX XXXX",
    "eskhata": "5058 XXXX XXXX XXXX",
    "alif": "5058 XXXX XXXX XXXX",
    "holder": "Администратор ХГУ Тест",
    "phone": "+992 XX XXX XX XX"
}

# Ссылка на Instagram (замените)
INSTAGRAM_URL = "https://www.instagram.com/hgu_test_tj/"

# Пакеты Pro (без VPS — оплата вручную, админ одобряет)
PRO_PACKAGES = {
    "1m": {"days": 30, "price": 7, "hints": 3, "label": {"ru": "1 месяц — 3 подсказки", "en": "1 month — 3 hints", "tg": "1 моҳ — 3 ишора"}},
    "2m": {"days": 60, "price": 10, "hints": 5, "label": {"ru": "2 месяца — 5 подсказок", "en": "2 months — 5 hints", "tg": "2 моҳ — 5 ишора"}},
    "6m": {"days": 180, "price": 25, "hints": 10, "label": {"ru": "6 месяцев — 10 подсказок", "en": "6 months — 10 hints", "tg": "6 моҳ — 10 ишора"}},
}
POINTS_CORRECT = 2
POINTS_WRONG = 0


def get_payment_settings():
    """Реквизиты из БД (редактирует админ), иначе значения по умолчанию."""
    defaults = dict(ADMIN_CARD)
    defaults.update({
        "link_dc": "", "link_eskhata": "", "link_alif": "",
        "pay_mode": "manual", "auto_approve": "0",
    })
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings WHERE key LIKE 'pay_%'").fetchall()
            for r in rows:
                k = r["key"].replace("pay_", "", 1)
                defaults[k] = r["value"]
    except Exception:
        pass
    return defaults


def set_payment_settings(data: dict):
    with get_db() as conn:
        for k, v in data.items():
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (f"pay_{k}", str(v).strip(), datetime.now().isoformat())
            )



def _ensure_app_settings_table():
    """Создаёт app_settings отдельно (не роняет другие транзакции)."""
    try:
        with get_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
    except Exception as e:
        print("_ensure_app_settings_table:", e)


def get_setting(key, default=""):
    try:
        _ensure_app_settings_table()
        with get_db() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row:
                return row["value"]
    except Exception:
        pass
    return default


def set_setting(key, value):
    """Надёжная запись настройки (Postgres-safe)."""
    _ensure_app_settings_table()
    val = str(value)
    now = datetime.now().isoformat()
    # 1) UPDATE
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?",
                (val, now, key),
            )
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                return True
    except Exception as e:
        print("set_setting update:", e)
    # 2) INSERT
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, val, now),
            )
        return True
    except Exception as e:
        print("set_setting insert:", e)
    # 3) DELETE + INSERT
    try:
        with get_db() as conn:
            try:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            except Exception:
                pass
        with get_db() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, val, now),
            )
        return True
    except Exception as e:
        print("set_setting final fail:", e)
        return False


def count_attempts_today(user_id, test_id=None):
    """Сколько попыток экзамена сегодня (mode=exam)."""
    day = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        if test_id:
            row = conn.execute(
                """SELECT COUNT(*) FROM test_results
                   WHERE user_id = ? AND test_id = ?
                   AND (mode IS NULL OR mode = 'exam')
                   AND created_at LIKE ?""",
                (user_id, test_id, day + "%")
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM test_results
                   WHERE user_id = ?
                   AND (mode IS NULL OR mode = 'exam')
                   AND created_at LIKE ?""",
                (user_id, day + "%")
            ).fetchone()
        return int(row[0] if row else 0)


def max_exam_attempts_per_day():
    try:
        return max(0, int(get_setting("max_exam_attempts", "3")))
    except Exception:
        return 3


def has_completed_exam(user_id, test_id):
    """Студент уже сдал этот экзамен (есть результат mode=exam)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT id FROM test_results
                   WHERE user_id = ? AND test_id = ?
                   AND (mode IS NULL OR mode = 'exam')
                   LIMIT 1""",
                (user_id, test_id),
            ).fetchone()
            return bool(row)
    except Exception:
        return False


# Оценки: балл всегда из 100 (50 вопросов × 2 балла)
# A 90–100, B 70–89, C 60–69, D 50–59, F 45–49, Fx 0–44
GRADE_BANDS = [
    ("A", 90, 100, 95),
    ("B", 70, 89, 80),
    ("C", 60, 69, 65),
    ("D", 50, 59, 55),
    ("F", 45, 49, 47),
    ("Fx", 0, 44, 20),
]
GRADE_LETTERS = [g[0] for g in GRADE_BANDS]

# Часовые пояса стран СНГ (обязательно при регистрации)
CIS_TIMEZONES = [
    ("Asia/Dushanbe", "Таджикистан (Dushanbe, UTC+5)"),
    ("Asia/Tashkent", "Узбекистан (Tashkent, UTC+5)"),
    ("Asia/Bishkek", "Кыргызстан (Bishkek, UTC+6)"),
    ("Asia/Almaty", "Казахстан (Almaty, UTC+5/6)"),
    ("Asia/Aqtobe", "Казахстан (Aqtobe, UTC+5)"),
    ("Asia/Ashgabat", "Туркменистан (Ashgabat, UTC+5)"),
    ("Europe/Moscow", "Россия (Москва, UTC+3)"),
    ("Asia/Yekaterinburg", "Россия (Екатеринбург, UTC+5)"),
    ("Asia/Novosibirsk", "Россия (Новосибирск, UTC+7)"),
    ("Asia/Vladivostok", "Россия (Владивосток, UTC+10)"),
    ("Europe/Minsk", "Беларусь (Минск, UTC+3)"),
    ("Europe/Kyiv", "Украина (Киев, UTC+2/3)"),
    ("Asia/Yerevan", "Армения (Ереван, UTC+4)"),
    ("Asia/Baku", "Азербайджан (Баку, UTC+4)"),
    ("Asia/Tbilisi", "Грузия (Тбилиси, UTC+4)"),
    ("Europe/Chisinau", "Молдова (Кишинёв, UTC+2/3)"),
]


def letter_grade(score_or_percent):
    """Оценка по баллу/проценту 0–100. Возвращает A/B/C/D/F/Fx."""
    s = float(score_or_percent or 0)
    if s < 0:
        s = 0
    if s > 100:
        s = 100
    for letter, lo, hi, _rep in GRADE_BANDS:
        if lo <= s <= hi:
            return letter
    return "Fx"


def grade_to_score(letter):
    """Представительный балл (из 100) для буквенной оценки."""
    letter = (letter or "Fx").strip()
    for L, lo, hi, rep in GRADE_BANDS:
        if L == letter:
            return float(rep)
    return 20.0


def score_percent(score, max_score):
    try:
        ms = float(max_score or 0)
        if ms <= 0:
            return 0.0
        p = float(score or 0) / ms * 100.0
        return max(0.0, min(100.0, round(p, 1)))
    except Exception:
        return 0.0


def format_dt(value, tz_name="Asia/Dushanbe"):
    """Форматирование даты/времени в поясе устройства (по умолчанию Таджикистан)."""
    if value is None or value == "":
        return "—"
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        if isinstance(value, str):
            raw = value.strip().replace("Z", "+00:00")
            # Postgres иногда отдаёт "2026-08-07 20:04:12.123456+00"
            if " " in raw and "T" not in raw[:11]:
                raw = raw.replace(" ", "T", 1)
            try:
                dt = datetime.fromisoformat(raw)
            except Exception:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        dt = datetime.strptime(value[:26].split("+")[0].strip(), fmt)
                        break
                    except Exception:
                        dt = None
                if dt is None:
                    return str(value)[:16]
        else:
            dt = value
        target = ZoneInfo(tz_name or "Asia/Dushanbe")
        if getattr(dt, "tzinfo", None) is None:
            # Наивное время из БД (Postgres UTC) — считаем UTC
            dt = dt.replace(tzinfo=_tz.utc)
        dt = dt.astimezone(target)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        s = str(value)
        return s[:16] if len(s) >= 16 else s


def now_tj():
    """Текущее время Asia/Dushanbe (UTC+5)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Dushanbe"))
    except Exception:
        return datetime.utcnow() + timedelta(hours=5)


def _zone(tz_name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name or "Asia/Dushanbe")
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo("Asia/Dushanbe")
        except Exception:
            return None


def user_timezone_name(user=None):
    """Часовой пояс устройства/аккаунта студента."""
    try:
        if user is not None and getattr(user, "timezone", None):
            return user.timezone or "Asia/Dushanbe"
    except Exception:
        pass
    try:
        from flask_login import current_user as cu
        if cu and getattr(cu, "is_authenticated", False) and getattr(cu, "timezone", None):
            return cu.timezone or "Asia/Dushanbe"
    except Exception:
        pass
    try:
        from flask import session
        if session.get("tz"):
            return session.get("tz")
    except Exception:
        pass
    return "Asia/Dushanbe"


def now_for_user(user=None):
    """Сейчас по часовому поясу устройства пользователя (не только Dushanbe)."""
    tz_name = user_timezone_name(user)
    z = _zone(tz_name)
    if z is not None:
        return datetime.now(z)
    return now_tj()


def parse_exam_dt(value, tz_name=None):
    """
    Разбор exam_start/exam_end из формы datetime-local (YYYY-MM-DDTHH:MM).
    Наивное время считаем временем в tz_name (пояс устройства/аккаунта).
    """
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except Exception:
                dt = None
        if dt is None:
            return None
    z = _zone(tz_name or user_timezone_name())
    if getattr(dt, "tzinfo", None) is None and z is not None:
        dt = dt.replace(tzinfo=z)
    elif getattr(dt, "tzinfo", None) is not None and z is not None:
        try:
            dt = dt.astimezone(z)
        except Exception:
            pass
    return dt


def exam_window_status(test, user=None):
    """
    Статус окна экзамена относительно времени устройства пользователя.
    Возвращает: open | upcoming | closed | always
    """
    es = (test.get("exam_start") or "").strip()
    ee = (test.get("exam_end") or "").strip()
    if not es and not ee:
        return "always", None, None
    tz_name = user_timezone_name(user)
    now = now_for_user(user)
    start_dt = parse_exam_dt(es, tz_name) if es else None
    end_dt = parse_exam_dt(ee, tz_name) if ee else None
    if start_dt and now < start_dt:
        return "upcoming", start_dt, end_dt
    if end_dt and now > end_dt:
        return "closed", start_dt, end_dt
    return "open", start_dt, end_dt


EXAM_QUESTION_COUNT = 50  # вопросов в одном экзамене


# Web Push (VAPID). На Render задайте VAPID_PRIVATE_KEY и VAPID_PUBLIC_KEY
# или ключи сгенерируются в /tmp при первом запуске.
import base64 as _b64
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@hgu.tj")

def _ensure_vapid():
    global VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY
    if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
        return
    key_file = "/tmp/hgu_vapid_keys.json"
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            VAPID_PUBLIC_KEY = data.get("public", "")
            VAPID_PRIVATE_KEY = data.get("private", "")
            if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
                return
        except Exception:
            pass
    try:
        from py_vapid import Vapid01
        v = Vapid01()
        v.generate_keys()
        # private as PEM, public as urlsafe
        priv = v.private_pem().decode("utf-8") if hasattr(v.private_pem(), "decode") else str(v.private_pem())
        pub = v.public_key.urlsafe_private if False else None
    except Exception:
        pass
    # Fallback: use cryptography to make simple keys via pywebpush util if available
    try:
        from pywebpush import webpush  # noqa
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        private_key = ec.generate_private_key(ec.SECP256R1())
        priv_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        pub_numbers = private_key.public_key().public_numbers()
        x = pub_numbers.x.to_bytes(32, "big")
        y = pub_numbers.y.to_bytes(32, "big")
        uncompressed = b"\x04" + x + y
        VAPID_PRIVATE_KEY = priv_bytes.decode("utf-8")
        VAPID_PUBLIC_KEY = _b64.urlsafe_b64encode(uncompressed).decode("utf-8").rstrip("=")
        try:
            with open(key_file, "w", encoding="utf-8") as f:
                json.dump({"public": VAPID_PUBLIC_KEY, "private": VAPID_PRIVATE_KEY}, f)
        except Exception:
            pass
    except Exception as e:
        print("VAPID generate failed:", e)


def send_push_to_user(user_id, title, body, url="/"):
    """Отправить web-push пользователю (все его устройства)."""
    _ensure_vapid()
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        app.logger.warning("pywebpush not installed")
        return 0
    sent = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        dead = []
        for row in rows:
            sub = {
                "endpoint": row["endpoint"],
                "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            }
            payload = json.dumps({
                "title": title,
                "body": body,
                "url": url,
            }, ensure_ascii=False)
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                )
                sent += 1
            except Exception as ex:
                app.logger.info("push fail: %s", ex)
                dead.append(row["id"])
        for did in dead:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (did,))
    return sent



PRO_PRICE = 10
PRO_DURATION_DAYS = 60
FREE_PRO_DAYS = 2

# ==================== БАЗА ДАННЫХ ====================
# На Railway/Render бесплатный диск стирается при перезапуске.
# Чтобы аккаунты сохранялись — задайте DATABASE_URL (PostgreSQL).
# Railway: Add → Database → PostgreSQL (DATABASE_URL подставится сам).
# Альтернатива: https://supabase.com или https://neon.tech

import sqlite3
from contextlib import contextmanager

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if (
    os.environ.get("RENDER")
    or os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("DATABASE_DIR")
):
    _data_dir = os.environ.get("DATABASE_DIR") or os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_data_dir, exist_ok=True)
    DB_PATH = os.path.join(_data_dir, "hgu_test.db")
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hgu_test.db")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


class _Row:
    """row['id'] и row[0] для SQLite и PostgreSQL."""
    def __init__(self, data):
        self._d = data
        if data is None:
            self._keys = []
        elif isinstance(data, dict):
            self._keys = list(data.keys())
        else:
            self._keys = list(data.keys()) if hasattr(data, "keys") else []

    def __getitem__(self, key):
        if isinstance(key, int):
            if isinstance(self._d, dict):
                return self._d[self._keys[key]]
            return self._d[key]
        try:
            return self._d[key]
        except (KeyError, TypeError, IndexError):
            return None

    def keys(self):
        if isinstance(self._d, dict):
            return self._d.keys()
        return self._d.keys()


class _DBCursor:
    """Единый курсор для SQLite и PostgreSQL."""

    def __init__(self, cursor, kind, lastrowid=None):
        self._cursor = cursor
        self._kind = kind
        self.lastrowid = lastrowid

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._kind == "pg":
            if isinstance(row, dict):
                return _Row(dict(row))
            # pg8000 returns tuple — map by cursor.description
            desc = self._cursor.description or []
            keys = [d[0] for d in desc]
            return _Row(dict(zip(keys, row)))
        if isinstance(row, dict):
            return _Row(dict(row))
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        out = []
        desc = self._cursor.description or []
        keys = [d[0] for d in desc]
        for row in rows:
            if self._kind == "pg":
                if isinstance(row, dict):
                    out.append(_Row(dict(row)))
                else:
                    out.append(_Row(dict(zip(keys, row))))
            elif isinstance(row, dict):
                out.append(_Row(dict(row)))
            else:
                out.append(row)
        return out

    def __iter__(self):
        for row in self.fetchall():
            yield row


class _DBConn:
    def __init__(self, raw, kind):
        self._raw = raw
        self._kind = kind
        self.lastrowid = None

    def _adapt_sql(self, sql):
        if self._kind != "pg":
            return sql
        s = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        s = s.replace("AUTOINCREMENT", "")  # на всякий случай
        s = s.replace("?", "%s")
        if "INSERT OR REPLACE INTO push_subscriptions" in s:
            s = """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id, endpoint) DO UPDATE
                   SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth"""
        if "INSERT OR REPLACE INTO" in s:
            s = s.replace("INSERT OR REPLACE INTO", "INSERT INTO")
        # SQLite-only helpers
        s = s.replace("SELECT last_insert_rowid()", "SELECT lastval()")
        # Postgres prefers space in ON CONFLICT (col)
        s = s.replace("ON CONFLICT(", "ON CONFLICT (")
        return s

    def execute(self, sql, params=None):
        params = tuple(params) if params is not None else ()
        sql2 = self._adapt_sql(sql)
        if self._kind == "pg":
            cur = self._raw.cursor()
            # Для INSERT без RETURNING — добавим RETURNING id, если похоже на insert в таблицу с id
            is_insert = sql2.lstrip().upper().startswith("INSERT")
            used_returning = False
            if is_insert and "RETURNING" not in sql2.upper():
                # безопаснее lastval после execute
                pass
            try:
                cur.execute(sql2, params)
            except Exception:
                cur.close()
                raise
            lastrowid = None
            if is_insert:
                try:
                    cur2 = self._raw.cursor()
                    cur2.execute("SELECT lastval()")
                    row = cur2.fetchone()
                    lastrowid = row[0] if row else None
                    cur2.close()
                except Exception:
                    lastrowid = None
            self.lastrowid = lastrowid
            return _DBCursor(cur, "pg", lastrowid)
        else:
            cur = self._raw.execute(sql2, params)
            self.lastrowid = getattr(cur, "lastrowid", None)
            return cur

    def cursor(self):
        return self._raw.cursor()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def _open_sqlite():
    raw = sqlite3.connect(DB_PATH, timeout=30)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return raw


def _is_cloud_host():
    """Railway / Render / облачный хостинг."""
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RENDER")
    )


def _pg_url():
    """Нормализация DATABASE_URL для Railway / Render / Supabase / Neon."""
    url = DATABASE_URL
    if not url:
        return ""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Внешние облачные Postgres почти всегда требуют SSL.
    # Внутренний Railway (*.railway.internal) — без SSL.
    if "sslmode=" not in url:
        host_part = url.split("@")[-1].lower() if "@" in url else url.lower()
        internal = (
            "railway.internal" in host_part
            or host_part.startswith("postgres.railway")
            or "localhost" in host_part
            or "127.0.0.1" in host_part
        )
        need_ssl = (not internal) and (
            _is_cloud_host()
            or "render.com" in url
            or "supabase" in url
            or "neon.tech" in url
            or "amazonaws.com" in url
            or "rlwy.net" in url
            or "railway.app" in url
            or "@dpg-" in url
        )
        if need_ssl:
            url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


@contextmanager
def get_db():
    """
    PostgreSQL, если задан DATABASE_URL — данные не стираются при перезапуске.
    Иначе локальный SQLite (только для разработки на ПК).
    """
    if DATABASE_URL:
        url = _pg_url()
        last_err = None
        # pg8000 — чистый Python, работает на любой версии Python / Railway / Render
        try:
            import pg8000.dbapi as pgdb
            from urllib.parse import urlparse, unquote, parse_qs
            u = urlparse(url)
            # password/user могут быть URL-encoded
            user = unquote(u.username or "")
            password = unquote(u.password or "")
            host = u.hostname or "localhost"
            port = u.port or 5432
            database = (u.path or "/").lstrip("/") or "postgres"
            # SSL: по query или по хосту
            qs = parse_qs(u.query or "")
            sslmode = (qs.get("sslmode") or [""])[0].lower()
            need_ssl = sslmode in ("require", "verify-ca", "verify-full") or (
                "sslmode=require" in url
            )
            # Внешний Railway proxy-host часто требует SSL
            if not need_ssl and host and (
                "rlwy.net" in host
                or "railway.app" in host
                or os.environ.get("RENDER")
            ):
                need_ssl = True
            ssl_context = None
            if need_ssl:
                import ssl
                ssl_context = ssl.create_default_context()
                try:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                except Exception:
                    pass
            connect_kwargs = dict(
                user=user,
                password=password,
                host=host,
                port=port,
                database=database,
                timeout=25,
            )
            if ssl_context is not None:
                connect_kwargs["ssl_context"] = ssl_context
            raw = pgdb.connect(**connect_kwargs)
            conn = _DBConn(raw, "pg")
            try:
                yield conn
                raw.commit()
            except Exception:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
        except Exception as e:
            last_err = e
            print("pg8000 failed:", e)
        print("ERROR PostgreSQL:", last_err)
        if _is_cloud_host():
            raise RuntimeError(str(last_err))
        print("Fallback SQLite (только локально)")

    raw = _open_sqlite()
    conn = _DBConn(raw, "sqlite")
    try:
        yield conn
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init_db():
    """Создание таблиц. Не должно ронять сервер."""
    with get_db() as conn:
        c = conn

        # Пользователи (дублирует IF NOT EXISTS — безопасно)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                is_pro INTEGER DEFAULT 0,
                pro_until TEXT,
                free_pro_used INTEGER DEFAULT 0,
                language TEXT DEFAULT 'ru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_login TEXT
            )
        """)
        try:
            c.commit()
        except Exception:
            pass

        # Результаты тестов
        c.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                test_id TEXT NOT NULL,
                score REAL NOT NULL,
                max_score REAL NOT NULL,
                correct INTEGER NOT NULL,
                incorrect INTEGER NOT NULL,
                answers_json TEXT,
                suggested_faculties TEXT,
                duration_seconds INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Заявки на Pro
        c.execute("""
            CREATE TABLE IF NOT EXISTS pro_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                payment_method TEXT NOT NULL,
                screenshot_path TEXT,
                status TEXT DEFAULT 'pending',
                admin_note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Уведомления
        c.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Глобальные уведомления (для всех)
        c.execute("""
            CREATE TABLE IF NOT EXISTS global_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Факультеты (создаёт админ)
        c.execute("""
            CREATE TABLE IF NOT EXISTS faculties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_ru TEXT NOT NULL,
                name_en TEXT DEFAULT '',
                name_tg TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Тесты (создаёт админ)
        c.execute("""
            CREATE TABLE IF NOT EXISTS content_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                title_ru TEXT NOT NULL,
                title_en TEXT DEFAULT '',
                title_tg TEXT DEFAULT '',
                time_limit INTEGER DEFAULT 600,
                pro_only INTEGER DEFAULT 0,
                faculty_ids TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Вопросы (создаёт админ)
        c.execute("""
            CREATE TABLE IF NOT EXISTS content_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                q_ru TEXT NOT NULL,
                q_en TEXT DEFAULT '',
                q_tg TEXT DEFAULT '',
                opt_a TEXT NOT NULL,
                opt_b TEXT NOT NULL,
                opt_c TEXT DEFAULT '',
                opt_d TEXT DEFAULT '',
                correct_index INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                FOREIGN KEY (test_id) REFERENCES content_tests(id) ON DELETE CASCADE
            )
        """)

        # Настройки пользователя (тема, звук)
        try:
            c.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN sound_enabled INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN hints_left INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN device_type TEXT DEFAULT 'unknown'")
        except Exception:
            pass

        try:
            c.execute("ALTER TABLE users ADD COLUMN password_plain TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Asia/Dushanbe'",
        "ALTER TABLE users ADD COLUMN last_seen TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE social_links ADD COLUMN ends_at TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_tests ADD COLUMN exam_start TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_tests ADD COLUMN exam_end TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_tests ADD COLUMN published INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_tests ADD COLUMN subject_name TEXT DEFAULT ''")
        except Exception:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS social_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                network TEXT NOT NULL,
                title TEXT DEFAULT '',
                url TEXT NOT NULL,
                is_promo INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        try:
            c.execute("ALTER TABLE content_tests ADD COLUMN test_type TEXT DEFAULT 'mcq'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_questions ADD COLUMN q_type TEXT DEFAULT 'mcq'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_questions ADD COLUMN match_json TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE content_questions ADD COLUMN correct_multi TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE pro_requests ADD COLUMN package TEXT DEFAULT '2m'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE pro_requests ADD COLUMN duration_days INTEGER DEFAULT 60")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE test_results ADD COLUMN mode TEXT DEFAULT 'exam'")
        except Exception:
            pass

        c.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, endpoint),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS webauthn_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                credential_id TEXT UNIQUE NOT NULL,
                public_key TEXT NOT NULL,
                sign_count INTEGER DEFAULT 0,
                device_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Админ создаётся отдельно в ensure_admin() после commit


_schema_ready = False

def ensure_admin():
    """Гарантирует наличие admin@hgu.tj с рабочим паролем (Postgres/SQLite)."""
    _admin_email = "admin@hgu.tj"
    _admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
    _admin_hash = generate_password_hash(_admin_pwd, method="pbkdf2:sha256")
    try:
        with get_db() as conn:
            admin = conn.execute(
                "SELECT id, password_hash, is_admin FROM users WHERE email = ?",
                (_admin_email,),
            ).fetchone()
            if not admin:
                try:
                    conn.execute(
                        "INSERT INTO users (full_name, email, password_hash, is_admin, language) VALUES (?, ?, ?, 1, 'ru')",
                        ("Администратор", _admin_email, _admin_hash),
                    )
                except Exception as e1:
                    # минимальный INSERT
                    try:
                        conn.execute(
                            "INSERT INTO users (full_name, email, password_hash, is_admin) VALUES (?, ?, ?, 1)",
                            ("Администратор", _admin_email, _admin_hash),
                        )
                    except Exception as e2:
                        print("ensure_admin INSERT failed:", e1, e2)
                        raise
                print("ensure_admin: created admin@hgu.tj")
            else:
                ph = admin["password_hash"] or ""
                try:
                    is_adm = int(admin["is_admin"] or 0)
                except Exception:
                    is_adm = 1 if admin["is_admin"] else 0
                hash_ok = False
                if ph:
                    try:
                        hash_ok = check_password_hash(ph, _admin_pwd)
                    except Exception:
                        hash_ok = False
                if not is_adm or not hash_ok:
                    conn.execute(
                        "UPDATE users SET password_hash = ?, is_admin = 1 WHERE email = ?",
                        (_admin_hash, _admin_email),
                    )
                    print("ensure_admin: repaired admin password/flag")
        return True
    except Exception as e:
        print("ensure_admin failed:", e)
        return False


def _table_exists(conn, name="users"):
    try:
        if DATABASE_URL:
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
                (name,),
            ).fetchone()
            return bool(row)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def ensure_schema():
    """Создаёт ВСЕ таблицы и админа. После успеха больше не гоняет CREATE."""
    global _schema_ready
    if _schema_ready:
        return
    critical = (
        "users", "test_results", "pro_requests", "notifications",
        "global_notifications", "content_tests", "content_questions",
        "faculties", "app_settings", "social_links",
    )
    try:
        _create_core_tables()
        try:
            init_db()
        except Exception as e:
            print("ensure_schema init_db error:", e)
        ensure_admin()
        with get_db() as conn:
            still = [t for t in critical if not _table_exists(conn, t)]
        if not still:
            _schema_ready = True
            print("ensure_schema: all critical tables OK")
        else:
            print("ensure_schema: still missing:", still)
    except Exception as e:
        print("ensure_schema failed:", e)
        try:
            _create_core_tables()
            ensure_admin()
        except Exception as e2:
            print("ensure_schema retry failed:", e2)


def _create_core_tables():
    """Гарантированное создание ВСЕХ таблиц по одной (отдельные транзакции, Postgres-safe)."""
    statements = [
        # users
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_pro INTEGER DEFAULT 0,
            pro_until TEXT,
            free_pro_used INTEGER DEFAULT 0,
            language TEXT DEFAULT 'ru',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        )""",
        # test_results
        """CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            test_id TEXT NOT NULL,
            score REAL NOT NULL,
            max_score REAL NOT NULL,
            correct INTEGER NOT NULL,
            incorrect INTEGER NOT NULL,
            answers_json TEXT,
            suggested_faculties TEXT,
            duration_seconds INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # pro_requests
        """CREATE TABLE IF NOT EXISTS pro_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            screenshot_path TEXT,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT
        )""",
        # notifications (колонка message — как в остальном коде)
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # global_notifications
        """CREATE TABLE IF NOT EXISTS global_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # faculties
        """CREATE TABLE IF NOT EXISTS faculties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name_ru TEXT NOT NULL,
            name_en TEXT DEFAULT '',
            name_tg TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # content_tests
        """CREATE TABLE IF NOT EXISTS content_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title_ru TEXT NOT NULL,
            title_en TEXT DEFAULT '',
            title_tg TEXT DEFAULT '',
            time_limit INTEGER DEFAULT 600,
            pro_only INTEGER DEFAULT 0,
            faculty_ids TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # content_questions
        """CREATE TABLE IF NOT EXISTS content_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            q_ru TEXT NOT NULL,
            q_en TEXT DEFAULT '',
            q_tg TEXT DEFAULT '',
            opt_a TEXT NOT NULL DEFAULT '',
            opt_b TEXT NOT NULL DEFAULT '',
            opt_c TEXT DEFAULT '',
            opt_d TEXT DEFAULT '',
            correct_index INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        )""",
        # social_links (network/is_promo/sort_order — как в коде админки)
        """CREATE TABLE IF NOT EXISTS social_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            network TEXT NOT NULL DEFAULT '',
            title TEXT DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            is_promo INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            ends_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # app_settings
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # push_subscriptions
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        # webauthn_credentials
        """CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            credential_id TEXT UNIQUE NOT NULL,
            public_key TEXT NOT NULL,
            sign_count INTEGER DEFAULT 0,
            device_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    extras = [
        "ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'",
        "ALTER TABLE users ADD COLUMN sound_enabled INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN hints_left INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN device_type TEXT DEFAULT 'unknown'",
        "ALTER TABLE users ADD COLUMN password_plain TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Asia/Dushanbe'",
        "ALTER TABLE test_results ADD COLUMN mode TEXT DEFAULT 'exam'",
        "ALTER TABLE pro_requests ADD COLUMN package TEXT DEFAULT '2m'",
        "ALTER TABLE pro_requests ADD COLUMN duration_days INTEGER DEFAULT 60",
        "ALTER TABLE content_tests ADD COLUMN exam_start TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN exam_end TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN published INTEGER DEFAULT 0",
        "ALTER TABLE social_links ADD COLUMN ends_at TEXT DEFAULT ''",
        "ALTER TABLE content_questions ADD COLUMN q_type TEXT DEFAULT 'mcq'",
        "ALTER TABLE content_questions ADD COLUMN match_json TEXT DEFAULT ''",
        "ALTER TABLE content_questions ADD COLUMN correct_multi TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN test_type TEXT DEFAULT 'mcq'",
        "ALTER TABLE content_tests ADD COLUMN subject_name TEXT DEFAULT ''",
        "ALTER TABLE notifications ADD COLUMN message TEXT DEFAULT ''",
        "ALTER TABLE global_notifications ADD COLUMN message TEXT DEFAULT ''",
        # social_links — если таблица создана со старыми колонками
        "ALTER TABLE social_links ADD COLUMN network TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN is_promo INTEGER DEFAULT 1",
        "ALTER TABLE social_links ADD COLUMN sort_order INTEGER DEFAULT 0",
        "ALTER TABLE social_links ADD COLUMN ends_at TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN title TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN url TEXT DEFAULT ''",
    ]
    ok_n = 0
    for sql in statements:
        try:
            with get_db() as conn:
                conn.execute(sql)
            ok_n += 1
        except Exception as e:
            print("_create_core_tables FAIL:", e, "|", sql[:60].replace("\n", " "))
    for sql in extras:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception:
            pass
    print(f"_create_core_tables: {ok_n}/{len(statements)} tables OK")

@app.context_processor
def inject_grade_helpers():
    return {
        "letter_grade": letter_grade,
        "score_percent": score_percent,
        "format_dt": format_dt,
        "GRADE_LETTERS": GRADE_LETTERS,
        "CIS_TIMEZONES": CIS_TIMEZONES,
    }

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Войдите в аккаунт"


class User(UserMixin):
    def __init__(self, row):
        def g(key, default=None):
            try:
                val = row[key]
                return default if val is None else val
            except (KeyError, IndexError, TypeError):
                return default

        self.id = g("id")
        self.full_name = g("full_name", "") or ""
        self.email = g("email", "") or ""
        self.password_hash = g("password_hash", "") or ""
        self.is_admin = bool(g("is_admin", 0))
        self.is_pro = bool(g("is_pro", 0))
        self.pro_until = g("pro_until")
        self.free_pro_used = bool(g("free_pro_used", 0))
        self.language = g("language", "ru") or "ru"
        self.created_at = g("created_at")
        self.last_login = g("last_login")
        self.theme = g("theme", "light") or "light"
        se = g("sound_enabled", 1)
        self.sound_enabled = True if se is None else bool(se)
        self.hints_left = int(g("hints_left", 0) or 0)
        self.device_type = g("device_type", "unknown") or "unknown"
        self.password_plain = g("password_plain", "") or ""
        self.timezone = g("timezone", "Asia/Dushanbe") or "Asia/Dushanbe"

    def check_pro(self):
        """Проверяет и обновляет статус Pro"""
        if not self.pro_until:
            return False
        try:
            until = datetime.fromisoformat(self.pro_until)
            if datetime.now() > until:
                with get_db() as conn:
                    conn.execute("UPDATE users SET is_pro = 0, pro_until = NULL WHERE id = ?", (self.id,))
                self.is_pro = False
                self.pro_until = None
                return False
            return True
        except Exception:
            return False


@app.before_request
def _before_request_ensure_schema():
    # Не блокируем static
    if request.endpoint in (None, "static"):
        return
    try:
        ensure_schema()
    except Exception:
        pass


@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            return User(row)
    return None


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Доступ только для администратора", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


# ==================== ТЕСТЫ И ФАКУЛЬТЕТЫ ====================

FACULTIES = {
    "math": {
        "ru": "Математический факультет",
        "en": "Faculty of Mathematics",
        "tg": "Факултети математика"
    },
    "physics": {
        "ru": "Физико-технический факультет",
        "en": "Faculty of Physics and Technology",
        "tg": "Факултети физикаву техника"
    },
    "it": {
        "ru": "Факультет телекоммуникаций и ИТ",
        "en": "Faculty of Telecommunications and IT",
        "tg": "Факултети телекоммуникатсия ва ТИ"
    },
    "finance": {
        "ru": "Факультет финансов и рыночной экономики",
        "en": "Faculty of Finance and Market Economy",
        "tg": "Факултети молия ва иқтисоди бозор"
    },
    "foreign_lang": {
        "ru": "Факультет иностранных языков",
        "en": "Faculty of Foreign Languages",
        "tg": "Факултети забонҳои хориҷӣ"
    },
    "oriental": {
        "ru": "Факультет восточных языков",
        "en": "Faculty of Oriental Languages",
        "tg": "Факултети забонҳои шарқӣ"
    },
    "history_law": {
        "ru": "Факультет истории и права",
        "en": "Faculty of History and Law",
        "tg": "Факултети таърих ва ҳуқуқ"
    },
    "tajik_phil": {
        "ru": "Факультет таджикской филологии",
        "en": "Faculty of Tajik Philology",
        "tg": "Факултети филологияи тоҷикӣ"
    },
    "russian_phil": {
        "ru": "Факультет русской филологии",
        "en": "Faculty of Russian Philology",
        "tg": "Факултети филологияи русӣ"
    },
    "pedagogy": {
        "ru": "Педагогический факультет",
        "en": "Faculty of Pedagogy",
        "tg": "Факултети омӯзгорӣ"
    },
    "geo_eco": {
        "ru": "Факультет геоэкологии",
        "en": "Faculty of Geo-Ecology",
        "tg": "Факултети геоэкология"
    },
    "arts": {
        "ru": "Факультет искусств",
        "en": "Faculty of Arts",
        "tg": "Факултети санъат"
    },
    "chem_bio": {
        "ru": "Факультет химии и биологии",
        "en": "Faculty of Chemistry and Biology",
        "tg": "Факултети химия ва биология"
    },
    "physical": {
        "ru": "Факультет физической культуры",
        "en": "Faculty of Physical Education",
        "tg": "Факултети тарбияи ҷисмонӣ"
    },
    "uzbek_phil": {
        "ru": "Факультет узбекской филологии",
        "en": "Faculty of Uzbek Philology",
        "tg": "Факултети филологияи ӯзбекӣ"
    }
}

# Вопросы тестов (базовый набор + расширенный для Pro)
# Каждый тест связан с одним или несколькими факультетами

TESTS = {
    "math_basic": {
        "title": {"ru": "Математика (базовый)", "en": "Mathematics (Basic)", "tg": "Математика (асосӣ)"},
        "faculties": ["math", "it", "physics"],
        "time_limit": 600,  # секунд
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Чему равно 2 + 2 * 2?", "en": "What is 2 + 2 * 2?", "tg": "2 + 2 * 2 баробар ба чист?"},
                "options": {"ru": ["6", "8", "4", "10"], "en": ["6", "8", "4", "10"], "tg": ["6", "8", "4", "10"]},
                "correct": 0
            },
            {
                "q": {"ru": "Корень из 144 равен?", "en": "Square root of 144 is?", "tg": "Решаи 144 баробар ба?"},
                "options": {"ru": ["10", "12", "14", "16"], "en": ["10", "12", "14", "16"], "tg": ["10", "12", "14", "16"]},
                "correct": 1
            },
            {
                "q": {"ru": "Решите: 5x = 20. x = ?", "en": "Solve: 5x = 20. x = ?", "tg": "Ҳал кунед: 5x = 20. x = ?"},
                "options": {"ru": ["2", "4", "5", "10"], "en": ["2", "4", "5", "10"], "tg": ["2", "4", "5", "10"]},
                "correct": 1
            },
            {
                "q": {"ru": "Площадь квадрата со стороной 5?", "en": "Area of square with side 5?", "tg": "Масоҳати квадрат бо тарафи 5?"},
                "options": {"ru": ["10", "20", "25", "30"], "en": ["10", "20", "25", "30"], "tg": ["10", "20", "25", "30"]},
                "correct": 2
            },
            {
                "q": {"ru": "Сколько градусов в прямом угле?", "en": "How many degrees in a right angle?", "tg": "Дар кунҷи рост чанд дараҷа?"},
                "options": {"ru": ["45", "90", "180", "360"], "en": ["45", "90", "180", "360"], "tg": ["45", "90", "180", "360"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что больше: 3/4 или 0.7?", "en": "Which is larger: 3/4 or 0.7?", "tg": "Кадом калонтар: 3/4 ё 0.7?"},
                "options": {"ru": ["3/4", "0.7", "равны", "нельзя сравнить"], "en": ["3/4", "0.7", "equal", "cannot compare"], "tg": ["3/4", "0.7", "баробар", "муқоиса кардан мумкин нест"]},
                "correct": 0
            },
            {
                "q": {"ru": "Сумма углов треугольника?", "en": "Sum of angles in a triangle?", "tg": "Ҷамъи кунҷҳои секунҷа?"},
                "options": {"ru": ["90", "180", "270", "360"], "en": ["90", "180", "270", "360"], "tg": ["90", "180", "270", "360"]},
                "correct": 1
            },
            {
                "q": {"ru": "10% от 200?", "en": "10% of 200?", "tg": "10% аз 200?"},
                "options": {"ru": ["10", "20", "30", "40"], "en": ["10", "20", "30", "40"], "tg": ["10", "20", "30", "40"]},
                "correct": 1
            },
            {
                "q": {"ru": "Если a=3, b=4, то a² + b² = ?", "en": "If a=3, b=4, then a² + b² = ?", "tg": "Агар a=3, b=4, он гоҳ a² + b² = ?"},
                "options": {"ru": ["7", "12", "25", "49"], "en": ["7", "12", "25", "49"], "tg": ["7", "12", "25", "49"]},
                "correct": 2
            },
            {
                "q": {"ru": "Сколько минут в 2.5 часах?", "en": "How many minutes in 2.5 hours?", "tg": "Дар 2.5 соат чанд дақиқа?"},
                "options": {"ru": ["120", "150", "180", "200"], "en": ["120", "150", "180", "200"], "tg": ["120", "150", "180", "200"]},
                "correct": 1
            }
        ]
    },
    "math_pro": {
        "title": {"ru": "Математика (продвинутый)", "en": "Mathematics (Advanced)", "tg": "Математика (пешрафта)"},
        "faculties": ["math", "it", "physics"],
        "time_limit": 900,
        "pro_only": True,
        "questions": [
            {
                "q": {"ru": "Решите уравнение: x² - 5x + 6 = 0", "en": "Solve: x² - 5x + 6 = 0", "tg": "Муодиларо ҳал кунед: x² - 5x + 6 = 0"},
                "options": {"ru": ["x=2 и x=3", "x=1 и x=6", "x=-2 и x=-3", "нет решений"], "en": ["x=2 and x=3", "x=1 and x=6", "x=-2 and x=-3", "no solutions"], "tg": ["x=2 ва x=3", "x=1 ва x=6", "x=-2 ва x=-3", "ҳал надорад"]},
                "correct": 0
            },
            {
                "q": {"ru": "Производная функции f(x) = x³?", "en": "Derivative of f(x) = x³?", "tg": "Ҳосилаи функсияи f(x) = x³?"},
                "options": {"ru": ["3x²", "x²", "3x", "x³"], "en": ["3x²", "x²", "3x", "x³"], "tg": ["3x²", "x²", "3x", "x³"]},
                "correct": 0
            },
            {
                "q": {"ru": "log₁₀(1000) = ?", "en": "log₁₀(1000) = ?", "tg": "log₁₀(1000) = ?"},
                "options": {"ru": ["2", "3", "4", "10"], "en": ["2", "3", "4", "10"], "tg": ["2", "3", "4", "10"]},
                "correct": 1
            },
            {
                "q": {"ru": "Интеграл от 2x dx?", "en": "Integral of 2x dx?", "tg": "Интеграли 2x dx?"},
                "options": {"ru": ["x² + C", "2x² + C", "x + C", "2x + C"], "en": ["x² + C", "2x² + C", "x + C", "2x + C"], "tg": ["x² + C", "2x² + C", "x + C", "2x + C"]},
                "correct": 0
            },
            {
                "q": {"ru": "sin(90°) = ?", "en": "sin(90°) = ?", "tg": "sin(90°) = ?"},
                "options": {"ru": ["0", "0.5", "1", "-1"], "en": ["0", "0.5", "1", "-1"], "tg": ["0", "0.5", "1", "-1"]},
                "correct": 2
            },
            {
                "q": {"ru": "Предел lim(x→0) sin(x)/x = ?", "en": "Limit lim(x→0) sin(x)/x = ?", "tg": "Ҳадди lim(x→0) sin(x)/x = ?"},
                "options": {"ru": ["0", "1", "∞", "не существует"], "en": ["0", "1", "∞", "does not exist"], "tg": ["0", "1", "∞", "мавҷуд нест"]},
                "correct": 1
            },
            {
                "q": {"ru": "Матрица 2x2. Определитель [[1,2],[3,4]]?", "en": "Determinant of [[1,2],[3,4]]?", "tg": "Детерминанти [[1,2],[3,4]]?"},
                "options": {"ru": ["-2", "2", "-1", "10"], "en": ["-2", "2", "-1", "10"], "tg": ["-2", "2", "-1", "10"]},
                "correct": 0
            },
            {
                "q": {"ru": "Комбинаторика: C(5,2) = ?", "en": "Combinatorics: C(5,2) = ?", "tg": "Комбинаторика: C(5,2) = ?"},
                "options": {"ru": ["5", "10", "15", "20"], "en": ["5", "10", "15", "20"], "tg": ["5", "10", "15", "20"]},
                "correct": 1
            },
            {
                "q": {"ru": "Вероятность выпадения орла при броске монеты?", "en": "Probability of heads when tossing a coin?", "tg": "Эҳтимолияти афтодани сар ҳангоми партофтани танга?"},
                "options": {"ru": ["0", "0.25", "0.5", "1"], "en": ["0", "0.25", "0.5", "1"], "tg": ["0", "0.25", "0.5", "1"]},
                "correct": 2
            },
            {
                "q": {"ru": "Ряд 1 + 2 + 4 + 8 + ... (геометрическая прогрессия). Сумма первых 5 членов?", "en": "Sum of first 5 terms of 1+2+4+8+...?", "tg": "Ҷамъи 5 узви аввали 1+2+4+8+...?"},
                "options": {"ru": ["15", "31", "63", "16"], "en": ["15", "31", "63", "16"], "tg": ["15", "31", "63", "16"]},
                "correct": 1
            }
        ]
    },
    "physics_basic": {
        "title": {"ru": "Физика (базовый)", "en": "Physics (Basic)", "tg": "Физика (асосӣ)"},
        "faculties": ["physics", "it", "chem_bio"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Единица силы в СИ?", "en": "SI unit of force?", "tg": "Воҳиди қувва дар СИ?"},
                "options": {"ru": ["Джоуль", "Ньютон", "Ватт", "Паскаль"], "en": ["Joule", "Newton", "Watt", "Pascal"], "tg": ["Ҷоул", "Нютон", "Ватт", "Паскал"]},
                "correct": 1
            },
            {
                "q": {"ru": "Скорость света примерно?", "en": "Speed of light approximately?", "tg": "Суръати нур тахминан?"},
                "options": {"ru": ["300 км/с", "3000 км/с", "300000 км/с", "30 км/с"], "en": ["300 km/s", "3000 km/s", "300000 km/s", "30 km/s"], "tg": ["300 км/с", "3000 км/с", "300000 км/с", "30 км/с"]},
                "correct": 2
            },
            {
                "q": {"ru": "Формула кинетической энергии?", "en": "Kinetic energy formula?", "tg": "Формулаи энергияи кинетикӣ?"},
                "options": {"ru": ["mgh", "mv²/2", "Fx", "P=W/t"], "en": ["mgh", "mv²/2", "Fx", "P=W/t"], "tg": ["mgh", "mv²/2", "Fx", "P=W/t"]},
                "correct": 1
            },
            {
                "q": {"ru": "Закон Ома: I = ?", "en": "Ohm's law: I = ?", "tg": "Қонуни Ом: I = ?"},
                "options": {"ru": ["U/R", "U*R", "R/U", "U+R"], "en": ["U/R", "U*R", "R/U", "U+R"], "tg": ["U/R", "U*R", "R/U", "U+R"]},
                "correct": 0
            },
            {
                "q": {"ru": "Температура кипения воды при нормальном давлении?", "en": "Boiling point of water at normal pressure?", "tg": "Ҳарорати ҷӯшиши об дар фишори муқаррарӣ?"},
                "options": {"ru": ["0°C", "50°C", "100°C", "200°C"], "en": ["0°C", "50°C", "100°C", "200°C"], "tg": ["0°C", "50°C", "100°C", "200°C"]},
                "correct": 2
            },
            {
                "q": {"ru": "Ускорение свободного падения g ≈ ?", "en": "Free fall acceleration g ≈ ?", "tg": "Шитоби афтиши озод g ≈ ?"},
                "options": {"ru": ["5 м/с²", "9.8 м/с²", "15 м/с²", "20 м/с²"], "en": ["5 m/s²", "9.8 m/s²", "15 m/s²", "20 m/s²"], "tg": ["5 м/с²", "9.8 м/с²", "15 м/с²", "20 м/с²"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что измеряется в Амперах?", "en": "What is measured in Amperes?", "tg": "Чӣ дар Амперҳо чен карда мешавад?"},
                "options": {"ru": ["Напряжение", "Сопротивление", "Сила тока", "Мощность"], "en": ["Voltage", "Resistance", "Current", "Power"], "tg": ["Шиддат", "Муқовимат", "Қувваи ҷараён", "Қувва"]},
                "correct": 2
            },
            {
                "q": {"ru": "Плотность воды?", "en": "Density of water?", "tg": "Зичии об?"},
                "options": {"ru": ["500 кг/м³", "1000 кг/м³", "1500 кг/м³", "2000 кг/м³"], "en": ["500 kg/m³", "1000 kg/m³", "1500 kg/m³", "2000 kg/m³"], "tg": ["500 кг/м³", "1000 кг/м³", "1500 кг/м³", "2000 кг/м³"]},
                "correct": 1
            },
            {
                "q": {"ru": "Первый закон Ньютона говорит о?", "en": "Newton's first law is about?", "tg": "Қонуни аввали Нютон дар бораи?"},
                "options": {"ru": ["Силе", "Инерции", "Действии и противодействии", "Гравитации"], "en": ["Force", "Inertia", "Action-reaction", "Gravity"], "tg": ["Қувва", "Инерсия", "Амал ва зиддиамал", "Граитатсия"]},
                "correct": 1
            },
            {
                "q": {"ru": "Частота тока в сети Таджикистана?", "en": "Mains frequency in Tajikistan?", "tg": "Басомади ҷараён дар шабакаи Тоҷикистон?"},
                "options": {"ru": ["40 Гц", "50 Гц", "60 Гц", "100 Гц"], "en": ["40 Hz", "50 Hz", "60 Hz", "100 Hz"], "tg": ["40 Гц", "50 Гц", "60 Гц", "100 Гц"]},
                "correct": 1
            }
        ]
    },
    "it_basic": {
        "title": {"ru": "Информатика (базовый)", "en": "Informatics (Basic)", "tg": "Информатика (асосӣ)"},
        "faculties": ["it", "math"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Что означает HTML?", "en": "What does HTML stand for?", "tg": "HTML чӣ маъно дорад?"},
                "options": {"ru": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"], "en": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"], "tg": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"]},
                "correct": 0
            },
            {
                "q": {"ru": "1 байт = ?", "en": "1 byte = ?", "tg": "1 байт = ?"},
                "options": {"ru": ["4 бита", "8 бит", "16 бит", "32 бита"], "en": ["4 bits", "8 bits", "16 bits", "32 bits"], "tg": ["4 бит", "8 бит", "16 бит", "32 бит"]},
                "correct": 1
            },
            {
                "q": {"ru": "Какой язык программирования?", "en": "Which is a programming language?", "tg": "Кадом забони барномасозӣ аст?"},
                "options": {"ru": ["HTML", "CSS", "Python", "HTTP"], "en": ["HTML", "CSS", "Python", "HTTP"], "tg": ["HTML", "CSS", "Python", "HTTP"]},
                "correct": 2
            },
            {
                "q": {"ru": "CPU расшифровывается как?", "en": "CPU stands for?", "tg": "CPU чӣ маъно дорад?"},
                "options": {"ru": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"], "en": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"], "tg": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"]},
                "correct": 0
            },
            {
                "q": {"ru": "Операционная система?", "en": "Which is an OS?", "tg": "Кадом системаи амалиётӣ аст?"},
                "options": {"ru": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"], "en": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"], "tg": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"]},
                "correct": 2
            },
            {
                "q": {"ru": "Двоичная система: 1010₂ = ?", "en": "Binary: 1010₂ = ?", "tg": "Системаи дуӣ: 1010₂ = ?"},
                "options": {"ru": ["8", "10", "12", "14"], "en": ["8", "10", "12", "14"], "tg": ["8", "10", "12", "14"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что такое алгоритм?", "en": "What is an algorithm?", "tg": "Алгоритм чист?"},
                "options": {"ru": ["Язык программирования", "Последовательность действий", "Операционная система", "Тип данных"], "en": ["Programming language", "Sequence of actions", "Operating system", "Data type"], "tg": ["Забони барномасозӣ", "Пайдарпаии амалҳо", "Системаи амалиётӣ", "Намуди маълумот"]},
                "correct": 1
            },
            {
                "q": {"ru": "RAM - это?", "en": "RAM is?", "tg": "RAM чист?"},
                "options": {"ru": ["Постоянная память", "Оперативная память", "Жёсткий диск", "Процессор"], "en": ["Permanent memory", "Random Access Memory", "Hard drive", "Processor"], "tg": ["Хотираи доимӣ", "Хотираи амалиётӣ", "Диски сахт", "Процессор"]},
                "correct": 1
            },
            {
                "q": {"ru": "Интернет-протокол для веб-страниц?", "en": "Internet protocol for web pages?", "tg": "Протоколи интернет барои саҳифаҳои веб?"},
                "options": {"ru": ["FTP", "HTTP", "SMTP", "SSH"], "en": ["FTP", "HTTP", "SMTP", "SSH"], "tg": ["FTP", "HTTP", "SMTP", "SSH"]},
                "correct": 1
            },
            {
                "q": {"ru": "В Python: print(type(5)) выведет?", "en": "In Python: print(type(5)) outputs?", "tg": "Дар Python: print(type(5)) чӣ мебарорад?"},
                "options": {"ru": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"], "en": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"], "tg": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"]},
                "correct": 1
            }
        ]
    },
    "history_basic": {
        "title": {"ru": "История Таджикистана (базовый)", "en": "History of Tajikistan (Basic)", "tg": "Таърихи Тоҷикистон (асосӣ)"},
        "faculties": ["history_law", "tajik_phil", "pedagogy"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Столица Таджикистана?", "en": "Capital of Tajikistan?", "tg": "Пойтахти Тоҷикистон?"},
                "options": {"ru": ["Худжанд", "Душанбе", "Куляб", "Курган-Тюбе"], "en": ["Khujand", "Dushanbe", "Kulob", "Qurghonteppa"], "tg": ["Хуҷанд", "Душанбе", "Кӯлоб", "Қурғонтеппа"]},
                "correct": 1
            },
            {
                "q": {"ru": "Год независимости Таджикистана?", "en": "Year of Tajikistan independence?", "tg": "Соли истиқлолияти Тоҷикистон?"},
                "options": {"ru": ["1989", "1991", "1992", "1994"], "en": ["1989", "1991", "1992", "1994"], "tg": ["1989", "1991", "1992", "1994"]},
                "correct": 1
            },
            {
                "q": {"ru": "Великий таджикский поэт?", "en": "Great Tajik poet?", "tg": "Шоири бузурги тоҷик?"},
                "options": {"ru": ["Пушкин", "Рудаки", "Шекспир", "Гёте"], "en": ["Pushkin", "Rudaki", "Shakespeare", "Goethe"], "tg": ["Пушкин", "Рӯдакӣ", "Шекспир", "Гёте"]},
                "correct": 1
            },
            {
                "q": {"ru": "Древнее государство на территории Таджикистана?", "en": "Ancient state on territory of Tajikistan?", "tg": "Давлати қадим дар ҳудуди Тоҷикистон?"},
                "options": {"ru": ["Согдиана", "Рим", "Египет", "Китай"], "en": ["Sogdiana", "Rome", "Egypt", "China"], "tg": ["Суғд", "Рим", "Миср", "Чин"]},
                "correct": 0
            },
            {
                "q": {"ru": "Худжанд ранее назывался?", "en": "Khujand was previously called?", "tg": "Хуҷанд қаблан чӣ ном дошт?"},
                "options": {"ru": ["Ленинабад", "Сталинабад", "Фрунзе", "Алма-Ата"], "en": ["Leninabad", "Stalinabad", "Frunze", "Alma-Ata"], "tg": ["Ленинобод", "Сталинобод", "Фрунзе", "Алма-Ата"]},
                "correct": 0
            },
            {
                "q": {"ru": "Официальный язык Таджикистана?", "en": "Official language of Tajikistan?", "tg": "Забони расмии Тоҷикистон?"},
                "options": {"ru": ["Русский", "Узбекский", "Таджикский", "Персидский"], "en": ["Russian", "Uzbek", "Tajik", "Persian"], "tg": ["Русӣ", "Ӯзбекӣ", "Тоҷикӣ", "Форсӣ"]},
                "correct": 2
            },
            {
                "q": {"ru": "Самая высокая гора Таджикистана?", "en": "Highest mountain in Tajikistan?", "tg": "Баландтарин кӯҳи Тоҷикистон?"},
                "options": {"ru": ["Эльбрус", "Пик Исмоила Сомони", "Арарат", "Казбек"], "en": ["Elbrus", "Ismoil Somoni Peak", "Ararat", "Kazbek"], "tg": ["Элбрус", "Қуллаи Исмоили Сомонӣ", "Арарат", "Қазбек"]},
                "correct": 1
            },
            {
                "q": {"ru": "В каком веке жил Авиценна (Ибн Сина)?", "en": "In which century did Avicenna live?", "tg": "Ибни Сино дар кадом аср зистааст?"},
                "options": {"ru": ["VIII-IX", "X-XI", "XII-XIII", "XIV-XV"], "en": ["8th-9th", "10th-11th", "12th-13th", "14th-15th"], "tg": ["VIII-IX", "X-XI", "XII-XIII", "XIV-XV"]},
                "correct": 1
            },
            {
                "q": {"ru": "Река, протекающая через Худжанд?", "en": "River flowing through Khujand?", "tg": "Дарёе, ки аз Хуҷанд мегузарад?"},
                "options": {"ru": ["Амударья", "Сырдарья", "Вахш", "Пяндж"], "en": ["Amu Darya", "Syr Darya", "Vakhsh", "Panj"], "tg": ["Амударё", "Сирдарё", "Вахш", "Панҷ"]},
                "correct": 1
            },
            {
                "q": {"ru": "ХГУ основан в?", "en": "KSU was founded in?", "tg": "ДДХ дар соли?"},
                "options": {"ru": ["1920", "1932", "1945", "1991"], "en": ["1920", "1932", "1945", "1991"], "tg": ["1920", "1932", "1945", "1991"]},
                "correct": 1
            }
        ]
    },
    "english_basic": {
        "title": {"ru": "Английский язык (базовый)", "en": "English (Basic)", "tg": "Забони англисӣ (асосӣ)"},
        "faculties": ["foreign_lang", "oriental"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Переведите: Hello", "en": "Translate: Hello", "tg": "Тарҷума кунед: Hello"},
                "options": {"ru": ["Привет", "Пока", "Спасибо", "Пожалуйста"], "en": ["Hello", "Bye", "Thanks", "Please"], "tg": ["Салом", "Хайр", "Раҳмат", "Лутфан"]},
                "correct": 0
            },
            {
                "q": {"ru": "How are you? означает?", "en": "How are you? means?", "tg": "How are you? чӣ маъно дорад?"},
                "options": {"ru": ["Как тебя зовут?", "Как дела?", "Где ты?", "Сколько тебе лет?"], "en": ["What is your name?", "How are you?", "Where are you?", "How old are you?"], "tg": ["Номи ту чист?", "Ҳолат чӣ гуна?", "Ту куҷо ҳастӣ?", "Чандсола ҳастӣ?"]},
                "correct": 1
            },
            {
                "q": {"ru": "Прошедшее время от go?", "en": "Past tense of go?", "tg": "Замони гузаштаи go?"},
                "options": {"ru": ["goed", "went", "goes", "going"], "en": ["goed", "went", "goes", "going"], "tg": ["goed", "went", "goes", "going"]},
                "correct": 1
            },
            {
                "q": {"ru": "I ___ a student.", "en": "I ___ a student.", "tg": "I ___ a student."},
                "options": {"ru": ["am", "is", "are", "be"], "en": ["am", "is", "are", "be"], "tg": ["am", "is", "are", "be"]},
                "correct": 0
            },
            {
                "q": {"ru": "Множественное число от child?", "en": "Plural of child?", "tg": "Ҷамъи child?"},
                "options": {"ru": ["childs", "children", "childes", "child"], "en": ["childs", "children", "childes", "child"], "tg": ["childs", "children", "childes", "child"]},
                "correct": 1
            },
            {
                "q": {"ru": "There ___ a book on the table.", "en": "There ___ a book on the table.", "tg": "There ___ a book on the table."},
                "options": {"ru": ["is", "are", "am", "be"], "en": ["is", "are", "am", "be"], "tg": ["is", "are", "am", "be"]},
                "correct": 0
            },
            {
                "q": {"ru": "She ___ to school every day.", "en": "She ___ to school every day.", "tg": "She ___ to school every day."},
                "options": {"ru": ["go", "goes", "going", "went"], "en": ["go", "goes", "going", "went"], "tg": ["go", "goes", "going", "went"]},
                "correct": 1
            },
            {
                "q": {"ru": "What is the opposite of hot?", "en": "What is the opposite of hot?", "tg": "Зидди hot чист?"},
                "options": {"ru": ["warm", "cold", "cool", "heat"], "en": ["warm", "cold", "cool", "heat"], "tg": ["warm", "cold", "cool", "heat"]},
                "correct": 1
            },
            {
                "q": {"ru": "Choose the correct article: ___ apple", "en": "Choose the correct article: ___ apple", "tg": "Артикли дурустро интихоб кунед: ___ apple"},
                "options": {"ru": ["a", "an", "the", "no article"], "en": ["a", "an", "the", "no article"], "tg": ["a", "an", "the", "бе артикл"]},
                "correct": 1
            },
            {
                "q": {"ru": "I have ___ books.", "en": "I have ___ books.", "tg": "I have ___ books."},
                "options": {"ru": ["much", "many", "a little", "little"], "en": ["much", "many", "a little", "little"], "tg": ["much", "many", "a little", "little"]},
                "correct": 1
            }
        ]
    },
    "chemistry_basic": {
        "title": {"ru": "Химия (базовый)", "en": "Chemistry (Basic)", "tg": "Химия (асосӣ)"},
        "faculties": ["chem_bio", "physics"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Химический символ воды?", "en": "Chemical formula of water?", "tg": "Формулаи химиявии об?"},
                "options": {"ru": ["H2O", "CO2", "O2", "NaCl"], "en": ["H2O", "CO2", "O2", "NaCl"], "tg": ["H2O", "CO2", "O2", "NaCl"]},
                "correct": 0
            },
            {
                "q": {"ru": "Атомный номер водорода?", "en": "Atomic number of hydrogen?", "tg": "Рақами атомии гидроген?"},
                "options": {"ru": ["1", "2", "8", "16"], "en": ["1", "2", "8", "16"], "tg": ["1", "2", "8", "16"]},
                "correct": 0
            },
            {
                "q": {"ru": "pH нейтральной среды?", "en": "pH of neutral medium?", "tg": "pH-и муҳити бетараф?"},
                "options": {"ru": ["0", "7", "14", "1"], "en": ["0", "7", "14", "1"], "tg": ["0", "7", "14", "1"]},
                "correct": 1
            },
            {
                "q": {"ru": "Газ, необходимый для дыхания?", "en": "Gas needed for breathing?", "tg": "Газе, ки барои нафаскашӣ лозим аст?"},
                "options": {"ru": ["CO2", "N2", "O2", "H2"], "en": ["CO2", "N2", "O2", "H2"], "tg": ["CO2", "N2", "O2", "H2"]},
                "correct": 2
            },
            {
                "q": {"ru": "Таблица Менделеева содержит элементы по?", "en": "Periodic table arranges elements by?", "tg": "Ҷадвали Менделеев элементҳоро аз рӯи?"},
                "options": {"ru": ["Массе", "Атомному номеру", "Цвету", "Плотности"], "en": ["Mass", "Atomic number", "Color", "Density"], "tg": ["Масса", "Рақами атомӣ", "Ранг", "Зичӣ"]},
                "correct": 1
            },
            {
                "q": {"ru": "NaCl - это?", "en": "NaCl is?", "tg": "NaCl чист?"},
                "options": {"ru": ["Сахар", "Поваренная соль", "Сода", "Уксус"], "en": ["Sugar", "Table salt", "Soda", "Vinegar"], "tg": ["Қанд", "Намак", "Сода", "Сирко"]},
                "correct": 1
            },
            {
                "q": {"ru": "Кислота имеет pH?", "en": "Acid has pH?", "tg": "Кислота pH-и?"},
                "options": {"ru": ["меньше 7", "равно 7", "больше 7", "равно 14"], "en": ["less than 7", "equal to 7", "greater than 7", "equal to 14"], "tg": ["камтар аз 7", "баробар ба 7", "зиёдтар аз 7", "баробар ба 14"]},
                "correct": 0
            },
            {
                "q": {"ru": "Химический символ золота?", "en": "Chemical symbol of gold?", "tg": "Аломати химиявии тилло?"},
                "options": {"ru": ["Ag", "Au", "Fe", "Cu"], "en": ["Ag", "Au", "Fe", "Cu"], "tg": ["Ag", "Au", "Fe", "Cu"]},
                "correct": 1
            },
            {
                "q": {"ru": "Реакция горения требует?", "en": "Combustion reaction requires?", "tg": "Реаксияи сӯзиш чӣ лозим дорад?"},
                "options": {"ru": ["Воду", "Кислород", "Азот", "Гелий"], "en": ["Water", "Oxygen", "Nitrogen", "Helium"], "tg": ["Об", "Оксиген", "Нитроген", "Гелий"]},
                "correct": 1
            },
            {
                "q": {"ru": "Молекула состоит из?", "en": "Molecule consists of?", "tg": "Молекула аз чӣ иборат аст?"},
                "options": {"ru": ["Атомов", "Клеток", "Протонов только", "Электронов только"], "en": ["Atoms", "Cells", "Protons only", "Electrons only"], "tg": ["Атомҳо", "Ҳуҷайраҳо", "Танҳо протонҳо", "Танҳо электронҳо"]},
                "correct": 0
            }
        ]
    }
}


def load_faculties_map():
    """Факультеты из БД (создаёт админ)."""
    result = {}
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
        for r in rows:
            result[str(r["id"])] = {
                "ru": r["name_ru"],
                "en": r["name_en"] or r["name_ru"],
                "tg": r["name_tg"] or r["name_ru"],
            }
    except Exception:
        pass
    return result


def load_all_tests():
    """Тесты и вопросы из БД. Если пусто — встроенные TESTS."""
    result = {}
    try:
        with get_db() as conn:
            tests = conn.execute("SELECT * FROM content_tests ORDER BY id").fetchall()
            for t in tests:
                qs = conn.execute(
                    "SELECT * FROM content_questions WHERE test_id = ? ORDER BY sort_order, id",
                    (t["id"],)
                ).fetchall()
                questions = []
                for q in qs:
                    opts_ru = [q["opt_a"], q["opt_b"]]
                    if q["opt_c"]:
                        opts_ru.append(q["opt_c"])
                    if q["opt_d"]:
                        opts_ru.append(q["opt_d"])
                    qtype = "mcq"
                    try:
                        qtype = q["q_type"] or "mcq"
                    except Exception:
                        qtype = "mcq"
                    multi = []
                    try:
                        if q["correct_multi"]:
                            multi = json.loads(q["correct_multi"]) if str(q["correct_multi"]).startswith("[") else [int(x) for x in str(q["correct_multi"]).split(",") if x.strip().isdigit()]
                    except Exception:
                        multi = []
                    match_answer = {}
                    try:
                        if q["match_json"]:
                            match_answer = json.loads(q["match_json"])
                    except Exception:
                        match_answer = {}
                    questions.append({
                        "q": {
                            "ru": q["q_ru"],
                            "en": q["q_en"] or q["q_ru"],
                            "tg": q["q_tg"] or q["q_ru"],
                        },
                        "options": {
                            "ru": opts_ru,
                            "en": opts_ru,
                            "tg": opts_ru,
                        },
                        "correct": int(q["correct_index"] or 0),
                        "q_type": qtype,
                        "correct_multi": multi,
                        "match_answer": match_answer,
                    })
                if not questions:
                    continue
                try:
                    fids = json.loads(t["faculty_ids"] or "[]")
                except Exception:
                    fids = []
                try:
                    _tt = t["test_type"] or "mcq"
                except Exception:
                    _tt = "mcq"
                result[t["code"]] = {
                    "title": {
                        "ru": t["title_ru"],
                        "en": t["title_en"] or t["title_ru"],
                        "tg": t["title_tg"] or t["title_ru"],
                    },
                    "faculties": [str(x) for x in fids],
                    "time_limit": int(t["time_limit"] or 600),
                    "pro_only": bool(t["pro_only"]),
                    "test_type": _tt,
                    "exam_start": (t["exam_start"] if "exam_start" in t.keys() else "") or "",
                    "exam_end": (t["exam_end"] if "exam_end" in t.keys() else "") or "",
                    "published": int(t["published"]) if "published" in t.keys() and t["published"] is not None else 1,
                    "subject_name": (t["subject_name"] if "subject_name" in t.keys() else "") or t["title_ru"],
                    "questions": questions,
                    "_db_id": t["id"],
                }
    except Exception:
        pass
    return result


def get_available_tests(is_pro, user=None):
    """Тесты для студента: опубликованные, с учётом окна экзамена (время устройства)."""
    result = {}
    for tid, t in load_all_tests().items():
        if t.get("pro_only") and not is_pro:
            continue
        # черновики не показываем студентам
        pub = t.get("published")
        if pub is not None and not int(pub) and not t.get("pro_only"):
            # pro_only тренировочные можно оставить; обычные — только published
            continue
        if not (t.get("questions") or []):
            continue
        status, start_dt, end_dt = exam_window_status(t, user)
        t = dict(t)
        t["exam_status"] = status
        t["exam_start_fmt"] = start_dt.strftime("%d.%m.%Y %H:%M") if start_dt else ""
        t["exam_end_fmt"] = end_dt.strftime("%d.%m.%Y %H:%M") if end_dt else ""
        # Уже сдал экзамен — показываем, но без повторного входа
        done = False
        try:
            uid = getattr(user, "id", None) if user is not None else None
            if uid:
                done = has_completed_exam(uid, tid)
        except Exception:
            done = False
        t["exam_done"] = done
        if done:
            t["exam_status"] = "done"
        # На главной: открытые, скоро и уже сданные. Закрытые по времени — скрываем.
        if status == "closed" and not done:
            continue
        result[tid] = t
    return result


def get_test(test_id):
    return load_all_tests().get(test_id)


def calculate_suggestions(test_id, score, max_score, lang="ru"):
    test = get_test(test_id)
    if not test:
        return []
    percent = (score / max_score * 100) if max_score > 0 else 0
    faculties = test.get("faculties") or []
    fmap = load_faculties_map()
    suggestions = []
    for fid in faculties:
        name = fmap.get(str(fid), {}).get(lang) or fmap.get(str(fid), {}).get("ru") or str(fid)
        if percent >= 80:
            chance = {"ru": "Высокие шансы", "en": "High chances", "tg": "Имконияти баланд"}
            level = "high"
        elif percent >= 60:
            chance = {"ru": "Средние шансы", "en": "Medium chances", "tg": "Имконияти миёна"}
            level = "medium"
        elif percent >= 40:
            chance = {"ru": "Низкие шансы, нужна подготовка", "en": "Low chances, need preparation", "tg": "Имконияти паст"}
            level = "low"
        else:
            chance = {"ru": "Нужна серьёзная подготовка", "en": "Serious preparation needed", "tg": "Омодагии ҷиддӣ лозим"}
            level = "very_low"
        suggestions.append({
            "id": str(fid),
            "name": name,
            "chance": chance.get(lang, chance["ru"]),
            "level": level,
            "percent": round(percent, 1),
        })
    return suggestions


# ==================== МАРШРУТЫ ====================

@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    ensure_schema()
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        lang = request.form.get("language", "ru")
        if lang not in ("ru", "en", "tg"):
            lang = "ru"
        device_type = request.form.get("device_type", "mobile")
        if device_type not in ("mobile", "tablet", "desktop"):
            device_type = "mobile"
        timezone = request.form.get("timezone", "").strip()
        valid_tz = {t[0] for t in CIS_TIMEZONES}
        if timezone not in valid_tz:
            flash("Выберите часовой пояс страны СНГ — обязательно", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

        if not full_name or not email or not password:
            flash("Заполните все поля", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

        if password != password2:
            flash("Пароли не совпадают", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

        if len(password) < 6:
            flash("Пароль должен быть не менее 6 символов", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

        try:
            pwd_hash = generate_password_hash(password, method="pbkdf2:sha256")
            with get_db() as conn:
                exists = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                if exists:
                    flash("Email уже зарегистрирован", "error")
                    return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

                insert_ok = False
                last_err = None
                for sql, params in (
                    (
                        "INSERT INTO users (full_name, email, password_hash, language, is_admin, is_pro, free_pro_used, password_plain, device_type, timezone) VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?)",
                        (full_name, email, pwd_hash, lang, password, device_type, timezone),
                    ),
                    (
                        "INSERT INTO users (full_name, email, password_hash, language, is_admin, is_pro, free_pro_used, password_plain, device_type) VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?)",
                        (full_name, email, pwd_hash, lang, password, device_type),
                    ),
                    (
                        "INSERT INTO users (full_name, email, password_hash, language, is_admin, is_pro, free_pro_used) VALUES (?, ?, ?, ?, 0, 0, 0)",
                        (full_name, email, pwd_hash, lang),
                    ),
                    (
                        "INSERT INTO users (full_name, email, password_hash, language) VALUES (?, ?, ?, ?)",
                        (full_name, email, pwd_hash, lang),
                    ),
                ):
                    try:
                        conn.execute(sql, params)
                        insert_ok = True
                        break
                    except Exception as ie:
                        last_err = ie
                        continue

                if not insert_ok:
                    app.logger.exception("register insert failed: %s", last_err)
                    flash(f"Ошибка записи в БД. Откройте /api/init-db. Детали в логах.", "error")
                    return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

                # Надёжно получаем id по email (работает и в SQLite, и в Postgres)
                row_tmp = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                user_id = row_tmp["id"] if row_tmp else None
                if not user_id:
                    flash("Ошибка создания аккаунта. Попробуйте снова.", "error")
                    return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

                try:
                    conn.execute(
                        "UPDATE users SET last_login = ?, timezone = ? WHERE id = ?",
                        (datetime.now().isoformat(), timezone, user_id)
                    )
                except Exception:
                    try:
                        conn.execute(
                            "UPDATE users SET last_login = ? WHERE id = ?",
                            (datetime.now().isoformat(), user_id)
                        )
                    except Exception:
                        pass

                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

            if not row:
                flash("Ошибка создания аккаунта. Попробуйте снова.", "error")
                return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

            user = User(row)
            login_user(user, remember=True)
            session["show_instagram_offer"] = True
            session["tz"] = timezone
            return redirect(url_for("dashboard"))
        except Exception as e:
            app.logger.exception("register failed: %s", e)
            flash(f"Ошибка регистрации: {str(e)[:180]}", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

    return render_template("register.html", lang="ru", cis_timezones=CIS_TIMEZONES)



@app.route("/api/init-db", methods=["POST", "GET"])
def api_init_db():
    """Принудительно создать таблицы и админа (после нового Postgres)."""
    global _schema_ready
    _schema_ready = False
    try:
        _create_core_tables()
        try:
            init_db()
        except Exception as e:
            print("api_init_db init_db:", e)
        ensure_admin()
        _schema_ready = False
        ensure_schema()
        with get_db() as conn:
            ok = _table_exists(conn, "users")
            admin = None
            cnt = 0
            if ok:
                admin = conn.execute(
                    "SELECT id, email, is_admin FROM users WHERE email = ?",
                    ("admin@hgu.tj",),
                ).fetchone()
                try:
                    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
                    cnt = int(row["c"] if row and row["c"] is not None else (row[0] if row else 0))
                except Exception:
                    cnt = 0
        return jsonify({
            "ok": ok and bool(admin),
            "users_table": ok,
            "admin_exists": bool(admin),
            "users_count": cnt,
            "database": "postgres" if DATABASE_URL else "sqlite",
            "login": "admin@hgu.tj",
            "password": "admin123 (or ADMIN_PASSWORD env)",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/health")
def api_health():
    try:
        ensure_schema()
    except Exception:
        pass
    db_kind = "postgres" if DATABASE_URL else "sqlite"
    ok = False
    err = ""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
            ok = True
    except Exception as e:
        err = str(e)
    return jsonify({"ok": ok, "database": db_kind, "error": err or None})


@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_schema()
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "") or ""
        user = None
        is_adm = False
        try:
            with get_db() as conn:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if not row and email:
                    try:
                        row = conn.execute(
                            "SELECT * FROM users WHERE lower(email) = ?", (email,)
                        ).fetchone()
                    except Exception:
                        row = None

                ok = False
                if row:
                    ph = row["password_hash"] or ""
                    try:
                        if ph and check_password_hash(ph, password):
                            ok = True
                    except Exception:
                        ok = False
                    if not ok:
                        plain = row["password_plain"] or ""
                        if plain and plain == password:
                            ok = True
                            try:
                                conn.execute(
                                    "UPDATE users SET password_hash = ? WHERE id = ?",
                                    (generate_password_hash(password, method="pbkdf2:sha256"), row["id"])
                                )
                            except Exception:
                                pass

                if ok and row:
                    user = User(row)
                    if user.email == "admin@hgu.tj" and not user.is_admin:
                        try:
                            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user.id,))
                            user.is_admin = True
                        except Exception:
                            pass
                    try:
                        conn.execute(
                            "UPDATE users SET last_login = ? WHERE id = ?",
                            (datetime.now().isoformat(), user.id)
                        )
                    except Exception:
                        pass
                    is_adm = bool(user.is_admin)
                    login_user(user, remember=True)
                else:
                    flash("Неверный email или пароль", "error")
        except Exception as e:
            app.logger.exception("login failed: %s", e)
            flash(f"Ошибка входа: {str(e)[:180]}", "error")
            return render_template("login.html", lang="ru")

        if user is not None:
            try:
                user.check_pro()
            except Exception:
                pass
            if is_adm:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))

    return render_template("login.html", lang="ru")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    current_user.check_pro()
    lang = current_user.language
    tests = get_available_tests(current_user.is_pro, current_user)

    # Последние результаты
    with get_db() as conn:
        results = conn.execute(
            "SELECT * FROM test_results WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (current_user.id,)
        ).fetchall()

        # Уведомления
        notifs = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
            (current_user.id,)
        ).fetchall()

        global_notifs = conn.execute(
            "SELECT * FROM global_notifications ORDER BY created_at DESC LIMIT 5"
        ).fetchall()

    show_instagram = session.pop("show_instagram_offer", False)
    show_buy_pro = False
    if not current_user.is_pro and current_user.free_pro_used:
        # Проверяем, закончился ли бесплатный Pro недавно
        show_buy_pro = True

    return render_template(
        "dashboard.html",
        lang=lang,
        tests=tests,
        results=results,
        notifs=notifs,
        global_notifs=global_notifs,
        faculties=load_faculties_map(),
        show_instagram=show_instagram,
        show_buy_pro=show_buy_pro,
        instagram_url=INSTAGRAM_URL,
        is_pro=current_user.is_pro,
        pro_until=current_user.pro_until
    )


@app.route("/test/<test_id>")
@login_required
def start_test(test_id):
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    current_user.check_pro()
    test = get_test(test_id)
    if not test:
        flash("Тест не найден", "error")
        return redirect(url_for("dashboard"))

    if test.get("pro_only") and not current_user.is_pro:
        flash("Этот тест доступен только в Pro режиме", "error")
        return redirect(url_for("dashboard"))

    mode = request.args.get("mode", "exam")
    if mode not in ("exam", "practice"):
        mode = "exam"
    if mode == "practice" and not current_user.is_pro:
        flash("Режим тренировки доступен только с Pro", "error")
        return redirect(url_for("dashboard"))
    if mode == "exam" and not current_user.is_admin:
        if has_completed_exam(current_user.id, test_id):
            flash("Вы уже прошли этот экзамен", "error")
            return redirect(url_for("dashboard"))
        lim = max_exam_attempts_per_day()
        if lim > 0:
            used = count_attempts_today(current_user.id, test_id)
            if used >= lim:
                flash(f"Лимит попыток на сегодня: {lim}. Завтра можно снова.", "error")
                return redirect(url_for("dashboard"))
        # Окно экзамена — по часовому поясу устройства студента
        if test.get("published") is not None and not int(test.get("published") or 0) and not test.get("pro_only"):
            flash("Этот тест ещё не опубликован", "error")
            return redirect(url_for("dashboard"))
        status, start_dt, end_dt = exam_window_status(test, current_user)
        if status == "upcoming":
            when = start_dt.strftime("%d.%m.%Y %H:%M") if start_dt else ""
            flash(f"Экзамен ещё не начался. Открытие: {when} (ваше местное время)", "error")
            return redirect(url_for("dashboard"))
        if status == "closed":
            when = end_dt.strftime("%d.%m.%Y %H:%M") if end_dt else ""
            flash(f"Экзамен уже завершён (до {when})", "error")
            return redirect(url_for("dashboard"))

    import random
    questions = list(test["questions"])
    # Экзамен: ровно до 50 вопросов (если больше — случайная выборка)
    if mode == "exam" and len(questions) > EXAM_QUESTION_COUNT:
        questions = random.sample(questions, EXAM_QUESTION_COUNT)
    order = list(range(len(questions)))
    random.shuffle(order)
    shuffled = []
    for oi in order:
        q = dict(questions[oi])
        q["_orig_index"] = oi
        shuffled.append(q)
    test_view = dict(test)
    test_view["questions"] = shuffled
    # таймер: если задано окно exam_end — оставшееся время до конца
    time_limit = int(test.get("time_limit") or 600)
    try:
        if mode == "exam":
            status, start_dt, end_dt = exam_window_status(test, current_user)
            if end_dt:
                now = now_for_user(current_user)
                left = int((end_dt - now).total_seconds())
                if left > 0:
                    time_limit = min(time_limit, left) if time_limit else left
    except Exception:
        pass

    lang = current_user.language
    hints_left = int(getattr(current_user, "hints_left", 0) or 0) if current_user.is_pro else 0
    if mode == "exam":
        session["in_exam"] = True
    else:
        session.pop("in_exam", None)
    return render_template(
        "test.html",
        lang=lang,
        test_id=test_id,
        test=test_view,
        time_limit=time_limit,
        mode=mode,
        sound_enabled=getattr(current_user, "sound_enabled", True),
        theme=getattr(current_user, "theme", "light"),
        question_order=order,
        hints_left=hints_left,
        is_pro=current_user.is_pro,
    )



@app.route("/api/use_hint", methods=["POST"])
@login_required
def use_hint():
    """PRO: убрать 2 неверных варианта, оставить 1 верный + 1 неверный."""
    if not current_user.is_pro and not current_user.is_admin:
        return jsonify({"error": "Pro required"}), 403
    data = request.get_json() or {}
    test_id = data.get("test_id")
    q_index = int(data.get("q_index", -1))  # index in shuffled list
    order = data.get("order") or []
    test = get_test(test_id)
    if not test:
        return jsonify({"error": "Test not found"}), 404
    questions = test["questions"]
    if order and 0 <= q_index < len(order):
        orig = int(order[q_index])
    else:
        orig = q_index
    if orig < 0 or orig >= len(questions):
        return jsonify({"error": "Bad question"}), 400

    with get_db() as conn:
        row = conn.execute("SELECT hints_left, is_pro FROM users WHERE id = ?", (current_user.id,)).fetchone()
        hints = int(row["hints_left"] or 0) if row else 0
        if not current_user.is_admin and hints <= 0:
            return jsonify({"error": "no_hints", "hints_left": 0}), 403
        q = questions[orig]
        correct = int(q["correct"])
        opts = q.get("options") or {}
        if isinstance(opts, dict):
            n = len(opts.get("ru") or opts.get(list(opts.keys())[0]) if opts else [])
        else:
            n = len(opts)
        wrong = [i for i in range(n) if i != correct]
        import random
        if len(wrong) >= 2:
            remove = random.sample(wrong, 2)
        elif len(wrong) == 1:
            remove = wrong
        else:
            remove = []
        keep_wrong = [i for i in wrong if i not in remove]
        keep = [correct] + keep_wrong[:1]
        keep = sorted(set(keep))
        if not current_user.is_admin:
            conn.execute("UPDATE users SET hints_left = hints_left - 1 WHERE id = ? AND hints_left > 0", (current_user.id,))
            hints = max(0, hints - 1)
        current_user.hints_left = hints
    return jsonify({"ok": True, "remove": remove, "keep": keep, "hints_left": hints})



@app.route("/api/push/vapid_public")
def push_vapid_public():
    _ensure_vapid()
    return jsonify({"publicKey": VAPID_PUBLIC_KEY or ""})


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json() or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh") or data.get("p256dh") or ""
    auth = keys.get("auth") or data.get("auth") or ""
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "bad subscription"}), 400
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                   VALUES (?, ?, ?, ?)""",
                (current_user.id, endpoint, p256dh, auth)
            )
        except Exception:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            conn.execute(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)",
                (current_user.id, endpoint, p256dh, auth)
            )
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json() or {}
    endpoint = (data.get("endpoint") or "").strip()
    with get_db() as conn:
        if endpoint:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (current_user.id, endpoint)
            )
        else:
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (current_user.id,))
    return jsonify({"ok": True})


@app.route("/api/submit_test", methods=["POST"])
@login_required
def submit_test():
    data = request.get_json() or {}
    test_id = data.get("test_id")
    answers = data.get("answers", {})
    duration = data.get("duration", 0)
    mode = data.get("mode", "exam")
    order = data.get("order")  # shuffled indices

    test = get_test(test_id)
    if not test:
        return jsonify({"error": "Test not found"}), 404
    if test["pro_only"] and not current_user.is_pro:
        return jsonify({"error": "Pro required"}), 403
    if mode == "practice" and not current_user.is_pro and not current_user.is_admin:
        return jsonify({"error": "Practice is Pro only"}), 403
    if mode == "exam" and not current_user.is_admin:
        if has_completed_exam(current_user.id, test_id):
            flash("Вы уже прошли этот экзамен", "error")
            return redirect(url_for("dashboard"))
        lim = max_exam_attempts_per_day()
        if lim > 0 and count_attempts_today(current_user.id, test_id) >= lim:
            return jsonify({"error": "daily_limit"}), 403

    questions = test["questions"]
    # order maps display position -> original index
    if order and isinstance(order, list) and len(order) == len(questions):
        q_list = [(int(oi), questions[int(oi)]) for oi in order if 0 <= int(oi) < len(questions)]
    else:
        q_list = list(enumerate(questions))

    correct = 0
    incorrect = 0
    score = 0.0
    max_score = len(q_list) * float(POINTS_CORRECT)
    detailed = []
    lang = current_user.language

    for disp_i, (orig_i, q) in enumerate(q_list):
        qtype = q.get("q_type") or "mcq"
        selected = answers.get(str(disp_i))
        if selected is None:
            selected = answers.get(str(orig_i))

        is_correct = False
        if qtype == "multi":
            # selected: list of indices or comma string
            if isinstance(selected, list):
                sel_set = set(int(x) for x in selected)
            elif isinstance(selected, str) and selected:
                sel_set = set(int(x) for x in selected.split(",") if x.strip().isdigit())
            else:
                sel_set = set()
            multi = q.get("correct_multi") or []
            if isinstance(multi, str) and multi:
                try:
                    multi = json.loads(multi)
                except Exception:
                    multi = [int(x) for x in multi.split(",") if x.strip().isdigit()]
            correct_set = set(int(x) for x in multi)
            is_correct = sel_set == correct_set and len(correct_set) > 0
        elif qtype == "match":
            # selected: {left_idx: right_idx}
            pairs = q.get("match_pairs") or []
            if isinstance(selected, dict):
                ok = 0
                total_p = len(pairs)
                for li, ri in enumerate(pairs):
                    # pairs as list of correct right indices for left order 0..n
                    pass
                # store match as list of correct right for left 0..n-1
                correct_map = q.get("match_answer") or {}
                if isinstance(correct_map, str):
                    try:
                        correct_map = json.loads(correct_map)
                    except Exception:
                        correct_map = {}
                is_correct = True
                for k, v in correct_map.items():
                    if str(selected.get(str(k), selected.get(int(k) if str(k).isdigit() else k, None))) != str(v):
                        is_correct = False
                        break
                if not correct_map:
                    is_correct = False
            else:
                is_correct = False
        else:
            # mcq single
            try:
                sel_i = int(selected) if selected is not None else None
            except Exception:
                sel_i = None
            is_correct = sel_i is not None and sel_i == int(q["correct"])

        if is_correct:
            correct += 1
            score += float(POINTS_CORRECT)
        else:
            incorrect += 1
            score += float(POINTS_WRONG)

        opts = q.get("options", {})
        if isinstance(opts, dict):
            opts = opts.get(lang) or opts.get("ru") or []
        detailed.append({
            "index": orig_i,
            "question": (q.get("q") or {}).get(lang) or (q.get("q") or {}).get("ru", ""),
            "options": opts,
            "selected": selected,
            "correct": q.get("correct"),
            "correct_multi": q.get("correct_multi"),
            "q_type": qtype,
            "is_correct": is_correct
        })

    # Экзамен: максимум всегда 100 (50×2). Неотвеченные уже в incorrect.
    if mode == "exam":
        max_score = float(EXAM_QUESTION_COUNT * POINTS_CORRECT)
    percent = score_percent(score, max_score)
    grade = letter_grade(percent)
    suggestions = calculate_suggestions(test_id, score, max_score, current_user.language)
    session.pop("in_exam", None)

    created_at = now_for_user(current_user).isoformat()
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO test_results
                   (user_id, test_id, score, max_score, correct, incorrect, answers_json, suggested_faculties, duration_seconds, mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (current_user.id, test_id, score, max_score, correct, incorrect,
                 json.dumps(detailed, ensure_ascii=False), json.dumps(suggestions, ensure_ascii=False),
                 duration, mode, created_at)
            )
        except Exception:
            try:
                conn.execute(
                    """INSERT INTO test_results
                       (user_id, test_id, score, max_score, correct, incorrect, answers_json, suggested_faculties, duration_seconds, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current_user.id, test_id, score, max_score, correct, incorrect,
                     json.dumps(detailed, ensure_ascii=False), json.dumps(suggestions, ensure_ascii=False), duration, created_at)
                )
            except Exception:
                conn.execute(
                    """INSERT INTO test_results
                       (user_id, test_id, score, max_score, correct, incorrect, answers_json, suggested_faculties, duration_seconds)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current_user.id, test_id, score, max_score, correct, incorrect,
                     json.dumps(detailed, ensure_ascii=False), json.dumps(suggestions, ensure_ascii=False), duration)
                )
        try:
            result_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            row = conn.execute(
                "SELECT id FROM test_results WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (current_user.id,),
            ).fetchone()
            result_id = row["id"] if row else 0

    # In-app + push уведомление о результате
    try:
        admins = []
        with get_db() as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (current_user.id, f"Результат: {grade}",
                 f"Тест «{test_id}»: {round(score,1)}/{max_score} ({percent}%), оценка {grade}")
            )
            admins = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchall()
            for a in admins:
                conn.execute(
                    "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                    (a["id"], "Новый результат",
                     f"{current_user.full_name}: {test_id} — {grade} ({percent}%)")
                )
        send_push_to_user(
            current_user.id,
            f"ХГУ Тест — оценка {grade}",
            f"Балл {round(score,1)} из {max_score} ({percent}%)",
            f"/result/{result_id}"
        )
        for a in admins:
            send_push_to_user(
                a["id"],
                "Новый результат теста",
                f"{current_user.full_name}: {grade} ({percent}%)",
                "/admin"
            )
    except Exception as ex:
        try:
            app.logger.info("notify result: %s", ex)
        except Exception:
            print("notify result:", ex)

    return jsonify({
        "result_id": result_id,
        "score": round(score, 1),
        "max_score": max_score,
        "correct": correct,
        "incorrect": incorrect,
        "percent": percent,
        "grade": grade,
        "suggestions": suggestions
    })


@app.route("/result/<int:result_id>")
@login_required
def view_result(result_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM test_results WHERE id = ? AND user_id = ?",
            (result_id, current_user.id)
        ).fetchone()
    if not row:
        flash("Результат не найден", "error")
        return redirect(url_for("dashboard"))

    test = get_test(row["test_id"]) or {}
    suggestions = json.loads(row["suggested_faculties"] or "[]")
    try:
        details = json.loads(row["answers_json"] or "[]")
    except Exception:
        details = []
    percent = 0
    try:
        percent = score_percent(row["score"], row["max_score"])
    except Exception:
        percent = 0
    grade = letter_grade(percent)
    # Разбор ответов только для PRO
    show_details = bool(current_user.is_pro or current_user.is_admin)
    return render_template(
        "result.html",
        lang=current_user.language,
        result=row,
        test=test,
        suggestions=suggestions,
        details=details if show_details else [],
        show_details=show_details,
        percent=percent,
        grade=grade,
        is_pro=current_user.is_pro,
    )


# ==================== PRO РЕЖИМ ====================

@app.route("/pro")
@login_required
def pro_page():
    current_user.check_pro()
    return render_template(
        "pro.html",
        lang=current_user.language,
        is_pro=current_user.is_pro,
        pro_until=current_user.pro_until,
        price=PRO_PRICE,
        packages=PRO_PACKAGES,
        admin_card=get_payment_settings(),
        free_used=current_user.free_pro_used
    )


@app.route("/pro/buy", methods=["POST"])
@login_required
def buy_pro():
    """Только ручная оплата: скриншот → админ проверяет."""
    payment_method = request.form.get("payment_method", "dc")
    package = request.form.get("package", "2m")
    if package not in PRO_PACKAGES:
        package = "2m"
    pkg = PRO_PACKAGES[package]

    if "screenshot" not in request.files:
        flash("Загрузите скриншот оплаты", "error")
        return redirect(url_for("pro_page"))
    f = request.files["screenshot"]
    if not f or not f.filename:
        flash("Загрузите скриншот оплаты", "error")
        return redirect(url_for("pro_page"))
    ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    filename = f"{current_user.id}_{int(datetime.now().timestamp())}{ext}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    f.save(path)

    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO pro_requests (user_id, payment_method, screenshot_path, status, package, duration_days) VALUES (?, ?, ?, 'pending', ?, ?)",
                (current_user.id, payment_method, filename, package, pkg["days"])
            )
        except Exception:
            conn.execute(
                "INSERT INTO pro_requests (user_id, payment_method, screenshot_path, status) VALUES (?, ?, ?, 'pending')",
                (current_user.id, payment_method, filename)
            )
        try:
            admin = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchone()
            if admin:
                conn.execute(
                    "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                    (admin["id"], "Заявка на Pro",
                     f"{current_user.full_name} — пакет {package}, {payment_method}, {pkg['price']} сом.")
                )
        except Exception:
            pass
    flash("Заявка отправлена. Админ проверит оплату по скриншоту.", "success")
    return redirect(url_for("pro_page"))



@app.route("/pro/instagram", methods=["POST"])
@login_required
def claim_instagram_pro():
    if current_user.free_pro_used:
        flash("Бесплатный Pro уже использован", "error")
        return redirect(url_for("dashboard"))

    until = (datetime.now() + timedelta(days=FREE_PRO_DAYS)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_pro = 1, pro_until = ?, free_pro_used = 1, hints_left = 1 WHERE id = ?",
            (until, current_user.id)
        )
        conn.execute(
            "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
            (current_user.id, "Pro активирован", f"Вам предоставлен бесплатный Pro на {FREE_PRO_DAYS} дня за подписку на Instagram.")
        )

    flash(f"Pro активирован на {FREE_PRO_DAYS} дня!", "success")
    return redirect(url_for("dashboard"))


# ==================== НАСТРОЙКИ ====================

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "change_password":
            old = request.form.get("old_password", "")
            new = request.form.get("new_password", "")
            new2 = request.form.get("new_password2", "")
            face_user = current_user.email.endswith("@hgu.local")
            old_ok = check_password_hash(current_user.password_hash, old) or (face_user and not old)
            if not old_ok and not face_user:
                flash("Неверный текущий пароль", "error")
            elif face_user and not old and not check_password_hash(current_user.password_hash, old):
                # первый пароль после Face ID — можно без старого
                pass
            if new != new2:
                flash("Новые пароли не совпадают", "error")
            elif len(new) < 6:
                flash("Пароль должен быть не менее 6 символов", "error")
            elif old_ok or face_user:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new, method="pbkdf2:sha256"), current_user.id)
                    )
                flash("Пароль изменён", "success")

        elif action == "change_language":
            lang = request.form.get("language", "ru")
            if lang in ("ru", "en", "tg"):
                with get_db() as conn:
                    conn.execute("UPDATE users SET language = ? WHERE id = ?", (lang, current_user.id))
                current_user.language = lang
                flash("Язык изменён", "success")

        elif action == "change_theme":
            theme = request.form.get("theme", "light")
            if theme in ("light", "dark"):
                with get_db() as conn:
                    try:
                        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, current_user.id))
                    except Exception:
                        pass
                current_user.theme = theme
                flash("Тема изменена", "success")

        elif action == "change_sound":
            sound = 1 if request.form.get("sound_enabled") == "1" else 0
            with get_db() as conn:
                try:
                    conn.execute("UPDATE users SET sound_enabled = ? WHERE id = ?", (sound, current_user.id))
                except Exception:
                    pass
            current_user.sound_enabled = bool(sound)
            flash("Настройка звука сохранена", "success")

        elif action == "change_name":
            full_name = request.form.get("full_name", "").strip()
            if len(full_name) < 2:
                flash("Укажите ФИО", "error")
            else:
                with get_db() as conn:
                    conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name, current_user.id))
                current_user.full_name = full_name
                flash("ФИО обновлено", "success")

        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        lang=current_user.language,
        theme=getattr(current_user, "theme", "light"),
        sound_enabled=getattr(current_user, "sound_enabled", True),
        full_name=current_user.full_name,
        instagram_url=INSTAGRAM_URL
    )


@app.route("/leaderboard")
@login_required
def leaderboard():
    """Только свой рейтинг / свои результаты."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tr.test_id, tr.score, tr.max_score, tr.correct, tr.incorrect, tr.created_at
               FROM test_results tr
               WHERE tr.user_id = ?
               ORDER BY tr.created_at DESC
               LIMIT 30""",
            (current_user.id,)
        ).fetchall()
        best = conn.execute(
            """SELECT MAX(score * 1.0 / CASE WHEN max_score=0 THEN 1 ELSE max_score END) as best_pct,
                      MAX(score) as best_score
               FROM test_results WHERE user_id = ?""",
            (current_user.id,)
        ).fetchone()
    tz = getattr(current_user, "timezone", None) or "Asia/Dushanbe"
    enriched = []
    for r in rows:
        pct = score_percent(r["score"], r["max_score"])
        # normalize display max to 100 if needed
        enriched.append({
            "test_id": r["test_id"],
            "score": float(r["score"] or 0),
            "max_score": float(r["max_score"] or 100) if float(r["max_score"] or 0) > 0 else 100.0,
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "created_at": r["created_at"],
            "_grade": letter_grade(pct),
            "_dt": format_dt(r["created_at"], tz),
        })
    best_grade = None
    if best and best["best_pct"] is not None:
        best_grade = letter_grade(float(best["best_pct"] or 0) * 100)
        try:
            best = dict(best) if not isinstance(best, dict) else best
        except Exception:
            pass
        # attach grade
        class _B: pass
        b = _B()
        b.best_pct = best["best_pct"] if not hasattr(best, "best_pct") else best["best_pct"]
        try:
            b.best_pct = best["best_pct"]
            b.best_score = best["best_score"]
        except Exception:
            b.best_pct = getattr(best, "best_pct", 0)
            b.best_score = getattr(best, "best_score", 0)
        b._grade = best_grade
        best = b
    return render_template(
        "leaderboard.html",
        lang=current_user.language,
        rows=enriched,
        tests=load_all_tests(),
        best=best
    )



# ==================== АДМИН: ФАКУЛЬТЕТЫ И ВОПРОСЫ ====================

@app.route("/admin/content")
@login_required
@admin_required
def admin_content():
    with get_db() as conn:
        faculties = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
        tests = conn.execute("SELECT * FROM content_tests ORDER BY id").fetchall()
        qcounts = {}
        for t in tests:
            qcounts[t["id"]] = conn.execute(
                "SELECT COUNT(*) FROM content_questions WHERE test_id = ?", (t["id"],)
            ).fetchone()[0]
    return render_template(
        "admin/content.html",
        faculties=faculties,
        tests=tests,
        qcounts=qcounts,
    )


@app.route("/admin/faculty/add", methods=["POST"])
@login_required
@admin_required
def admin_faculty_add():
    name_ru = request.form.get("name_ru", "").strip()
    name_en = request.form.get("name_en", "").strip()
    name_tg = request.form.get("name_tg", "").strip()
    if not name_ru:
        flash("Укажите название факультета", "error")
        return redirect(url_for("admin_content"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO faculties (name_ru, name_en, name_tg) VALUES (?, ?, ?)",
            (name_ru, name_en or name_ru, name_tg or name_ru),
        )
    flash("Факультет добавлен", "success")
    return redirect(url_for("admin_content"))


@app.route("/admin/faculty/<int:fid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_faculty_delete(fid):
    with get_db() as conn:
        conn.execute("DELETE FROM faculties WHERE id = ?", (fid,))
    flash("Факультет удалён", "success")
    return redirect(url_for("admin_content"))


@app.route("/admin/test/add", methods=["POST"])
@login_required
@admin_required
def admin_test_add():
    ensure_schema()
    for sql in (
        "ALTER TABLE content_tests ADD COLUMN test_type TEXT DEFAULT 'mcq'",
        "ALTER TABLE content_tests ADD COLUMN exam_start TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN exam_end TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN published INTEGER DEFAULT 0",
        "ALTER TABLE content_tests ADD COLUMN subject_name TEXT DEFAULT ''",
    ):
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception:
            pass
    try:
        title_ru = request.form.get("title_ru", "").strip()
        code = request.form.get("code", "").strip().replace(" ", "_")
        try:
            time_limit = int(request.form.get("time_limit") or 3600)
        except Exception:
            time_limit = 3600
        pro_only = 1 if request.form.get("pro_only") == "1" else 0
        faculty_ids = request.form.getlist("faculty_ids")
        test_type = request.form.get("test_type", "mcq")
        exam_start = request.form.get("exam_start", "").strip()
        exam_end = request.form.get("exam_end", "").strip()
        subject_name = request.form.get("subject_name", "").strip() or title_ru
        if test_type not in ("mcq", "multi", "match"):
            test_type = "mcq"
        if not title_ru or not code:
            flash("Укажите код и название теста", "error")
            return redirect(url_for("admin_content"))
        if not faculty_ids:
            flash("Выберите хотя бы один факультет", "error")
            return redirect(url_for("admin_content"))
        with get_db() as conn:
            ok = False
            last_err = None
            for sql, params in (
                (
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids, test_type, exam_start, exam_end, published, subject_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids), test_type, exam_start, exam_end, subject_name),
                ),
                (
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids, test_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids), test_type),
                ),
                (
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids)),
                ),
            ):
                try:
                    conn.execute(sql, params)
                    ok = True
                    break
                except Exception as e:
                    last_err = e
            if not ok:
                flash(f"Ошибка: возможно код уже есть. {last_err}", "error")
                return redirect(url_for("admin_content"))
        type_names = {"mcq": "один ответ", "multi": "два ответа", "match": "соединение"}
        flash(f"Тест создан ({type_names.get(test_type)}). Добавьте вопросы этого типа.", "success")
        return redirect(url_for("admin_test_edit", code=code))
    except Exception as e:
        app.logger.exception("admin_test_add: %s", e)
        flash(f"Ошибка создания теста: {e}", "error")
        return redirect(url_for("admin_content"))


@app.route("/admin/test/<code>/delete", methods=["POST"])
@login_required
@admin_required
def admin_test_delete(code):
    with get_db() as conn:
        t = conn.execute("SELECT id FROM content_tests WHERE code = ?", (code,)).fetchone()
        if t:
            conn.execute("DELETE FROM content_questions WHERE test_id = ?", (t["id"],))
            conn.execute("DELETE FROM content_tests WHERE id = ?", (t["id"],))
    flash("Тест удалён", "success")
    return redirect(url_for("admin_content"))



def parse_questions_file(file_storage, q_type="mcq"):
    """Парсинг txt/csv/json/docx → список вопросов. Не ZIP."""
    import csv, io, re
    name = (file_storage.filename or "").lower()
    if name.endswith(".zip"):
        raise ValueError("ZIP не поддерживается")
    raw = file_storage.read()
    questions = []

    def add_mcq(qtext, opts, correct_idx=0, multi=None):
        opts = [o.strip() for o in opts if o and str(o).strip()]
        while len(opts) < 2:
            opts.append("—")
        opts = opts[:4]
        a = opts[0] if len(opts) > 0 else ""
        b = opts[1] if len(opts) > 1 else ""
        c = opts[2] if len(opts) > 2 else ""
        d = opts[3] if len(opts) > 3 else ""
        questions.append({
            "q_ru": qtext.strip(),
            "opt_a": a, "opt_b": b, "opt_c": c, "opt_d": d,
            "correct_index": int(correct_idx) if multi is None else 0,
            "q_type": q_type,
            "correct_multi": multi or [],
            "match_json": "",
        })

    if name.endswith(".json"):
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
        items = data if isinstance(data, list) else data.get("questions", [])
        for it in items:
            qtext = it.get("q") or it.get("question") or it.get("text") or ""
            opts = it.get("options") or it.get("opts") or []
            if isinstance(opts, dict):
                opts = [opts.get("a") or opts.get("A"), opts.get("b") or opts.get("B"),
                        opts.get("c") or opts.get("C"), opts.get("d") or opts.get("D")]
            correct = it.get("correct", it.get("answer", 0))
            multi = it.get("correct_multi")
            if multi is not None:
                add_mcq(qtext, opts, 0, multi)
            else:
                if isinstance(correct, str) and correct.upper() in "ABCD":
                    correct = "ABCD".index(correct.upper())
                add_mcq(qtext, opts, int(correct) if str(correct).isdigit() else 0)
    elif name.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if rows and rows[0] and "question" in (rows[0][0] or "").lower():
            rows = rows[1:]
        for row in rows:
            if len(row) < 3:
                continue
            qtext, opts = row[0], row[1:5]
            correct = 0
            if len(row) > 5 and str(row[5]).strip().isdigit():
                correct = int(row[5])
            elif len(row) > 5 and str(row[5]).strip().upper() in "ABCD":
                correct = "ABCD".index(str(row[5]).strip().upper())
            add_mcq(qtext, opts, correct)
    else:
        # txt / docx as text: блоки Q: ... A) B) C) D) Answer: A
        text = raw.decode("utf-8-sig", errors="replace")
        if name.endswith(".docx"):
            try:
                import zipfile
                z = zipfile.ZipFile(io.BytesIO(raw))
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "\n", xml)
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        blocks = re.split(r"\n\s*\n", text)
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if len(lines) < 3:
                continue
            qtext = re.sub(r"^\d+[\).\:\-]\s*", "", lines[0])
            qtext = re.sub(r"^[Qq][:\.]?\s*", "", qtext)
            opts = []
            correct = 0
            for ln in lines[1:]:
                m = re.match(r"^[A-Da-d][\).\:\-]\s*(.+)$", ln)
                if m:
                    opts.append(m.group(1))
                    continue
                m2 = re.match(r"^(?:Answer|Ответ|Правильный)[:\s]+([A-Da-d0-3])", ln, re.I)
                if m2:
                    v = m2.group(1).upper()
                    correct = "ABCD".index(v) if v in "ABCD" else int(v)
            if opts:
                add_mcq(qtext, opts, correct)
    return questions


@app.route("/admin/test/<code>/import", methods=["POST"])
@login_required
@admin_required
def admin_test_import(code):
    with get_db() as conn:
        trow = conn.execute("SELECT * FROM content_tests WHERE code = ?", (code,)).fetchone()
        if not trow:
            flash("Тест не найден", "error")
            return redirect(url_for("admin_content"))
        if "file" not in request.files:
            flash("Выберите файл", "error")
            return redirect(url_for("admin_test_edit", code=code))
        f = request.files["file"]
        if not f.filename:
            flash("Файл пустой", "error")
            return redirect(url_for("admin_test_edit", code=code))
        if f.filename.lower().endswith(".zip"):
            flash("ZIP нельзя. Используйте TXT, CSV, JSON или DOCX", "error")
            return redirect(url_for("admin_test_edit", code=code))
        try:
            qtype = "mcq"
            try:
                qtype = trow["test_type"] or "mcq"
            except Exception:
                pass
            items = parse_questions_file(f, qtype)
        except Exception as e:
            flash(f"Ошибка чтения файла: {e}", "error")
            return redirect(url_for("admin_test_edit", code=code))
        if not items:
            flash("В файле не найдено вопросов. Формат: вопрос + варианты A B C D", "error")
            return redirect(url_for("admin_test_edit", code=code))
        n = 0
        for it in items:
            multi = json.dumps(it.get("correct_multi") or [])
            try:
                conn.execute(
                    """INSERT INTO content_questions
                       (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index, q_type, correct_multi, match_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trow["id"], it["q_ru"], it["q_ru"], it["q_ru"],
                     it["opt_a"], it["opt_b"], it["opt_c"], it["opt_d"],
                     it["correct_index"], it["q_type"], multi, it.get("match_json") or "")
                )
            except Exception:
                conn.execute(
                    """INSERT INTO content_questions
                       (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trow["id"], it["q_ru"], it["q_ru"], it["q_ru"],
                     it["opt_a"], it["opt_b"], it["opt_c"], it["opt_d"], it["correct_index"])
                )
            n += 1
        flash(f"Загружено вопросов: {n}. Проверьте и нажмите «Опубликовать».", "success")
    return redirect(url_for("admin_test_edit", code=code))


@app.route("/admin/test/<code>/publish", methods=["POST"])
@login_required
@admin_required
def admin_test_publish(code):
    try:
        faculty_ids = request.form.getlist("faculty_ids")
        if not faculty_ids:
            flash("Выберите хотя бы один факультет для публикации", "error")
            return redirect(url_for("admin_test_edit", code=code))
        exam_start = request.form.get("exam_start", "").strip()
        exam_end = request.form.get("exam_end", "").strip()
        subject_name = request.form.get("subject_name", "").strip()

        # ALTER в отдельных соединениях — иначе Postgres abort'ит всю транзакцию
        for sql in (
            "ALTER TABLE content_tests ADD COLUMN published INTEGER DEFAULT 0",
            "ALTER TABLE content_tests ADD COLUMN exam_start TEXT DEFAULT ''",
            "ALTER TABLE content_tests ADD COLUMN exam_end TEXT DEFAULT ''",
            "ALTER TABLE content_tests ADD COLUMN subject_name TEXT DEFAULT ''",
        ):
            try:
                with get_db() as conn:
                    conn.execute(sql)
            except Exception:
                pass

        ok = False
        last_err = None
        for sql, params in (
            (
                """UPDATE content_tests SET published=1, faculty_ids=?, exam_start=?, exam_end=?, subject_name=? WHERE code=?""",
                (json.dumps(faculty_ids), exam_start, exam_end, subject_name, code),
            ),
            (
                """UPDATE content_tests SET published=1, faculty_ids=?, exam_start=?, exam_end=? WHERE code=?""",
                (json.dumps(faculty_ids), exam_start, exam_end, code),
            ),
            (
                """UPDATE content_tests SET published=1, faculty_ids=? WHERE code=?""",
                (json.dumps(faculty_ids), code),
            ),
        ):
            try:
                with get_db() as conn:
                    conn.execute(sql, params)
                ok = True
                break
            except Exception as e:
                last_err = e
                continue

        if ok:
            flash("Тест опубликован. Студенты увидят его в назначенное время (по своему часовому поясу).", "success")
        else:
            flash(f"Не удалось опубликовать: {last_err}", "error")
        return redirect(url_for("admin_content"))
    except Exception as e:
        app.logger.exception("admin_test_publish: %s", e)
        flash(f"Ошибка публикации: {e}", "error")
        return redirect(url_for("admin_content"))


@app.route("/admin/social", methods=["GET", "POST"])
@login_required
@admin_required
def admin_social():
    ensure_schema()
    # добиваем колонки social_links на случай старой схемы
    for sql in (
        "ALTER TABLE social_links ADD COLUMN network TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN is_promo INTEGER DEFAULT 1",
        "ALTER TABLE social_links ADD COLUMN sort_order INTEGER DEFAULT 0",
        "ALTER TABLE social_links ADD COLUMN ends_at TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN title TEXT DEFAULT ''",
        "ALTER TABLE social_links ADD COLUMN url TEXT DEFAULT ''",
    ):
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception:
            pass

    if request.method == "POST":
        action = request.form.get("action", "add")
        try:
            if action == "add":
                network = (request.form.get("network") or request.form.get("platform") or "").strip()
                title = (request.form.get("title") or network).strip()
                url = (request.form.get("url") or "").strip()
                ends_at = (request.form.get("ends_at") or "").strip()
                if not url:
                    flash("Укажите ссылку", "error")
                    return redirect(url_for("admin_social"))
                with get_db() as conn:
                    try:
                        conn.execute(
                            "INSERT INTO social_links (network, title, url, is_promo, ends_at) VALUES (?, ?, ?, 1, ?)",
                            (network or "link", title, url, ends_at),
                        )
                    except Exception:
                        try:
                            conn.execute(
                                "INSERT INTO social_links (network, title, url, is_promo) VALUES (?, ?, ?, 1)",
                                (network or "link", title, url),
                            )
                        except Exception as e:
                            flash(f"Ошибка добавления: {e}", "error")
                            return redirect(url_for("admin_social"))
                flash("Ссылка добавлена", "success")
            elif action == "delete":
                sid = request.form.get("id")
                with get_db() as conn:
                    conn.execute("DELETE FROM social_links WHERE id = ?", (sid,))
                flash("Удалено", "success")
            elif action == "broadcast":
                _ensure_app_settings_table()
                cid = now_for_user().strftime("%Y%m%d%H%M%S")
                ok = True
                for k, v in (
                    ("promo_broadcast", "1"),
                    ("promo_broadcast_at", now_for_user().isoformat()),
                    ("promo_auto_count", "0"),
                    ("promo_campaign_id", cid),
                ):
                    if not set_setting(k, v):
                        ok = False
                if ok:
                    flash("Реклама запущена: увидят студенты, которые не на экзамене", "success")
                else:
                    # запасной путь — файл не нужен, пишем напрямую ещё раз
                    try:
                        _ensure_app_settings_table()
                        with get_db() as conn:
                            for k, v in (
                                ("promo_broadcast", "1"),
                                ("promo_campaign_id", cid),
                            ):
                                conn.execute("DELETE FROM app_settings WHERE key = ?", (k,))
                                conn.execute(
                                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                                    (k, v, datetime.now().isoformat()),
                                )
                        flash("Реклама запущена", "success")
                    except Exception as e2:
                        flash(f"Ошибка рекламы: {e2}", "error")
        except Exception as e:
            flash(f"Ошибка: {e}", "error")
        return redirect(url_for("admin_social"))

    links = []
    try:
        with get_db() as conn:
            try:
                links = conn.execute("SELECT * FROM social_links ORDER BY sort_order, id").fetchall()
            except Exception:
                links = conn.execute("SELECT * FROM social_links ORDER BY id").fetchall()
    except Exception as e:
        flash(f"Ошибка загрузки: {e}", "error")
    return render_template("admin/social.html", links=links)



@app.route("/api/promo/status")
@login_required
def promo_status():
    """Реклама для студентов, которые НЕ на экзамене."""
    if current_user.is_admin:
        return jsonify({"show": False, "links": []})
    if session.get("in_exam"):
        return jsonify({"show": False, "links": []})
    broadcast = get_setting("promo_broadcast", "0") == "1"
    if not broadcast:
        return jsonify({"show": False, "links": []})
    campaign = get_setting("promo_campaign_id", "")
    seen_campaign = get_setting(f"promo_seen_campaign_{current_user.id}", "")
    if campaign and seen_campaign == campaign:
        return jsonify({"show": False, "links": []})
    # старый session-dismiss только в рамках той же кампании
    if session.get("promo_dismissed_campaign") == campaign and campaign:
        return jsonify({"show": False, "links": []})
    links = []
    now = now_tj()
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, network, title, url, ends_at FROM social_links WHERE is_promo = 1 ORDER BY id"
            ).fetchall()
            for r in rows:
                ends = ""
                try:
                    ends = r["ends_at"] or ""
                except Exception:
                    ends = ""
                if ends:
                    try:
                        end_dt = datetime.fromisoformat(ends.replace("Z", ""))
                        if getattr(end_dt, "tzinfo", None) is None and now.tzinfo:
                            end_dt = end_dt.replace(tzinfo=now.tzinfo)
                        if now > end_dt:
                            conn.execute("DELETE FROM social_links WHERE id = ?", (r["id"],))
                            continue
                    except Exception:
                        pass
                links.append({
                    "id": r["id"],
                    "network": r["network"],
                    "title": r["title"] or r["network"],
                    "url": r["url"],
                })
    except Exception as e:
        print("promo_status:", e)
    if not links:
        return jsonify({"show": False, "links": []})
    return jsonify({"show": True, "links": links, "campaign": get_setting("promo_campaign_id", "")})


@app.route("/api/promo/subscribe", methods=["POST"])
@login_required
def promo_subscribe():
    """Студент подтвердил подписку на все аккаунты."""
    campaign = get_setting("promo_campaign_id", "")
    session["promo_dismissed_campaign"] = campaign
    set_setting(f"promo_seen_campaign_{current_user.id}", campaign)
    return jsonify({"ok": True})


@app.route("/api/promo/dismiss", methods=["POST"])
@login_required
def promo_dismiss():
    campaign = get_setting("promo_campaign_id", "")
    session["promo_dismissed_campaign"] = campaign
    set_setting(f"promo_seen_campaign_{current_user.id}", campaign)
    return jsonify({"ok": True})


@app.route("/admin/test/<code>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_test_edit(code):
    with get_db() as conn:
        t = conn.execute("SELECT * FROM content_tests WHERE code = ?", (code,)).fetchone()
        if not t:
            flash("Тест не найден", "error")
            return redirect(url_for("admin_content"))
        if request.method == "POST":
            q_ru = request.form.get("q_ru", "").strip()
            opt_a = request.form.get("opt_a", "").strip()
            opt_b = request.form.get("opt_b", "").strip()
            opt_c = request.form.get("opt_c", "").strip()
            opt_d = request.form.get("opt_d", "").strip()
            correct = int(request.form.get("correct_index") or 0)
            # тип вопроса = тип теста (отдельные наборы вопросов)
            try:
                q_type = t["test_type"] or "mcq"
            except Exception:
                q_type = request.form.get("q_type", "mcq")
            if q_type not in ("mcq", "multi", "match"):
                q_type = "mcq"
            multi_raw = request.form.getlist("correct_multi")
            correct_multi = json.dumps([int(x) for x in multi_raw]) if multi_raw else ""
            # match: left A-D = right indices as JSON {"0":1,"1":0,...}
            match_json = request.form.get("match_json", "").strip()
            if q_type == "match" and not match_json:
                # auto from fields match_0 .. match_3
                mj = {}
                for i in range(4):
                    v = request.form.get(f"match_{i}", "").strip()
                    if v != "":
                        mj[str(i)] = int(v)
                match_json = json.dumps(mj)
            if not q_ru or not opt_a or not opt_b:
                flash("Нужны вопрос и минимум 2 варианта", "error")
            else:
                try:
                    conn.execute(
                        """INSERT INTO content_questions
                           (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index, q_type, correct_multi, match_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (t["id"], q_ru, q_ru, q_ru, opt_a, opt_b, opt_c, opt_d, correct, q_type, correct_multi, match_json),
                    )
                except Exception:
                    conn.execute(
                        """INSERT INTO content_questions
                           (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (t["id"], q_ru, q_ru, q_ru, opt_a, opt_b, opt_c, opt_d, correct),
                    )
                flash("Вопрос добавлен", "success")
            return redirect(url_for("admin_test_edit", code=code))
        questions = conn.execute(
            "SELECT * FROM content_questions WHERE test_id = ? ORDER BY sort_order, id",
            (t["id"],),
        ).fetchall()
        faculties = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
    return render_template(
        "admin/test_edit.html",
        test=t,
        questions=questions,
        faculties=faculties,
    )


@app.route("/admin/question/<int:qid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_question_delete(qid):
    with get_db() as conn:
        q = conn.execute("SELECT test_id FROM content_questions WHERE id = ?", (qid,)).fetchone()
        conn.execute("DELETE FROM content_questions WHERE id = ?", (qid,))
        code = "x"
        if q:
            t = conn.execute("SELECT code FROM content_tests WHERE id = ?", (q["test_id"],)).fetchone()
            if t:
                code = t["code"]
    flash("Вопрос удалён", "success")
    return redirect(url_for("admin_test_edit", code=code))


# ==================== FACE ID / УСТРОЙСТВО (работает и без HTTPS) ====================

@app.route("/api/face/register", methods=["POST"])
@app.route("/api/face/enroll", methods=["POST"])
@login_required
def face_enroll():
    """Привязка Face ID к уже зарегистрированному аккаунту (только из Настроек)."""
    data = request.get_json() or {}
    cred_id = (data.get("credential_id") or "").strip()
    if not cred_id or len(cred_id) < 8:
        cred_id = "face_" + uuid.uuid4().hex

    with get_db() as conn:
        # этот ключ уже у другого пользователя?
        exists = conn.execute(
            "SELECT user_id FROM webauthn_credentials WHERE credential_id = ?", (cred_id,)
        ).fetchone()
        if exists and int(exists[0]) != int(current_user.id):
            return jsonify({"error": "Этот Face ID уже привязан к другому аккаунту"}), 400

        # удалить старые ключи этого пользователя
        conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (current_user.id,))
        conn.execute(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, device_name) VALUES (?, ?, ?, ?)",
            (current_user.id, cred_id, data.get("public_key") or "face", (data.get("device_name") or "camera")[:120])
        )
        photo_b64 = data.get("photo")
        if photo_b64 and str(photo_b64).startswith("data:image"):
            try:
                import base64 as b64mod
                header, bdata = photo_b64.split(",", 1)
                ext = "jpg" if "jpeg" in header else "png"
                fname = f"face_{current_user.id}.{ext}"
                fpath = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
                with open(fpath, "wb") as f:
                    f.write(b64mod.b64decode(bdata))
            except Exception:
                pass

    return jsonify({"ok": True, "credential_id": cred_id})


@app.route("/api/face/status")
@login_required
def face_status():
    with get_db() as conn:
        row = conn.execute(
            "SELECT credential_id FROM webauthn_credentials WHERE user_id = ? LIMIT 1",
            (current_user.id,)
        ).fetchone()
    return jsonify({"enabled": bool(row), "credential_id": row[0] if row else None})


@app.route("/api/face/login", methods=["POST"])
def face_login():
    data = request.get_json() or {}
    cred_id = (data.get("credential_id") or "").strip()
    if not cred_id:
        return jsonify({"error": "Нет ключа устройства. Сначала зарегистрируйтесь на этом телефоне."}), 400

    with get_db() as conn:
        row = conn.execute(
            """SELECT u.* FROM webauthn_credentials w
               JOIN users u ON w.user_id = u.id
               WHERE w.credential_id = ?""",
            (cred_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Ключ не найден. Зарегистрируйтесь на этом устройстве."}), 404
        user = User(row)
        login_user(user)
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user.id))

    redirect = url_for("admin_dashboard") if user.is_admin else url_for("dashboard")
    return jsonify({"ok": True, "redirect": redirect})



@app.route("/admin/student/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_student_delete(uid):
    with get_db() as conn:
        u = conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if u and not u["is_admin"]:
            conn.execute("DELETE FROM test_results WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM pro_requests WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM notifications WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            flash("Студент удалён", "success")
        else:
            flash("Нельзя удалить", "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/student/add", methods=["POST"])
@login_required
@admin_required
def admin_student_add():
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "student123")
    if not name or not email:
        flash("ФИО и email обязательны", "error")
        return redirect(url_for("admin_users"))
    with get_db() as conn:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            flash("Email уже есть", "error")
            return redirect(url_for("admin_users"))
        conn.execute(
            "INSERT INTO users (full_name, email, password_hash, language, password_plain) VALUES (?, ?, ?, 'ru', ?)",
            (name, email, generate_password_hash(password, method="pbkdf2:sha256"), password)
        )
    flash("Студент добавлен", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/result/<int:rid>/edit", methods=["POST"])
@login_required
@admin_required
def admin_result_edit(rid):
    """Правка результата: админ меняет БУКВУ (оценку) → балл пересчитывается из 100."""
    grade = (request.form.get("grade") or "").strip()
    if grade not in GRADE_LETTERS:
        flash("Выберите оценку: A, B, C, D, F или Fx", "error")
        return redirect(request.referrer or url_for("admin_stats"))
    score = grade_to_score(grade)
    max_score = float(EXAM_QUESTION_COUNT * POINTS_CORRECT)  # 100
    # correct ≈ score/2, incorrect = 50 - correct
    correct = int(round(score / float(POINTS_CORRECT)))
    incorrect = max(0, EXAM_QUESTION_COUNT - correct)
    with get_db() as conn:
        conn.execute(
            "UPDATE test_results SET score=?, max_score=?, correct=?, incorrect=? WHERE id=?",
            (score, max_score, correct, incorrect, rid)
        )
    flash(f"Оценка изменена на {grade} (балл {score:.0f}/100)", "success")
    return redirect(request.referrer or url_for("admin_stats"))



@app.route("/admin/stats")
@login_required
@admin_required
def admin_stats():
    with get_db() as conn:
        by_test = conn.execute(
            """SELECT test_id,
                      COUNT(*) as attempts,
                      SUM(correct) as sum_ok,
                      SUM(incorrect) as sum_bad,
                      AVG(score * 1.0 / CASE WHEN max_score=0 THEN 1 ELSE max_score END) as avg_pct
               FROM test_results GROUP BY test_id"""
        ).fetchall()
        recent = conn.execute(
            """SELECT tr.*, u.full_name, u.email FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.created_at DESC LIMIT 40"""
        ).fetchall()
    recent_e = []
    for r in recent:
        pct = score_percent(r["score"], r["max_score"])
        recent_e.append({
            "id": r["id"],
            "full_name": r["full_name"],
            "email": r["email"],
            "test_id": r["test_id"],
            "score": float(r["score"] or 0),
            "max_score": float(r["max_score"] or 100),
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "created_at": r["created_at"],
            "grade": letter_grade(pct),
        })
    return render_template("admin/stats.html", by_test=by_test, recent=recent_e, tests=load_all_tests(), letter_grade=letter_grade)



@app.route("/admin/payment", methods=["GET", "POST"])
@login_required
@admin_required
def admin_payment():
    if request.method == "POST":
        set_payment_settings({
            "dc": request.form.get("dc", ""),
            "eskhata": request.form.get("eskhata", ""),
            "alif": request.form.get("alif", ""),
            "holder": request.form.get("holder", ""),
            "phone": request.form.get("phone", ""),
        })
        set_setting("max_exam_attempts", request.form.get("max_exam_attempts", "3"))
        set_setting("backup_webhook", request.form.get("backup_webhook", ""))
        flash("Настройки сохранены", "success")
        return redirect(url_for("admin_payment"))
    return render_template(
        "admin/payment.html",
        card=get_payment_settings(),
        max_exam_attempts=get_setting("max_exam_attempts", "3"),
        backup_webhook=get_setting("backup_webhook", ""),
    )


@app.route("/admin/backup")
@login_required
@admin_required
def admin_backup():
    """Скачать копию БД. Дополнительно копирует в BACKUP_DIR / шлёт на BACKUP_WEBHOOK_URL."""
    import shutil
    if not os.path.exists(DB_PATH):
        flash("База ещё не создана", "error")
        return redirect(url_for("admin_dashboard"))
    # внешняя копия (Render disk / локальная папка)
    backup_dir = os.environ.get("BACKUP_DIR") or get_setting("backup_dir", "")
    if backup_dir:
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(backup_dir, f"hgu_test_{stamp}.db")
            shutil.copy2(DB_PATH, dest)
        except Exception as e:
            app.logger.info("backup copy: %s", e)
    webhook = os.environ.get("BACKUP_WEBHOOK_URL") or get_setting("backup_webhook", "")
    if webhook:
        try:
            import urllib.request
            with open(DB_PATH, "rb") as f:
                data = f.read()
            req = urllib.request.Request(webhook, data=data, method="POST")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("X-HGU-Backup", "1")
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            app.logger.info("backup webhook: %s", e)
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=f"hgu_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
    )



@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "ХГУ Тест",
        "short_name": "ХГУ Тест",
        "description": "Подготовка к поступлению в Худжандский государственный университет",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a5f7a",
        "theme_color": "#1a5f7a",
        "lang": "ru",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    })


@app.route("/sw.js")
def service_worker():
    js = """
const CACHE = 'hgu-v3';
const ASSETS = ['/', '/static/css/style.css', '/static/icon-192.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => { try { c.put(e.request, copy); } catch(err){} }).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
self.addEventListener('push', event => {
  let data = { title: 'ХГУ Тест', body: 'Новое уведомление', url: '/' };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch (e) {
    try { data.body = event.data.text(); } catch (e2) {}
  }
  event.waitUntil(
    self.registration.showNotification(data.title || 'ХГУ Тест', {
      body: data.body || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: data.url || '/' }
    })
  );
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    ensure_schema()
    if _is_cloud_host() and not DATABASE_URL:
        flash("БД: SQLite (временно). Добавьте PostgreSQL (DATABASE_URL), иначе данные сотрутся при перезапуске.", "error")

    total_users = pro_users = total_tests = pending_requests = 0
    recent_users = pending = recent_results = notifs = weak = []
    ai_insights = []
    avg_pct = 0.0

    try:
        with get_db() as conn:
            def _cnt(sql, params=None):
                row = conn.execute(sql, params).fetchone() if params is not None else conn.execute(sql).fetchone()
                if not row:
                    return 0
                try:
                    return int(row[0] or 0)
                except Exception:
                    return 0

            total_users = _cnt("SELECT COUNT(*) FROM users WHERE is_admin = 0")
            pro_users = _cnt("SELECT COUNT(*) FROM users WHERE is_pro = 1 AND is_admin = 0")
            total_tests = _cnt("SELECT COUNT(*) FROM test_results")
            pending_requests = _cnt("SELECT COUNT(*) FROM pro_requests WHERE status = 'pending'")

            recent_users = conn.execute(
                "SELECT id, full_name, email, is_pro, pro_until, created_at, last_login FROM users WHERE is_admin = 0 ORDER BY created_at DESC LIMIT 20"
            ).fetchall()

            pending = conn.execute(
                """SELECT pr.*, u.full_name, u.email
                   FROM pro_requests pr
                   JOIN users u ON pr.user_id = u.id
                   WHERE pr.status = 'pending'
                   ORDER BY pr.created_at DESC"""
            ).fetchall()

            recent_results = conn.execute(
                """SELECT tr.*, u.full_name
                   FROM test_results tr
                   JOIN users u ON tr.user_id = u.id
                   ORDER BY tr.created_at DESC LIMIT 15"""
            ).fetchall()

            notifs = conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC",
                (current_user.id,)
            ).fetchall()

            # Аналитика — SQL совместим с PostgreSQL и SQLite
            try:
                avg_row = conn.execute(
                    "SELECT AVG(score * 1.0 / CASE WHEN max_score = 0 THEN 1 ELSE max_score END) FROM test_results"
                ).fetchone()
                avg_pct = round(float(avg_row[0] or 0) * 100, 1)
            except Exception:
                avg_pct = 0.0

            try:
                weak = conn.execute(
                    """SELECT u.full_name, u.email,
                              AVG(tr.score * 1.0 / CASE WHEN tr.max_score = 0 THEN 1 ELSE tr.max_score END) AS pct,
                              COUNT(tr.id) AS cnt
                       FROM test_results tr
                       JOIN users u ON tr.user_id = u.id
                       WHERE u.is_admin = 0
                       GROUP BY tr.user_id, u.full_name, u.email
                       HAVING AVG(tr.score * 1.0 / CASE WHEN tr.max_score = 0 THEN 1 ELSE tr.max_score END) < 0.5
                          AND COUNT(tr.id) >= 1
                       ORDER BY pct ASC
                       LIMIT 8"""
                ).fetchall()
            except Exception as _we:
                print("admin weak query:", _we)
                weak = []

            try:
                by_test = conn.execute(
                    """SELECT test_id, COUNT(*) AS cnt,
                              AVG(score * 1.0 / CASE WHEN max_score = 0 THEN 1 ELSE max_score END) AS pct
                       FROM test_results
                       GROUP BY test_id
                       ORDER BY cnt DESC"""
                ).fetchall()
            except Exception as _be:
                print("admin by_test query:", _be)
                by_test = []

            if total_tests == 0:
                ai_insights.append("Пока мало данных. Когда студенты начнут проходить тесты, здесь появятся рекомендации.")
            else:
                ai_insights.append(f"Средний результат по всем тестам: {avg_pct}%.")
                if avg_pct < 50:
                    ai_insights.append("Общий уровень подготовки низкий — имеет смысл добавить больше тренировочных тестов и напоминания.")
                elif avg_pct >= 70:
                    ai_insights.append("Уровень подготовки хороший. Можно усложнить Pro-тесты.")
                for t in by_test[:5]:
                    try:
                        tid = t["test_id"]
                        title = (get_test(tid) or {}).get("title", {}).get("ru", tid)
                        pct = round(float(t["pct"] or 0) * 100, 1)
                        cnt = int(t["cnt"] or 0)
                        if pct < 45:
                            ai_insights.append(f"Слабое место: «{title}» (средний {pct}%, прохождений {cnt}). Рекомендуется усилить вопросы или добавить разбор.")
                        elif cnt >= 3:
                            ai_insights.append(f"Популярный тест: «{title}» — {cnt} прохождений, средний {pct}%.")
                    except Exception:
                        pass
                if weak:
                    ai_insights.append(f"Студентов с результатом ниже 50%: {len(weak)}. Имеет смысл отправить им уведомление с советом пройти тренировку.")
                if pending_requests:
                    ai_insights.append(f"Ожидают оплаты Pro: {pending_requests}. Проверьте заявки.")
    except Exception as e:
        print("admin_dashboard error:", e)
        import traceback
        traceback.print_exc()
        flash(f"Ошибка загрузки админки: {e}", "error")

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        pro_users=pro_users,
        total_tests=total_tests,
        pending_requests=pending_requests,
        recent_users=recent_users,
        pending=pending,
        recent_results=recent_results,
        notifs=notifs,
        tests=load_all_tests(),
        faculties=load_faculties_map(),
        ai_insights=ai_insights,
        weak_students=weak,
        avg_pct=avg_pct
    )


@app.route("/admin/pro/<int:req_id>/<action>", methods=["POST"])
@login_required
@admin_required
def admin_pro_action(req_id, action):
    with get_db() as conn:
        req = conn.execute("SELECT * FROM pro_requests WHERE id = ?", (req_id,)).fetchone()
        if not req:
            flash("Заявка не найдена", "error")
            return redirect(url_for("admin_dashboard"))

        if action == "approve":
            days = PRO_DURATION_DAYS
            package = "2m"
            try:
                if req["duration_days"]:
                    days = int(req["duration_days"])
            except Exception:
                pass
            try:
                package = req["package"] or "2m"
            except Exception:
                package = "2m"
            hints = int(PRO_PACKAGES.get(package, {}).get("hints", 5))
            until = (datetime.now() + timedelta(days=days)).isoformat()
            conn.execute(
                "UPDATE users SET is_pro = 1, pro_until = ?, hints_left = ? WHERE id = ?",
                (until, hints, req["user_id"])
            )
            conn.execute(
                "UPDATE pro_requests SET status = 'approved', processed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), req_id)
            )
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (req["user_id"], "Pro одобрен",
                 f"Pro на {days} дн. Подсказки: {hints}. Спасибо за оплату!")
            )
            flash(f"Pro одобрен (+{hints} подсказок)", "success")

        elif action == "reject":
            note = request.form.get("note", "Оплата не подтверждена")
            conn.execute(
                "UPDATE pro_requests SET status = 'rejected', admin_note = ?, processed_at = ? WHERE id = ?",
                (note, datetime.now().isoformat(), req_id)
            )
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (req["user_id"], "Заявка отклонена", f"Ваша заявка на Pro отклонена. Причина: {note}")
            )
            flash("Заявка отклонена", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/notify", methods=["POST"])
@login_required
@admin_required
def admin_notify():
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    target = request.form.get("target", "all")  # all / pro / free

    if not title or not message:
        flash("Заполните заголовок и текст", "error")
        return redirect(url_for("admin_dashboard"))

    with get_db() as conn:
        if target == "all":
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0").fetchall()
        elif target == "pro":
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0 AND is_pro = 1").fetchall()
        else:
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0 AND is_pro = 0").fetchall()

        for u in users:
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (u["id"], title, message)
            )

        # Также глобальное
        conn.execute(
            "INSERT INTO global_notifications (title, message) VALUES (?, ?)",
            (title, message)
        )

    flash(f"Уведомление отправлено {len(users)} пользователям", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    with get_db() as conn:
        users = conn.execute(
            "SELECT * FROM users WHERE is_admin = 0 ORDER BY created_at DESC"
        ).fetchall()
    return render_template("admin/users.html", users=users, tests=load_all_tests())



@app.route("/admin/export/results.xlsx")
@login_required
@admin_required
def admin_export_results():
    """Экспорт всех результатов в Excel (или CSV, если openpyxl нет)."""
    import io
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tr.id, u.full_name, u.email, tr.test_id, tr.score, tr.max_score,
                      tr.correct, tr.incorrect, tr.mode, tr.duration_seconds, tr.created_at
               FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.created_at DESC"""
        ).fetchall()
    headers = ["ID", "ФИО", "Email", "Тест", "Балл", "Макс", "Верно", "Неверно", "Режим", "Сек", "Дата"]
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Результаты"
        ws.append(headers)
        for r in rows:
            ws.append([r["id"], r["full_name"], r["email"], r["test_id"], r["score"], r["max_score"],
                       r["correct"], r["incorrect"], r["mode"] or "exam", r["duration_seconds"], r["created_at"]])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="hgu_results.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception:
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        for r in rows:
            w.writerow([r["id"], r["full_name"], r["email"], r["test_id"], r["score"], r["max_score"],
                        r["correct"], r["incorrect"], r["mode"] or "exam", r["duration_seconds"], r["created_at"]])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(data, as_attachment=True, download_name="hgu_results.csv", mimetype="text/csv")


@app.route("/admin/results")
@login_required
@admin_required
def admin_results():
    with get_db() as conn:
        results = conn.execute(
            """SELECT tr.*, u.full_name, u.email, u.timezone AS user_tz
               FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.id DESC LIMIT 100"""
        ).fetchall()
    enriched = []
    for r in results:
        try:
            pct = score_percent(r["score"], r["max_score"])
        except Exception:
            pct = 0.0
        grade = letter_grade(pct)
        tz = "Asia/Dushanbe"
        try:
            tz = (r["user_tz"] or tz) if "user_tz" in r.keys() else tz
        except Exception:
            pass
        dt = format_dt(r["created_at"], tz)
        # _Row не поддерживает присвоение — делаем dict
        item = {
            "id": r["id"],
            "full_name": r["full_name"],
            "email": r["email"],
            "test_id": r["test_id"],
            "score": r["score"],
            "max_score": r["max_score"],
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "duration_seconds": r["duration_seconds"],
            "created_at": r["created_at"],
            "_grade": grade,
            "_dt": dt,
            "_pct": pct,
        }
        enriched.append(item)
    return render_template("admin/results.html", results=enriched, tests=load_all_tests())


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    if not current_user.is_admin:
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/api/mark_read/<int:notif_id>", methods=["POST"])
@login_required
def mark_read(notif_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notif_id, current_user.id)
        )
    return jsonify({"ok": True})


# ==================== ONLINE + CHAT ====================

def _ensure_presence_cols():
    for sql in (
        "ALTER TABLE users ADD COLUMN last_seen TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
    ):
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception:
            pass


@app.route("/api/heartbeat", methods=["POST", "GET"])
@login_required
def api_heartbeat():
    """Отметка онлайн. Вызывается со страницы каждые 30 сек."""
    _ensure_presence_cols()
    now = now_for_user(current_user).isoformat()
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, current_user.id))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "ts": now})


def _is_online(last_seen, minutes=2):
    if not last_seen:
        return False
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        raw = str(last_seen).replace("Z", "+00:00")
        if " " in raw and "T" not in raw[:11]:
            raw = raw.replace(" ", "T", 1)
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        now = datetime.now(dt.tzinfo)
        return (now - dt).total_seconds() < minutes * 60
    except Exception:
        return False


@app.route("/api/chat/send", methods=["POST"])
@login_required
def chat_send():
    _ensure_presence_cols()
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or request.form.get("body") or "").strip()
    if not body or len(body) > 2000:
        return jsonify({"ok": False, "error": "empty"}), 400
    try:
        receiver_id = int(data.get("receiver_id") or request.form.get("receiver_id") or 0)
    except Exception:
        receiver_id = 0
    # студент пишет только админу
    if not current_user.is_admin:
        with get_db() as conn:
            adm = conn.execute(
                "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        if not adm:
            return jsonify({"ok": False, "error": "no admin"}), 400
        receiver_id = int(adm["id"])
    if not receiver_id:
        return jsonify({"ok": False, "error": "no receiver"}), 400
    now = now_for_user(current_user).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (sender_id, receiver_id, body, is_read, created_at) VALUES (?, ?, ?, 0, ?)",
            (current_user.id, receiver_id, body, now),
        )
    return jsonify({"ok": True})


@app.route("/api/chat/messages")
@login_required
def chat_messages():
    _ensure_presence_cols()
    try:
        peer_id = int(request.args.get("peer_id") or 0)
    except Exception:
        peer_id = 0
    if not current_user.is_admin:
        with get_db() as conn:
            adm = conn.execute(
                "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        peer_id = int(adm["id"]) if adm else 0
    if not peer_id:
        return jsonify({"messages": []})
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, sender_id, receiver_id, body, is_read, created_at
               FROM chat_messages
               WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
               ORDER BY id DESC LIMIT 80""",
            (current_user.id, peer_id, peer_id, current_user.id),
        ).fetchall()
        # отметить прочитанным входящие
        try:
            conn.execute(
                "UPDATE chat_messages SET is_read = 1 WHERE receiver_id = ? AND sender_id = ? AND is_read = 0",
                (current_user.id, peer_id),
            )
        except Exception:
            pass
    msgs = []
    for r in reversed(list(rows)):
        msgs.append({
            "id": r["id"],
            "sender_id": r["sender_id"],
            "body": r["body"],
            "mine": int(r["sender_id"]) == int(current_user.id),
            "created_at": format_dt(r["created_at"], getattr(current_user, "timezone", None) or "Asia/Dushanbe"),
        })
    return jsonify({"messages": msgs, "peer_id": peer_id})


@app.route("/api/chat/peers")
@login_required
@admin_required
def chat_peers():
    """Список студентов для чата админа + онлайн."""
    _ensure_presence_cols()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, full_name, email, last_seen FROM users WHERE is_admin = 0 ORDER BY full_name"""
        ).fetchall()
        unread = {}
        try:
            ur = conn.execute(
                """SELECT sender_id, COUNT(*) AS c FROM chat_messages
                   WHERE receiver_id = ? AND is_read = 0 GROUP BY sender_id""",
                (current_user.id,),
            ).fetchall()
            for u in ur:
                unread[int(u["sender_id"])] = int(u["c"] or 0)
        except Exception:
            pass
    peers = []
    for r in rows:
        ls = ""
        try:
            ls = r["last_seen"] or ""
        except Exception:
            ls = ""
        peers.append({
            "id": r["id"],
            "name": r["full_name"],
            "email": r["email"],
            "online": _is_online(ls),
            "last_seen": format_dt(ls) if ls else "",
            "unread": unread.get(int(r["id"]), 0),
        })
    peers.sort(key=lambda x: (not x["online"], -x["unread"], x["name"] or ""))
    return jsonify({"peers": peers})


@app.route("/admin/chat")
@login_required
@admin_required
def admin_chat():
    return render_template("admin/chat.html")


@app.route("/api/chat/unread_count")
@login_required
def chat_unread_count():
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM chat_messages WHERE receiver_id = ? AND is_read = 0",
                (current_user.id,),
            ).fetchone()
            c = int(row["c"] if row and row["c"] is not None else (row[0] if row else 0))
    except Exception:
        c = 0
    return jsonify({"count": c})



# ==================== ЗАПУСК ====================

print("=" * 50)
print("HGU Test starting")
print("DATABASE:", "PostgreSQL" if DATABASE_URL else "SQLite (temporary on cloud!)")
if DATABASE_URL:
    print("DATABASE_URL: set (hidden)")
else:
    print("WARNING: set DATABASE_URL for permanent storage")
print("CLOUD:", "yes" if _is_cloud_host() else "no (local)")
print("=" * 50)
try:
    _create_core_tables()
    print("core tables: OK")
except Exception as _init_err:
    print("CRITICAL core tables:", _init_err)
    import traceback
    traceback.print_exc()
try:
    init_db()
    print("init_db: OK")
except Exception as _init_err2:
    print("init_db error (non-fatal):", _init_err2)
try:
    ensure_admin()
    print("ensure_admin: OK")
except Exception as _adm_err:
    print("ensure_admin at startup failed:", _adm_err)


if __name__ == "__main__":
    _port = int(os.environ.get("PORT", "5000"))
    print("=" * 50)
    print("ХГУ Тест - сервер запущен")
    print("Админ: admin@hgu.tj / admin123")
    print(f"Откройте: http://127.0.0.1:{_port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=_port, debug=not _is_cloud_host())
