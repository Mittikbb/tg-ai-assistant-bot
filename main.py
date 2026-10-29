import asyncio
import hmac
import html
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    CallbackQuery,
    FSInputFile,
    Message
)
from dotenv import load_dotenv
from aiohttp import web

import db
from ai_brain import (
    analyze_message,
    make_cute_text,
    make_draft_reply,
    parse_user_intent,
    summarize_chat,
    MODELS_TO_TRY
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
PORT = int(os.environ.get("PORT", 8080))
# Render сам выставляет RENDER_EXTERNAL_URL; локально можно задать PUBLIC_URL
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or f"http://localhost:{PORT}").rstrip("/")

user_last_manual_msg = {}
PAUSE_TIMEOUT = 600  # 10 минут (в секундах)

# (chat_id, режим) -> время последнего статусного автоответа, чтобы не отвечать на каждое сообщение
status_reply_sent = {}
STATUS_REPLY_COOLDOWN = 1800  # 30 минут

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ==============================================================================
# ОБЩИЙ СТИЛЬ СООБЩЕНИЙ
# ==============================================================================

MODES = {
    "default": ("💼", "Обычный", "отвечаю на сообщения за тебя"),
    "busy": ("🎮", "Занят", "прошу писать «срочно», если дело важное"),
    "sleep": ("🌙", "Сплю", "собираю сообщения в утреннюю сводку"),
    "ignore": ("🚫", "Не беспокоить", "вежливо сообщаю, что ты не на связи"),
}

STAT_LABELS = [
    ("formal", "💬", "Обычные"),
    ("personal", "👥", "Личные"),
    ("tech_vpn", "💻", "Технические"),
    ("urgent", "🚨", "Срочные"),
]

def esc(value) -> str:
    return html.escape(str(value)) if value else ""

def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

def tz_label(tz: int) -> str:
    return f"UTC{'+' if tz >= 0 else ''}{tz}"

def mode_line(status: str) -> str:
    emoji, title, desc = MODES.get(status, MODES["default"])
    return f"{emoji} <b>{title}</b> — {desc}"

def get_cat_icon(cat: str) -> str:
    return db.CATEGORY_EMOJIS.get(db.normalize_category(cat), "📌")

def back_to_menu_row():
    return [InlineKeyboardButton(text="⬅️ В меню", callback_data="home")]

async def safe_edit(message: Message, text: str, reply_markup=None):
    """edit_text, который не падает на «message is not modified»"""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise

# ==============================================================================
# ДОСТУП ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА (ЛС бота и кнопки)
# ==============================================================================

class OwnerOnlyMiddleware(BaseMiddleware):
    """Бот личный: команды, заметки и Gemini-квоту может использовать только владелец.
    Бизнес-сообщения (автоответчик) идут через отдельный роутер и сюда не попадают."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and MY_ID and user.id == MY_ID:
            return await handler(event, data)
        # Служебные сообщения (например, «бот закрепил файл» после автобэкапа) и сообщения ботов
        # не от человека — молча пропускаем, иначе бот ответит отказом сам себе
        if (user and user.is_bot) or (isinstance(event, Message) and event.pinned_message):
            # «Бот закрепил файл» после каждого бэкапа — только мусор в чате, убираем
            if isinstance(event, Message) and event.pinned_message and user and user.id == event.bot.id:
                try:
                    await event.delete()
                except Exception:
                    pass
            return None
        if isinstance(event, Message) and event.chat.type == "private":
            await event.answer(
                "🔒 <b>Это личный ИИ-ассистент.</b>\n"
                "Управлять им может только владелец."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Доступ только у владельца", show_alert=True)
        return None

dp.message.outer_middleware(OwnerOnlyMiddleware())
dp.callback_query.outer_middleware(OwnerOnlyMiddleware())

# ==============================================================================
# ВЕБ-ПАНЕЛЬ: КЛЮЧ ДОСТУПА
# ==============================================================================

def get_dashboard_token() -> str:
    env_token = os.getenv("DASHBOARD_TOKEN")
    if env_token:
        return env_token
    token = db.get_setting("dashboard_token")
    if not token:
        token = secrets.token_urlsafe(24)
        db.set_setting("dashboard_token", token)
    return token

def rotate_dashboard_token() -> Optional[str]:
    if os.getenv("DASHBOARD_TOKEN"):
        return None  # ключ задан в окружении — меняется только там
    token = secrets.token_urlsafe(24)
    db.set_setting("dashboard_token", token)
    return token

def dashboard_link() -> str:
    # Ключ передаётся во фрагменте (#) — он не уходит на сервер и не попадает в логи
    return f"{PUBLIC_URL}/#token={get_dashboard_token()}"

# ==============================================================================
# ВЕБ-СЕРВЕР (Render / UptimeRobot) + ЗАЩИЩЁННЫЙ API
# ==============================================================================

AUTH_WINDOW = 600      # окно подсчёта неудачных попыток, сек
AUTH_MAX_FAILS = 10    # после стольких ошибок IP получает 429
_auth_failures: dict = {}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}

def json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        status=status,
    )

def client_ip(request: web.Request) -> str:
    # За прокси Render реальный адрес — последний добавленный в X-Forwarded-For
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote or "?"

def check_api_auth(request: web.Request) -> Optional[web.Response]:
    """None — доступ разрешён, иначе готовый ответ с ошибкой"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    # Верный ключ проходит всегда: за общим прокси чужой перебор не должен блокировать владельца
    if token and hmac.compare_digest(token.encode(), get_dashboard_token().encode()):
        return None

    ip = client_ip(request)
    now = time.time()
    fails = [t for t in _auth_failures.get(ip, []) if now - t < AUTH_WINDOW]
    if len(fails) >= AUTH_MAX_FAILS:
        _auth_failures[ip] = fails
        return json_response({"ok": False, "error": "Слишком много попыток. Подождите 10 минут."}, 429)

    fails.append(now)
    _auth_failures[ip] = fails
    if len(_auth_failures) > 1000:  # не даём словарю разрастаться
        for key in [k for k, v in _auth_failures.items() if not v or now - v[-1] > AUTH_WINDOW]:
            _auth_failures.pop(key, None)
    return json_response({"ok": False, "error": "unauthorized"}, 401)

@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        denied = check_api_auth(request)
        response = denied if denied is not None else await handler(request)
        response.headers["Cache-Control"] = "no-store"
    else:
        response = await handler(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response

async def read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}

async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!", content_type="text/plain")

async def handle_get_data(request):
    try:
        data = {
            "ok": True,
            "status": db.get_status(),
            "model": MODELS_TO_TRY[0],
            "fallback_model": MODELS_TO_TRY[1] if len(MODELS_TO_TRY) > 1 else None,
            "timezone_offset": db.get_user_tz_offset(MY_ID),
            "stats": db.get_stats_summary(),
            "reminders": db.get_all_pending_reminders(),
            "notes": db.get_all_notes(limit=100),
            "notes_categories": db.get_notes_categories_stats(MY_ID),
            "night_logs": db.get_recent_night_logs(limit=20),
            "activity": db.get_recent_activity(limit=60),
            "activity_24h": db.get_activity_today_counts(),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        return json_response(data)
    except Exception as e:
        logging.error(f"API get_data error: {e}")
        return json_response({"ok": False, "error": "internal error"}, 500)

async def handle_reminders_action(request):
    body = await read_json(request)
    try:
        action = body.get("action")
        rem_id = int(body.get("id", 0))
        if action == "complete":
            db.mark_reminder_completed(rem_id)
        elif action == "delete":
            db.delete_reminder(rem_id)
        elif action == "snooze":
            minutes = max(1, min(int(body.get("minutes", 15)), 24 * 60))
            if not db.snooze_reminder(rem_id, MY_ID, minutes):
                return json_response({"ok": False, "error": "Напоминание не найдено"}, 404)
        else:
            return json_response({"ok": False, "error": "Неизвестное действие"}, 400)
        return json_response({"ok": True})
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "Некорректные данные"}, 400)

async def handle_notes_action(request):
    body = await read_json(request)
    try:
        action = body.get("action")
        if action == "delete":
            note_id = int(body.get("id", 0))
            db.delete_note_by_id(note_id, MY_ID)
        elif action == "create":
            cat = db.normalize_category(str(body.get("category", "Другое")))
            content = str(body.get("content", "")).strip()
            if not content:
                return json_response({"ok": False, "error": "Пустая заметка"}, 400)
            db.add_note(MY_ID, cat, content[:2000])
        else:
            return json_response({"ok": False, "error": "Неизвестное действие"}, 400)
        return json_response({"ok": True})
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "Некорректные данные"}, 400)

async def handle_mode_action(request):
    body = await read_json(request)
    mode = body.get("mode", "default")
    if mode in MODES:
        db.set_status(mode)
        return json_response({"ok": True, "status": mode})
    return json_response({"ok": False, "error": "Invalid mode"}, 400)

async def handle_activity_clear(request):
    db.clear_activity()
    return json_response({"ok": True})

async def handle_dashboard(request):
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        response = web.Response(text=content, content_type="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response
    return web.Response(text="Dashboard not found", status=404)

async def start_web_server():
    app = web.Application(middlewares=[security_middleware], client_max_size=64 * 1024)

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/ping", handle_ping)

    # API (только с ключом панели)
    app.router.add_get("/api/data", handle_get_data)
    app.router.add_post("/api/reminders/action", handle_reminders_action)
    app.router.add_post("/api/notes/action", handle_notes_action)
    app.router.add_post("/api/mode", handle_mode_action)
    app.router.add_post("/api/activity/clear", handle_activity_clear)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 Веб-панель запущена на порту {PORT} (ссылка с ключом — команда /panel в боте)")

# ==============================================================================
# ФОНОВЫЙ ВОРКЕР НАПОМИНАНИЙ (Модуль 3)
# ==============================================================================

async def reminder_worker(bot_instance: Bot):
    """Каждые 15 секунд проверяет базу на наступление времени напоминаний"""
    logging.info("⏰ Фоновый воркер напоминаний запущен.")
    while True:
        try:
            due_reminders = db.get_due_reminders()
            for rem in due_reminders:
                rem_id = rem["id"]
                chat_id = rem["chat_id"]
                source_info = rem.get("source_info", "")

                source_block = f"\n<i>📎 {esc(source_info)}</i>" if source_info else ""
                msg_text = (
                    f"⏰ <b>Напоминание</b>\n\n"
                    f"<blockquote><b>{esc(rem['text'])}</b></blockquote>"
                    f"{source_block}"
                )

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Готово", callback_data=f"done_rem_{rem_id}"),
                        InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snooze_{rem_id}_15"),
                        InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze_{rem_id}_60")
                    ]
                ])

                try:
                    await bot_instance.send_message(chat_id=chat_id, text=msg_text, reply_markup=kb)
                    # «sent», а не «completed»: сработавшее напоминание ещё можно отложить
                    db.mark_reminder_sent(rem_id)
                except (TelegramForbiddenError, TelegramBadRequest) as e:
                    # Бот заблокирован / чат недоступен — повтор не поможет, иначе ретрай каждые 15 сек вечно
                    logging.error(f"Напоминание {rem_id} не может быть доставлено в чат {chat_id}: {e}")
                    db.set_reminder_status(rem_id, "failed")
                except Exception as e:
                    logging.error(f"Не удалось отправить напоминание {rem_id} в чат {chat_id}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в цикле reminder_worker: {e}")

        await asyncio.sleep(15)

