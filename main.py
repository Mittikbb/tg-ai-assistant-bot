import asyncio
import html
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
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
from ai_brain import analyze_message, make_cute_text, parse_user_intent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_ID = int(os.getenv("MY_TELEGRAM_ID", 0))

user_last_manual_msg = {}
PAUSE_TIMEOUT = 600  # 10 минут (в секундах)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

CATEGORY_ICONS = {
    "учеба": "🎓",
    "покупки": "🛒",
    "софт/vpn": "💻",
    "софт": "💻",
    "vpn": "🌐",
    "идеи": "💡",
    "другое": "📌"
}

def get_cat_icon(cat: str) -> str:
    return CATEGORY_ICONS.get(cat.lower(), "📌")

# ==============================================================================
# ВЕБ-СЕРВЕР (Для Render, UptimeRobot, 24/7 работы)
# ==============================================================================

async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Веб-сервер успешно запущен на порту {port}")

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
                rem_text = rem["text"]
                source_info = rem.get("source_info", "")

                source_block = f"\n<i>📌 Источник: {html.escape(source_info)}</i>" if source_info else ""
                msg_text = (
                    f"⏰ <b>НАПОМИНАНИЕ!</b>\n\n"
                    f"👉 <b>{html.escape(rem_text)}</b>"
                    f"{source_block}"
                )

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_rem_{rem_id}"),
                        InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snooze_{rem_id}_15")
                    ],
                    [
                        InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze_{rem_id}_60"),
                        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cancel_rem_{rem_id}")
                    ]
                ])

                try:
                    await bot_instance.send_message(chat_id=chat_id, text=msg_text, reply_markup=kb)
                    db.mark_reminder_completed(rem_id)
                except Exception as e:
                    logging.error(f"Не удалось отправить напоминание {rem_id} в чат {chat_id}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в цикле reminder_worker: {e}")

        await asyncio.sleep(15)

# ==============================================================================
# КОМАНДЫ БИЗНЕС-АССИСТЕНТА (Модули 1 и 2)
# ==============================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    status = db.get_status()
    tz = db.get_user_timezone(user_id)

    is_owner = (user_id == MY_ID)
    owner_section = ""
    if is_owner:
        owner_section = (
            f"💼 <b>Режим бизнес-автоответчика:</b> <code>{status}</code>\n"
            f"• /default — Обычный режим\n"
            f"• /ignore_all — Тотальный игнор\n"
            f"• /sleep — Режим сна\n"
            f"• /busy — Режим «Занят»\n"
            f"• /goodmorning — Утренний дайджест\n"
            f"• /stats — Статистика ответов\n"
            f"• /disable_chat ID — Отключить автоответ в чате\n"
            f"• /enable_chat ID — Включить автоответ в чате\n"
            f"• /aggressive ID — Вкл агрессивный режим\n"
            f"• /unaggressive ID — Выкл агрессивный режим\n"
            f"• /unban ID — Разблокировать пользователя\n"
            f"✨ <b>Няшный режим:</b> Начни сообщение с <code>~</code> (например: <code>~привет</code>)\n\n"
        )

    text = (
        f"👋 <b>Добро пожаловать в ИИ-Ассистент!</b>\n\n"
        f"{owner_section}"
        f"⏰ <b>Модуль 3: Напоминания и Заметки:</b>\n"
        f"• Просто напиши боту или перешли сообщение:\n"
        f"  <i>«завтра в 14:00 сдать отчет»</i> ➔ поставит напоминание\n"
        f"  <i>«купить переходник на Type-C»</i> ➔ сохранит в заметки\n\n"
        f"<b>Быстрые команды:</b>\n"
        f"• /reminders — Мои активные напоминания\n"
        f"• /notes — Мои сохраненные заметки\n"
        f"• /timezone — Часовой пояс (сейчас UTC{'+' if tz>=0 else ''}{tz})\n"
        f"• /help — Подробное руководство"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏰ Мои напоминания", callback_data="show_reminders_list"),
            InlineKeyboardButton(text="📝 Мои заметки", callback_data="notes_menu")
        ],
        [
            InlineKeyboardButton(text="🌍 Часовой пояс", callback_data="open_tz_menu")
        ]
    ])
    await message.answer(text, reply_markup=kb)

