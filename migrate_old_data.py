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

import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_PREFIX = "business_bot_backup"  # как в main.py — по этому префиксу бот ищет бэкап


def clip_reason(reason: str) -> str:
    return reason if len(reason) <= 80 else reason[:79] + "…"


def parse_panel_link(link: str):
    parts = urlsplit(link.strip())
    fragment = dict(p.split("=", 1) for p in parts.fragment.split("&") if "=" in p)
    token = fragment.get("token")
    if not parts.scheme or not parts.netloc or not token:
        sys.exit("⚠️ Нужна ссылка вида https://…/#token=… — её присылает команда /panel.")
    return f"{parts.scheme}://{parts.netloc}", token


def fetch_old_data(base_url: str, token: str, attempts: int = 5) -> dict:
    request = urllib.request.Request(f"{base_url}/api/data", headers={"Authorization": f"Bearer {token}"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("⚠️ Ключ не подошёл (401). Возьми свежую ссылку командой /panel.")
            if e.code == 429:
                sys.exit("⚠️ Слишком много попыток (429). Подожди 10 минут.")
            if e.code < 500 or attempt == attempts:
                sys.exit(f"⚠️ Сервер ответил ошибкой {e.code}.")
            reason = f"ошибка {e.code}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == attempts:
                sys.exit(f"⚠️ Не удалось связаться с ботом: {getattr(e, 'reason', e)}. "
                         "Проверь, что включён VPN/прокси, открой ссылку в браузере и повтори.")
            reason = str(getattr(e, "reason", e))
        # Бесплатный Render усыпляет бота: первый запрос будит его и может оборваться
        print(f"⏳ Попытка {attempt}/{attempts} не удалась ({clip_reason(reason)}), бот, возможно, просыпается — жду 15 сек…")
        time.sleep(15)
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


def tg_call(bot_token: str, method: str, fields: dict, file_field: str = None, file_path: str = None, filename: str = None) -> dict:
    """Вызов Bot API через urllib — он, в отличие от aiogram, сам берёт прокси из HTTPS_PROXY / настроек Windows"""
    boundary = uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode("utf-8")
    if file_field:
        with open(file_path, "rb") as f:
            content = f.read()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    if fields or file_field:
        request = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        request = urllib.request.Request(url)  # пустую multipart-форму Telegram отклоняет с 400
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            result = json.loads(raw)
        except ValueError:
            result = {"description": f"HTTP {e.code}"}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        sys.exit(f"⚠️ Не удалось связаться с Telegram: {getattr(e, 'reason', e)}. Проверь VPN/прокси "
                 "или пришли сохранённый файл боту вручную после деплоя.")
    if not result.get("ok"):
        sys.exit(f"⚠️ Telegram ответил ошибкой в {method}: {result.get('description')}")
    return result["result"]


def send_and_pin(path: str, filename: str, bot_token: str, owner_id: int, summary: str):
    me = tg_call(bot_token, "getMe", {})
    print(f"🤖 Бот из .env: @{me.get('username')}. Это должен быть тот же бот, что работает на Render.")
    sent = tg_call(bot_token, "sendDocument", {
        "chat_id": owner_id,
        "caption": f"💾 Бэкап базы · перенос со старой версии\n{summary}\nНе открепляй — новая версия восстановится из этого файла.",
        "disable_notification": "true",
    }, file_field="document", file_path=path, filename=filename)
    tg_call(bot_token, "pinChatMessage", {
        "chat_id": owner_id, "message_id": sent["message_id"], "disable_notification": "true"
    })


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
    print("📤 Отправляю файл в Telegram …")
    send_and_pin(path, filename, bot_token, owner_id, summary)
    print("📌 Файл отправлен тебе в чат с ботом и закреплён. Теперь можно деплоить — новая версия восстановится из него сама.")


if __name__ == "__main__":
    main()