# ==============================================================================
# АВТОБЭКАП БАЗЫ В TELEGRAM
# Диск Render стирается при каждом деплое и перезапуске. Бот хранит копию базы
# закреплённым файлом в чате с владельцем и при старте на чистом диске восстанавливается из неё.
# ==============================================================================

BACKUP_CHAT_ID = int(os.getenv("BACKUP_CHAT_ID", 0) or 0) or MY_ID
BACKUP_INTERVAL = 600        # раз в 10 минут — если изменились заметки, напоминания, настройки и т.п.
MINOR_BACKUP_INTERVAL = 6 * 3600  # история переписки, лента ИИ, досье — не чаще раза в 6 часов
BACKUP_PREFIX = "business_bot_backup"
RESTORE_RETRIES = 10         # попыток восстановиться при старте (по одной в минуту)

backup_state = {
    "allowed": False,        # пока не восстановились — не перезаписываем хорошую копию пустой базой
    "last_hash": None,
    "last_important_hash": None,
    "last_sent_at": time.time(),
    "msg_ids": [],           # последние сообщения с бэкапами (храним две)
    "restore_attempts": 0,
}
backup_lock = asyncio.Lock()
pending_restores: dict = {}  # короткий ключ -> file_id присланного файла базы

def remove_quietly(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

async def find_pinned_backup() -> Optional[Message]:
    chat = await bot.get_chat(BACKUP_CHAT_ID)
    pinned = chat.pinned_message
    if pinned and pinned.document and (pinned.document.file_name or "").startswith(BACKUP_PREFIX):
        return pinned
    return None

async def restore_from_file_id(file_id: str):
    tmp = f"temp_restore_{secrets.token_hex(4)}.db"
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, tmp)
        if not await asyncio.to_thread(db.validate_backup, tmp):
            raise ValueError("файл не похож на базу бота или повреждён")
        await asyncio.to_thread(db.restore_from, tmp)
    finally:
        remove_quietly(tmp)
    backup_state["last_hash"], backup_state["last_important_hash"] = await asyncio.to_thread(db.content_hashes)

async def make_backup(reason: str, force: bool = False, important_only: bool = False) -> Optional[Message]:
    """Шлёт копию базы в чат бэкапов и закрепляет её. None — если бэкап не нужен или запрещён.
    important_only — слать, только если изменилось что-то кроме «шумных» таблиц (db.NOISY_TABLES)"""
    if not BACKUP_CHAT_ID:
        return None
    async with backup_lock:
        if not backup_state["allowed"]:
            return None
        current_hash, important_hash = await asyncio.to_thread(db.content_hashes)
        if not force:
            if important_only and important_hash == backup_state["last_important_hash"]:
                return None
            if current_hash == backup_state["last_hash"]:
                return None

        tmp = f"temp_backup_{secrets.token_hex(4)}.db"
        try:
            await asyncio.to_thread(db.snapshot_to, tmp)
            now_utc = datetime.now(timezone.utc)
            local = now_utc.astimezone(timezone(timedelta(hours=db.get_user_tz_offset(MY_ID))))
            sent = await bot.send_document(
                BACKUP_CHAT_ID,
                FSInputFile(tmp, filename=f"{BACKUP_PREFIX}_{now_utc.strftime('%Y%m%d_%H%M%S')}.db"),
                caption=(
                    f"💾 <b>Бэкап базы</b> · {local.strftime('%d.%m %H:%M')}\n"
                    f"<i>{esc(reason)}. Не открепляй — по закреплённому файлу бот восстановит данные после деплоя.</i>"
                ),
                disable_notification=True
            )
        finally:
            remove_quietly(tmp)

        await bot.pin_chat_message(BACKUP_CHAT_ID, sent.message_id, disable_notification=True)
        backup_state["last_hash"] = current_hash
        backup_state["last_important_hash"] = important_hash
        backup_state["last_sent_at"] = time.time()

        # Держим две копии: свежую (закреплена) и предыдущую на всякий случай, остальные удаляем
        ids = backup_state["msg_ids"] + [sent.message_id]
        for old_id in ids[:-2]:
            try:
                await bot.delete_message(BACKUP_CHAT_ID, old_id)
            except Exception:
                pass
        for old_id in ids[-2:-1]:
            try:
                await bot.unpin_chat_message(BACKUP_CHAT_ID, message_id=old_id)
            except Exception:
                pass
        backup_state["msg_ids"] = ids[-2:]
        logging.info(f"💾 Бэкап базы отправлен ({reason})")
        return sent

async def try_restore_on_start() -> bool:
    """True — можно работать (восстановились или бэкапа нет), False — повторим позже"""
    backup_state["restore_attempts"] += 1
    try:
        pinned = await find_pinned_backup()
        if not pinned:
            logging.info("💾 Закреплённого бэкапа нет — начинаем с чистой базы.")
            backup_state["allowed"] = True
            return True
        await restore_from_file_id(pinned.document.file_id)
        backup_state["msg_ids"] = [pinned.message_id]
        backup_state["allowed"] = True
        saved_at = pinned.date.astimezone(timezone(timedelta(hours=db.get_user_tz_offset(MY_ID))))
        logging.info(f"♻️ База восстановлена из бэкапа от {saved_at:%d.%m %H:%M}")
        await bot.send_message(
            MY_ID,
            f"♻️ <b>Данные восстановлены</b> из бэкапа от {saved_at.strftime('%d.%m %H:%M')} — "
            f"заметки, напоминания и настройки на месте.",
            disable_notification=True
        )
        return True
    except Exception as e:
        logging.error(f"Не удалось восстановить базу из бэкапа (попытка {backup_state['restore_attempts']}): {e}")
        if backup_state["restore_attempts"] == RESTORE_RETRIES:
            try:
                await bot.send_message(
                    MY_ID,
                    f"⚠️ <b>Не получилось восстановить данные из бэкапа</b>\n<code>{esc(clip(str(e), 300))}</code>\n\n"
                    "Автобэкап на паузе, чтобы не перезаписать хорошую копию пустой базой.\n"
                    "Можно переслать мне файл бэкапа вручную или нажать /backup, чтобы сохранить текущую базу как есть.",
                )
            except Exception:
                pass
        return False

async def init_backup():
    if not BACKUP_CHAT_ID:
        logging.warning("💾 MY_TELEGRAM_ID не задан — автобэкап базы выключен.")
        return
    if db.DB_EXISTED_AT_START:
        # Локальная база на месте (локальный запуск или перезапуск без потери диска) — восстанавливать нечего
        backup_state["allowed"] = True
        try:
            pinned = await find_pinned_backup()
            if pinned:
                backup_state["msg_ids"] = [pinned.message_id]
        except Exception as e:
            logging.warning(f"Не удалось прочитать закреплённый бэкап: {e}")
        return
    await try_restore_on_start()

async def backup_worker():
    logging.info("💾 Воркер автобэкапа запущен.")
    last_backup = time.time()
    while True:
        await asyncio.sleep(60)
        try:
            if not backup_state["allowed"]:
                if backup_state["restore_attempts"] < RESTORE_RETRIES:
                    await try_restore_on_start()
                continue
            if time.time() - last_backup >= BACKUP_INTERVAL:
                last_backup = time.time()
                sent = await make_backup("Автобэкап", important_only=True)
                # Мелкие изменения (история чатов, лента ИИ, досье) сохраняем не чаще раза в 6 часов;
                # перед деплоем/перезапуском они всё равно уходят бэкапом при выключении
                if not sent and time.time() - backup_state["last_sent_at"] >= MINOR_BACKUP_INTERVAL:
                    await make_backup("Автобэкап истории переписки")
        except Exception as e:
            logging.error(f"Ошибка в цикле backup_worker: {e}")

# ==============================================================================
# ГЛАВНОЕ МЕНЮ, РЕЖИМЫ, СПРАВКА, СТАТИСТИКА
# ==============================================================================

def build_home(first_name: str):
    status = db.get_status()
    tz = db.get_user_tz_offset(MY_ID)
    reminders_count = len(db.get_active_reminders(MY_ID))
    notes_count = sum(db.get_notes_categories_stats(MY_ID).values())
    replies_24h = db.get_activity_today_counts().get("reply", 0)

    text = (
        f"🤖 <b>ИИ-Ассистент</b>\n"
        f"<i>Привет, {esc(first_name) or 'друг'}! Отвечаю в твоих чатах и держу в голове твои дела.</i>\n\n"
        f"<b>Режим</b>\n"
        f"{mode_line(status)}\n\n"
        f"<b>Сводка</b>\n"
        f"⏰ Напоминаний: <b>{reminders_count}</b>\n"
        f"📝 Заметок: <b>{notes_count}</b>\n"
        f"💬 Ответов ИИ за сутки: <b>{replies_24h}</b>\n"
        f"🌙 Расписание сна: <b>{schedule_label()}</b>\n"
        f"🌍 Часовой пояс: <b>{tz_label(tz)}</b>\n\n"
        f"<b>Как пользоваться</b>\n"
        f"Просто напиши или перешли мне сообщение:\n"
        f"<blockquote>завтра в 14:00 сдать отчёт → ⏰ напоминание\n"
        f"купить переходник Type-C → 📝 заметка</blockquote>\n"
        f"✨ Начни сообщение в любом чате с <code>~</code> — перепишу его мило."
    )

    def mode_btn(key):
        emoji, title, _ = MODES[key]
        mark = "● " if key == status else ""
        return InlineKeyboardButton(text=f"{mark}{emoji} {title}", callback_data=f"mode_{key}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [mode_btn("default"), mode_btn("busy")],
        [mode_btn("sleep"), mode_btn("ignore")],
        [
            InlineKeyboardButton(text="⏰ Напоминания", callback_data="show_reminders_list"),
            InlineKeyboardButton(text="📝 Заметки", callback_data="notes_menu")
        ],
        [
            InlineKeyboardButton(text=f"⭐ VIP ({len(db.get_vip_users())})", callback_data="vips_menu"),
            InlineKeyboardButton(text=f"🛑 Откл. чаты ({len(db.get_disabled_chats())})", callback_data="disabled_menu")
        ],
        [
            InlineKeyboardButton(text="🌙 Расписание", callback_data="schedule_menu"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")
        ],
        [
            InlineKeyboardButton(text="🌍 Пояс", callback_data="open_tz_menu")
        ],
        [
            InlineKeyboardButton(text="🖥 Веб-панель", callback_data="show_panel"),
            InlineKeyboardButton(text="📖 Справка", callback_data="show_help")
        ]
    ])
    return text, kb

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text, kb = build_home(message.from_user.first_name)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "home")
async def cb_home(callback: types.CallbackQuery):
    text, kb = build_home(callback.from_user.first_name)
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(callback: types.CallbackQuery):
    mode = callback.data.replace("mode_", "")
    if mode not in MODES:
        await callback.answer()
        return
    db.set_status(mode)
    emoji, title, _ = MODES[mode]
    await callback.answer(f"{emoji} Режим: {title}")
    text, kb = build_home(callback.from_user.first_name)
    await safe_edit(callback.message, text, kb)