@dp.message(Command("default"))
async def cmd_default(message: types.Message):
    if message.from_user.id == MY_ID:
        db.set_status("default")
        await message.answer("🟢 Режим изменен на: <b>Обычный</b>")

@dp.message(Command("ignore_all"))
async def cmd_ignore(message: types.Message):
    if message.from_user.id == MY_ID:
        db.set_status("ignore")
        await message.answer("🔴 Режим изменен на: <b>Тотальный игнор</b>")

@dp.message(Command("sleep"))
async def cmd_sleep(message: types.Message):
    if message.from_user.id == MY_ID:
        db.set_status("sleep")
        await message.answer("🌙 Режим изменен на: <b>Сплю</b>")

@dp.message(Command("busy"))
async def cmd_busy(message: types.Message):
    if message.from_user.id == MY_ID:
        db.set_status("busy")
        await message.answer("🎮 Режим изменен на: <b>Занят</b>")

@dp.message(Command("goodmorning"))
async def cmd_goodmorning(message: types.Message):
    if message.from_user.id == MY_ID:
        db.set_status("default")
        logs = db.pop_night_messages()
        if logs:
            report = "🌅 <b>Ночной дайджест:</b>\n\n"
            for name, msg in logs:
                report += f"• <b>{name}</b>: {html.escape(msg)}\n"
        else:
            report = "🌅 Ночью никто не писал."
        await message.answer(report)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    stats = db.get_stats_summary()
    total = stats.get("total", 0)
    categories = stats.get("categories", {})
    
    text = (
        f"📊 <b>Статистика ассистента:</b>\n\n"
        f"Всего обработано сообщений: <b>{total}</b>\n\n"
        f"По категориям:\n"
        f"• 👥 Личные (personal): {categories.get('personal', 0)}\n"
        f"• 💬 Обычные (formal): {categories.get('formal', 0)}\n"
        f"• 💻 Технические (tech_vpn): {categories.get('tech_vpn', 0)}\n"
        f"• 🚨 Срочные (urgent): {categories.get('urgent', 0)}\n"
    )
    await message.answer(text)

