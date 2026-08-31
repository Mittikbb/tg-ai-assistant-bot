import sqlite3

DB_NAME = "business_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
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
    conn.commit()
    conn.close()

# --- Статусы бота ---

def get_status() -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'status'")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "default"

def set_status(status_name: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'status'", (status_name,))
    conn.commit()
    conn.close()

# --- Черный список (Blacklist) ---

def is_blacklisted(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def add_to_blacklist(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- Агрессивный режим по ID ---

def is_aggressive(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM aggressive_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def add_to_aggressive(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO aggressive_users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_from_aggressive(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM aggressive_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- Отключенные чаты (полный запрет автоответа) ---

def is_chat_disabled(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM disabled_chats WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def disable_chat(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO disabled_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def enable_chat(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM disabled_chats WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

# --- Ночные логи ---

def save_night_message(sender_name: str, text: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO night_logs (sender_name, message_text) VALUES (?, ?)", (sender_name, text))
    conn.commit()
    conn.close()

def pop_night_messages() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT sender_name, message_text FROM night_logs")
    rows = cursor.fetchall()
    cursor.execute("DELETE FROM night_logs")
    conn.commit()
    conn.close()
    return rows

# --- Досье и заметки ---

def get_user_profile(user_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT notes FROM users_profiles WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else ""

def update_user_profile(user_id: int, user_name: str, new_note: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users_profiles (user_id, user_name, notes)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            user_name = excluded.user_name,
            notes = excluded.notes
    """, (user_id, user_name, new_note))
    conn.commit()
    conn.close()

# --- Статистика ---

def log_stat(user_id: int, category: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO stats_log (user_id, category) VALUES (?, ?)", (user_id, category))
    conn.commit()
    conn.close()

def get_stats_summary() -> dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM stats_log")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT category, COUNT(*) FROM stats_log GROUP BY category")
    by_cat = dict(cursor.fetchall())
    
    conn.close()
    return {"total": total, "categories": by_cat}

init_db()