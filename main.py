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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    CallbackQuery,
    Message
)
from dotenv import load_dotenv
from aiohttp import web

import db
from ai_brain import analyze_message, make_cute_text, parse_user_intent, MODELS_TO_TRY

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
    "sleep": ("🌙", "Сплю", "собираю сообщения в утренний дайджест"),
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
                except Exception as e:
                    logging.error(f"Не удалось отправить напоминание {rem_id} в чат {chat_id}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в цикле reminder_worker: {e}")

        await asyncio.sleep(15)

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
            InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats"),
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

@dp.message(Command("goodmorning"))
async def cmd_goodmorning(message: types.Message):
    db.set_status("default")
    logs = db.pop_night_messages()
    header = f"🌅 <b>Доброе утро!</b>\n{mode_line('default')}\n\n"
    if not logs:
        await message.answer(header + "Ночью никто не писал — можно спокойно пить кофе ☕")
        return

    lines = []
    budget = 3500  # лимит сообщения Telegram — 4096 символов
    for name, msg in logs:
        line = f"• <b>{esc(name) or 'Без имени'}</b>: {esc(clip(msg, 300))}"
        if budget - len(line) < 0:
            lines.append(f"…и ещё {len(logs) - len(lines)} сообщ.")
            break
        budget -= len(line)
        lines.append(line)

    await message.answer(
        header
        + f"Пока ты спал, писали ({len(logs)}):\n"
        + "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
    )

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
    "/default · /busy · /sleep · /ignore_all — режимы\n"
    "/goodmorning — ночной дайджест\n"
    "/stats — статистика\n\n"
    "🛠 <b>Управление чатами</b>\n"
    "/disable_chat · /enable_chat <code>ID</code> — автоответ в чате\n"
    "/aggressive · /unaggressive <code>ID</code> — дерзкие ответы\n"
    "/unban <code>ID</code> — убрать из игнора\n\n"
    "🖥 <b>Веб-панель</b>\n"
    "/panel — личная ссылка на панель с лентой ответов ИИ\n\n"
    "✨ <code>~текст</code> в любом чате — перепишу сообщение мило"
)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_to_menu_row()]))

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

@dp.business_message()
async def handle_business_message(message: types.Message):
    sender_id = message.from_user.id
    chat_id = message.chat.id
    sender_name = message.from_user.first_name or "Пользователь"
    text = message.text or message.caption or ""
    shown_text = esc(clip(text, 1500)) or "<i>голосовое / медиа</i>"

    last_msg_time = user_last_manual_msg.get(chat_id, 0)
    was_paused = (time.time() - last_msg_time < PAUSE_TIMEOUT)

    # 1. Если сообщение отправлено ТОБОЙ (пишешь сам вручную)
    if sender_id == MY_ID:
        user_last_manual_msg[chat_id] = time.time()

        # --- ФОРМАТИРОВАНИЕ ПО ПРЕФИКСУ ~ ---
        if text.startswith("~"):
            clean_text = text[1:].strip()
            if clean_text:
                try:
                    cute_text = await make_cute_text(clean_text)
                    if cute_text:
                        await bot.edit_message_text(
                            text=cute_text,
                            chat_id=chat_id,
                            message_id=message.message_id,
                            business_connection_id=message.business_connection_id,
                            parse_mode=None
                        )
                except Exception as e:
                    logging.error(f"Ошибка при редактировании няшного сообщения: {e}")

        # Отправляем уведомление ТОЛЬКО если чат еще не был на паузе
        if not was_paused:
            logging.info(f"⏸️ Зафиксирован личный ответ в чате {chat_id}. Ставим на паузу.")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Вернуть автоответ", callback_data=f"resume_{chat_id}")
            ]])
            await bot.send_message(
                MY_ID,
                f"⏸️ <b>Пауза 10 минут</b>\n"
                f"Ты ответил сам в чате <code>{chat_id}</code> — я пока помолчу.",
                reply_markup=kb
            )
        return

    # Проверка глобальных запретов (Черный список или Отключенный чат)
    if db.is_blacklisted(sender_id) or db.is_chat_disabled(chat_id):
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
        await asyncio.sleep(2)
        await message.answer(STATUS_REPLIES[mode], parse_mode=None)
        db.log_activity("status", chat_id, sender_id, sender_name, text, STATUS_REPLIES[mode], mode)

    if status == "ignore":
        await status_reply("ignore")
        return

    if status == "sleep":
        db.save_night_message(sender_name, text or "[Голосовое сообщение/Медиа]")
        await status_reply("sleep")
        return

    if status == "busy" and "срочно" not in text.lower():
        await status_reply("busy")
        return

    # Если чат на временной 10-минутной паузе после ручного ответа
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

    analysis = await analyze_message(text, photo_path, voice_path, user_profile, is_aggressive=is_aggr)

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
    kb_actions = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Игнорировать", callback_data=f"ban_{sender_id}"),
            InlineKeyboardButton(text="⏸️ Пауза", callback_data=f"pause_{chat_id}")
        ],
        [aggr_btn]
    ])

    if category == "personal":
        db.log_activity("personal", chat_id, sender_id, sender_name, incoming_for_log, "", category, summary)
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
            db.log_activity("reply", chat_id, sender_id, sender_name, incoming_for_log, reply_text, category, summary)
            aggr_tag = " 🔥" if is_aggr else ""
            await bot.send_message(
                MY_ID,
                f"🤖 <b>Ответил за тебя</b>{aggr_tag} · {who}\n"
                f"<blockquote>{shown_text}</blockquote>\n"
                f"↳ {esc(reply_text)}{summary_block}",
                reply_markup=kb_actions
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
        await message.answer(f"🤖 {esc(reply)}")

@dp.message(F.chat.type == "private")
async def handle_private_message(message: types.Message):
    """Прием любого текста или пересланного сообщения в ЛС бота"""
    # Пропускаем служебные команды
    if message.text and message.text.startswith("/"):
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

# ==============================================================================
# ТОЧКА ВХОДА (MAIN)
# ==============================================================================

async def main():
    # Инициализация всех таблиц базы данных (Бизнес-бот + Модуль 3)
    db.init_db()
    if not MY_ID:
        logging.warning("⚠️ MY_TELEGRAM_ID не задан — бот никому не ответит в ЛС.")

    # Запуск встроенного веб-сервера для Render
    await start_web_server()

    # Запуск фонового воркера отправки напоминаний
    asyncio.create_task(reminder_worker(bot))

    # Регистрация меню команд бота в Telegram
    commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="reminders", description="⏰ Напоминания"),
        BotCommand(command="notes", description="📝 Заметки"),
        BotCommand(command="panel", description="🖥 Веб-панель"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="goodmorning", description="🌅 Ночной дайджест"),
        BotCommand(command="default", description="💼 Режим: обычный"),
        BotCommand(command="busy", description="🎮 Режим: занят"),
        BotCommand(command="sleep", description="🌙 Режим: сплю"),
        BotCommand(command="ignore_all", description="🚫 Режим: не беспокоить"),
        BotCommand(command="timezone", description="🌍 Часовой пояс"),
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