@dp.message(Command("disable_chat"))
async def cmd_disable_chat(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer("⚠️ Использование: <code>/disable_chat ID_ЧАТА</code>")
        return
    chat_id = int(args[1])
    db.disable_chat(chat_id)
    await message.answer(f"🛑 Автоответчик <b>полностью отключен</b> для чата <code>{chat_id}</code>!")

@dp.message(Command("enable_chat"))
async def cmd_enable_chat(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer("⚠️ Использование: <code>/enable_chat ID_ЧАТА</code>")
        return
    chat_id = int(args[1])
    db.enable_chat(chat_id)
    await message.answer(f"🟢 Автоответчик <b>снова включен</b> для чата <code>{chat_id}</code>!")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer("⚠️ Использование: <code>/unban ID_ПОЛЬЗОВАТЕЛЯ</code>")
        return
    target_id = int(args[1])
    db.remove_from_blacklist(target_id)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> удален из черного списка!")

@dp.message(Command("aggressive"))
async def cmd_aggressive(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer("⚠️ Использование: <code>/aggressive ID_ПОЛЬЗОВАТЕЛЯ</code>")
        return
    target_id = int(args[1])
    db.add_to_aggressive(target_id)
    await message.answer(f"🔥 Агрессивный режим <b>включен</b> для пользователя <code>{target_id}</code>!")

@dp.message(Command("unaggressive"))
async def cmd_unaggressive(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip('-').isdigit():
        await message.answer("⚠️ Использование: <code>/unaggressive ID_ПОЛЬЗОВАТЕЛЯ</code>")
        return
    target_id = int(args[1])
    db.remove_from_aggressive(target_id)
    await message.answer(f"🟢 Агрессивный режим <b>отключен</b> для пользователя <code>{target_id}</code>.")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 <b>Справочник возможностей ИИ-Ассистента</b>\n\n"
        "⚡ <b>1. Умные напоминания (Модуль 3)</b>\n"
        "Напишите в ЛС боту дату/время и суть задачи, либо перешлите сообщение:\n"
        "• <i>«завтра в 14:00 скинуть отчет по физике»</i>\n"
        "• <i>«через 2 часа проверить духовку»</i>\n"
        "• <i>«в пятницу в 18:00 созвон»</i>\n"
        "Команда: /reminders — просмотр активных задач с кнопками управления.\n\n"
        "📝 <b>2. Быстрые заметки (Модуль 3)</b>\n"
        "Если во входящем сообщении нет времени, ИИ автоматически определит категорию:\n"
        "• <i>«купить переходник на Type-C»</i> ➔ Покупки\n"
        "• <i>«скачать zapret или hiddify»</i> ➔ Софт/VPN\n"
        "• <i>«тема для курсовой по квантам»</i> ➔ Учеба\n"
        "Команда: /notes — интерактивный каталог заметок.\n\n"
        "💼 <b>3. Бизнес-Автоответчик в Telegram</b>\n"
        "Работает через Telegram Business (@dp.business_message):\n"
        "• Автоматически отвечает на обычные и тех. вопросы\n"
        "• Распознает голосовые сообщения и скриншоты\n"
        "• Предупреждает о повышенном тоне собеседника\n"
        "• /sleep, /busy, /goodmorning, /stats\n"
        "• Префикс <code>~текст</code> для кавайной стилизации сообщений."
    )
    await message.answer(help_text)

# ==============================================================================
# ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ (Telegram Business)
# ==============================================================================

@dp.business_message()
async def handle_business_message(message: types.Message):
    sender_id = message.from_user.id
    chat_id = message.chat.id
    sender_name = message.from_user.first_name or "Пользователь"
    text = message.text or message.caption or ""

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
                InlineKeyboardButton(text="▶️ Включить автоответ в чате", callback_data=f"resume_{chat_id}")
            ]])
            await bot.send_message(
                MY_ID, 
                f"⏸️ <b>Автоответчик приостановлен</b> на 10 мин для чата с <code>{chat_id}</code>.",
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

    if status == "ignore":
        await mark_read()
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь временно не на связи.")
        return

    if status == "sleep":
        await mark_read()
        db.save_night_message(sender_name, text or "[Голосовое сообщение/Медиа]")
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь спит. Сообщение передам утром.")
        return

    if status == "busy":
        if "срочно" not in text.lower():
            await mark_read()
            await asyncio.sleep(2)
            await message.answer("[ИИ-Ассистент] Пользователь занят. Если дело срочное, напишите 'Срочно'.")
            return

    # Если чат на временной 10-минутной паузе после ручного ответа
    if was_paused and not message.voice:
        if message.photo or "срочно" in text.lower():
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Включить автоответ в чате", callback_data=f"resume_{chat_id}")
            ]])
            info_msg = f"📩 <b>Разбор медиа/срочного (Чат на паузе)</b> от {sender_name}:\nТекст: {html.escape(text)}"
            await bot.send_message(MY_ID, info_msg, reply_markup=kb)
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

    if photo_path and os.path.exists(photo_path):
        try:
            os.remove(photo_path)
        except Exception:
            pass
    if voice_path and os.path.exists(voice_path):
        try:
            os.remove(voice_path)
        except Exception:
            pass

    category = analysis.get("category", "formal")
    summary = analysis.get("summary", "")
    new_profile = analysis.get("user_profile")

    db.log_stat(sender_id, category)
    if new_profile:
        db.update_user_profile(sender_id, sender_name, new_profile)

    # 3. ОБРАБОТКА И ОТВЕТЫ
    if analysis.get("tone_warning"):
        await bot.send_message(
            MY_ID,
            f"⚠️ <b>Внимание: Повышенный тон!</b>\nОт: <code>{sender_name}</code>\nТекст: <i>{html.escape(text) or '[Голосовое/Медиа]'}</i>"
        )

    aggr_btn = InlineKeyboardButton(text="🟢 Выкл Агрессию", callback_data=f"unaggr_{sender_id}") if is_aggr else InlineKeyboardButton(text="🔥 Вкл Агрессию", callback_data=f"aggr_{sender_id}")

    kb_actions = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Заигнорить", callback_data=f"ban_{sender_id}"),
            InlineKeyboardButton(text="⏸️ На паузу", callback_data=f"pause_{chat_id}")
        ],
        [aggr_btn]
    ])

    if category == "personal":
        msg_out = f"📥 <b>Личное от {sender_name}</b> (<code>{sender_id}</code>):\n{html.escape(text) or '[Голосовое сообщение/Медиа]'}"
        if summary:
            msg_out += f"\n\n💡 <i>Контекст:</i> {html.escape(summary)}"
        await bot.send_message(MY_ID, msg_out, reply_markup=kb_actions)

    elif category in ["formal", "tech_vpn", "urgent"] or message.voice:
        await asyncio.sleep(2)
        reply_text = analysis.get("suggested_reply")
        
        if message.photo and category == "tech_vpn":
            report_msg = f"📸 <b>Разбор скриншота от {sender_name}:</b>\n💡 <b>ИИ определил:</b> {html.escape(summary)}"
            await bot.send_message(MY_ID, report_msg, reply_markup=kb_actions)
        else:
            if reply_text:
                await message.answer(reply_text)
                aggr_tag = " 🔥 [АГРЕССИВНЫЙ]" if is_aggr else ""
                report_msg = f"🤖 <b>ИИ ответил {sender_name}{aggr_tag}:</b>\n{html.escape(reply_text)}"
                if summary:
                    report_msg += f"\n💡 <b>Контекст/Расшифровка:</b> {html.escape(summary)}"
                await bot.send_message(MY_ID, report_msg, reply_markup=kb_actions)

