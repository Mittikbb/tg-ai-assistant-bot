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
    
    # Таблица для ночных сообщений
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS night_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_name TEXT,
            message_text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('status', 'default')")
    conn.commit()
    conn.close()

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

init_db()