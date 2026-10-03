import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message
)
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field

# --- Инициализация переменных окружения ---
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PORT = int(os.getenv("PORT", 8080))
DEFAULT_TIMEZONE_OFFSET = int(os.getenv("DEFAULT_TIMEZONE_OFFSET", 3))  # По умолчанию UTC+3 (Москва)
DB_NAME = os.getenv("DB_NAME", "bot_data.db")
AUTH_SECRET = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# Инициализация Gemini Client
ai_client = None
if GEMINI_API_KEY:
    try:
        http_options = None
        base_url = os.getenv("GEMINI_BASE_URL")
        if base_url:
            http_options = genai_types.HttpOptions(base_url=base_url)
        ai_client = genai.Client(api_key=GEMINI_API_KEY, http_options=http_options)
        logging.info(f"Gemini AI Client успешно инициализирован (модель: {GEMINI_MODEL}).")
    except Exception as e:
        logging.error(f"Ошибка при создании Gemini Client: {e}")

# ==============================================================================
# БАЗА ДАННЫХ (SQLite с WAL, timeout и индексами)
# ==============================================================================

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Белый список пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)
    if ADMIN_ID != 0:
        cursor.execute("INSERT OR REPLACE INTO whitelist (user_id, username) VALUES (?, ?)", (ADMIN_ID, "Admin"))

    # 2. Настройки пользователей (часовой пояс)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            timezone_offset INTEGER DEFAULT 3
        )
    """)

    # 3. Напоминания (Модуль 3)
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

    # 4. Заметки (Модуль 3)
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

    conn.commit()
    conn.close()

# --- Whitelist DB helpers ---
def get_allowed_users() -> set:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM whitelist")
    users = {row[0] for row in cursor.fetchall()}
    conn.close()
    return users

def add_to_whitelist(user_id: int, username: str = ""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO whitelist (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def remove_from_whitelist(user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_whitelist_with_names():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username FROM whitelist")
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- User settings DB helpers ---
def get_user_tz_offset(user_id: int) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT timezone_offset FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row is not None else DEFAULT_TIMEZONE_OFFSET

def set_user_tz_offset(user_id: int, offset: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_settings (user_id, timezone_offset) VALUES (?, ?)", (user_id, offset))
    conn.commit()
    conn.close()

# --- Reminders DB helpers ---
def add_reminder(user_id: int, chat_id: int, text: str, remind_at_utc: str, remind_at_local: str, source_info: str = "") -> int:
    conn = get_db()
    cursor = conn.cursor()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO reminders (user_id, chat_id, text, remind_at_utc, remind_at_local, created_at, status, source_info)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (user_id, chat_id, text, remind_at_utc, remind_at_local, created_at, source_info))
    rem_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return rem_id

def get_active_reminders(user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, text, remind_at_local, remind_at_utc, source_info
        FROM reminders
        WHERE user_id = ? AND status = 'pending'
        ORDER BY remind_at_utc ASC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_reminder_by_id(rem_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, chat_id, text, remind_at_local, remind_at_utc, status, source_info FROM reminders WHERE id = ?", (rem_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def set_reminder_status(rem_id: int, status: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, rem_id))
    conn.commit()
    conn.close()

def snooze_reminder_by_id(rem_id: int, minutes: int, tz_offset: int):
    rem = get_reminder_by_id(rem_id)
    if not rem:
        return None, None
    now_utc = datetime.now(timezone.utc)
    new_utc = now_utc + timedelta(minutes=minutes)
    user_tz = timezone(timedelta(hours=tz_offset))
    new_local = new_utc.astimezone(user_tz)
    
    utc_str = new_utc.strftime("%Y-%m-%d %H:%M:%S")
    local_str = new_local.strftime("%d.%m в %H:%M")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE reminders
        SET remind_at_utc = ?, remind_at_local = ?, status = 'pending'
        WHERE id = ?
    """, (utc_str, local_str, rem_id))
    conn.commit()
    conn.close()
    return local_str, utc_str

def get_due_reminders(current_utc_str: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, chat_id, text, remind_at_local, source_info
        FROM reminders
        WHERE status = 'pending' AND remind_at_utc <= ?
        ORDER BY remind_at_utc ASC
    """, (current_utc_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_reminder_sent(rem_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (rem_id,))
    conn.commit()
    conn.close()

# --- Notes DB helpers ---
NOTE_CATEGORIES = ["Учеба", "Покупки", "Софт/VPN", "Идеи", "Другое"]
CATEGORY_EMOJIS = {
    "Учеба": "🎓",
    "Покупки": "🛒",
    "Софт/VPN": "💻",
    "Идеи": "💡",
    "Другое": "📌"
}

def add_note(user_id: int, category: str, content: str) -> int:
    if category not in NOTE_CATEGORIES:
        category = "Другое"
    conn = get_db()
    cursor = conn.cursor()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO notes (user_id, category, content, created_at, is_archived)
        VALUES (?, ?, ?, ?, 0)
    """, (user_id, category, content, created_at))
    note_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return note_id