# --- Инлайн-кнопки Бизнес-Ассистента ---

@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_blacklist(user_id)
    await callback.answer("Пользователь заблокирован!", show_alert=True)
    await callback.message.edit_text(f"🚫 Пользователь <code>{user_id}</code> заблокирован.")

@dp.callback_query(F.data.startswith("aggr_"))
async def callback_aggr(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_aggressive(user_id)
    await callback.answer("Агрессивный режим включен!", show_alert=True)
    await callback.message.edit_text(f"🔥 Агрессивный режим <b>включен</b> для пользователя <code>{user_id}</code>.")

@dp.callback_query(F.data.startswith("unaggr_"))
async def callback_unaggr(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.remove_from_aggressive(user_id)
    await callback.answer("Агрессивный режим отключен!", show_alert=True)
    await callback.message.edit_text(f"🟢 Агрессивный режим <b>отключен</b> для пользователя <code>{user_id}</code>.")

@dp.callback_query(F.data.startswith("resume_"))
async def callback_resume(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    user_last_manual_msg[chat_id] = 0
    await callback.answer("Автоответчик возобновлен для этого чата!", show_alert=True)
    await callback.message.edit_text(f"▶️ <b>Автоответчик снова активен</b> для чата <code>{chat_id}</code>.")

@dp.callback_query(F.data.startswith("pause_"))
async def callback_pause(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    user_last_manual_msg[chat_id] = time.time()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Включить автоответ в чате", callback_data=f"resume_{chat_id}")
    ]])
    await callback.answer("Чат поставлен на паузу на 10 минут!", show_alert=True)
    await callback.message.edit_text(f"⏸️ <b>Чат <code>{chat_id}</code> на паузе</b> на 10 минут.", reply_markup=kb)

# ==============================================================================
# МОДУЛЬ 3: УПРАВЛЕНИЕ НАПОМИНАНИЯМИ И ЗАМЕТКАМИ (ЛС БОТА)
# ==============================================================================

@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    user_id = message.from_user.id
    tz_offset = db.get_user_timezone(user_id)
    active = db.get_active_reminders(user_id)

    if not active:
        await message.answer(
            "⏰ <b>У вас нет активных напоминаний.</b>\n\n"
            "Чтобы создать, напишите боту в ЛС, например:\n"
            "• <i>«завтра в 14:00 скинуть отчет по физике»</i>\n"
            "• <i>«через 30 минут выключить духовку»</i>"
        )
        return

    text = f"⏰ <b>Ваши активные напоминания ({len(active)}):</b>\n\n"
    keyboard_rows = []

    for idx, rem in enumerate(active, 1):
        rem_id = rem["id"]
        rem_text = rem["text"]
        dt_local_str = rem["remind_at_local"]
        try:
            dt = datetime.fromisoformat(dt_local_str)
            time_display = dt.strftime("%d.%m в %H:%M")
        except Exception:
            time_display = dt_local_str[:16]

        text += f"<b>{idx}.</b> 📅 {time_display} — <b>{html.escape(rem_text)}</b>\n"
        
        row = [
            InlineKeyboardButton(text=f"✅ #{idx}", callback_data=f"done_rem_{rem_id}"),
            InlineKeyboardButton(text=f"⏰ +15м", callback_data=f"snooze_{rem_id}_15"),
            InlineKeyboardButton(text=f"🗑 #{idx}", callback_data=f"cancel_rem_{rem_id}")
        ]
        keyboard_rows.append(row)

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.answer(text, reply_markup=kb)

@dp.message(Command("notes"))
async def cmd_notes(message: types.Message):
    user_id = message.from_user.id
    counts = db.get_all_notes_count(user_id)
    total = sum(counts.values())

    categories_list = [
        ("учеба", "🎓 Учеба"),
        ("покупки", "🛒 Покупки"),
        ("софт/vpn", "💻 Софт / VPN"),
        ("идеи", "💡 Идеи"),
        ("другое", "📌 Другое")
    ]

    kb_rows = []
    text = f"📝 <b>Ваши сохраненные заметки (всего: {total}):</b>\nВыберите категорию для просмотра:\n\n"

    for cat_key, cat_title in categories_list:
        c = counts.get(cat_key, 0)
        text += f"• {cat_title}: <b>{c}</b>\n"
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{cat_title} ({c})",
                callback_data=f"view_cat_{cat_key}"
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer(text, reply_markup=kb)

@dp.message(Command("timezone"))
async def cmd_timezone(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) == 2 and (args[1].isdigit() or (args[1].startswith(("-", "+")) and args[1][1:].isdigit())):
        offset = int(args[1])
        db.set_user_timezone(user_id, offset)
        await message.answer(f"✅ Часовой пояс успешно сохранен: <b>UTC{'+' if offset >= 0 else ''}{offset}</b>")
        return

    tz = db.get_user_timezone(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="UTC+2 (Калининград)", callback_data="set_tz_2"),
            InlineKeyboardButton(text="UTC+3 (Москва)", callback_data="set_tz_3")
        ],
        [
            InlineKeyboardButton(text="UTC+4 (Самара)", callback_data="set_tz_4"),
            InlineKeyboardButton(text="UTC+5 (Екатеринбург)", callback_data="set_tz_5")
        ],
        [
            InlineKeyboardButton(text="UTC+6 (Омск)", callback_data="set_tz_6"),
            InlineKeyboardButton(text="UTC+7 (Новосибирск)", callback_data="set_tz_7")
        ]
    ])
    await message.answer(
        f"🌍 <b>Настройка часового пояса</b>\n\n"
        f"Текущий пояс: <b>UTC{'+' if tz >= 0 else ''}{tz}</b>\n\n"
        f"Выберите из списка ниже или введите команду, например: <code>/timezone 3</code>",
        reply_markup=kb
    )

