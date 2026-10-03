import sqlite3
from contextlib import contextmanager

DB_NAME = "business_bot.db"

_conn = sqlite3.connect(DB_NAME, check_same_thread=False)

@contextmanager
def get_cursor():
    cursor = _conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def init_db():
    with get_cursor() as cursor:

        # Таблица текущих настроек и статусов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Таблица черного списка (персональный игнор)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            )
        """)

        # Таблица пользователей с включенным агрессивным режимом
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aggressive_users (
                user_id INTEGER PRIMARY KEY
            )
        """)

        # Таблица чатов с ПОЛНОСТЬЮ отключенным автоответчиком
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS disabled_chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)

        # Таблица для ночных сообщений
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS night_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_name TEXT,
                message_text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица досье собеседников (память ИИ)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users_profiles (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                notes TEXT DEFAULT ''
            )
        """)

        # Таблица статистики ответов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('status', 'default')")
        _conn.commit()

_status_cache = None

# --- Статусы бота ---

def get_status() -> str:
    global _status_cache
    if _status_cache is not None:
        return _status_cache

    with get_cursor() as cursor:
        cursor.execute("SELECT value FROM settings WHERE key = 'status'")
        row = cursor.fetchone()
        _status_cache = row[0] if row else "default"
        return _status_cache

def set_status(status_name: str):
    global _status_cache
    with get_cursor() as cursor:
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'status'", (status_name,))
        _conn.commit()
        _status_cache = status_name

# --- Черный список (Blacklist) ---

def is_blacklisted(user_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row is not None

def add_to_blacklist(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (user_id,))
        _conn.commit()

def remove_from_blacklist(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        _conn.commit()

# --- Агрессивный режим по ID ---

def is_aggressive(user_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT user_id FROM aggressive_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row is not None

def add_to_aggressive(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("INSERT OR IGNORE INTO aggressive_users (user_id) VALUES (?)", (user_id,))
        _conn.commit()

def remove_from_aggressive(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM aggressive_users WHERE user_id = ?", (user_id,))
        _conn.commit()

# --- Отключенные чаты (полный запрет автоответа) ---

def is_chat_disabled(chat_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT chat_id FROM disabled_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row is not None

def disable_chat(chat_id: int):
    with get_cursor() as cursor:
        cursor.execute("INSERT OR IGNORE INTO disabled_chats (chat_id) VALUES (?)", (chat_id,))
        _conn.commit()

def enable_chat(chat_id: int):
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM disabled_chats WHERE chat_id = ?", (chat_id,))
        _conn.commit()

# --- Ночные логи ---

def save_night_message(sender_name: str, text: str):
    with get_cursor() as cursor:
        cursor.execute("INSERT INTO night_logs (sender_name, message_text) VALUES (?, ?)", (sender_name, text))
        _conn.commit()

def pop_night_messages() -> list:
    with get_cursor() as cursor:
        cursor.execute("SELECT sender_name, message_text FROM night_logs")
        rows = cursor.fetchall()
        cursor.execute("DELETE FROM night_logs")
        _conn.commit()
        return rows

# --- Досье и заметки ---

def get_user_profile(user_id: int) -> str:
    with get_cursor() as cursor:
        cursor.execute("SELECT notes FROM users_profiles WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""

def update_user_profile(user_id: int, user_name: str, new_note: str):
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO users_profiles (user_id, user_name, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name = excluded.user_name,
                notes = excluded.notes
        """, (user_id, user_name, new_note))
        _conn.commit()

# --- Статистика ---

def log_stat(user_id: int, category: str):
    with get_cursor() as cursor:
        cursor.execute("INSERT INTO stats_log (user_id, category) VALUES (?, ?)", (user_id, category))
        _conn.commit()

def get_stats_summary() -> dict:
    with get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM stats_log")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT category, COUNT(*) FROM stats_log GROUP BY category")
        by_cat = dict(cursor.fetchall())

        return {"total": total, "categories": by_cat}

init_db()