def get_notes_categories_stats(user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT category, COUNT(*)
        FROM notes
        WHERE user_id = ? AND is_archived = 0
        GROUP BY category
    """, (user_id,))
    rows = dict(cursor.fetchall())
    conn.close()
    
    stats = {}
    for cat in NOTE_CATEGORIES:
        stats[cat] = rows.get(cat, 0)
    return stats

def get_notes_by_cat(user_id: int, category: str, limit: int = 15):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, content, created_at
        FROM notes
        WHERE user_id = ? AND category = ? AND is_archived = 0
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, category, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_active_notes(user_id: int, limit: int = 25):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, category, content, created_at
        FROM notes
        WHERE user_id = ? AND is_archived = 0
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_note_by_id(note_id: int, user_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE notes SET is_archived = 1 WHERE id = ? AND user_id = ?", (note_id, user_id))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return affected

def get_note_by_id(note_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, category, content FROM notes WHERE id = ?", (note_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def clear_notes_by_cat(user_id: int, category: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE notes SET is_archived = 1 WHERE user_id = ? AND category = ?", (user_id, category))
    conn.commit()
    conn.close()

def get_db_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM whitelist")
    users_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM reminders WHERE status = 'pending'")
    active_rem_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM reminders")
    total_rem_cnt = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM notes WHERE is_archived = 0")
    active_notes_cnt = cursor.fetchone()[0]
    conn.close()
    return {
        "users": users_cnt,
        "active_reminders": active_rem_cnt,
        "total_reminders": total_rem_cnt,
        "active_notes": active_notes_cnt,
    }

init_db()

# ==============================================================================
# ИИ-СЕССИИ И СТРУКТУРИРОВАННЫЙ ПАРСИНГ (Gemini + Fallback)
# ==============================================================================

user_sessions = {}

def get_user_chat(user_id: int):
    if user_id not in user_sessions and ai_client:
        user_sessions[user_id] = ai_client.aio.chats.create(model=GEMINI_MODEL)
    return user_sessions.get(user_id)

class ParsedIntent(BaseModel):
    intent: Literal["reminder", "note", "chat"] = Field(
        description="The determined intent: 'reminder' if specific date/time or interval mentioned, 'note' if note/task/item without specific time, 'chat' if conversational question or discussion"
    )
    datetime: Optional[str] = Field(
        default=None,
        description="Target datetime in 'YYYY-MM-DD HH:MM:SS' format in user's local timezone if intent is reminder"
    )
    title: Optional[str] = Field(
        default=None,
        description="Clean concise summary/title of the reminder or note (without time phrases)"
    )
    category: Optional[Literal["Учеба", "Покупки", "Софт/VPN", "Идеи", "Другое"]] = Field(
        default="Другое",
        description="Category for the note: Учеба, Покупки, Софт/VPN, Идеи, or Другое"
    )
    reply: Optional[str] = Field(
        default=None,
        description="Friendly and helpful conversational response if intent is chat"
    )

def build_ai_system_prompt(local_now: datetime, tz_offset: int) -> str:
    weekday_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    now_str = local_now.strftime("%Y-%m-%d %H:%M:%S")
    weekday_str = weekday_names[local_now.weekday()]
    tz_sign = "+" if tz_offset >= 0 else ""
    return f"""Ты — интеллектуальный персональный ассистент в Telegram.
Твоя задача — точно классифицировать входящее сообщение пользователя (написанное им лично или пересланное из другого чата/канала) и извлечь структурированные данные.

ТЕКУЩЕЕ ВРЕМЯ ПОЛЬЗОВАТЕЛЯ:
- Дата и время: {now_str}
- День недели: {weekday_str}
- Часовой пояс: UTC{tz_sign}{tz_offset}

ПРАВИЛА ОПРЕДЕЛЕНИЯ НАМЕРЕНИЯ (intent):
1. "reminder" (Напоминание) — если в сообщении содержится задача или действие с указанием времени, даты, дня недели или интервала:
   - Примеры: «завтра в 14:00 скинуть отчет по физике», «через 2 часа позвонить врачу», «в пятницу в 16:30 созвон», «напомни в 19:00 купить переходник», «через 15 минут выключить духовку».
   - datetime: СТРОГО вычисли дату и время в формате "YYYY-MM-DD HH:MM:SS" по местному времени пользователя относительно {now_str}.
     * «через N минут/часов/дней»: прибавь к {now_str}.
     * «завтра в HH:MM»: завтрашний день и указанное время.
     * «в пятницу в HH:MM» / день недели: найди ближайшую предстоящую дату этого дня недели.
     * если указано только время («в 15:00») и оно сегодня уже прошло, перенеси на завтра.
   - title: сформулируй четкую суть задачи БЕЗ слов о дате и времени (например: «Скинуть отчет по физике», «Позвонить врачу»).

2. "note" (Заметка) — если это список, покупка, ссылка, идея, технический конфиг, полезная информация или задача БЕЗ конкретного времени/дедлайна (ЛИБО если сообщение переслано и не содержит будущего времени):
   - Примеры: «купить переходник на Type-C», «сохрани пароль от Wi-Fi», «vless://... конфиг», «идея: сделать бота», «заметка: почитать про Docker», «список продуктов: хлеб, молоко, сыр».
   - category: выбери наиболее подходящую категорию:
     * "Учеба" — лабы, лекции, ДЗ, экзамены, отчеты, книги, курсы, университет;
     * "Покупки" — товары, продукты, заказы, магазины, чеки, список покупок;
     * "Софт/VPN" — VPN, vless, конфиги, пароли, ключи, код, серверы, Docker, скрипты, программы;
     * "Идеи" — стартапы, мысли, проекты, планы, гипотезы;
     * "Другое" — если ни одна категория выше не подходит.
   - title: сохраненный текст заметки (очищенный от вводных слов «сохрани», «заметка:» и т.п.).

3. "chat" (Диалог) — если сообщение является вопросом, просьбой что-то объяснить, написать код/стих, обычным приветствием или беседой («Привет!», «Как дела?», «Напиши скрипт на Python», «Что такое квазар?»):
   - reply: дай качественный, понятный и дружелюбный ответ на вопрос пользователя.

Всегда возвращай СТРОГО JSON по заданной схеме.
"""

def parse_fallback(text: str, now: datetime) -> dict:
    """Правило-ориентированный парсер на случай отсутствия доступа к Gemini API."""
    clean_text = text.strip()
    lower = clean_text.lower()
    
    # 1. Относительное время: "через N (мин/часов/дней)"
    rel_match = re.search(r'(?:напомни\s+)?через\s+(\d+)\s*(м|мин|минут|минуты|минуту|ч|час|часа|часов|д|дн|дня|дней)\s*(.*)', lower)
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2)
        title = re.sub(r'^(?:напомни\s+)?через\s+\d+\s*(?:м|мин|минут|минуты|минуту|ч|час|часа|часов|д|дн|дня|дней)\s*', '', clean_text, flags=re.IGNORECASE).strip()
        if not title:
            title = "Напоминание"
            
        if 'мин' in unit or unit == 'м':
            target_dt = now + timedelta(minutes=val)
        elif 'час' in unit or unit == 'ч':
            target_dt = now + timedelta(hours=val)
        elif 'дн' in unit or unit == 'д' or 'дня' in unit:
            target_dt = now + timedelta(days=val)
        else:
            target_dt = now + timedelta(minutes=val)
            
        return {
            "intent": "reminder",
            "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "title": title.capitalize(),
            "category": None
        }

    # 2. "завтра в HH:MM"
    tomorrow_match = re.search(r'(?:напомни\s+)?завтра\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)', lower)
    if tomorrow_match:
        hour = int(tomorrow_match.group(1))
        minute = int(tomorrow_match.group(2))
        title = re.sub(r'^(?:напомни\s+)?завтра\s+в\s+\d{1,2}[:.]\d{2}\s*', '', clean_text, flags=re.IGNORECASE).strip()
        if not title:
            title = "Напоминание"
        target_dt = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {
            "intent": "reminder",
            "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "title": title.capitalize(),
            "category": None
        }

    # 3. "сегодня в HH:MM" или "в HH:MM"
    today_match = re.search(r'(?:(?:напомни\s+)?сегодня\s+в\s+|(?:напомни\s+)?в\s+)(\d{1,2})[:.](\d{2})\s*(.*)', lower)
    if today_match:
        hour = int(today_match.group(1))
        minute = int(today_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            title = re.sub(r'^(?:напомни\s+)?(?:сегодня\s+)?в\s+\d{1,2}[:.]\d{2}\s*', '', clean_text, flags=re.IGNORECASE).strip()
            if not title:
                title = "Напоминание"
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_dt <= now:
                target_dt += timedelta(days=1)
            return {
                "intent": "reminder",
                "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "title": title.capitalize(),
                "category": None
            }

    # 4. Дни недели: "в пятницу в 16:30"
    weekdays = {
        "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1,
        "среду": 2, "среда": 2, "ср": 2, "четверг": 3, "чт": 3,
        "пятницу": 4, "пятница": 4, "пт": 4, "субботу": 5, "суббота": 5, "сб": 5,
        "воскресенье": 6, "вс": 6
    }
    weekday_pattern = r'(?:напомни\s+)?(?:в\s+|во\s+)?(' + '|'.join(weekdays.keys()) + r')\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)'
    weekday_match = re.search(weekday_pattern, lower)
    if weekday_match:
        day_name = weekday_match.group(1)
        hour = int(weekday_match.group(2))
        minute = int(weekday_match.group(3))
        target_day = weekdays[day_name]
        cur_day = now.weekday()
        days_ahead = (target_day - cur_day) % 7
        if days_ahead == 0:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                days_ahead = 7
        target_dt = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        title = re.sub(r'^(?:напомни\s+)?(?:в\s+|во\s+)?\w+\s+в\s+\d{1,2}[:.]\d{2}\s*', '', clean_text, flags=re.IGNORECASE).strip()
        if not title:
            title = "Напоминание"
        return {
            "intent": "reminder",
            "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "title": title.capitalize(),
            "category": None
        }

    # 5. Категоризация заметок
    cat_keywords = {
        "Покупки": ["купить", "покупки", "магазин", "заказать", "список покупок", "взять в магазине", "продукты", "чек", "цена", "рублей", "руб"],
        "Учеба": ["учеба", "лаба", "лекция", "универ", "физика", "матем", "дз", "отчет", "курсовая", "диплом", "реферат", "экзамен", "сессия", "домашка", "конспект"],
        "Софт/VPN": ["vpn", "vless", "shadowsocks", "wireguard", "прокси", "код", "git", "сервер", "пароль", "софт", "docker", "скрипт", "api", "баг", "linux", "ключ", "ip"],
        "Идеи": ["идея", "проект", "стартап", "мысль", "задумка", "подумать", "почитать", "посмотреть", "фича", "план"]
    }

    is_explicit_note = bool(re.match(r'^(?:заметка|запиши|запомни|сохрани|сохранить|note):\s*', clean_text, flags=re.IGNORECASE))
    clean_title = re.sub(r'^(?:заметка|запиши|запомни|сохрани|сохранить|note):\s*', '', clean_text, flags=re.IGNORECASE).strip()
    
    matched_category = None
    for cat, kws in cat_keywords.items():
        if any(kw in lower for kw in kws):
            matched_category = cat
            break

    if not matched_category and is_explicit_note:
        matched_category = "Другое"

    if matched_category:
        return {
            "intent": "note",
            "datetime": None,
            "title": clean_title if clean_title else clean_text,
            "category": matched_category
        }

    return {
        "intent": "chat",
        "datetime": None,
        "title": None,
        "category": None
    }

def parse_flexible_datetime(dt_str: str, now: datetime) -> Optional[datetime]:
    if not dt_str:
        return None
    dt_str = dt_str.strip().replace("T", " ")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            pass
            
    # Формат без года "DD.MM HH:MM"
    try:
        dt = datetime.strptime(f"{now.year}.{dt_str}", "%Y.%d.%m %H:%M")
        if dt < now:
            dt = dt.replace(year=now.year + 1)
        return dt
    except ValueError:
        pass
        
    return None

async def parse_user_intent(text: str, user_id: int, is_forwarded: bool = False, source_info: str = "") -> ParsedIntent:
    offset = get_user_tz_offset(user_id)
    tz = timezone(timedelta(hours=offset))
    local_now = datetime.now(tz)
    
    if ai_client:
        try:
            sys_prompt = build_ai_system_prompt(local_now, offset)
            input_text = text
            if is_forwarded:
                src_note = f" (переслано от {source_info})" if source_info else ""
                input_text = f"[Пересланное сообщение{src_note}]:\n{text}"
                
            config = genai_types.GenerateContentConfig(
                system_instruction=sys_prompt,
                response_mime_type="application/json",
                response_schema=ParsedIntent,
                temperature=0.2
            )
            
            response = await ai_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=input_text,
                config=config
            )
            
            parsed = ParsedIntent.model_validate_json(response.text)
            
            # Если пересланное сообщение было классифицировано как chat, делаем его заметкой
            if is_forwarded and parsed.intent == "chat":
                parsed.intent = "note"
                parsed.category = parsed.category or "Другое"
                parsed.title = parsed.title or text
                
            return parsed
        except Exception as e:
            logging.warning(f"Gemini API parse warning: {e}. Переключаемся на резервный парсер.")
            
    # Резервный парсер
    fallback = parse_fallback(text, local_now)
    if is_forwarded and fallback["intent"] == "chat":
        fallback["intent"] = "note"
        fallback["category"] = "Другое"
        fallback["title"] = text
        
    return ParsedIntent(
        intent=fallback["intent"],
        datetime=fallback.get("datetime"),
        title=fallback.get("title") or text,
        category=fallback.get("category") or "Другое",
        reply=None
    )

# ==============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОТПРАВКИ И РАЗБИЕНИЯ ТЕКСТА
# ==============================================================================

async def safe_reply_text(message: Message, text: str, reply_markup=None, parse_mode: str = "Markdown"):
    """Безопасная отправка длинных сообщений с разбивкой на куски <= 4000 символов."""
    if len(text) <= 4000:
        try:
            return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            return await message.answer(text, reply_markup=reply_markup)
            
    # Разбивка на части
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    last_msg = None
    for idx, chunk in enumerate(chunks):
        kb = reply_markup if idx == len(chunks) - 1 else None
        try:
            last_msg = await message.answer(chunk, reply_markup=kb, parse_mode=parse_mode)
        except Exception:
            last_msg = await message.answer(chunk, reply_markup=kb)
    return last_msg

# ==============================================================================
# КЛАВИАТУРЫ И КОМАНДЫ
# ==============================================================================

def get_main_keyboard(is_admin: bool):
    buttons = [
        [
            InlineKeyboardButton(text="⏰ Мои напоминания", callback_data="list_reminders"),
            InlineKeyboardButton(text="📝 Мои заметки", callback_data="list_notes")
        ],
        [
            InlineKeyboardButton(text="🔄 Сбросить диалог", callback_data="reset_chat"),
            InlineKeyboardButton(text="⚙️ Часовой пояс", callback_data="show_tz_menu")
        ]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="📋 Белый список (Админ)", callback_data="show_whitelist")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_timezone_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="Калининград (UTC+2)", callback_data="set_tz_2"),
            InlineKeyboardButton(text="Москва, СПб (UTC+3)", callback_data="set_tz_3")
        ],
        [
            InlineKeyboardButton(text="Самара (UTC+4)", callback_data="set_tz_4"),
            InlineKeyboardButton(text="Екатеринбург (UTC+5)", callback_data="set_tz_5")
        ],
        [
            InlineKeyboardButton(text="Омск (UTC+6)", callback_data="set_tz_6"),
            InlineKeyboardButton(text="Красноярск (UTC+7)", callback_data="set_tz_7")
        ],
        [
            InlineKeyboardButton(text="Иркутск (UTC+8)", callback_data="set_tz_8"),
            InlineKeyboardButton(text="Владивосток (UTC+10)", callback_data="set_tz_10")
        ],
        [
            InlineKeyboardButton(text="UTC 0 (Лондон)", callback_data="set_tz_0"),
            InlineKeyboardButton(text="🔙 Главное меню", callback_data="to_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def set_bot_commands(bot_instance: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню и статус"),
        BotCommand(command="reminders", description="Список активных напоминаний"),
        BotCommand(command="notes", description="Заметки по категориям"),
        BotCommand(command="remind", description="Создать напоминание: /remind <текст>"),
        BotCommand(command="note", description="Создать заметку: /note <текст>"),
        BotCommand(command="timezone", description="Настройка часового пояса"),
        BotCommand(command="reset", description="Сбросить контекст диалога"),
        BotCommand(command="help", description="Справка по всем возможностям"),
        BotCommand(command="whitelist", description="[Admin] Список ID"),
        BotCommand(command="allow", description="[Admin] Добавить ID"),
        BotCommand(command="deny", description="[Admin] Удалить ID"),
    ]
    await bot_instance.set_my_commands(commands)

# ==============================================================================
# КОМАНДЫ БОТА
# ==============================================================================

@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    allowed = get_allowed_users()
    if user_id not in allowed:
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return

    is_admin = (user_id == ADMIN_ID)
    tz_offset = get_user_tz_offset(user_id)
    tz_sign = "+" if tz_offset >= 0 else ""
    user_tz = timezone(timedelta(hours=tz_offset))
    now_user = datetime.now(user_tz).strftime("%H:%M")

    text = (
        f"👋 **Привет! Я твой персональный ИИ-ассистент.**\n\n"
        f"⏰ **Умные напоминания:** пиши «завтра в 14:00 скинуть отчет» или «через 2 часа позвонить».\n"
        f"📝 **Быстрые заметки:** пиши «купить переходник на Type-C», пароли, списки или просто пересылай мне сообщения из любых каналов!\n"
        f"💬 **ИИ-диалог:** задавай любые вопросы, как в ChatGPT.\n\n"
        f"🕒 Твой часовой пояс: `UTC{tz_sign}{tz_offset}` (сейчас {now_user})."
    )
    await safe_reply_text(message, text, reply_markup=get_main_keyboard(is_admin))

@dp.message(Command("help"))
async def help_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен.")
        return

    text = (
        "📖 **Справка по возможностям бота:**\n\n"
        "⏰ **1. Умные напоминания:**\n"
        "• Отправь сообщение с временем: «_завтра в 14:00 скинуть отчет по физике_» или «_через 30 минут выключить духовку_».\n"
        "• Бот сам рассчитает точный момент и пришлет пуш-уведомление с кнопками «Выполнено» и «Отложить».\n"
        "• Команда `/reminders` — посмотреть активные напоминания и управлять ими.\n"
        "• Команда `/remind <текст>` — быстрое создание напоминания.\n\n"
        "📝 **2. Быстрые заметки и пересылка сообщений:**\n"
        "• Напиши в чат то, что хочешь запомнить (например: «_купить переходник на Type-C_», «_vless://... конфиг_»).\n"
        "• Или **перешли сообщение из любого чата/канала** — бот автоматически определит категорию (`Учеба`, `Покупки`, `Софт/VPN`, `Идеи`, `Другое`) и сохранит его!\n"
        "• Команда `/notes` — просмотр сохраненных заметок по категориям.\n"
        "• Команда `/note <текст>` — быстрое сохранение заметки.\n\n"
        "💬 **3. Общение с ИИ:**\n"
        "• Задавай любые вопросы — бот ответит с учетом контекста беседы.\n"
        "• Команда `/reset` — очистить историю диалога.\n\n"
        "⚙️ **4. Настройки:**\n"
        "• Команда `/timezone` — выбор часового пояса для точных напоминаний."
    )
    await safe_reply_text(message, text)

@dp.message(Command("reset"))
async def reset_cmd(message: Message):
    user_id = message.from_user.id
    user_sessions.pop(user_id, None)
    await message.answer("🔄 Контекст вашего диалога с ИИ успешно сброшен!")

@dp.message(Command("timezone"))
async def timezone_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        return
        
    args = message.text.split()
    if len(args) > 1:
        try:
            offset = int(args[1].replace("+", ""))
            if -12 <= offset <= 14:
                set_user_tz_offset(user_id, offset)
                tz_sign = "+" if offset >= 0 else ""
                now_str = datetime.now(timezone(timedelta(hours=offset))).strftime("%H:%M")
                await message.answer(f"✅ Часовой пояс установлен на `UTC{tz_sign}{offset}` (сейчас у вас {now_str}).", parse_mode="Markdown")
                return
        except ValueError:
            pass

    offset = get_user_tz_offset(user_id)
    tz_sign = "+" if offset >= 0 else ""
    now_str = datetime.now(timezone(timedelta(hours=offset))).strftime("%H:%M")
    text = (
        f"⚙️ **Настройка часового пояса:**\n\n"
        f"Текущий пояс: `UTC{tz_sign}{offset}` (местное время: **{now_str}**).\n"
        f"Выберите ваш регион ниже или укажите числом: `/timezone 3`"
    )
    await message.answer(text, reply_markup=get_timezone_keyboard(), parse_mode="Markdown")

# --- Команды Модуля 3: /reminders и /notes ---

@dp.message(Command("reminders"))
async def reminders_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        return
    await show_reminders_list(message.chat.id, user_id)

async def show_reminders_list(chat_id: int, user_id: int, message_to_edit: Optional[Message] = None):
    reminders = get_active_reminders(user_id)
    if not reminders:
        text = (
            "⏰ **У вас нет активных напоминаний.**\n\n"
            "Чтобы создать напоминание, просто напишите мне, например:\n"
            "• «_завтра в 14:00 скинуть отчет по физике_»\n"
            "• «_через 2 часа позвонить врачу_»\n"
            "• или перешлите мне сообщение из любого чата!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="list_reminders")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="to_main_menu")]
        ])
        if message_to_edit:
            await message_to_edit.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown")
        return

    text_lines = ["⏰ **Ваши активные напоминания:**\n"]
    buttons = []
    
    for idx, (rem_id, rem_text, rem_local, rem_utc, src_info) in enumerate(reminders[:8], 1):
        src_note = f" _(от {src_info})_" if src_info else ""
        text_lines.append(f"**{idx}.** 📅 `{rem_local}`\n📌 {rem_text}{src_note}\n")
        # Кнопки для управления каждым напоминанием
        buttons.append([
            InlineKeyboardButton(text=f"✅ Выполнить #{idx}", callback_data=f"done_rem_{rem_id}"),
            InlineKeyboardButton(text=f"❌ Отменить #{idx}", callback_data=f"cancel_rem_{rem_id}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="list_reminders"),
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="to_main_menu")
    ])
    
    full_text = "\n".join(text_lines)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if message_to_edit:
        try:
            await message_to_edit.edit_text(full_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await message_to_edit.edit_text(full_text, reply_markup=kb)
    else:
        try:
            await bot.send_message(chat_id=chat_id, text=full_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=full_text, reply_markup=kb)

@dp.message(Command("notes"))
async def notes_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        return
    await show_notes_categories(message.chat.id, user_id)

async def show_notes_categories(chat_id: int, user_id: int, message_to_edit: Optional[Message] = None):
    stats = get_notes_categories_stats(user_id)
    total_notes = sum(stats.values())
    
    text = (
        f"📝 **Ваши сохраненные заметки** (всего: {total_notes}):\n\n"
        f"Выберите категорию для просмотра:\n"
        f"• 🎓 **Учеба**: {stats.get('Учеба', 0)} шт.\n"
        f"• 🛒 **Покупки**: {stats.get('Покупки', 0)} шт.\n"
        f"• 💻 **Софт/VPN**: {stats.get('Софт/VPN', 0)} шт.\n"
        f"• 💡 **Идеи**: {stats.get('Идеи', 0)} шт.\n"
        f"• 📌 **Другое**: {stats.get('Другое', 0)} шт."
    )
    
    buttons = [
        [
            InlineKeyboardButton(text=f"🎓 Учеба ({stats.get('Учеба', 0)})", callback_data="view_cat_Учеба"),
            InlineKeyboardButton(text=f"🛒 Покупки ({stats.get('Покупки', 0)})", callback_data="view_cat_Покупки")
        ],
        [
            InlineKeyboardButton(text=f"💻 Софт/VPN ({stats.get('Софт/VPN', 0)})", callback_data="view_cat_Софт/VPN"),
            InlineKeyboardButton(text=f"💡 Идеи ({stats.get('Идеи', 0)})", callback_data="view_cat_Идеи")
        ],
        [
            InlineKeyboardButton(text=f"📌 Другое ({stats.get('Другое', 0)})", callback_data="view_cat_Другое"),
            InlineKeyboardButton(text="📋 Все заметки", callback_data="view_all_notes")
        ],
        [
            InlineKeyboardButton(text="🔙 Главное меню", callback_data="to_main_menu")
        ]
    ]
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if message_to_edit:
        await message_to_edit.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("remind"))
async def explicit_remind_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        return
        
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/remind завтра в 14:00 скинуть отчет`", parse_mode="Markdown")
        return
        
    await process_incoming_text(message, args[1], is_forwarded=False, force_intent="reminder")

@dp.message(Command("note"))
async def explicit_note_cmd(message: Message):
    user_id = message.from_user.id
    if user_id not in get_allowed_users():
        return
        
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/note купить переходник на Type-C`", parse_mode="Markdown")
        return
        
    await process_incoming_text(message, args[1], is_forwarded=False, force_intent="note")

# --- Административные команды ---
@dp.message(Command("allow"))
async def allow_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: `/allow <telegram_id>`", parse_mode="Markdown")
        return
    target_id = int(args[1])
    add_to_whitelist(target_id)
    await message.answer(f"✅ Пользователь `{target_id}` сохранен в базу данных и добавлен в белый список.", parse_mode="Markdown")

@dp.message(Command("deny"))
async def deny_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: `/deny <telegram_id>`", parse_mode="Markdown")
        return
    target_id = int(args[1])
    if target_id == ADMIN_ID:
        await message.answer("❌ Нельзя удалить себя из белого списка!")
        return
    remove_from_whitelist(target_id)
    user_sessions.pop(target_id, None)
    await message.answer(f"🚫 Пользователь `{target_id}` удален из базы данных и белого списка.", parse_mode="Markdown")

@dp.message(Command("whitelist"))
async def whitelist_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    users = get_allowed_users()
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(users)])
    await message.answer(f"📋 **Белый список (из БД):**\n{users_list}", parse_mode="Markdown")

# ==============================================================================
# ОБРАБОТКА CALLBACK ЗАПРОСОВ (ИНТЕРАКТИВНЫЕ КНОПКИ)
# ==============================================================================

@dp.callback_query(F.data == "to_main_menu")
async def cb_main_menu(callback: CallbackQuery):
    is_admin = (callback.from_user.id == ADMIN_ID)
    await callback.answer()
    try:
        await callback.message.edit_text("👋 Главное меню бота:", reply_markup=get_main_keyboard(is_admin))
    except Exception:
        pass

@dp.callback_query(F.data == "reset_chat")
async def cb_reset(callback: CallbackQuery):
    user_id = callback.from_user.id
    user_sessions.pop(user_id, None)
    await callback.answer("Контекст очищен!")
    await callback.message.answer("🔄 Контекст общения сброшен.")

@dp.callback_query(F.data == "show_whitelist")
async def cb_whitelist(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    users = get_allowed_users()
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(users)])
    await callback.answer()
    await callback.message.answer(f"📋 **Белый список (из БД):**\n{users_list}", parse_mode="Markdown")

@dp.callback_query(F.data == "list_reminders")
async def cb_list_reminders(callback: CallbackQuery):
    await callback.answer()
    await show_reminders_list(callback.message.chat.id, callback.from_user.id, message_to_edit=callback.message)

@dp.callback_query(F.data == "list_notes")
async def cb_list_notes(callback: CallbackQuery):
    await callback.answer()
    await show_notes_categories(callback.message.chat.id, callback.from_user.id, message_to_edit=callback.message)

@dp.callback_query(F.data == "show_tz_menu")
async def cb_tz_menu(callback: CallbackQuery):
    await callback.answer()
    offset = get_user_tz_offset(callback.from_user.id)
    tz_sign = "+" if offset >= 0 else ""
    now_str = datetime.now(timezone(timedelta(hours=offset))).strftime("%H:%M")
    text = (
        f"⚙️ **Настройка часового пояса:**\n\n"
        f"Текущий пояс: `UTC{tz_sign}{offset}` (сейчас {now_str}).\n"
        f"Выберите ваш регион:"
    )
    await callback.message.edit_text(text, reply_markup=get_timezone_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_tz_"))
async def cb_set_timezone(callback: CallbackQuery):
    try:
        offset = int(callback.data.split("_")[2])
        set_user_tz_offset(callback.from_user.id, offset)
        tz_sign = "+" if offset >= 0 else ""
        now_str = datetime.now(timezone(timedelta(hours=offset))).strftime("%H:%M")
        await callback.answer(f"Часовой пояс сохранен: UTC{tz_sign}{offset}!")
        is_admin = (callback.from_user.id == ADMIN_ID)
        await callback.message.edit_text(
            f"✅ **Часовой пояс успешно обновлен:** `UTC{tz_sign}{offset}` (время: {now_str}).",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.answer(f"Ошибка: {e}")

# --- Действия с напоминаниями: Выполнить, Отменить, Отложить ---

@dp.callback_query(F.data.startswith("cancel_rem_"))
async def cb_cancel_reminder(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    rem = get_reminder_by_id(rem_id)
    if not rem:
        await callback.answer("Напоминание не найдено.")
        return
    set_reminder_status(rem_id, "canceled")
    await callback.answer("Напоминание отменено!")
    try:
        await callback.message.edit_text(
            f"❌ **Напоминание отменено:**\n~{rem[3]}~",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏰ Мои напоминания", callback_data="list_reminders")]
            ]),
            parse_mode="Markdown"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("done_rem_"))
async def cb_done_reminder(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    rem = get_reminder_by_id(rem_id)
    if not rem:
        await callback.answer("Напоминание не найдено.")
        return
    set_reminder_status(rem_id, "done")
    await callback.answer("Выполнено! 🎉")
    try:
        await callback.message.edit_text(
            f"✅ **Напоминание выполнено:**\n📌 {rem[3]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏰ Мои напоминания", callback_data="list_reminders")]
            ]),
            parse_mode="Markdown"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("snooze_"))
async def cb_snooze_reminder(callback: CallbackQuery):
    # snooze_{rem_id}_{minutes}
    parts = callback.data.split("_")
    rem_id = int(parts[1])
    minutes = int(parts[2])
    
    offset = get_user_tz_offset(callback.from_user.id)
    new_local, new_utc = snooze_reminder_by_id(rem_id, minutes, offset)
    if not new_local:
        await callback.answer("Напоминание не найдено.")
        return
        
    await callback.answer(f"Отложено на {minutes} минут!")
    rem = get_reminder_by_id(rem_id)
    try:
        await callback.message.edit_text(
            f"⏰ **Напоминание отложено на {minutes} мин.** (до {new_local}):\n📌 {rem[3]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_rem_{rem_id}")],
                [InlineKeyboardButton(text="⏰ Все напоминания", callback_data="list_reminders")]
            ]),
            parse_mode="Markdown"
        )
    except Exception:
        pass

# --- Действия с заметками: Просмотр категорий, Удаление, Очистка ---

@dp.callback_query(F.data.startswith("view_cat_"))
async def cb_view_category(callback: CallbackQuery):
    category = callback.data.replace("view_cat_", "")
    user_id = callback.from_user.id
    await callback.answer()
    
    notes = get_notes_by_cat(user_id, category, limit=10)
    emoji = CATEGORY_EMOJIS.get(category, "📌")
    
    if not notes:
        text = f"{emoji} В категории **{category}** пока нет заметок."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    text_lines = [f"{emoji} **Заметки в категории [{category}]** ({len(notes)} шт.):\n"]
    buttons = []
    
    for idx, (n_id, n_content, n_date) in enumerate(notes, 1):
        clean_content = n_content.strip()
        text_lines.append(f"**{idx}.** • {clean_content}")
        # Кнопка быстрого удаления
        buttons.append([
            InlineKeyboardButton(text=f"🗑 Удалить #{idx}", callback_data=f"del_note_{n_id}_{category}")
        ])
        
    buttons.append([
        InlineKeyboardButton(text="🗑 Очистить категорию", callback_data=f"clear_cat_{category}"),
        InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")
    ])
    
    full_text = "\n".join(text_lines)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(full_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.edit_text(full_text, reply_markup=kb)

@dp.callback_query(F.data.startswith("del_note_"))
async def cb_delete_note(callback: CallbackQuery):
    parts = callback.data.split("_")
    note_id = int(parts[2])
    category = parts[3] if len(parts) > 3 else None
    
    note = get_note_by_id(note_id)
    delete_note_by_id(note_id, callback.from_user.id)
    await callback.answer("Заметка удалена!")
    
    if category:
        # Обновляем вид категории
        notes = get_notes_by_cat(callback.from_user.id, category, limit=10)
        emoji = CATEGORY_EMOJIS.get(category, "📌")
        if not notes:
            text = f"{emoji} В категории **{category}** нет заметок."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
            
        text_lines = [f"{emoji} **Заметки в категории [{category}]** ({len(notes)} шт.):\n"]
        buttons = []
        for idx, (n_id, n_content, n_date) in enumerate(notes, 1):
            text_lines.append(f"**{idx}.** • {n_content.strip()}")
            buttons.append([
                InlineKeyboardButton(text=f"🗑 Удалить #{idx}", callback_data=f"del_note_{n_id}_{category}")
            ])
        buttons.append([
            InlineKeyboardButton(text="🗑 Очистить категорию", callback_data=f"clear_cat_{category}"),
            InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")
        ])
        await callback.message.edit_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    else:
        content = note[3] if note else ""
        await callback.message.edit_text(f"🗑 **Заметка удалена:**\n~{content}~", reply_markup=None, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("clear_cat_"))
async def cb_clear_category(callback: CallbackQuery):
    category = callback.data.replace("clear_cat_", "")
    clear_notes_by_cat(callback.from_user.id, category)
    await callback.answer(f"Категория {category} очищена!")
    emoji = CATEGORY_EMOJIS.get(category, "📌")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")]
    ])
    await callback.message.edit_text(f"{emoji} Категория **{category}** успешно очищена.", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "view_all_notes")
async def cb_view_all_notes(callback: CallbackQuery):
    await callback.answer()
    notes = get_all_active_notes(callback.from_user.id, limit=20)
    if not notes:
        text = "📝 У вас пока нет сохраненных заметок."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        return
        
    text_lines = ["📋 **Все ваши недавние заметки:**\n"]
    for idx, (n_id, n_cat, n_content, n_date) in enumerate(notes, 1):
        cat_emoji = CATEGORY_EMOJIS.get(n_cat, "📌")
        text_lines.append(f"**{idx}.** {cat_emoji} `[{n_cat}]` {n_content.strip()}")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К категориям", callback_data="list_notes")]
    ])
    await callback.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="Markdown")

# ==============================================================================
# ОБРАБОТКА ВХОДЯЩИХ ТЕКСТОВЫХ И ПЕРЕСЛАННЫХ СООБЩЕНИЙ (Модуль 3)
# ==============================================================================

async def process_incoming_text(message: Message, raw_text: str, is_forwarded: bool = False, source_info: str = "", force_intent: Optional[str] = None):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    # 1. Интеллектуальный ИИ-парсинг намерения
    parsed: ParsedIntent = await parse_user_intent(raw_text, user_id, is_forwarded=is_forwarded, source_info=source_info)
    
    if force_intent:
        parsed.intent = force_intent
        
    user_offset = get_user_tz_offset(user_id)
    user_tz = timezone(timedelta(hours=user_offset))
    local_now = datetime.now(user_tz)

    # 2. Сценарий: НАПОМИНАНИЕ (есть дата/время)
    if parsed.intent == "reminder":
        target_dt = parse_flexible_datetime(parsed.datetime, local_now) if parsed.datetime else None
        
        # Если время не удалось спарсить из ИИ, пробуем через fallback
        if not target_dt:
            fb = parse_fallback(raw_text, local_now)
            if fb.get("datetime"):
                target_dt = parse_flexible_datetime(fb["datetime"], local_now)
                
        # Если все равно не определили точное время, ставим по умолчанию через 1 час или просим уточнить
        if not target_dt:
            target_dt = local_now + timedelta(hours=1)
            
        # Рассчитываем UTC datetime для точного воркера
        target_dt_aware = target_dt.replace(tzinfo=user_tz)
        target_dt_utc = target_dt_aware.astimezone(timezone.utc)
        
        utc_str = target_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        local_display_str = target_dt.strftime("%d.%m в %H:%M")
        reminder_title = parsed.title or raw_text
        
        rem_id = add_reminder(
            user_id=user_id,
            chat_id=chat_id,
            text=reminder_title,
            remind_at_utc=utc_str,
            remind_at_local=local_display_str,
            source_info=source_info
        )
        
        src_note = f"\n💬 _Переслано от: {source_info}_" if source_info else ""
        card_text = (
            f"✅ **Напоминание установлено на {local_display_str}:**\n"
            f"📌 {reminder_title}{src_note}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_rem_{rem_id}")],
            [InlineKeyboardButton(text="⏰ Все напоминания", callback_data="list_reminders")]
        ])
        await safe_reply_text(message, card_text, reply_markup=kb)
        return

    # 3. Сценарий: ЗАМЕТКА (нет времени или пересланный контент)
    elif parsed.intent == "note":
        category = parsed.category or "Другое"
        if category not in NOTE_CATEGORIES:
            category = "Другое"
            
        note_content = parsed.title or raw_text
        note_id = add_note(user_id=user_id, category=category, content=note_content)
        
        emoji = CATEGORY_EMOJIS.get(category, "📌")
        src_note = f"\n💬 _Переслано от: {source_info}_" if source_info else ""
        card_text = (
            f"📝 **Заметка сохранена в категорию [{category}]** {emoji}\n\n"
            f"• {note_content}{src_note}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_note_{note_id}"),
                InlineKeyboardButton(text=f"📁 Открыть «{category}»", callback_data=f"view_cat_{category}")
            ],
            [InlineKeyboardButton(text="📋 Все заметки", callback_data="list_notes")]
        ])
        await safe_reply_text(message, card_text, reply_markup=kb)
        return

    # 4. Сценарий: ОБЫЧНЫЙ ЧАТ С ИИ
    else:
        # Если ИИ в первом запросе уже сформировал содержательный ответ
        if parsed.reply:
            await safe_reply_text(message, parsed.reply)
            return
            
        # Иначе используем многосессионный чат
        user_chat = get_user_chat(user_id)
        if not user_chat:
            await message.answer("⚠️ Ошибка: Gemini API не подключен или не задан GEMINI_API_KEY.")
            return

        try:
            response = await user_chat.send_message(raw_text)
            await safe_reply_text(message, response.text)
        except Exception as e:
            logging.error(f"Ошибка Gemini Chat: {e}")
            await message.answer(f"⚠️ Ошибка при запросе к ИИ: {e}")

@dp.message(F.text | F.caption)
async def handle_message(message: Message):
    user_id = message.from_user.id
    allowed = get_allowed_users()
    if user_id not in allowed:
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return

    # Игнорируем команды, они обрабатываются соответствующими хэндлерами
    text_content = message.text or message.caption or ""
    if text_content.startswith("/"):
        return

    # Проверка, переслано ли сообщение
    is_forwarded = bool(
        message.forward_origin or
        message.forward_date or
        message.forward_from or
        message.forward_from_chat or
        message.forward_sender_name
    )

    source_info = ""
    if message.forward_origin:
        orig = message.forward_origin
        if hasattr(orig, "sender_user") and orig.sender_user:
            source_info = orig.sender_user.full_name
        elif hasattr(orig, "chat") and orig.chat:
            source_info = orig.chat.title or "Канал/Группа"
        elif hasattr(orig, "sender_user_name") and orig.sender_user_name:
            source_info = orig.sender_user_name
    elif message.forward_from:
        source_info = message.forward_from.full_name
    elif message.forward_from_chat:
        source_info = message.forward_from_chat.title or "Чат"
    elif message.forward_sender_name:
        source_info = message.forward_sender_name

    await process_incoming_text(
        message=message,
        raw_text=text_content,
        is_forwarded=is_forwarded,
        source_info=source_info
    )

# ==============================================================================
# ФОНОВЫЙ ВОРКЕР НАПОМИНАНИЙ (Push-уведомления в ЛС)
# ==============================================================================

async def reminder_worker(bot_instance: Bot):
    logging.info("Фоновый воркер напоминаний запущен.")
    while True:
        try:
            now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            due_reminders = get_due_reminders(now_utc_str)
            
            for rem_id, user_id, chat_id, rem_text, rem_local, src_info in due_reminders:
                mark_reminder_sent(rem_id)
                src_note = f"\n\n💬 _(Переслано от: {src_info})_" if src_info else ""
                msg_text = (
                    f"⏰ **НАПОМИНАНИЕ!**\n\n"
                    f"📌 {rem_text}{src_note}\n\n"
                    f"_(Запланировано на {rem_local})_"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_rem_{rem_id}"),
                        InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snooze_{rem_id}_15"),
                        InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze_{rem_id}_60"),
                    ]
                ])
                try:
                    await bot_instance.send_message(
                        chat_id=chat_id,
                        text=msg_text,
                        reply_markup=kb,
                        parse_mode="Markdown"
                    )
                except Exception as send_err:
                    logging.error(f"Не удалось отправить напоминание {rem_id} пользователю {user_id}: {send_err}")
                    try:
                        await bot_instance.send_message(
                            chat_id=chat_id,
                            text=f"⏰ НАПОМИНАНИЕ!\n\n📌 {rem_text}\n\n(Запланировано на {rem_local})",
                            reply_markup=kb
                        )
                    except Exception:
                        pass
        except asyncio.CancelledError:
            logging.info("Фоновый воркер напоминаний остановлен.")
            break
        except Exception as e:
            logging.error(f"Ошибка в цикле reminder_worker: {e}")

        await asyncio.sleep(10)

# ==============================================================================
# ВЕБ-АДМИНКА (aiohttp с защитой сессий и статистикой)
# ==============================================================================

def get_login_html(error: str = "") -> str:
    error_block = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Вход в панель управления</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #e0e0e0; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
        .card {{ background: #1e1e1e; padding: 32px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.6); width: 100%; max-width: 360px; border: 1px solid #2d2d2d; }}
        h2 {{ margin-top: 0; color: #fff; text-align: center; font-size: 22px; }}
        input[type="password"] {{ width: 100%; padding: 12px; margin: 16px 0; border-radius: 6px; border: 1px solid #3d3d3d; background: #2a2a2a; color: #fff; font-size: 15px; outline: none; }}
        input[type="password"]:focus {{ border-color: #3b82f6; }}
        button {{ width: 100%; padding: 12px; background: #2563eb; border: none; color: #fff; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: 500; transition: background 0.2s; }}
        button:hover {{ background: #1d4ed8; }}
        .error {{ color: #ef4444; margin-top: 14px; text-align: center; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>🤖 Админ-панель бота</h2>
        <form method="POST" action="/login">
            <input type="password" name="password" placeholder="Введите пароль администратора" required autofocus>
            <button type="submit">Войти</button>
        </form>
        {error_block}
    </div>
</body>
</html>"""

def get_dashboard_html(users_rows: str, stats: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Панель управления ИИ-ассистентом</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 24px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #2d2d2d; }}
        h1 {{ margin: 0; font-size: 24px; color: #fff; }}
        .logout {{ background: #374151; color: #fff; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 14px; }}
        .logout:hover {{ background: #4b5563; }}
        
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .stat-card {{ background: #1e1e1e; border: 1px solid #2d2d2d; padding: 16px; border-radius: 8px; }}
        .stat-title {{ font-size: 13px; color: #9ca3af; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-val {{ font-size: 26px; font-weight: bold; color: #3b82f6; }}
        
        .card {{ background: #1e1e1e; border: 1px solid #2d2d2d; padding: 20px; border-radius: 8px; margin-bottom: 24px; }}
        .card h3 {{ margin-top: 0; color: #fff; font-size: 18px; margin-bottom: 16px; }}
        .form-row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
        input[type="text"] {{ padding: 10px 14px; border-radius: 6px; border: 1px solid #3d3d3d; background: #2a2a2a; color: #fff; font-size: 14px; flex: 1; min-width: 200px; outline: none; }}
        input[type="text"]:focus {{ border-color: #3b82f6; }}
        button.btn {{ padding: 10px 18px; background: #2563eb; border: none; color: #fff; border-radius: 6px; cursor: pointer; font-weight: 500; font-size: 14px; }}
        button.btn:hover {{ background: #1d4ed8; }}
        button.del {{ background: #dc2626; padding: 6px 12px; border: none; color: #fff; border-radius: 4px; cursor: pointer; font-size: 13px; }}
        button.del:hover {{ background: #b91c1c; }}
        
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }}
        th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid #2d2d2d; }}
        th {{ background: #262626; color: #9ca3af; font-weight: 600; }}
        tr:hover td {{ background: #242424; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Панель управления ИИ-ассистентом</h1>
            <a href="/logout" class="logout">Выйти</a>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-title">👥 В белом списке</div>
                <div class="stat-val">{stats.get('users', 0)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">⏰ Активных напоминаний</div>
                <div class="stat-val">{stats.get('active_reminders', 0)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">🔔 Всего напоминаний</div>
                <div class="stat-val">{stats.get('total_reminders', 0)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">📝 Активных заметок</div>
                <div class="stat-val">{stats.get('active_notes', 0)}</div>
            </div>
        </div>

        <div class="card">
            <h3>➕ Добавить пользователя в Белый Список</h3>
            <form method="POST" action="/add">
                <div class="form-row">
                    <input type="text" name="user_id" placeholder="Telegram User ID (число)" required>
                    <input type="text" name="username" placeholder="Username (необязательно)">
                    <button type="submit" class="btn">Добавить</button>
                </div>
            </form>
        </div>

        <div class="card">
            <h3>👥 Разрешенные пользователи</h3>
            <table>
                <thead>
                    <tr>
                        <th>User ID</th>
                        <th>Username</th>
                        <th style="width: 100px;">Действие</th>
                    </tr>
                </thead>
                <tbody>
                    {users_rows}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""

def is_authenticated(request) -> bool:
    return request.cookies.get("auth_token") == AUTH_SECRET

async def handle_root(request):
    if is_authenticated(request):
        users = get_whitelist_with_names()
        stats = get_db_stats()
        rows = ""
        for u_id, u_name in users:
            del_btn = ""
            if u_id != ADMIN_ID:
                del_btn = f"""
                <form method="POST" action="/delete" style="margin:0;">
                    <input type="hidden" name="user_id" value="{u_id}">
                    <button type="submit" class="del">Удалить</button>
                </form>"""
            else:
                del_btn = "<span style='color:#10b981;font-size:13px;'>Владелец</span>"
                
            safe_name = html.escape(u_name) if u_name else 'N/A'
            rows += f"""
            <tr>
                <td><code>{u_id}</code></td>
                <td>@{safe_name}</td>
                <td>{del_btn}</td>
            </tr>"""
        if not rows:
            rows = "<tr><td colspan='3' style='text-align:center;'>Список пуст</td></tr>"
        return web.Response(text=get_dashboard_html(rows, stats), content_type="text/html", charset="utf-8")
    return web.Response(text=get_login_html(), content_type="text/html", charset="utf-8")

async def handle_login(request):
    data = await request.post()
    if data.get("password") == ADMIN_PASSWORD:
        response = web.HTTPFound('/')
        response.set_cookie("auth_token", AUTH_SECRET, max_age=86400 * 7, httponly=True)
        raise response
    return web.Response(text=get_login_html("Неверный пароль администратора"), content_type="text/html", charset="utf-8")

async def handle_logout(request):
    response = web.HTTPFound('/')
    response.del_cookie("auth_token")
    raise response

async def handle_add(request):
    if not is_authenticated(request):
        raise web.HTTPFound('/')
    data = await request.post()
    u_id = data.get("user_id")
    u_name = data.get("username", "").replace("@", "")
    if u_id and u_id.isdigit():
        add_to_whitelist(int(u_id), u_name)
    raise web.HTTPFound('/')

async def handle_delete(request):
    if not is_authenticated(request):
        raise web.HTTPFound('/')
    data = await request.post()
    u_id = data.get("user_id")
    if u_id and u_id.isdigit():
        target_id = int(u_id)
        if target_id != ADMIN_ID:
            remove_from_whitelist(target_id)
            user_sessions.pop(target_id, None)
    raise web.HTTPFound('/')

# ==============================================================================
# ТОЧКА ВХОДА И ЗАПУСК ПРИЛОЖЕНИЯ
# ==============================================================================

async def main():
    if not BOT_TOKEN:
        logging.error("КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не задан в .env файле!")
        return

    # Настройка веб-сервера
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_post('/login', handle_login)
    app.router.add_get('/logout', handle_logout)
    app.router.add_post('/add', handle_add)
    app.router.add_post('/delete', handle_delete)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)

    logging.info(f"Запуск панели управления на http://0.0.0.0:{PORT}...")
    await site.start()

    # Установка подсказок команд в Telegram
    await set_bot_commands(bot)

    # Запуск фонового воркера для напоминаний (Модуль 3)
    worker_task = asyncio.create_task(reminder_worker(bot))

    logging.info("Запуск Telegram-бота...")
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())