@dp.message(Command("remind"))
async def cmd_remind_manual(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Формат команды:</b>\n"
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
            "⚠️ <b>Формат команды:</b>\n"
            "<code>/note покупки молоко, сыр, хлеб</code>\n"
            "<code>/note учеба решить 5 задач по матану</code>\n"
            "<code>/note просто любая мысль без категории</code>"
        )
        return
    
    if len(args) == 3 and args[1].lower() in ["учеба", "покупки", "софт/vpn", "софт", "vpn", "идеи", "другое"]:
        cat = args[1].lower()
        if cat in ["софт", "vpn"]:
            cat = "софт/vpn"
        content = args[2]
        note_id = db.add_note(message.from_user.id, cat, content)
        icon = get_cat_icon(cat)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📂 Открыть категорию", callback_data=f"view_cat_{cat}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_note_{note_id}")
        ]])
        await message.answer(f"📝 <b>Заметка сохранена!</b>\nКатегория: {icon} <b>{cat.capitalize()}</b>\n📌 {html.escape(content)}", reply_markup=kb)
    else:
        full_text = message.text.split(maxsplit=1)[1]
        await process_incoming_text(message, full_text)

# ==============================================================================
# ОБРАБОТКА ВХОДЯЩИХ СООБЩЕНИЙ В ЛС БОТА (ИИ-ПАРСИНГ НАПОМИНАНИЙ И ЗАМЕТОК)
# ==============================================================================