async def set_mode_and_reply(message: types.Message, mode: str):
    db.set_status(mode)
    await message.answer(
        f"🔄 <b>Режим изменён</b>\n\n{mode_line(mode)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])
    )

@dp.message(Command("default"))
async def cmd_default(message: types.Message):
    await set_mode_and_reply(message, "default")

@dp.message(Command("ignore_all"))
async def cmd_ignore(message: types.Message):
    await set_mode_and_reply(message, "ignore")

@dp.message(Command("sleep"))
async def cmd_sleep(message: types.Message):
    await set_mode_and_reply(message, "sleep")

@dp.message(Command("busy"))
async def cmd_busy(message: types.Message):
    await set_mode_and_reply(message, "busy")

def build_morning_summary() -> str:
    """Утренняя сводка: кто писал ночью + напоминания на сегодня. Ночные логи при этом очищаются"""
    text = f"🌅 <b>Утренняя сводка</b>\n{mode_line('default')}\n\n"

    logs = db.pop_night_messages()
    if logs:
        lines = []
        budget = 2500  # лимит сообщения Telegram — 4096 символов, оставляем место под напоминания
        for name, msg in logs:
            line = f"• <b>{esc(name) or 'Без имени'}</b>: {esc(clip(msg, 300))}"
            if budget - len(line) < 0:
                lines.append(f"…и ещё {len(logs) - len(lines)} сообщ.")
                break
            budget -= len(line)
            lines.append(line)
        text += f"💬 <b>Пока ты спал, писали</b> · {len(logs)}\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>\n"
    else:
        text += "💬 Ночью никто не писал — можно спокойно пить кофе ☕\n"

    local_tz = timezone(timedelta(hours=db.get_user_tz_offset(MY_ID)))
    today = datetime.now(local_tz).date()
    todays = []
    for rem in db.get_active_reminders(MY_ID):
        try:
            dt = datetime.fromisoformat(rem["remind_at_local"])
        except Exception:
            continue
        if dt.date() <= today:
            todays.append((dt, rem["text"]))
    if todays:
        lines = []
        for dt, rem_text in todays[:15]:
            when = dt.strftime("%H:%M") if dt.date() == today else f"просрочено с {dt.strftime('%d.%m')}"
            lines.append(f"<code>{when}</code> — {esc(clip(rem_text, 120))}")
        text += f"\n⏰ <b>На сегодня</b> · {len(todays)}\n" + "\n".join(lines)
    else:
        text += "\n⏰ На сегодня напоминаний нет."
    return text

async def send_morning_summary(chat_id: int):
    await bot.send_message(
        chat_id, build_morning_summary(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])
    )

@dp.message(Command("goodmorning"))
async def cmd_goodmorning(message: types.Message):
    db.set_status("default")
    db.set_setting("schedule_auto_sleep", "0")
    await send_morning_summary(message.chat.id)

# --- Расписание сна ---

def parse_hhmm(value: str) -> Optional[int]:
    """'01:00' -> минуты от полуночи"""
    try:
        h, m = (value or "").replace(".", ":").split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None

def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"

def get_schedule():
    """(start, end) в минутах или None, если расписание выключено"""
    if db.get_setting("schedule_enabled") != "1":
        return None
    start = parse_hhmm(db.get_setting("sleep_start"))
    end = parse_hhmm(db.get_setting("sleep_end"))
    if start is None or end is None or start == end:
        return None
    return start, end

def schedule_label() -> str:
    sch = get_schedule()
    return f"{fmt_minutes(sch[0])}–{fmt_minutes(sch[1])}" if sch else "выкл"

def schedule_window_id(now_local: datetime, start: int, end: int) -> Optional[str]:
    """Дата начала текущего окна сна (окно может переходить через полночь) или None вне окна"""
    cur = now_local.hour * 60 + now_local.minute
    if start < end:
        return now_local.date().isoformat() if start <= cur < end else None
    if cur >= start:
        return now_local.date().isoformat()
    if cur < end:
        return (now_local.date() - timedelta(days=1)).isoformat()
    return None

SCHEDULE_PRESETS = [("00:00", "08:00"), ("01:00", "09:00"), ("23:00", "07:00"), ("02:00", "10:00")]

def build_schedule_menu():
    sch = get_schedule()
    rows = [[
        InlineKeyboardButton(
            text=f"{'● ' if sch and (fmt_minutes(sch[0]), fmt_minutes(sch[1])) == (s, e) else ''}{s}–{e}",
            callback_data=f"sched_{s.replace(':', '')}_{e.replace(':', '')}"
        )
        for s, e in SCHEDULE_PRESETS[i:i + 2]
    ] for i in range(0, len(SCHEDULE_PRESETS), 2)]
    if sch:
        rows.append([InlineKeyboardButton(text="⏹ Выключить расписание", callback_data="sched_off")])
    rows.append(back_to_menu_row())
    text = (
        f"🌙 <b>Расписание сна</b>\n\n"
        f"Сейчас: <b>{schedule_label()}</b> ({tz_label(db.get_user_tz_offset(MY_ID))})\n\n"
        f"В начале окна сам включу 🌙 режим сна, в конце — 💼 обычный и пришлю 🌅 утреннюю сводку.\n"
        f"Если ночью сам переключишь режим (например, на «Занят»), расписание его не перебьёт.\n\n"
        f"Выбери окно или задай своё: <code>/schedule 01:30 09:00</code>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

def set_schedule(start: str, end: str):
    db.set_setting("sleep_start", start)
    db.set_setting("sleep_end", end)
    db.set_setting("schedule_enabled", "1")

@dp.message(Command("schedule"))
async def cmd_schedule(message: types.Message):
    args = (message.text or "").split()
    if len(args) == 2 and args[1].lower() in ("off", "выкл"):
        db.set_setting("schedule_enabled", "0")
    elif len(args) == 3:
        start, end = parse_hhmm(args[1]), parse_hhmm(args[2])
        if start is None or end is None or start == end:
            await message.answer("⚠️ <b>Формат:</b> <code>/schedule 01:00 09:00</code> — начало и конец сна, не совпадающие.")
            return
        set_schedule(fmt_minutes(start), fmt_minutes(end))
    text, kb = build_schedule_menu()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "schedule_menu")
async def cb_schedule_menu(callback: types.CallbackQuery):
    text, kb = build_schedule_menu()
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("sched_"))
async def cb_schedule_set(callback: types.CallbackQuery):
    if callback.data == "sched_off":
        db.set_setting("schedule_enabled", "0")
        await callback.answer("Расписание выключено")
    else:
        _, s, e = callback.data.split("_")
        set_schedule(f"{s[:2]}:{s[2:]}", f"{e[:2]}:{e[2:]}")
        await callback.answer(f"🌙 Сплю {schedule_label()}")
    text, kb = build_schedule_menu()
    await safe_edit(callback.message, text, kb)

async def schedule_worker():
    """Раз в 30 сек проверяет расписание сна: включает режим сна и утром присылает сводку"""
    logging.info("🌙 Воркер расписания сна запущен.")
    while True:
        try:
            sch = get_schedule()
            if sch and MY_ID:
                now_local = datetime.now(timezone(timedelta(hours=db.get_user_tz_offset(MY_ID))))
                window = schedule_window_id(now_local, *sch)
                if window and db.get_setting("schedule_last_window") != window:
                    # Каждое окно обрабатываем один раз: ручная смена режима посреди ночи не перебивается
                    db.set_setting("schedule_last_window", window)
                    current = db.get_status()
                    if current in ("default", "sleep"):
                        # Если ты уже сам включил «Сплю» — утром всё равно разбужу и пришлю сводку
                        db.set_setting("schedule_auto_sleep", "1")
                    if current == "default":
                        db.set_status("sleep")
                        await bot.send_message(
                            MY_ID, f"🌙 <b>По расписанию включён режим сна</b> до {fmt_minutes(sch[1])}. Спокойной ночи!",
                            disable_notification=True
                        )
                elif not window and db.get_setting("schedule_auto_sleep") == "1":
                    db.set_setting("schedule_auto_sleep", "0")
                    # Сводку шлём, только если ты сам не сменил режим за ночь
                    if db.get_status() == "sleep":
                        db.set_status("default")
                        await send_morning_summary(MY_ID)
        except Exception as e:
            logging.error(f"Ошибка в цикле schedule_worker: {e}")
        await asyncio.sleep(30)

def build_stats_text() -> str:
    stats = db.get_stats_summary()
    total = stats.get("total", 0)
    categories = stats.get("categories", {})
    top = max([categories.get(k, 0) for k, _, _ in STAT_LABELS] + [1])

    lines = []
    for key, emoji, title in STAT_LABELS:
        count = categories.get(key, 0)
        filled = round(8 * count / top) if count else 0
        lines.append(f"{emoji} {title}\n<code>{'▰' * filled}{'▱' * (8 - filled)}</code> <b>{count}</b>")

    last_24h = db.get_activity_today_counts()
    return (
        f"📊 <b>Статистика ассистента</b>\n\n"
        f"Всего обработано: <b>{total}</b>\n"
        f"За сутки ответил сам: <b>{last_24h.get('reply', 0)}</b>, "
        f"переслал тебе: <b>{last_24h.get('personal', 0)}</b>\n\n"
        + "\n".join(lines)
    )

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await message.answer(build_stats_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()]))

@dp.callback_query(F.data == "show_stats")
async def cb_stats(callback: types.CallbackQuery):
    await safe_edit(callback.message, build_stats_text(), InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()]))
    await callback.answer()

async def id_command(message: types.Message, usage: str, action, done_text: str):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer(f"⚠️ <b>Формат:</b> <code>{usage}</code>")
        return
    target_id = int(args[1])
    action(target_id)
    await message.answer(done_text.format(id=f"<code>{target_id}</code>"))

@dp.message(Command("disable_chat"))
async def cmd_disable_chat(message: types.Message):
    await id_command(message, "/disable_chat ID_ЧАТА", db.disable_chat,
                     "🛑 <b>Автоответ выключен</b> в чате {id}")

@dp.message(Command("enable_chat"))
async def cmd_enable_chat(message: types.Message):
    await id_command(message, "/enable_chat ID_ЧАТА", db.enable_chat,
                     "🟢 <b>Автоответ включён</b> в чате {id}")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    await id_command(message, "/unban ID_ПОЛЬЗОВАТЕЛЯ", db.remove_from_blacklist,
                     "✅ <b>Разблокирован</b> пользователь {id}")

@dp.message(Command("aggressive"))
async def cmd_aggressive(message: types.Message):
    await id_command(message, "/aggressive ID_ПОЛЬЗОВАТЕЛЯ", db.add_to_aggressive,
                     "🔥 <b>Агрессивный режим включён</b> для {id}")

