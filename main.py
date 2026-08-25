import asyncio
import logging
import os
import time
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from dotenv import load_dotenv
from aiohttp import web

import db
from ai_brain import analyze_message

load_dotenv()

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_ID = int(os.getenv("MY_TELEGRAM_ID", 0))

# --- Логика автопаузы бота при твоих ответах ---
user_last_manual_msg = {}
PAUSE_TIMEOUT = 600  # 10 минут (в секундах)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --- Фейковый веб-сервер для Render и UptimeRobot ---
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

# --- Команды личного управления ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    status = db.get_status()
    await message.answer(
        f"👋 <b>Бизнес-Ассистент активен!</b>\n\n"
        f"Текущий режим: <code>{status}</code>\n\n"
        "Команды:\n"
        "/default — Обычный режим\n"
        "/ignore_all — Тотальный игнор\n"
        "/sleep — Режим сна\n"
        "/busy — Режим «Занят»\n"
        "/goodmorning — Утренний дайджест"
    )

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
                report += f"• <b>{name}</b>: {msg}\n"
        else:
            report = "🌅 Ночью никто не писал."
        await message.answer(report)

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != MY_ID:
        return
    
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Использование: <code>/unban ID_ПОЛЬЗОВАТЕЛЯ</code>")
        return
    
    target_id = int(args[1])
    db.remove_from_blacklist(target_id)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> удален из черного списка!")

# --- Обработка бизнес-сообщений ---

@dp.business_message()
async def handle_business_message(message: types.Message):
    sender_id = message.from_user.id
    chat_id = message.chat.id

    # 1. Если сообщение отправлено ТОБОЙ (пишешь сам вручную)
    if sender_id == MY_ID:
        user_last_manual_msg[chat_id] = time.time()
        logging.info(f"⏸️ Зафиксирован личный ответ в чате {chat_id}. Бот уходит на паузу на 10 мин.")
        return

    # 2. Проверяем автопаузу (не писал ли ты сам в последние 10 минут)
    last_msg_time = user_last_manual_msg.get(chat_id, 0)
    if time.time() - last_msg_time < PAUSE_TIMEOUT:
        logging.info(f"⏸️ Бот пропустил сообщение в {chat_id}: активна пауза 10 минут.")
        return

    sender_name = message.from_user.first_name or "Пользователь"
    text = message.text or message.caption or ""
    status = db.get_status()

    try:
        await bot.read_business_message(
            business_connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as e:
        logging.warning(f"Не удалось прочитать сообщение: {e}")

    if db.is_blacklisted(sender_id):
        return

    if status == "ignore":
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь временно не на связи.")
        return

    if status == "sleep":
        db.save_night_message(sender_name, text or "[Голосовое сообщение/Медиа]")
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь спит. Сообщение передам утром.")
        return

    if status == "busy":
        if "срочно" not in text.lower():
            await asyncio.sleep(2)
            await message.answer("[ИИ-Ассистент] Пользователь занят. Если дело срочное, напишите 'Срочно'.")
            return

    # Скачивание фото (если есть)
    photo_path = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_path = f"temp_{photo.file_id}.jpg"
        await bot.download_file(file_info.file_path, photo_path)

    # Скачивание голосового сообщения (если есть)
    voice_path = None
    if message.voice:
        file_info = await bot.get_file(message.voice.file_id)
        voice_path = f"temp_{message.voice.file_id}.ogg"
        await bot.download_file(file_info.file_path, voice_path)

    # Передаём тексты и файлы в ИИ
    analysis = analyze_message(text, photo_path, voice_path)

    # Удаляем временные файлы
    if photo_path and os.path.exists(photo_path):
        os.remove(photo_path)
    if voice_path and os.path.exists(voice_path):
        os.remove(voice_path)

    if analysis.get("tone_warning"):
        await bot.send_message(
            MY_ID,
            f"⚠️ <b>Внимание: Повышенный тон!</b>\nОт: <code>{sender_name}</code>\nТекст: <i>{text or '[Голосовое]'}</i>"
        )

    category = analysis.get("category")
    summary = analysis.get("summary")

    if category == "personal":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Заигнорить", callback_data=f"ban_{sender_id}")
        ]])
        msg_out = f"📥 <b>Личное от {sender_name}</b> (<code>{sender_id}</code>):\n{text or '[Голосовое сообщение]'}"
        if summary:
            msg_out += f"\n\n💡 <i>Контекст:</i> {summary}"
        await bot.send_message(MY_ID, msg_out, reply_markup=kb)

    elif category in ["formal", "tech_vpn", "urgent"]:
        await asyncio.sleep(2)
        reply_text = analysis.get("suggested_reply")
        if reply_text:
            await message.answer(reply_text)
            report_msg = f"🤖 <b>ИИ ответил {sender_name}:</b>\n{reply_text}"
            if summary:
                report_msg += f"\n💡 <b>Контекст:</b> {summary}"
            await bot.send_message(MY_ID, report_msg)

@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_blacklist(user_id)
    await callback.answer("Пользователь заблокирован!", show_alert=True)
    await callback.message.edit_text(f"🚫 Пользователь <code>{user_id}</code> заблокирован.")

# --- Точка входа ---

async def main():
    await start_web_server()

    commands = [
        BotCommand(command="start", description="Главное меню и статус"),
        BotCommand(command="default", description="Обычный режим"),
        BotCommand(command="ignore_all", description="Тотальный игнор"),
        BotCommand(command="sleep", description="Режим сна"),
        BotCommand(command="busy", description="Режим Занят"),
        BotCommand(command="goodmorning", description="Утренняя сводка"),
        BotCommand(command="unban", description="Разблокировать (/unban ID)")
    ]
    await bot.set_my_commands(commands)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 Бизнес-бот запущен на Render!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())