async def process_incoming_text(message: types.Message, raw_text: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    tz_offset = db.get_user_timezone(user_id)

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
        
        # Если время не определилось, ставим через 1 час по умолчанию
        if not remind_dt_iso:
            local_now = datetime.now(timezone(timedelta(hours=tz_offset)))
            target_dt = local_now + timedelta(hours=1)
        else:
            try:
                target_dt = datetime.fromisoformat(remind_dt_iso)
                if target_dt.tzinfo is None:
                    target_dt = target_dt.replace(tzinfo=timezone(timedelta(hours=tz_offset)))
            except Exception:
                local_now = datetime.now(timezone(timedelta(hours=tz_offset)))
                target_dt = local_now + timedelta(hours=1)

        dt_utc = target_dt.astimezone(timezone.utc)
        dt_local = target_dt.astimezone(timezone(timedelta(hours=tz_offset)))

        dt_utc_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        dt_local_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        time_display = dt_local.strftime("%d.%m в %H:%M")

        rem_id = db.add_reminder(
            user_id=user_id,
            chat_id=chat_id,
            text=title,
            remind_at_utc=dt_utc_str,
            remind_at_local=dt_local_str,
            source_info=source_info
        )

        source_block = f"\n<i>📌 Источник: {html.escape(source_info)}</i>" if source_info else ""
        text_out = (
            f"✅ <b>Напоминание установлено на {time_display}:</b>\n"
            f"📌 <b>{html.escape(title)}</b>"
            f"{source_block}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_rem_{rem_id}"),
                InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snooze_{rem_id}_15")
            ],
            [
                InlineKeyboardButton(text="📋 Все напоминания", callback_data="show_reminders_list")
            ]
        ])
        await message.answer(text_out, reply_markup=kb)

    # 2. ЗАМЕТКА (нет времени)
    elif intent == "note":
        raw_cat = res_data.get("category") or "Другое"
        cat = db.normalize_category(raw_cat)
        content = res_data.get("title") or raw_text
        icon = get_cat_icon(cat)

        note_id = db.add_note(user_id=user_id, category=cat, content=content)

        source_block = f"\n<i>📌 Источник: {html.escape(source_info)}</i>" if source_info else ""
        text_out = (
            f"📝 <b>Заметка сохранена в категорию [{icon} {cat.capitalize()}]</b>\n\n"
            f"📌 {html.escape(content)}"
            f"{source_block}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"📂 Открыть {icon} {cat.capitalize()}", callback_data=f"view_cat_{cat.lower()}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_note_{note_id}")
            ],
            [
                InlineKeyboardButton(text="📚 Все категории", callback_data="notes_menu")
            ]
        ])
        await message.answer(text_out, reply_markup=kb)

    # 3. ОБЫЧНЫЙ ЧАТ / ВОПРОС К ИИ
    else:
        reply = res_data.get("reply")
        if not reply:
            reply = "Я записал сообщение! Вы также можете отправить задачу с датой (например: «завтра в 14:00 отчет») или заметку."
        await message.answer(f"🤖 {html.escape(reply)}")

@dp.message(F.chat.type == "private")
async def handle_private_message(message: types.Message):
    """Прием любого текста или пересланного сообщения в ЛС бота"""
    # Пропускаем служебные команды
    if message.text and message.text.startswith("/"):
        return

    text = message.text or message.caption or ""
    if not text:
        if message.voice or message.photo:
            await message.answer("💡 Отправьте текст или перешлите сообщение с описанием задачи / заметки.")
        return

    await process_incoming_text(message, text)

# ==============================================================================
# ИНЛАЙН-ОБРАБОТЧИКИ МОДУЛЯ 3 (КНОПКИ НАПОМИНАНИЙ И ЗАМЕТОК)
# ==============================================================================

