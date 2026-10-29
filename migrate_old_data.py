"""Разовый перенос данных со старой версии бота (без автобэкапа) перед первым деплоем новой.

Скрипт забирает данные у работающего бота через API веб-панели, собирает из них базу
в формате бэкапа и закрепляет её в чате с ботом. Новая версия при первом запуске на
чистом диске найдёт закреплённый бэкап и восстановится из него сама.

Запуск (ссылку взять командой /panel у бота):
    py migrate_old_data.py "https://tg-ai-assistant-bot.onrender.com/#token=..."
    py migrate_old_data.py "<ссылка>" --no-send   # только собрать файл, в Telegram не слать

Переносится то, что отдаёт API старой версии: заметки (до 100 последних), активные
напоминания, ночные сообщения, лента ИИ, режим, часовой пояс и ключ панели (ссылка
из /panel продолжит работать). Чёрный список, досье и отключённые чаты API не отдаёт.
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_PREFIX = "business_bot_backup"  # как в main.py — по этому префиксу бот ищет бэкап


def parse_panel_link(link: str):
    parts = urlsplit(link.strip())
    fragment = dict(p.split("=", 1) for p in parts.fragment.split("&") if "=" in p)
    token = fragment.get("token")
    if not parts.scheme or not parts.netloc or not token:
        sys.exit("⚠️ Нужна ссылка вида https://…/#token=… — её присылает команда /panel.")
    return f"{parts.scheme}://{parts.netloc}", token


def fetch_old_data(base_url: str, token: str) -> dict:
    request = urllib.request.Request(f"{base_url}/api/data", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("⚠️ Ключ не подошёл (401). Возьми свежую ссылку командой /panel.")
        if e.code == 429:
            sys.exit("⚠️ Слишком много попыток (429). Подожди 10 минут.")
        sys.exit(f"⚠️ Сервер ответил ошибкой {e.code}.")
    except urllib.error.URLError as e:
        sys.exit(f"⚠️ Не удалось связаться с ботом: {e.reason}. Если он спит на Render — открой ссылку в браузере и повтори.")
    if not data.get("ok"):
        sys.exit(f"⚠️ API вернул ошибку: {data.get('error')}")
    return data


def build_backup(data: dict, token: str, owner_id: int, path: str) -> dict:
    """Создаёт базу по схеме новой версии и заполняет её данными старой"""
    # db.py создаёт базу в текущей папке при импорте — импортируем его во временной папке
    cwd = os.getcwd()
    os.chdir(os.path.dirname(path))
    try:
        sys.path.insert(0, PROJECT_DIR)
        import db
        db.DB_NAME = os.path.basename(path)
        db.init_db()
    finally:
        os.chdir(cwd)

    conn = sqlite3.connect(path)
    counts = {}
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('status', ?)", (data.get("status") or "default",))
        # Тот же ключ панели — ссылка из /panel продолжит работать после деплоя
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dashboard_token', ?)", (token,))
        if owner_id and data.get("timezone_offset") is not None:
            conn.execute("INSERT OR REPLACE INTO user_settings (user_id, timezone_offset) VALUES (?, ?)",
                         (owner_id, int(data["timezone_offset"])))

        reminders = data.get("reminders") or []
        conn.executemany("""
            INSERT INTO reminders (id, user_id, chat_id, text, remind_at_utc, remind_at_local, created_at, status, source_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(r["id"], r["user_id"], r["chat_id"], r["text"], r["remind_at_utc"], r["remind_at_local"],
               r.get("created_at") or "", r.get("status") or "pending", r.get("source_info") or "") for r in reminders])
        counts["напоминаний"] = len(reminders)

        notes = data.get("notes") or []
        conn.executemany("""
            INSERT INTO notes (id, user_id, category, content, created_at, is_archived) VALUES (?, ?, ?, ?, ?, 0)
        """, [(n["id"], n["user_id"], n["category"], n["content"], n["created_at"]) for n in notes])
        counts["заметок"] = len(notes)

        night = data.get("night_logs") or []
        conn.executemany("""
            INSERT INTO night_logs (id, sender_name, message_text, timestamp) VALUES (?, ?, ?, ?)
        """, [(n["id"], n["sender_name"], n["message_text"], n["timestamp"]) for n in night])
        counts["ночных сообщений"] = len(night)

        activity = data.get("activity") or []
        conn.executemany("""
            INSERT INTO ai_activity (id, kind, chat_id, sender_id, sender_name, incoming, reply, category, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(a["id"], a["kind"], a.get("chat_id"), a.get("sender_id"), a.get("sender_name"), a.get("incoming"),
               a.get("reply"), a.get("category"), a.get("summary"), a["created_at"]) for a in activity])
        counts["записей ленты ИИ"] = len(activity)
    conn.close()

    if not db.validate_backup(path):
        sys.exit("⚠️ Собранная база не прошла проверку — ничего не отправляю.")
    return counts


async def send_and_pin(path: str, filename: str, bot_token: str, owner_id: int, summary: str):
    from aiogram import Bot
    from aiogram.types import FSInputFile

    bot = Bot(token=bot_token)
    try:
        me = await bot.get_me()
        print(f"🤖 Бот из .env: @{me.username}. Это должен быть тот же бот, что работает на Render.")
        sent = await bot.send_document(
            owner_id,
            FSInputFile(path, filename=filename),
            caption=f"💾 Бэкап базы · перенос со старой версии\n{summary}\nНе открепляй — новая версия восстановится из этого файла.",
            disable_notification=True
        )
        await bot.pin_chat_message(owner_id, sent.message_id, disable_notification=True)
    finally:
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Перенос данных со старой версии бота перед первым деплоем")
    parser.add_argument("panel_link", help="ссылка из команды /panel (https://…/#token=…)")
    parser.add_argument("--no-send", action="store_true", help="только собрать файл, не отправлять в Telegram")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    owner_id = int(os.getenv("MY_TELEGRAM_ID", 0) or 0)
    bot_token = os.getenv("BOT_TOKEN")

    base_url, token = parse_panel_link(args.panel_link)
    print(f"⬇️ Забираю данные с {base_url} …")
    data = fetch_old_data(base_url, token)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{BACKUP_PREFIX}_migration_{stamp}.db"
    workdir = tempfile.mkdtemp(prefix="bot_migration_")
    path = os.path.join(workdir, filename)
    counts = build_backup(data, token, owner_id, path)
    summary = ", ".join(f"{v} {k}" for k, v in counts.items())
    print(f"📦 Собрано: {summary}")

    local_copy = os.path.join(PROJECT_DIR, filename)  # *.db в .gitignore — в репозиторий не попадёт
    with open(path, "rb") as src, open(local_copy, "wb") as dst:
        dst.write(src.read())
    print(f"💾 Копия файла: {local_copy}")

    if args.no_send:
        print("ℹ️ --no-send: в Telegram не отправлял. Этот файл можно прислать боту после деплоя — он предложит восстановиться.")
        return
    if not bot_token or not owner_id:
        sys.exit("⚠️ В .env нет BOT_TOKEN или MY_TELEGRAM_ID — не могу отправить файл. Пришли его боту вручную после деплоя.")
    asyncio.run(send_and_pin(path, filename, bot_token, owner_id, summary))
    print("📌 Файл отправлен тебе в чат с ботом и закреплён. Теперь можно деплоить — новая версия восстановится из него сама.")


if __name__ == "__main__":
    main()