@dp.message(Command("unaggressive"))
async def cmd_unaggressive(message: types.Message):
    await id_command(message, "/unaggressive ID_ПОЛЬЗОВАТЕЛЯ", db.remove_from_aggressive,
                     "🟢 <b>Агрессивный режим выключен</b> для {id}")

# --- Отключённые чаты и VIP-контакты ---

def contact_label(c: dict) -> str:
    label = c.get("name") or f"ID {c['id']}"
    if c.get("username"):
        label += f" (@{c['username']})"
    return label

def build_disabled_menu():
    chats = db.get_disabled_chats()
    if not chats:
        text = (
            "🛑 <b>Отключённые чаты</b>\n\n"
            "Пусто — автоответ работает во всех чатах.\n"
            "<i>Отключить чат можно кнопкой «🛑 Отключить чат» под любым уведомлением.</i>"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])
    lines = [f"• {esc(contact_label(c))} · <code>{c['id']}</code>" for c in chats]
    rows = [[InlineKeyboardButton(text=clip(f"🟢 {contact_label(c)}", 60), callback_data=f"menu_enable_{c['id']}")] for c in chats]
    rows.append(back_to_menu_row())
    text = f"🛑 <b>Отключённые чаты</b> · {len(chats)}\n\n" + "\n".join(lines) + "\n\n<i>Нажми на чат, чтобы снова включить автоответ.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

def build_vip_menu():
    vips = db.get_vip_users()
    if not vips:
        text = (
            "⭐ <b>VIP-контакты</b>\n\n"
            "Пусто. Сообщения VIP я не обрабатываю и не отвечаю на них — сразу пересылаю тебе, даже ночью.\n"
            "<i>Добавить: кнопка «⭐ В VIP» под уведомлением или</i> <code>/vip ID</code>"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])
    lines = [f"• {esc(contact_label(v))} · <code>{v['id']}</code>" for v in vips]
    rows = [[InlineKeyboardButton(text=clip(f"✖️ {contact_label(v)}", 60), callback_data=f"menu_unvip_{v['id']}")] for v in vips]
    rows.append(back_to_menu_row())
    text = f"⭐ <b>VIP-контакты</b> · {len(vips)}\n\n" + "\n".join(lines) + "\n\n<i>Их сообщения сразу пересылаются тебе. Нажми, чтобы убрать.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

def contact_name(user_id: int) -> str:
    known = db.get_known_chat(user_id)
    return esc(known["name"]) if known and known.get("name") else f"<code>{user_id}</code>"

@dp.message(Command("disabled"))
async def cmd_disabled(message: types.Message):
    text, kb = build_disabled_menu()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "disabled_menu")
async def cb_disabled_menu(callback: types.CallbackQuery):
    text, kb = build_disabled_menu()
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("menu_enable_"))
async def cb_menu_enable(callback: types.CallbackQuery):
    db.enable_chat(int(callback.data.replace("menu_enable_", "")))
    await callback.answer("🟢 Автоответ включён")
    text, kb = build_disabled_menu()
    await safe_edit(callback.message, text, kb)

@dp.callback_query(F.data.startswith("disablechat_"))
async def cb_disable_chat(callback: types.CallbackQuery):
    chat_id = int(callback.data.replace("disablechat_", ""))
    db.disable_chat(chat_id)
    await callback.answer("🛑 Автоответ в чате выключен")
    await safe_edit(
        callback.message,
        f"🛑 <b>Автоответ выключен</b> в чате с {contact_name(chat_id)}\n"
        f"<i>Уведомлений о паузе по нему больше не будет.</i>",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🟢 Включить обратно", callback_data=f"enablechat_{chat_id}"),
            InlineKeyboardButton(text="🛑 Все отключённые", callback_data="disabled_menu")
        ]])
    )

@dp.callback_query(F.data.startswith("enablechat_"))
async def cb_enable_chat(callback: types.CallbackQuery):
    chat_id = int(callback.data.replace("enablechat_", ""))
    db.enable_chat(chat_id)
    await callback.answer("🟢 Автоответ включён")
    await safe_edit(callback.message, f"🟢 <b>Автоответ включён</b> в чате с {contact_name(chat_id)}")

@dp.message(Command("vips"))
async def cmd_vips(message: types.Message):
    text, kb = build_vip_menu()
    await message.answer(text, reply_markup=kb)

@dp.message(Command("vip"))
async def cmd_vip(message: types.Message):
    await id_command(message, "/vip ID_ПОЛЬЗОВАТЕЛЯ", db.add_vip,
                     "⭐ {id} теперь <b>VIP</b> — его сообщения буду сразу пересылать тебе")

@dp.message(Command("unvip"))
async def cmd_unvip(message: types.Message):
    await id_command(message, "/unvip ID_ПОЛЬЗОВАТЕЛЯ", db.remove_vip,
                     "✖️ {id} больше не VIP — снова работает автоответчик")

@dp.callback_query(F.data == "vips_menu")
async def cb_vips_menu(callback: types.CallbackQuery):
    text, kb = build_vip_menu()
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("menu_unvip_"))
async def cb_menu_unvip(callback: types.CallbackQuery):
    db.remove_vip(int(callback.data.replace("menu_unvip_", "")))
    await callback.answer("Убран из VIP")
    text, kb = build_vip_menu()
    await safe_edit(callback.message, text, kb)

@dp.callback_query(F.data.startswith("vip_"))
async def cb_vip(callback: types.CallbackQuery):
    user_id = int(callback.data.replace("vip_", ""))
    db.add_vip(user_id)
    await callback.answer("⭐ Добавлен в VIP")
    await callback.message.answer(
        f"⭐ {contact_name(user_id)} теперь <b>VIP</b>: я не буду ему отвечать и сразу перешлю тебе его сообщения.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖️ Убрать из VIP", callback_data=f"unvip_{user_id}")
        ]])
    )

@dp.callback_query(F.data.startswith("unvip_"))
async def cb_unvip(callback: types.CallbackQuery):
    user_id = int(callback.data.replace("unvip_", ""))
    db.remove_vip(user_id)
    await callback.answer("Убран из VIP")
    await callback.message.answer(f"✖️ {contact_name(user_id)} больше не VIP — снова работает автоответчик.")

HELP_TEXT = (
    "📖 <b>Справка</b>\n\n"
    "⏰ <b>Напоминания</b>\n"
    "Напиши время и задачу или перешли сообщение:\n"
    "<blockquote>завтра в 14:00 скинуть отчёт\n"
    "через 2 часа проверить духовку\n"
    "в пятницу в 18:00 созвон</blockquote>\n"
    "/reminders — список с кнопками\n\n"
    "📝 <b>Заметки</b>\n"
    "Без времени — ИИ сам выберет категорию:\n"
    "<blockquote>купить переходник Type-C → 🛒 Покупки\n"
    "скачать hiddify → 💻 Софт/VPN\n"
    "тема курсовой по квантам → 🎓 Учеба</blockquote>\n"
    "/notes — каталог · <code>/note покупки молоко</code> — сразу в категорию\n\n"
    "💼 <b>Автоответчик</b> <i>(Telegram Business)</i>\n"
    "Отвечает на обычные и технические вопросы, понимает голосовые и скриншоты, "
    "предупреждает о грубом тоне. После твоего ответа в чате молчит 10 минут.\n"
    "На личное не отвечает сам, а присылает ✍️ черновик в твоём стиле: «📤 Отправить», «🔄 Другой» "
    "или ответь на уведомление своим текстом. «🧾 Что я пропустил?» — сводка переписки.\n"
    "/style — на каких твоих сообщениях ИИ учится писать как ты\n"
    "/default · /busy · /sleep · /ignore_all — режимы\n"
    "/goodmorning — утренняя сводка (кто писал ночью + дела на сегодня)\n"
    "/schedule — расписание сна: сам включу «Сплю» и утром пришлю сводку\n"
    "/stats — статистика\n\n"
    "🛠 <b>Управление чатами</b>\n"
    "/disabled — отключённые чаты (кнопка «🛑 Отключить чат» под уведомлениями)\n"
    "/vips · /vip · /unvip <code>ID</code> — VIP: не отвечаю, сразу пересылаю тебе\n"
    "/disable_chat · /enable_chat <code>ID</code> — автоответ в чате\n"
    "/aggressive · /unaggressive <code>ID</code> — дерзкие ответы\n"
    "/unban <code>ID</code> — убрать из игнора\n\n"
    "🖥 <b>Веб-панель</b>\n"
    "/panel — личная ссылка на панель с лентой ответов ИИ\n\n"
    "💾 <b>Бэкап</b>\n"
    "База сама сохраняется сюда закреплённым файлом (раз в 10 минут при изменениях и перед деплоем) "
    "и восстанавливается после перезапуска на Render.\n"
    "/backup — сохранить прямо сейчас · пришли файл <code>.db</code> — восстановлю из него\n\n"
    "✨ <code>~текст</code> в любом чате — перепишу сообщение мило"
)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()]))

@dp.message(Command("style"))
async def cmd_style(message: types.Message):
    total = db.count_owner_style_samples()
    samples = db.get_owner_style_samples(limit=8)
    if not samples:
        await message.answer(
            "✍️ <b>Мой стиль</b>\n\n"
            "Пока не на чем учиться: я запоминаю сообщения, которые ты пишешь сам в бизнес-чатах. "
            "Ответь пару раз вручную — и черновики начнут звучать как ты."
        )
        return
    examples = "\n".join(f"• {esc(clip(s, 120))}" for s in samples)
    await message.answer(
        f"✍️ <b>Мой стиль</b>\n\n"
        f"Черновики пишутся по образцу твоих последних ручных сообщений "
        f"(в памяти: <b>{total}</b>, в каждом черновике используется до 30).\n\n"
        f"Свежие образцы:\n<blockquote expandable>{examples}</blockquote>\n"
        f"<i>Сообщения с «~» не учитываются — это не твой обычный стиль.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])
    )

@dp.callback_query(F.data == "show_help")
async def cb_help(callback: types.CallbackQuery):
    await safe_edit(callback.message, HELP_TEXT, InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()]))
    await callback.answer()

def build_panel_message():
    link = dashboard_link()
    text = (
        "🖥 <b>Веб-панель</b>\n\n"
        "Живая лента ответов ИИ, режимы, напоминания и заметки — в браузере.\n\n"
        f"🔗 Твоя личная ссылка:\n<code>{esc(link)}</code>\n\n"
        "<i>🔐 В ссылке ключ доступа — никому её не пересылай. "
        "Если ссылка утекла, нажми «Сменить ключ»: старая перестанет работать.</i>"
    )
    rows = []
    if link.startswith("https://"):
        rows.append([InlineKeyboardButton(text="🚀 Открыть панель", url=link)])
    if not os.getenv("DASHBOARD_TOKEN"):
        rows.append([InlineKeyboardButton(text="🔄 Сменить ключ", callback_data="rotate_panel_token")])
    rows.append(back_to_menu_row())
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    text, kb = build_panel_message()
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