@dp.callback_query(F.data.startswith("cancel_rem_"))
async def cb_cancel_rem(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    db.delete_reminder(rem_id, callback.from_user.id)
    await callback.answer("Напоминание отменено!", show_alert=False)
    await callback.message.edit_text("❌ <b>Напоминание отменено и удалено.</b>")

@dp.callback_query(F.data.startswith("done_rem_"))
async def cb_done_rem(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[2])
    db.mark_reminder_completed(rem_id, callback.from_user.id)
    await callback.answer("Отлично! Напоминание выполнено.", show_alert=False)
    await callback.message.edit_text("✅ <b>Напоминание отмечено выполненным!</b>")

@dp.callback_query(F.data.startswith("snooze_"))
async def cb_snooze_rem(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    rem_id = int(parts[1])
    minutes = int(parts[2])
    
    new_dt_local = db.snooze_reminder(rem_id, callback.from_user.id, minutes)
    if new_dt_local:
        time_display = new_dt_local.strftime("%d.%m в %H:%M")
        await callback.answer(f"Отложено на {minutes} мин!", show_alert=False)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_rem_{rem_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cancel_rem_{rem_id}")
        ]])
        await callback.message.edit_text(
            f"⏰ <b>Напоминание перенесено на {time_display} (+{minutes} мин).</b>",
            reply_markup=kb
        )
    else:
        await callback.answer("Не удалось перенести напоминание.", show_alert=True)

@dp.callback_query(F.data == "show_reminders_list")
async def cb_show_reminders_list(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    active = db.get_active_reminders(user_id)
    if not active:
        await callback.answer("Активных напоминаний нет.", show_alert=True)
        return
    
    text = f"⏰ <b>Ваши активные напоминания ({len(active)}):</b>\n\n"
    keyboard_rows = []

    for idx, rem in enumerate(active, 1):
        rem_id = rem["id"]
        rem_text = rem["text"]
        dt_local_str = rem["remind_at_local"]
        try:
            dt = datetime.fromisoformat(dt_local_str)
            time_display = dt.strftime("%d.%m в %H:%M")
        except Exception:
            time_display = dt_local_str[:16]

        text += f"<b>{idx}.</b> 📅 {time_display} — <b>{html.escape(rem_text)}</b>\n"
        
        row = [
            InlineKeyboardButton(text=f"✅ #{idx}", callback_data=f"done_rem_{rem_id}"),
            InlineKeyboardButton(text=f"⏰ +15м", callback_data=f"snooze_{rem_id}_15"),
            InlineKeyboardButton(text=f"🗑 #{idx}", callback_data=f"cancel_rem_{rem_id}")
        ]
        keyboard_rows.append(row)

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "notes_menu")
async def cb_notes_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    counts = db.get_all_notes_count(user_id)
    total = sum(counts.values())

    categories_list = [
        ("учеба", "🎓 Учеба"),
        ("покупки", "🛒 Покупки"),
        ("софт/vpn", "💻 Софт / VPN"),
        ("идеи", "💡 Идеи"),
        ("другое", "📌 Другое")
    ]

    kb_rows = []
    text = f"📝 <b>Каталог заметок (всего: {total}):</b>\nВыберите категорию:\n\n"

    for cat_key, cat_title in categories_list:
        c = counts.get(cat_key, 0)
        text += f"• {cat_title}: <b>{c}</b>\n"
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{cat_title} ({c})",
                callback_data=f"view_cat_{cat_key}"
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("view_cat_"))
async def cb_view_cat(callback: types.CallbackQuery):
    cat_key = callback.data.replace("view_cat_", "").lower()
    user_id = callback.from_user.id
    notes = db.get_notes_by_category(user_id, cat_key)
    icon = get_cat_icon(cat_key)

    if not notes:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Назад в меню заметок", callback_data="notes_menu")
        ]])
        await callback.message.edit_text(
            f"📂 В категории <b>{icon} {cat_key.capitalize()}</b> пока нет заметок.",
            reply_markup=kb
        )
        return

    text = f"📂 <b>Категория: {icon} {cat_key.capitalize()} ({len(notes)}):</b>\n\n"
    kb_rows = []

    for idx, n in enumerate(notes, 1):
        note_id = n["id"]
        content = n["content"]
        created = n["created_at"][:10]
        text += f"<b>{idx}.</b> {html.escape(content)} <i>({created})</i>\n"
        kb_rows.append([
            InlineKeyboardButton(text=f"🗑 Удалить #{idx}", callback_data=f"del_note_{note_id}")
        ])

    kb_rows.append([
        InlineKeyboardButton(text="🧹 Очистить всю категорию", callback_data=f"clear_cat_{cat_key}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="notes_menu")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("del_note_"))
async def cb_del_note(callback: types.CallbackQuery):
    note_id = int(callback.data.split("_")[2])
    db.delete_note(note_id, callback.from_user.id)
    await callback.answer("Заметка удалена!", show_alert=False)
    await callback.message.edit_text("🗑 <b>Заметка успешно удалена.</b>")

@dp.callback_query(F.data.startswith("clear_cat_"))
async def cb_clear_cat(callback: types.CallbackQuery):
    cat_key = callback.data.replace("clear_cat_", "")
    db.clear_category(callback.from_user.id, cat_key)
    await callback.answer("Категория очищена!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ В меню заметок", callback_data="notes_menu")
    ]])
    await callback.message.edit_text(f"🧹 Все заметки в категории <b>{cat_key.capitalize()}</b> удалены.", reply_markup=kb)

