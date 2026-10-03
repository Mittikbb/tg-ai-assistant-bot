import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

DB_NAME = "business_bot.db"

@contextmanager
def get_cursor(commit: bool = False):
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    cursor = conn.cursor()
    try:
        yield cursor
        if commit:
            conn.commit()
    finally:
        conn.close()

def init_db():
    with get_cursor(commit=True) as cursor:
        # --- Таблицы Бизнес-Ассистента (Модули 1 и 2) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aggressive_users (
                user_id INTEGER PRIMARY KEY
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS disabled_chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS night_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_name TEXT,
                message_text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users_profiles (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                notes TEXT DEFAULT ''
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('status', 'default')")

        # --- Таблицы Модуля 3 (Напоминания и Заметки) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                timezone_offset INTEGER DEFAULT 3
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_at_utc TEXT NOT NULL,
                remind_at_local TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                source_info TEXT DEFAULT ''
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_utc ON reminders(status, remind_at_utc)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, status)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_archived INTEGER DEFAULT 0
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_user_cat ON notes(user_id, category, is_archived)")

_status_cache = None

# ==============================================================================
# ФУНКЦИИ БИЗНЕС-АССИСТЕНТА
# ==============================================================================

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
    with get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'status'", (status_name,))
    _status_cache = status_name

def is_blacklisted(user_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row is not None

def add_to_blacklist(user_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (user_id,))

def remove_from_blacklist(user_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))