@dp.callback_query(F.data == "show_panel")
async def cb_panel(callback: types.CallbackQuery):
    text, kb = build_panel_message()
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data == "rotate_panel_token")
async def cb_rotate_panel_token(callback: types.CallbackQuery):
    if rotate_dashboard_token() is None:
        await callback.answer("Ключ задан в DASHBOARD_TOKEN — меняй его там.", show_alert=True)
        return
    await callback.answer("🔄 Ключ обновлён, старая ссылка больше не работает", show_alert=True)
    text, kb = build_panel_message()
    await safe_edit(callback.message, text, kb)

# ==============================================================================
# ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ (Telegram Business)
# ==============================================================================

STATUS_REPLIES = {
    "ignore": "[ИИ-Ассистент] Пользователь временно не на связи.",
    "sleep": "[ИИ-Ассистент] Пользователь спит. Сообщение передам утром.",
    "busy": "[ИИ-Ассистент] Пользователь занят. Если дело срочное, напишите 'Срочно'.",
}

async def forward_vip_message(message: types.Message):
    """Пересылает сообщение VIP-контакта владельцу. Медиа шлём по file_id — он валиден для бота"""
    sender_id = message.from_user.id
    text = message.text or message.caption or ""
    header = f"⭐ <b>VIP</b> · {esc(message.from_user.first_name) or 'Без имени'} · <code>{sender_id}</code>"
    caption = f"{header}\n<blockquote>{esc(clip(text, 800))}</blockquote>" if text else header
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧾 Что я пропустил?", callback_data=f"chat_summary_{message.chat.id}"),
        InlineKeyboardButton(text="✖️ Убрать из VIP", callback_data=f"unvip_{sender_id}")
    ]])
    try:
        if message.photo:
            await bot.send_photo(MY_ID, message.photo[-1].file_id, caption=caption, reply_markup=kb)
        elif message.voice:
            await bot.send_voice(MY_ID, message.voice.file_id, caption=caption, reply_markup=kb)
        elif message.video:
            await bot.send_video(MY_ID, message.video.file_id, caption=caption, reply_markup=kb)
        elif message.document:
            await bot.send_document(MY_ID, message.document.file_id, caption=caption, reply_markup=kb)
        elif message.video_note:
            await bot.send_video_note(MY_ID, message.video_note.file_id)
            await bot.send_message(MY_ID, f"{header}\n<i>видеосообщение</i>", reply_markup=kb)
        elif message.sticker:
            await bot.send_message(MY_ID, f"{header}\n<i>стикер {esc(message.sticker.emoji or '')}</i>", reply_markup=kb)
        else:
            await bot.send_message(MY_ID, f"{header}\n<blockquote>{esc(clip(text, 3500)) or '<i>медиа</i>'}</blockquote>", reply_markup=kb)
    except Exception as e:
        logging.error(f"Не удалось переслать VIP-сообщение от {sender_id}: {e}")
        await bot.send_message(MY_ID, f"{header}\n<blockquote>{esc(clip(text, 3500)) or '<i>медиа — не удалось переслать</i>'}</blockquote>", reply_markup=kb)
    db.log_activity("personal", message.chat.id, sender_id, message.from_user.first_name or "", text or "[Медиа]", "", "vip", "VIP — переслано владельцу")

@dp.business_message()
async def handle_business_message(message: types.Message):
    sender_id = message.from_user.id
    chat_id = message.chat.id
    sender_name = message.from_user.first_name or "Пользователь"
    text = message.text or message.caption or ""
    shown_text = esc(clip(text, 1500)) or "<i>голосовое / медиа</i>"

    # Сообщения, которые отправил сам бот (автоответ, черновик), не считаем ручными ответами
    if message.sender_business_bot:
        return

    last_msg_time = user_last_manual_msg.get(chat_id, 0)
    was_paused = (time.time() - last_msg_time < PAUSE_TIMEOUT)

    # Запоминаем собеседника (чат в Business — всегда личка с ним) и соединение для отправки черновиков
    try:
        db.upsert_known_chat(chat_id, message.chat.full_name or str(chat_id), message.chat.username,
                             message.business_connection_id)
    except Exception as e:
        logging.warning(f"Не удалось сохранить чат {chat_id}: {e}")

    # 1. Если сообщение отправлено ТОБОЙ (пишешь сам вручную)
    if sender_id == MY_ID:
        user_last_manual_msg[chat_id] = time.time()
        final_text = text

        # --- ФОРМАТИРОВАНИЕ ПО ПРЕФИКСУ ~ (редактируем исходное сообщение, а не отвечаем) ---
        if text.startswith("~"):
            clean_text = text[1:].strip()
            final_text = ""  # кавайный текст — не образец твоего стиля
            if clean_text:
                try:
                    cute_text = await make_cute_text(clean_text)
                    if cute_text and cute_text != clean_text:
                        if message.caption is not None:
                            await bot.edit_message_caption(
                                caption=cute_text[:1024],
                                chat_id=chat_id,
                                message_id=message.message_id,
                                business_connection_id=message.business_connection_id,
                                parse_mode=None
                            )
                        else:
                            await bot.edit_message_text(
                                text=cute_text,
                                chat_id=chat_id,
                                message_id=message.message_id,
                                business_connection_id=message.business_connection_id,
                                parse_mode=None
                            )
                except Exception as e:
                    logging.error(f"Ошибка при редактировании няшного сообщения: {e}")

        db.add_chat_message(chat_id, "me", final_text)

        # Если автоответ в этом чате и так не работает — не о чем уведомлять
        autoreply_off = db.is_chat_disabled(chat_id) or db.is_blacklisted(chat_id)

        # Отправляем уведомление ТОЛЬКО если чат еще не был на паузе
        if not was_paused and not autoreply_off:
            logging.info(f"⏸️ Зафиксирован личный ответ в чате {chat_id}. Ставим на паузу.")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Вернуть автоответ", callback_data=f"resume_{chat_id}"),
                InlineKeyboardButton(text="🛑 Отключить чат", callback_data=f"disablechat_{chat_id}")
            ]])
            await bot.send_message(
                MY_ID,
                f"⏸️ <b>Пауза 10 минут</b>\n"
                f"Ты ответил сам в чате с <b>{esc(message.chat.full_name) or chat_id}</b> — я пока помолчу.",
                reply_markup=kb
            )
        return

    # Проверка глобальных запретов (Черный список или Отключенный чат)
    if db.is_blacklisted(sender_id) or db.is_chat_disabled(chat_id):
        return

    # История берётся ДО записи текущего сообщения, чтобы оно не попало в контекст дважды
    history = db.get_chat_history(chat_id, limit=10)
    if text:
        db.add_chat_message(chat_id, "them", text)

    # VIP: бот никогда не отвечает, сразу пересылает тебе (в любом режиме, без отметки «прочитано»)
    if db.is_vip(sender_id):
        await forward_vip_message(message)
        return

    # 2. ПРОВЕРКА СПЕЦИАЛЬНЫХ РЕЖИМОВ И ПАУЗЫ
    status = db.get_status()

    # Прочитываем сообщение ТОЛЬКО если бот реально собирается отвечать/обрабатывать
    async def mark_read():
        try:
            await bot.read_business_message(
                business_connection_id=message.business_connection_id,
                chat_id=chat_id,
                message_id=message.message_id
            )
        except Exception as e:
            logging.warning(f"Не удалось прочитать сообщение: {e}")

    async def status_reply(mode: str):
        await mark_read()
        # Статусный ответ — не чаще раза в STATUS_REPLY_COOLDOWN на чат, а не на каждое сообщение
        key = (chat_id, mode)
        if time.time() - status_reply_sent.get(key, 0) < STATUS_REPLY_COOLDOWN:
            return
        status_reply_sent[key] = time.time()
        await asyncio.sleep(2)
        await message.answer(STATUS_REPLIES[mode], parse_mode=None)
        db.add_chat_message(chat_id, "bot", STATUS_REPLIES[mode])
        db.log_activity("status", chat_id, sender_id, sender_name, text, STATUS_REPLIES[mode], mode)

    # Чат на 10-минутной паузе после твоего ответа — ты сейчас в диалоге, поэтому бот молчит
    # в любом режиме (иначе писал бы «Пользователь спит» посреди переписки).
    # Голосовые на паузе идут дальше: их расшифруем и пришлём только тебе.
    paused_voice = was_paused and bool(message.voice)

    if status == "ignore" and not was_paused:
        await status_reply("ignore")
        return

    if status == "sleep" and not was_paused:
        db.save_night_message(sender_name, text or "[Голосовое сообщение/Медиа]")
        await status_reply("sleep")
        return

    if status == "busy" and not was_paused and "срочно" not in text.lower():
        await status_reply("busy")
        return

    if was_paused and not message.voice:
        if message.photo or "срочно" in text.lower():
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Вернуть автоответ", callback_data=f"resume_{chat_id}")
            ]])
            await bot.send_message(
                MY_ID,
                f"📩 <b>Срочное / медиа</b> · {esc(sender_name)}\n"
                f"<i>Чат на паузе, я не отвечал.</i>\n"
                f"<blockquote>{shown_text}</blockquote>",
                reply_markup=kb
            )
        return

    if not paused_voice:
        await mark_read()

    photo_path = None
    if message.photo:
        try:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            photo_path = f"temp_{photo.file_id}.jpg"
            await bot.download_file(file_info.file_path, photo_path)
        except Exception as e:
            logging.error(f"Ошибка загрузки фото: {e}")

    voice_path = None
    if message.voice:
        try:
            file_info = await bot.get_file(message.voice.file_id)
            voice_path = f"temp_{message.voice.file_id}.ogg"
            await bot.download_file(file_info.file_path, voice_path)
        except Exception as e:
            logging.error(f"Ошибка загрузки голосового: {e}")

    user_profile = db.get_user_profile(sender_id)
    is_aggr = db.is_aggressive(sender_id)

    analysis = await analyze_message(text, photo_path, voice_path, user_profile, is_aggressive=is_aggr, history=history)

    for path in (photo_path, voice_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    category = analysis.get("category", "formal")
    summary = analysis.get("summary", "")
    new_profile = analysis.get("user_profile")
    incoming_for_log = text or ("[Голосовое]" if message.voice else "[Медиа]")

    db.log_stat(sender_id, category)
    if new_profile:
        db.update_user_profile(sender_id, sender_name, new_profile)

    # Голосовые и фото без подписи попадают в историю через расшифровку/описание
    if not text and summary:
        db.add_chat_message(chat_id, "them", f"[{'Голосовое' if message.voice else 'Медиа'}] {summary}")

    summary_block = f"\n💡 <i>{esc(summary)}</i>" if summary else ""
    who = f"{esc(sender_name)} · <code>{sender_id}</code>"

    # 3. ОБРАБОТКА И ОТВЕТЫ
    if analysis.get("tone_warning"):
        db.log_activity("tone", chat_id, sender_id, sender_name, incoming_for_log, "", category, summary)
        await bot.send_message(
            MY_ID,
            f"⚠️ <b>Повышенный тон</b> · {who}\n<blockquote>{shown_text}</blockquote>"
        )

    aggr_btn = (
        InlineKeyboardButton(text="🟢 Выкл агрессию", callback_data=f"unaggr_{sender_id}")
        if is_aggr else
        InlineKeyboardButton(text="🔥 Вкл агрессию", callback_data=f"aggr_{sender_id}")
    )
    summary_btn = InlineKeyboardButton(text="🧾 Что я пропустил?", callback_data=f"chat_summary_{chat_id}")
    action_rows = [
        [
            InlineKeyboardButton(text="🚫 Игнорировать", callback_data=f"ban_{sender_id}"),
            InlineKeyboardButton(text="⏸️ Пауза", callback_data=f"pause_{chat_id}")
        ],
        [aggr_btn, summary_btn],
        [
            InlineKeyboardButton(text="⭐ В VIP", callback_data=f"vip_{sender_id}"),
            InlineKeyboardButton(text="🛑 Отключить чат", callback_data=f"disablechat_{chat_id}")
        ]
    ]
    kb_actions = InlineKeyboardMarkup(inline_keyboard=action_rows)

    if paused_voice:
        db.log_activity("personal", chat_id, sender_id, sender_name, incoming_for_log, "", category, summary)
        await bot.send_message(
            MY_ID,
            f"🎤 <b>Голосовое</b> · {who}\n<i>Чат на паузе, я не отвечал.</i>\n"
            f"<blockquote expandable>{esc(clip(summary, 3000)) or 'не удалось расшифровать'}</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Вернуть автоответ", callback_data=f"resume_{chat_id}"),
                summary_btn
            ]])
        )
        return

    if category == "personal":
        db.log_activity("personal", chat_id, sender_id, sender_name, incoming_for_log, "", category, summary)
        # Сам не отвечаю, но готовлю черновик в твоём стиле — отправишь одной кнопкой
        incoming_for_draft = text or summary
        draft_text = await make_draft_reply(
            incoming_for_draft,
            history=db.get_chat_history(chat_id, limit=15),
            style_samples=db.get_owner_style_samples(),
            user_profile=new_profile or user_profile
        )
        if draft_text and message.business_connection_id:
            draft_id = db.add_draft(chat_id, message.business_connection_id, incoming_for_draft, draft_text)
            sent = await bot.send_message(
                MY_ID,
                f"📥 <b>Личное</b> · {who}\n<blockquote>{shown_text}</blockquote>{summary_block}\n\n"
                + draft_block(draft_text),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=draft_rows(draft_id) + action_rows)
            )
            db.update_draft(draft_id, owner_msg_id=sent.message_id)
        else:
            await bot.send_message(
                MY_ID,
                f"📥 <b>Личное</b> · {who}\n<blockquote>{shown_text}</blockquote>{summary_block}",
                reply_markup=kb_actions
            )

    elif category in ["formal", "tech_vpn", "urgent"] or message.voice:
        await asyncio.sleep(2)
        reply_text = analysis.get("suggested_reply")

        if message.photo and category == "tech_vpn":
            db.log_activity("screenshot", chat_id, sender_id, sender_name, incoming_for_log, "", category, summary)
            await bot.send_message(
                MY_ID,
                f"📸 <b>Разбор скриншота</b> · {who}{summary_block}",
                reply_markup=kb_actions
            )
        elif reply_text:
            # Ответ ИИ отправляем как обычный текст: символы < > & не должны ломать отправку
            await message.answer(reply_text, parse_mode=None)
            db.add_chat_message(chat_id, "bot", reply_text)
            db.log_activity("reply", chat_id, sender_id, sender_name, incoming_for_log, reply_text, category, summary)
            aggr_tag = " 🔥" if is_aggr else ""
            await bot.send_message(
                MY_ID,
                f"🤖 <b>Ответил за тебя</b>{aggr_tag} · {who}\n"
                f"<blockquote>{shown_text}</blockquote>\n"
                f"↳ {esc(reply_text)}{summary_block}",
                reply_markup=kb_actions
            )