@dp.callback_query(F.data == "open_tz_menu")
async def cb_open_tz_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    tz = db.get_user_timezone(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="UTC+2 (Калининград)", callback_data="set_tz_2"),
            InlineKeyboardButton(text="UTC+3 (Москва)", callback_data="set_tz_3")
        ],
        [
            InlineKeyboardButton(text="UTC+4 (Самара)", callback_data="set_tz_4"),
            InlineKeyboardButton(text="UTC+5 (Екатеринбург)", callback_data="set_tz_5")
        ],
        [
            InlineKeyboardButton(text="UTC+6 (Омск)", callback_data="set_tz_6"),
            InlineKeyboardButton(text="UTC+7 (Новосибирск)", callback_data="set_tz_7")
        ]
    ])
    await callback.message.edit_text(
        f"🌍 <b>Настройка часового пояса</b>\n\n"
        f"Текущий пояс: <b>UTC{'+' if tz >= 0 else ''}{tz}</b>\n"
        f"Выберите ваш часовой пояс:",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("set_tz_"))
async def cb_set_tz(callback: types.CallbackQuery):
    offset = int(callback.data.replace("set_tz_", ""))
    db.set_user_timezone(callback.from_user.id, offset)
    await callback.answer(f"Пояс изменен на UTC{'+' if offset >= 0 else ''}{offset}!", show_alert=True)
    await callback.message.edit_text(
        f"✅ <b>Часовой пояс установлен: UTC{'+' if offset >= 0 else ''}{offset}</b>\n\n"
        f"Все напоминания будут рассчитываться по этому времени."
    )

# ==============================================================================
# ТОЧКА ВХОДА (MAIN)
# ==============================================================================

async def main():
    # Инициализация всех таблиц базы данных (Бизнес-бот + Модуль 3)
    db.init_db()

    # Запуск встроенного веб-сервера для Render
    await start_web_server()

    # Запуск фонового воркера отправки напоминаний
    asyncio.create_task(reminder_worker(bot))

    # Регистрация меню команд бота в Telegram
    commands = [
        BotCommand(command="start", description="Главное меню и статус"),
        BotCommand(command="reminders", description="⏰ Активные напоминания"),
        BotCommand(command="notes", description="📝 Заметки по категориям"),
        BotCommand(command="timezone", description="🌍 Настройка часового пояса"),
        BotCommand(command="help", description="📖 Полный справочник"),
        BotCommand(command="default", description="💼 Обычный режим"),
        BotCommand(command="ignore_all", description="🚫 Тотальный игнор"),
        BotCommand(command="sleep", description="🌙 Режим сна"),
        BotCommand(command="busy", description="🎮 Режим «Занят»"),
        BotCommand(command="goodmorning", description="🌅 Утренний дайджест"),
        BotCommand(command="stats", description="📊 Статистика автоответов"),
        BotCommand(command="disable_chat", description="🛑 Выкл автоответ (/disable_chat ID)"),
        BotCommand(command="enable_chat", description="🟢 Вкл автоответ (/enable_chat ID)"),
        BotCommand(command="aggressive", description="🔥 Вкл агрессию (/aggressive ID)"),
        BotCommand(command="unaggressive", description="🟢 Выкл агрессию (/unaggressive ID)"),
        BotCommand(command="unban", description="✅ Разблокировать (/unban ID)")
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