def is_aggressive(user_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT user_id FROM aggressive_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row is not None

def add_to_aggressive(user_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT OR IGNORE INTO aggressive_users (user_id) VALUES (?)", (user_id,))

def remove_from_aggressive(user_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM aggressive_users WHERE user_id = ?", (user_id,))

def is_chat_disabled(chat_id: int) -> bool:
    with get_cursor() as cursor:
        cursor.execute("SELECT chat_id FROM disabled_chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
    return row is not None

def disable_chat(chat_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT OR IGNORE INTO disabled_chats (chat_id) VALUES (?)", (chat_id,))

def enable_chat(chat_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM disabled_chats WHERE chat_id = ?", (chat_id,))

def save_night_message(sender_name: str, text: str):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT INTO night_logs (sender_name, message_text) VALUES (?, ?)", (sender_name, text))

def pop_night_messages() -> list:
    with get_cursor(commit=True) as cursor:
        cursor.execute("SELECT sender_name, message_text FROM night_logs")
        rows = cursor.fetchall()
        cursor.execute("DELETE FROM night_logs")
    return rows

def get_user_profile(user_id: int) -> str:
    with get_cursor() as cursor:
        cursor.execute("SELECT notes FROM users_profiles WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row[0] if row and row[0] else ""

def update_user_profile(user_id: int, user_name: str, new_note: str):
    with get_cursor(commit=True) as cursor:
        cursor.execute("""
            INSERT INTO users_profiles (user_id, user_name, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name = excluded.user_name,
                notes = excluded.notes
        """, (user_id, user_name, new_note))

def log_stat(user_id: int, category: str):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT INTO stats_log (user_id, category) VALUES (?, ?)", (user_id, category))

def get_stats_summary() -> dict:
    with get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM stats_log")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT category, COUNT(*) FROM stats_log GROUP BY category")
        by_cat = dict(cursor.fetchall())
    return {"total": total, "categories": by_cat}

# ==============================================================================
# ФУНКЦИИ МОДУЛЯ 3 (НАПОМИНАНИЯ И ЗАМЕТКИ)
# ==============================================================================

DEFAULT_TIMEZONE_OFFSET = 3

def get_user_tz_offset(user_id: int) -> int:
    with get_cursor() as cursor:
        cursor.execute("SELECT timezone_offset FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row[0] if row is not None else DEFAULT_TIMEZONE_OFFSET

def set_user_tz_offset(user_id: int, offset: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("INSERT OR REPLACE INTO user_settings (user_id, timezone_offset) VALUES (?, ?)", (user_id, offset))

# --- Напоминания ---
def add_reminder(user_id: int, chat_id: int, text: str, remind_at_utc: str, remind_at_local: str, source_info: str = "") -> int:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_cursor(commit=True) as cursor:
        cursor.execute("""
            INSERT INTO reminders (user_id, chat_id, text, remind_at_utc, remind_at_local, created_at, status, source_info)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (user_id, chat_id, text, remind_at_utc, remind_at_local, created_at, source_info))
        return cursor.lastrowid

def get_active_reminders(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, text, remind_at_local, remind_at_utc, source_info
            FROM reminders
            WHERE user_id = ? AND status = 'pending'
            ORDER BY remind_at_utc ASC
        """, (user_id,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "remind_at_local": r[2],
                "remind_at_utc": r[3],
                "source_info": r[4]
            }
            for r in rows
        ]

def get_reminder_by_id(rem_id: int):
    with get_cursor() as cursor:
        cursor.execute("SELECT id, user_id, chat_id, text, remind_at_local, remind_at_utc, status, source_info FROM reminders WHERE id = ?", (rem_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "chat_id": row[2],
            "text": row[3],
            "remind_at_local": row[4],
            "remind_at_utc": row[5],
            "status": row[6],
            "source_info": row[7]
        }

def set_reminder_status(rem_id: int, status: str):
    with get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, rem_id))

def mark_reminder_completed(rem_id: int, user_id: Optional[int] = None):
    with get_cursor(commit=True) as cursor:
        if user_id:
            cursor.execute("UPDATE reminders SET status = 'completed' WHERE id = ? AND user_id = ?", (rem_id, user_id))
        else:
            cursor.execute("UPDATE reminders SET status = 'completed' WHERE id = ?", (rem_id,))

def delete_reminder(rem_id: int, user_id: Optional[int] = None):
    with get_cursor(commit=True) as cursor:
        if user_id:
            cursor.execute("UPDATE reminders SET status = 'deleted' WHERE id = ? AND user_id = ?", (rem_id, user_id))
        else:
            cursor.execute("UPDATE reminders SET status = 'deleted' WHERE id = ?", (rem_id,))

def snooze_reminder_by_id(rem_id: int, minutes: int, tz_offset: int):
    rem = get_reminder_by_id(rem_id)
    if not rem:
        return None, None
    now_utc = datetime.now(timezone.utc)
    new_utc = now_utc + timedelta(minutes=minutes)
    user_tz = timezone(timedelta(hours=tz_offset))
    new_local = new_utc.astimezone(user_tz)

    utc_str = new_utc.strftime("%Y-%m-%d %H:%M:%S")
    local_str = new_local.strftime("%Y-%m-%d %H:%M:%S")

    with get_cursor(commit=True) as cursor:
        cursor.execute("""
            UPDATE reminders
            SET remind_at_utc = ?, remind_at_local = ?, status = 'pending'
            WHERE id = ?
        """, (utc_str, local_str, rem_id))
    return new_local, utc_str

def snooze_reminder(rem_id: int, user_id: int, minutes: int = 15):
    tz = get_user_tz_offset(user_id)
    new_local, _ = snooze_reminder_by_id(rem_id, minutes, tz)
    return new_local

def get_due_reminders(current_utc_str: Optional[str] = None):
    if not current_utc_str:
        current_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, user_id, chat_id, text, remind_at_local, source_info
            FROM reminders
            WHERE status = 'pending' AND remind_at_utc <= ?
            ORDER BY remind_at_utc ASC
        """, (current_utc_str,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "chat_id": r[2],
                "text": r[3],
                "remind_at_local": r[4],
                "source_info": r[5]
            }
            for r in rows
        ]

def mark_reminder_sent(rem_id: int):
    with get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (rem_id,))

# --- Заметки ---
NOTE_CATEGORIES = ["Учеба", "Покупки", "Софт/VPN", "Идеи", "Другое"]
CATEGORY_EMOJIS = {
    "Учеба": "🎓",
    "Покупки": "🛒",
    "Софт/VPN": "💻",
    "Идеи": "💡",
    "Другое": "📌"
}

def normalize_category(cat: str) -> str:
    if not cat:
        return "Другое"
    c = cat.strip().lower()
    if "учеб" in c or "школ" in c or "инст" in c or "дз" in c:
        return "Учеба"
    elif "покуп" in c or "купит" in c or "магаз" in c:
        return "Покупки"
    elif "софт" in c or "vpn" in c or "впн" in c or "прог" in c:
        return "Софт/VPN"
    elif "иде" in c or "мысл" in c:
        return "Идеи"
    else:
        return "Другое"

def add_note(user_id: int, category: str, content: str) -> int:
    category = normalize_category(category)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_cursor(commit=True) as cursor:
        cursor.execute("""
            INSERT INTO notes (user_id, category, content, created_at, is_archived)
            VALUES (?, ?, ?, ?, 0)
        """, (user_id, category, content, created_at))
        return cursor.lastrowid

def get_notes_categories_stats(user_id: int):
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT category, COUNT(*)
            FROM notes
            WHERE user_id = ? AND is_archived = 0
            GROUP BY category
        """, (user_id,))
        rows = dict(cursor.fetchall())
    stats = {}
    for cat in NOTE_CATEGORIES:
        stats[cat] = rows.get(cat, 0)
    return stats

def get_notes_by_cat(user_id: int, category: str, limit: int = 15):
    category = normalize_category(category)
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, content, created_at
            FROM notes
            WHERE user_id = ? AND category = ? AND is_archived = 0
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, category, limit))
        rows = cursor.fetchall()
        return [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]

def get_all_active_notes(user_id: int, limit: int = 25):
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, category, content, created_at
            FROM notes
            WHERE user_id = ? AND is_archived = 0
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        return [{"id": r[0], "category": r[1], "content": r[2], "created_at": r[3]} for r in rows]

def delete_note_by_id(note_id: int, user_id: int) -> bool:
    with get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE notes SET is_archived = 1 WHERE id = ? AND user_id = ?", (note_id, user_id))
        return cursor.rowcount > 0

def get_note_by_id(note_id: int):
    with get_cursor() as cursor:
        cursor.execute("SELECT id, user_id, category, content FROM notes WHERE id = ?", (note_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "user_id": row[1], "category": row[2], "content": row[3]}

def clear_notes_by_cat(user_id: int, category: str):
    category = normalize_category(category)
    with get_cursor(commit=True) as cursor:
        cursor.execute("UPDATE notes SET is_archived = 1 WHERE user_id = ? AND category = ?", (user_id, category))

def get_all_pending_reminders():
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, user_id, chat_id, text, remind_at_local, remind_at_utc, created_at, source_info
            FROM reminders
            WHERE status = 'pending'
            ORDER BY remind_at_utc ASC
        """)
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "chat_id": r[2],
                "text": r[3],
                "remind_at_local": r[4],
                "remind_at_utc": r[5],
                "created_at": r[6],
                "source_info": r[7]
            }
            for r in rows
        ]

def get_all_notes(limit: int = 100):
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, user_id, category, content, created_at
            FROM notes
            WHERE is_archived = 0
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "category": r[2],
                "content": r[3],
                "created_at": r[4]
            }
            for r in rows
        ]

def get_recent_night_logs(limit: int = 20):
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT id, sender_name, message_text, timestamp
            FROM night_logs
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "sender_name": r[1],
                "message_text": r[2],
                "timestamp": r[3]
            }
            for r in rows
        ]

# --- Псевдонимы функций (для полной обратной совместимости) ---
get_user_timezone = get_user_tz_offset
set_user_timezone = set_user_tz_offset
get_all_notes_count = get_notes_categories_stats
get_notes_by_category = get_notes_by_cat
delete_note = delete_note_by_id
clear_category = clear_notes_by_cat

init_db()