# --- Черновики ответов на личное ---

DRAFT_MARKER = "✍️ <b>Черновик ответа</b>"

def draft_block(draft_text: str) -> str:
    return (
        f"{DRAFT_MARKER} <i>(в твоём стиле)</i>\n"
        f"<blockquote>{esc(clip(draft_text, 1800))}</blockquote>\n"
        f"<i>Можно ответить на это сообщение своим текстом — отправлю его вместо черновика.</i>"
    )

def draft_rows(draft_id: int) -> list:
    return [[
        InlineKeyboardButton(text="📤 Отправить", callback_data=f"draft_send_{draft_id}"),
        InlineKeyboardButton(text="🔄 Другой", callback_data=f"draft_regen_{draft_id}"),
        InlineKeyboardButton(text="✖️", callback_data=f"draft_drop_{draft_id}")
    ]]

async def send_as_owner(draft: dict, text_to_send: str) -> Optional[str]:
    """Отправляет текст собеседнику от твоего имени. Возвращает текст ошибки или None"""
    try:
        await bot.send_message(
            draft["chat_id"], text_to_send,
            business_connection_id=draft["business_connection_id"],
            parse_mode=None
        )
    except TelegramBadRequest as e:
        return str(e)
    db.update_draft(draft["id"], text=text_to_send, status="sent")
    db.add_chat_message(draft["chat_id"], "me", text_to_send)
    # Ты ответил (пусть и черновиком) — автоответчик в этом чате молчит, как после ручного ответа
    user_last_manual_msg[draft["chat_id"]] = time.time()
    return None

def draft_chat_name(draft: dict) -> str:
    known = db.get_known_chat(draft["chat_id"])
    return esc(known["name"]) if known and known.get("name") else f"<code>{draft['chat_id']}</code>"

@dp.callback_query(F.data.startswith("draft_send_"))
async def cb_draft_send(callback: types.CallbackQuery):
    draft = db.get_draft(int(callback.data.replace("draft_send_", "")))
    if not draft or draft["status"] != "pending":
        await callback.answer("Этот черновик уже отправлен или отменён.", show_alert=True)
        return
    error = await send_as_owner(draft, draft["text"])
    if error:
        await callback.answer("Не получилось отправить", show_alert=True)
        await callback.message.answer(f"⚠️ <b>Черновик не отправлен</b>\n<code>{esc(clip(error, 300))}</code>")
        return
    await callback.answer("📤 Отправлено")
    await safe_edit(
        callback.message,
        f"📤 <b>Отправлено</b> → {draft_chat_name(draft)}\n<blockquote>{esc(draft['text'])}</blockquote>",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🧾 Что я пропустил?", callback_data=f"chat_summary_{draft['chat_id']}")
        ]])
    )

@dp.callback_query(F.data.startswith("draft_regen_"))
async def cb_draft_regen(callback: types.CallbackQuery):
    draft = db.get_draft(int(callback.data.replace("draft_regen_", "")))
    if not draft or draft["status"] != "pending":
        await callback.answer("Этот черновик уже отправлен или отменён.", show_alert=True)
        return
    await callback.answer("✍️ Пишу другой вариант…")
    new_text = await make_draft_reply(
        draft["incoming"],
        history=db.get_chat_history(draft["chat_id"], limit=15),
        style_samples=db.get_owner_style_samples(),
        user_profile=db.get_user_profile(draft["chat_id"]),
        previous_draft=draft["text"]
    )
    if not new_text:
        await callback.message.answer("⚠️ ИИ сейчас не ответил — попробуй ещё раз чуть позже.")
        return
    db.update_draft(draft["id"], text=new_text)
    # Заменяем только блок черновика, оставляя исходное уведомление над ним
    head = (callback.message.html_text or "").rsplit(DRAFT_MARKER, 1)[0].rstrip()
    await safe_edit(
        callback.message,
        f"{head}\n\n{draft_block(new_text)}" if head else draft_block(new_text),
        callback.message.reply_markup
    )

