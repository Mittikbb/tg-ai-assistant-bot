import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from aiohttp import web

import db
from ai_brain import analyze_message
from aiohttp import web

# Фейковый веб-сервер для удержания Render в активном состоянии
async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

load_dotenv()

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_ID = int(os.getenv("MY_TELEGRAM_ID", 0))

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --- Управление ботом из личного чата c ботом ---

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
        await message.answer("⚠️ Использование: <code>/unban ID_ПОЛЬЗОВАТЕЛЯ</code>\nПример: <code>/unban 123456789</code>")
        return
    
    target_id = int(args[1])
    db.remove_from_blacklist(target_id)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> удален из черного списка!")

# --- Обработка входящих сообщений из Telegram Business ---

@dp.business_message()
async def handle_business_message(message: types.Message):
    # Игнорируем собственные сообщения
    if message.from_user.id == MY_ID:
        return

    sender_id = message.from_user.id
    sender_name = message.from_user.first_name or "Пользователь"
    text = message.text or message.caption or ""
    status = db.get_status()

    # 1. Помечаем входящее сообщение как прочитанное (две галочки)
    try:
        await bot.read_business_message(
            business_connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as e:
        logging.warning(f"Не удалось пометить сообщение прочитанным: {e}")

    # 2. Персональный игнор
    if db.is_blacklisted(sender_id):
        return

    # 3. Режимы доступности
    if status == "ignore":
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь временно не на связи.")
        return

    if status == "sleep":
        db.save_night_message(sender_name, text)
        await asyncio.sleep(2)
        await message.answer("[ИИ-Ассистент] Пользователь спит. Сообщение передам утром.")
        return

    if status == "busy":
        if "срочно" not in text.lower():
            await asyncio.sleep(2)
            await message.answer("[ИИ-Ассистент] Пользователь занят. Если дело срочное, напишите 'Срочно'.")
            return

    # 4. Обработка фото / скриншотов
    photo_path = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_path = f"temp_{photo.file_id}.jpg"
        await bot.download_file(file_info.file_path, photo_path)

    # Анализ Gemini
    analysis = analyze_message(text, photo_path)

    if photo_path and os.path.exists(photo_path):
        os.remove(photo_path)

    # 5. Предупреждение о конфликте
    if analysis.get("tone_warning"):
        await bot.send_message(
            MY_ID,
            f"⚠️ <b>Внимание: Повышенный тон общения!</b>\nОт: <code>{sender_name}</code> (ID: <code>{sender_id}</code>)\nТекст: <i>{text}</i>"
        )

    # 6. Разделение логики
    category = analysis.get("category")
    summary = analysis.get("summary")

    if category == "personal":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Заигнорить", callback_data=f"ban_{sender_id}")
        ]])
        msg_out = f"📥 <b>Личное от {sender_name}</b> (<code>{sender_id}</code>):\n{text}"
        if summary:
            msg_out += f"\n\n💡 <i>Контекст:</i> {summary}"
        await bot.send_message(MY_ID, msg_out, reply_markup=kb)

    elif category in ["formal", "tech_vpn", "urgent"]:
        await asyncio.sleep(2)

        reply_text = analysis.get("suggested_reply")
        if reply_text:
            await message.answer(reply_text)

            # Отчёт владельцу в ЛС
            report_msg = f"🤖 <b>ИИ ответил {sender_name}:</b>\n{reply_text}"
            if summary:
                report_msg += f"\n💡 <b>Расшифровка:</b> {summary}"
            await bot.send_message(MY_ID, report_msg)

# --- Кнопка бана из отчета ---
@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    db.add_to_blacklist(user_id)
    await callback.answer("Пользователь заблокирован!", show_alert=True)
    await callback.message.edit_text(f"🚫 Пользователь <code>{user_id}</code> добавлен в черный список.")

from aiogram.types import BotCommand

async def main():
    await start_web_server()
    commands = [
        BotCommand(command="start", description="Главное меню и статус"),
        BotCommand(command="default", description="Обычный режим"),
        BotCommand(command="ignore_all", description="Тотальный игнор"),
        BotCommand(command="sleep", description="Режим сна"),
        BotCommand(command="busy", description="Режим Занят"),
        BotCommand(command="goodmorning", description="Утренний дайджест"),
        BotCommand(command="unban", description="Разблокировать по ID (/unban ID)")
    ]
    await bot.set_my_commands(commands)
    
    print("🚀 Telegram Business Bot запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())