@dp.callback_query(F.data.startswith("draft_drop_"))
async def cb_draft_drop(callback: types.CallbackQuery):
    draft_id = int(callback.data.replace("draft_drop_", ""))
    db.update_draft(draft_id, status="dropped")
    await callback.answer("Черновик убран")
    # Кнопки черновика убираем, остальные (игнор, пауза, сводка) оставляем
    rows = [row for row in (callback.message.reply_markup.inline_keyboard if callback.message.reply_markup else [])
            if not any((b.callback_data or "").startswith("draft_") for b in row)]
    head = (callback.message.html_text or "").rsplit(DRAFT_MARKER, 1)[0].rstrip()
    await safe_edit(callback.message, head or "✖️ Черновик убран", InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

async def try_send_draft_reply(message: types.Message) -> bool:
    """Ответ реплаем на уведомление с черновиком — отправляем собеседнику твой текст"""
    reply_to = message.reply_to_message
    if not reply_to or not message.text:
        return False
    draft = db.get_draft_by_owner_msg(reply_to.message_id)
    if not draft:
        return False
    if draft["status"] == "sent":
        await message.answer("ℹ️ На это сообщение ты уже ответил. Новый текст отправь прямо в чат.")
        return True
    error = await send_as_owner(draft, message.text)
    if error:
        await message.answer(f"⚠️ <b>Не отправилось</b>\n<code>{esc(clip(error, 300))}</code>")
    else:
        await message.answer(f"📤 <b>Отправлено</b> → {draft_chat_name(draft)}")
        try:
            await reply_to.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    return True

# --- Сводка «Что я пропустил?» ---

@dp.callback_query(F.data.startswith("chat_summary_"))
async def cb_chat_summary(callback: types.CallbackQuery):
    chat_id = int(callback.data.replace("chat_summary_", ""))
    history = db.get_chat_history(chat_id, limit=30)
    if not history:
        await callback.answer("Я пока не видел переписки в этом чате.", show_alert=True)
        return
    await callback.answer("🧾 Собираю сводку…")
    known = db.get_known_chat(chat_id)
    name = known["name"] if known and known.get("name") else str(chat_id)
    summary_text = await summarize_chat(history, name)
    if not summary_text:
        await callback.message.answer("⚠️ ИИ сейчас не ответил — попробуй ещё раз чуть позже.")
        return
    await callback.message.answer(
        f"🧾 <b>Что ты пропустил</b> · {esc(name)}\n"
        f"<i>по последним {len(history)} сообщениям</i>\n\n{esc(summary_text)}"
    )

# --- Инлайн-кнопки Бизнес-Ассистента ---

@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_blacklist(user_id)
    await callback.answer("Больше не отвечаю этому человеку", show_alert=True)
    await callback.message.edit_text(
        f"🚫 <b>Игнорирую</b> пользователя <code>{user_id}</code>\nВернуть: <code>/unban {user_id}</code>"
    )

@dp.callback_query(F.data.startswith("aggr_"))
async def callback_aggr(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_aggressive(user_id)
    await callback.answer("Агрессивный режим включён", show_alert=True)
    await callback.message.edit_text(f"🔥 <b>Агрессивный режим включён</b> для <code>{user_id}</code>")

@dp.callback_query(F.data.startswith("unaggr_"))
async def callback_unaggr(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.remove_from_aggressive(user_id)
    await callback.answer("Агрессивный режим выключен", show_alert=True)
    await callback.message.edit_text(f"🟢 <b>Агрессивный режим выключен</b> для <code>{user_id}</code>")

@dp.callback_query(F.data.startswith("resume_"))
async def callback_resume(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    user_last_manual_msg[chat_id] = 0
    await callback.answer("Автоответ снова работает", show_alert=True)
    await callback.message.edit_text(f"▶️ <b>Автоответ включён</b> в чате <code>{chat_id}</code>")

@dp.callback_query(F.data.startswith("pause_"))
async def callback_pause(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    user_last_manual_msg[chat_id] = time.time()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Вернуть автоответ", callback_data=f"resume_{chat_id}")
    ]])
    await callback.answer("Пауза на 10 минут", show_alert=True)
    await callback.message.edit_text(f"⏸️ <b>Пауза 10 минут</b> в чате <code>{chat_id}</code>", reply_markup=kb)

# ==============================================================================
# МОДУЛЬ 3: УПРАВЛЕНИЕ НАПОМИНАНИЯМИ И ЗАМЕТКАМИ (ЛС БОТА)
# ==============================================================================

def format_local_dt(dt_local_str: str) -> str:
    try:
        return datetime.fromisoformat(dt_local_str).strftime("%d.%m в %H:%M")
    except Exception:
        return (dt_local_str or "")[:16]

def build_reminders_list(user_id: int):
    active = db.get_active_reminders(user_id)
    if not active:
        text = (
            "⏰ <b>Напоминания</b>\n\n"
            "Пока пусто. Чтобы создать, просто напиши:\n"
            "<blockquote>завтра в 14:00 скинуть отчёт по физике\n"
            "через 30 минут выключить духовку</blockquote>"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()])

    lines = []
    keyboard_rows = []
    for idx, rem in enumerate(active, 1):
        rem_id = rem["id"]
        lines.append(f"<b>{idx}.</b> <code>{format_local_dt(rem['remind_at_local'])}</code> — {esc(rem['text'])}")
        keyboard_rows.append([
            InlineKeyboardButton(text=f"✅ {idx}", callback_data=f"done_rem_{rem_id}"),
            InlineKeyboardButton(text="⏰ +15м", callback_data=f"snooze_{rem_id}_15"),
            InlineKeyboardButton(text=f"🗑 {idx}", callback_data=f"cancel_rem_{rem_id}")
        ])
    keyboard_rows.append(back_to_menu_row())

    text = f"⏰ <b>Напоминания</b> · {len(active)}\n\n" + "\n".join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    text, kb = build_reminders_list(message.from_user.id)
    await message.answer(text, reply_markup=kb)

def build_notes_menu(user_id: int):
    counts = db.get_notes_categories_stats(user_id)
    total = sum(counts.values())

    kb_rows = []
    lines = []
    for cat in db.NOTE_CATEGORIES:
        c = counts.get(cat, 0)
        icon = db.CATEGORY_EMOJIS[cat]
        lines.append(f"{icon} {cat} — <b>{c}</b>")
        kb_rows.append([InlineKeyboardButton(text=f"{icon} {cat} ({c})", callback_data=f"view_cat_{cat}")])
    kb_rows.append(back_to_menu_row())

    text = (
        f"📝 <b>Заметки</b> · {total}\n\n"
        + "\n".join(lines)
        + "\n\n<i>Выбери категорию, чтобы открыть.</i>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)

@dp.message(Command("notes"))
async def cmd_notes(message: types.Message):
    text, kb = build_notes_menu(message.from_user.id)
    await message.answer(text, reply_markup=kb)

TZ_CHOICES = [
    (2, "Калининград"), (3, "Москва"),
    (4, "Самара"), (5, "Екатеринбург"),
    (6, "Омск"), (7, "Новосибирск"),
]

def build_tz_menu(user_id: int):
    tz = db.get_user_tz_offset(user_id)
    rows = []
    for i in range(0, len(TZ_CHOICES), 2):
        rows.append([
            InlineKeyboardButton(
                text=f"{'● ' if off == tz else ''}{tz_label(off)} {city}",
                callback_data=f"set_tz_{off}"
            )
            for off, city in TZ_CHOICES[i:i + 2]
        ])
    rows.append(back_to_menu_row())
    text = (
        f"🌍 <b>Часовой пояс</b>\n\n"
        f"Сейчас: <b>{tz_label(tz)}</b>\n\n"
        f"Выбери из списка или отправь, например, <code>/timezone 3</code>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("timezone"))
async def cmd_timezone(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) == 2 and args[1].lstrip("+-").isdigit():
        offset = int(args[1])
        if not -12 <= offset <= 14:
            await message.answer("⚠️ Часовой пояс должен быть от <code>-12</code> до <code>14</code>.")
            return
        db.set_user_tz_offset(user_id, offset)
        await message.answer(f"✅ <b>Часовой пояс: {tz_label(offset)}</b>")
        return

    text, kb = build_tz_menu(user_id)
    await message.answer(text, reply_markup=kb)

@dp.message(Command("remind"))
async def cmd_remind_manual(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Формат:</b>\n"
            "<code>/remind завтра в 15:00 сдать курсовую</code>\n"
            "<code>/remind через 20 минут проверить пирог</code>"
        )
        return
    await process_incoming_text(message, args[1])

@dp.message(Command("note"))
async def cmd_note_manual(message: types.Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Формат:</b>\n"
            "<code>/note покупки молоко, сыр, хлеб</code>\n"
            "<code>/note учеба решить 5 задач по матану</code>\n"
            "<code>/note просто любая мысль</code>"
        )
        return

    if len(args) == 3 and args[1].lower() in ["учеба", "учёба", "покупки", "софт/vpn", "софт", "vpn", "идеи", "другое"]:
        cat = db.normalize_category(args[1])
        note_id = db.add_note(message.from_user.id, cat, args[2])
        await message.answer(note_saved_text(cat, args[2]), reply_markup=note_saved_kb(cat, note_id))
    else:
        full_text = message.text.split(maxsplit=1)[1]
        await process_incoming_text(message, full_text)

# ==============================================================================
# ОБРАБОТКА ВХОДЯЩИХ СООБЩЕНИЙ В ЛС БОТА (ИИ-ПАРСИНГ НАПОМИНАНИЙ И ЗАМЕТОК)
# ==============================================================================

def note_saved_text(cat: str, content: str, source_info: str = "") -> str:
    source_block = f"\n<i>📎 {esc(source_info)}</i>" if source_info else ""
    return (
        f"📝 <b>Заметка сохранена</b> · {get_cat_icon(cat)} {cat}\n"
        f"<blockquote>{esc(content)}</blockquote>"
        f"{source_block}"
    )

def note_saved_kb(cat: str, note_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"📂 {get_cat_icon(cat)} {cat}", callback_data=f"view_cat_{cat}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_note_{note_id}")
        ],
        [InlineKeyboardButton(text="📚 Все заметки", callback_data="notes_menu")]
    ])

async def process_incoming_text(message: types.Message, raw_text: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    tz_offset = db.get_user_tz_offset(user_id)

    # Определяем источник (если переслано из другого чата)
    source_info = ""
    if message.forward_origin:
        origin = message.forward_origin
        if origin.type == "user" and getattr(origin, "sender_user", None):
            source_info = f"Переслано от {origin.sender_user.full_name}"
        elif origin.type == "chat" and getattr(origin, "chat", None):
            source_info = f"Переслано из {origin.chat.title}"
        elif origin.type == "channel" and getattr(origin, "chat", None):
            source_info = f"Переслано из канала {origin.chat.title}"
        elif origin.type == "hidden_user":
            source_info = f"Переслано от {getattr(origin, 'sender_user_name', 'Скрытый пользователь')}"
        else:
            source_info = "Пересланное сообщение"

    # Запускаем интеллектуальный парсинг Gemini (с fallback-парсером)
    res = await parse_user_intent(
        raw_text,
        user_id=user_id,
        is_forwarded=bool(message.forward_origin),
        source_info=source_info,
        tz_offset=tz_offset
    )
    if hasattr(res, "model_dump"):
        res_data = res.model_dump()
    elif isinstance(res, dict):
        res_data = res
    else:
        res_data = {}

    intent = res_data.get("intent", "note")

    # 1. НАПОМИНАНИЕ (есть дата/время)
    if intent == "reminder":
        title = res_data.get("title") or raw_text
        remind_dt_iso = res_data.get("datetime")
        local_tz = timezone(timedelta(hours=tz_offset))

        # Если время не определилось, ставим через 1 час по умолчанию
        target_dt = None
        if remind_dt_iso:
            try:
                target_dt = datetime.fromisoformat(remind_dt_iso)
                if target_dt.tzinfo is None:
                    target_dt = target_dt.replace(tzinfo=local_tz)
            except Exception:
                target_dt = None
        if target_dt is None:
            target_dt = datetime.now(local_tz) + timedelta(hours=1)

        dt_utc = target_dt.astimezone(timezone.utc)
        dt_local = target_dt.astimezone(local_tz)

        rem_id = db.add_reminder(
            user_id=user_id,
            chat_id=chat_id,
            text=title,
            remind_at_utc=dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
            remind_at_local=dt_local.strftime("%Y-%m-%d %H:%M:%S"),
            source_info=source_info
        )

        source_block = f"\n<i>📎 {esc(source_info)}</i>" if source_info else ""
        text_out = (
            f"✅ <b>Напомню {dt_local.strftime('%d.%m в %H:%M')}</b>\n"
            f"<blockquote>{esc(title)}</blockquote>"
            f"{source_block}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_rem_{rem_id}"),
                InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snooze_{rem_id}_15")
            ],
            [InlineKeyboardButton(text="📋 Все напоминания", callback_data="show_reminders_list")]
        ])
        await message.answer(text_out, reply_markup=kb)

    # 2. ЗАМЕТКА (нет времени)
    elif intent == "note":
        cat = db.normalize_category(res_data.get("category") or "Другое")
        content = res_data.get("title") or raw_text
        note_id = db.add_note(user_id=user_id, category=cat, content=content)
        await message.answer(note_saved_text(cat, content, source_info), reply_markup=note_saved_kb(cat, note_id))

    # 3. ОБЫЧНЫЙ ЧАТ / ВОПРОС К ИИ
    else:
        reply = res_data.get("reply")
        if not reply:
            reply = "Записал! Пришли задачу со временем («завтра в 14:00 отчёт») или мысль для заметки."
        # Ответы ИИ (код, длинные объяснения) легко превышают лимит Telegram в 4096 символов
        full = f"🤖 {reply}"
        for start in range(0, len(full), 4000):
            await message.answer(full[start:start + 4000], parse_mode=None)

# --- Бэкап вручную и восстановление из файла ---

@dp.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if not backup_state["allowed"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💾 Всё равно сохранить", callback_data="backup_force")
        ]])
        await message.answer(
            "⚠️ <b>Автобэкап на паузе</b>: при запуске не удалось восстановить данные из прошлого бэкапа.\n"
            "Если сохранить сейчас, закреплённой станет текущая (возможно, пустая) база. "
            "Предыдущий файл останется в чате.",
            reply_markup=kb
        )
        return
    sent = await make_backup("Бэкап по команде /backup", force=True)
    if not sent:
        await message.answer("⚠️ Не удалось сделать бэкап — проверь, что задан MY_TELEGRAM_ID.")

@dp.callback_query(F.data == "backup_force")
async def cb_backup_force(callback: types.CallbackQuery):
    backup_state["allowed"] = True
    backup_state["restore_attempts"] = RESTORE_RETRIES  # больше не пытаемся восстановиться автоматически
    await callback.answer("Сохраняю текущую базу")
    await make_backup("Бэкап по команде /backup", force=True)
    await safe_edit(callback.message, "💾 <b>Автобэкап снова работает</b>, текущая база сохранена.")

@dp.message(F.chat.type == "private", F.document)
async def handle_backup_file(message: types.Message):
    """Присланный файл .db — предлагаем восстановить из него базу"""
    name = (message.document.file_name or "").lower()
    if not name.endswith(".db"):
        # Обычный файл: как и раньше, разбираем подпись как задачу/заметку
        if message.caption:
            await process_incoming_text(message, message.caption)
        else:
            await message.answer("💡 Пришли текстом или перешли сообщение с описанием задачи / заметки.")
        return
    key = secrets.token_hex(6)
    pending_restores[key] = message.document.file_id
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♻️ Восстановить", callback_data=f"restore_db_{key}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"restore_cancel_{key}")
    ]])
    await message.answer(
        f"♻️ <b>Восстановить базу из «{esc(message.document.file_name)}»?</b>\n"
        "Текущие заметки, напоминания и настройки заменятся содержимым файла. "
        "Перед этим я сохраню текущую базу отдельным бэкапом.",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("restore_cancel_"))
async def cb_restore_cancel(callback: types.CallbackQuery):
    pending_restores.pop(callback.data.replace("restore_cancel_", ""), None)
    await callback.answer("Отменено")
    await safe_edit(callback.message, "✖️ Восстановление отменено.")

@dp.callback_query(F.data.startswith("restore_db_"))
async def cb_restore_db(callback: types.CallbackQuery):
    file_id = pending_restores.pop(callback.data.replace("restore_db_", ""), None)
    if not file_id:
        await callback.answer("Кнопка устарела — пришли файл ещё раз.", show_alert=True)
        return
    await callback.answer("Восстанавливаю…")
    try:
        await make_backup("Копия перед восстановлением из файла", force=True)
    except Exception as e:
        logging.warning(f"Не удалось сохранить копию перед восстановлением: {e}")
    try:
        await restore_from_file_id(file_id)
    except Exception as e:
        await safe_edit(callback.message, f"⚠️ <b>Не получилось восстановить</b>\n<code>{esc(clip(str(e), 300))}</code>")
        return
    backup_state["allowed"] = True
    backup_state["restore_attempts"] = RESTORE_RETRIES
    await safe_edit(callback.message, "♻️ <b>База восстановлена из файла.</b> Закрепляю её как актуальный бэкап.")
    await make_backup("Восстановлено из присланного файла", force=True)

@dp.message(F.chat.type == "private")
async def handle_private_message(message: types.Message):
    """Прием любого текста или пересланного сообщения в ЛС бота"""
    # Пропускаем служебные команды
    if message.text and message.text.startswith("/"):
        return

    # Ответ реплаем на черновик — это текст для собеседника, а не задача/заметка
    if await try_send_draft_reply(message):
        return

    text = message.text or message.caption or ""
    if not text:
        if message.voice or message.photo:
            await message.answer("💡 Пришли текстом или перешли сообщение с описанием задачи / заметки.")
        return

    await process_incoming_text(message, text)

# ==============================================================================
# ИНЛАЙН-ОБРАБОТЧИКИ МОДУЛЯ 3 (КНОПКИ НАПОМИНАНИЙ И ЗАМЕТОК)
# ==============================================================================

@dp.callback_query(F.data.startswith("cancel_rem_"))
async def cb_cancel_rem(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    db.delete_reminder(rem_id, callback.from_user.id)
    await callback.answer("Напоминание удалено")
    await callback.message.edit_text(
        "❌ <b>Напоминание удалено</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все напоминания", callback_data="show_reminders_list")]
        ])
    )

@dp.callback_query(F.data.startswith("done_rem_"))
async def cb_done_rem(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    db.mark_reminder_completed(rem_id, callback.from_user.id)
    await callback.answer("Отлично! 🎉")
    await callback.message.edit_text("✅ <b>Готово!</b> Напоминание выполнено.")

@dp.callback_query(F.data.startswith("snooze_"))
async def cb_snooze_rem(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    rem_id = int(parts[1])
    minutes = int(parts[2])

    new_dt_local = db.snooze_reminder(rem_id, callback.from_user.id, minutes)
    if not new_dt_local:
        await callback.answer("Это напоминание уже закрыто или удалено.", show_alert=True)
        return

    await callback.answer(f"Отложено на {minutes} мин")
    rem = db.get_reminder_by_id(rem_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data=f"done_rem_{rem_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cancel_rem_{rem_id}")
    ]])
    await callback.message.edit_text(
        f"⏰ <b>Перенёс на {new_dt_local.strftime('%d.%m в %H:%M')}</b> (+{minutes} мин)\n"
        f"<blockquote>{esc(rem['text']) if rem else ''}</blockquote>",
        reply_markup=kb
    )

@dp.callback_query(F.data == "show_reminders_list")
async def cb_show_reminders_list(callback: types.CallbackQuery):
    text, kb = build_reminders_list(callback.from_user.id)
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data == "notes_menu")
async def cb_notes_menu(callback: types.CallbackQuery):
    text, kb = build_notes_menu(callback.from_user.id)
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_cat_"))
async def cb_view_cat(callback: types.CallbackQuery):
    cat = db.normalize_category(callback.data.replace("view_cat_", ""))
    notes = db.get_notes_by_cat(callback.from_user.id, cat)
    icon = get_cat_icon(cat)

    if not notes:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ К категориям", callback_data="notes_menu")
        ]])
        await safe_edit(callback.message, f"{icon} <b>{cat}</b>\n\nЗдесь пока пусто.", kb)
        await callback.answer()
        return

    lines = []
    kb_rows = []
    for idx, n in enumerate(notes, 1):
        lines.append(f"<b>{idx}.</b> {esc(n['content'])} <i>· {n['created_at'][:10]}</i>")
        kb_rows.append([InlineKeyboardButton(text=f"🗑 Удалить {idx}", callback_data=f"del_note_{n['id']}")])

    kb_rows.append([
        InlineKeyboardButton(text="🧹 Очистить всё", callback_data=f"clear_cat_{cat}"),
        InlineKeyboardButton(text="⬅️ К категориям", callback_data="notes_menu")
    ])

    text = f"{icon} <b>{cat}</b> · {len(notes)}\n\n" + "\n".join(lines)
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("del_note_"))
async def cb_del_note(callback: types.CallbackQuery):
    note_id = int(callback.data.split("_")[2])
    note = db.get_note_by_id(note_id)
    db.delete_note_by_id(note_id, callback.from_user.id)
    await callback.answer("Заметка удалена")
    back_cat = note["category"] if note else None
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"📂 {back_cat}", callback_data=f"view_cat_{back_cat}") if back_cat
        else InlineKeyboardButton(text="📚 Все заметки", callback_data="notes_menu")
    ]])
    await callback.message.edit_text("🗑 <b>Заметка удалена</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("clear_cat_"))
async def cb_clear_cat(callback: types.CallbackQuery):
    cat = db.normalize_category(callback.data.replace("clear_cat_", ""))
    db.clear_notes_by_cat(callback.from_user.id, cat)
    await callback.answer("Категория очищена", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ К категориям", callback_data="notes_menu")
    ]])
    await callback.message.edit_text(f"🧹 <b>{get_cat_icon(cat)} {cat}</b> — все заметки удалены", reply_markup=kb)

@dp.callback_query(F.data == "open_tz_menu")
async def cb_open_tz_menu(callback: types.CallbackQuery):
    text, kb = build_tz_menu(callback.from_user.id)
    await safe_edit(callback.message, text, kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_tz_"))
async def cb_set_tz(callback: types.CallbackQuery):
    offset = int(callback.data.replace("set_tz_", ""))
    db.set_user_tz_offset(callback.from_user.id, offset)
    await callback.answer(f"Пояс: {tz_label(offset)}")
    text, kb = build_tz_menu(callback.from_user.id)
    await safe_edit(callback.message, text, kb)

@dp.error()
async def on_error(event: types.ErrorEvent):
    # Повторное нажатие той же кнопки даёт «message is not modified» — это не ошибка
    if isinstance(event.exception, TelegramBadRequest) and "not modified" in str(event.exception):
        if event.update.callback_query:
            try:
                await event.update.callback_query.answer()
            except Exception:
                pass
        return True
    logging.error(f"Необработанная ошибка: {event.exception}", exc_info=event.exception)
    return True

# ==============================================================================
# ТОЧКА ВХОДА (MAIN)
# ==============================================================================

async def main():
    # Инициализация всех таблиц базы данных (Бизнес-бот + Модуль 3)
    db.init_db()
    if not MY_ID:
        logging.warning("⚠️ MY_TELEGRAM_ID не задан — бот никому не ответит в ЛС.")

    # Восстановление базы из бэкапа — до запуска панели и воркеров, чтобы они сразу видели данные
    await init_backup()

    # Запуск встроенного веб-сервера для Render
    await start_web_server()

    # Запуск фонового воркера отправки напоминаний
    asyncio.create_task(reminder_worker(bot))
    asyncio.create_task(backup_worker())
    asyncio.create_task(schedule_worker())

    # Регистрация меню команд бота в Telegram
    commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="reminders", description="⏰ Напоминания"),
        BotCommand(command="notes", description="📝 Заметки"),
        BotCommand(command="panel", description="🖥 Веб-панель"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="goodmorning", description="🌅 Утренняя сводка"),
        BotCommand(command="schedule", description="🌙 Расписание сна"),
        BotCommand(command="vips", description="⭐ VIP-контакты"),
        BotCommand(command="disabled", description="🛑 Отключённые чаты"),
        BotCommand(command="default", description="💼 Режим: обычный"),
        BotCommand(command="busy", description="🎮 Режим: занят"),
        BotCommand(command="sleep", description="🌙 Режим: сплю"),
        BotCommand(command="ignore_all", description="🚫 Режим: не беспокоить"),
        BotCommand(command="timezone", description="🌍 Часовой пояс"),
        BotCommand(command="backup", description="💾 Бэкап базы сейчас"),
        BotCommand(command="style", description="✍️ Мой стиль для черновиков"),
        BotCommand(command="help", description="📖 Справка"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Не удалось обновить список команд: {e}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning(f"Не удалось удалить webhook: {e}")

    logging.info("🚀 Бизнес-Ассистент с Модулем 3 успешно запущен!")
    try:
        # Сессию закрываем сами: после остановки polling ещё нужно отправить финальный бэкап
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        # Render шлёт SIGTERM перед деплоем/перезапуском — успеваем сохранить последние изменения
        try:
            await asyncio.wait_for(make_backup("Бэкап перед выключением"), timeout=20)
        except Exception as e:
            logging.error(f"Не удалось сделать бэкап при выключении: {e}")
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
