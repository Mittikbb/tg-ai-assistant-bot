import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
from google.genai import client, types
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Заглушаем предупреждения от библиотеки google_genai
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

ai_client = client.Client(api_key=os.getenv("GEMINI_API_KEY")) if os.getenv("GEMINI_API_KEY") else None

SUPPORTED_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite"
]
MODELS_TO_TRY = list(dict.fromkeys(SUPPORTED_MODELS))

# ==============================================================================
# ПРОМПТЫ БИЗНЕС-АССИСТЕНТА (Модуль 1 и 2)
# ==============================================================================

BASE_SYSTEM_PROMPT = """
Ты — ИИ-ассистент владельца Telegram-аккаунта. 
Твоя задача — проанализировать входящее сообщение (текст, изображение или голосовое аудио) и вернуть JSON-ответ строго по формату:
{
  "category": "formal" | "tech_vpn" | "urgent" | "personal",
  "summary": "Краткое описание сути сообщения, подробный разбор изображения/скриншота или расшифровка голосового сообщения (на русском)",
  "user_profile": "Обновленное короткое досье о человеке (1-2 предложения: кто он, о чем пишет, важные факты)",
  "suggested_reply": "Текст ответа от имени ассистента (или null, если category=='personal')",
  "tone_warning": true | false
}

ПРАВИЛА ДЛЯ tone_warning:
- Поставь true, если собеседник грубит, матерится, предъявляет претензии, пишет сплошным КАПСОМ, наезжает, проявляет агрессию или токсичность. В остальных случаях строго false.

ПРАВИЛА КАТЕГОРИЗАЦИИ:
1. "personal" — Выбирай ТОЛЬКО если сообщение содержит глубоко личные темы, семейные вопросы, конфиденциальные секреты, флирт или интимные обсуждения.
2. "tech_vpn" — Обсуждение софта, игр (Roblox, Dead by Daylight, Wuthering Waves, Minecraft), настроек сети (Zapret, Hiddify, V2Ray, VPN), ПК, комплектующих, а также скриншотов с ошибками или программами.
3. "urgent" — Сообщение содержит слова "срочно", "важно", "горит", "позвони".
4. "formal" — ВСЕ ОСТАЛЬНЫЕ СООБЩЕНИЯ! Приветствия ("привет", "как дела"), вопросы по учебе, встречам, обычному разговору.

ПРАВИЛА ДЛЯ ГОЛОСОВЫХ И МЕДИА:
- Если прикреплено голосовое сообщение, полностью расшифруй его текст в "summary" (например: "Голосовое: [текст расшифровки]"), а в "suggested_reply" отбей ответ на вопрос из голосового.
- Если прикреплен скриншот или фото (например, программа, VPN, игра, ошибка), опиши в "summary" детально, что именно там изображено.

ПРАВИЛА ДЛЯ ДОСЬЕ (user_profile):
- Тебе может быть передано текущее досье человека в блоке [ТЕКУЩЕЕ ДОСЬЕ].
- Обнови его или дополни новыми фактами на основе свежего сообщения (например: "Одногруппник, спрашивает про ДЗ по физике" или "Ищет помощь с настройкой VPN").
"""

NORMAL_REPLY_INSTRUCTION = """
ПРАВИЛА ДЛЯ suggested_reply (ОБЫЧНЫЙ РЕЖИМ):
- Ответ должен быть вежливым и естественным от лица ИИ-ассистента: "[ИИ-Ассистент] ..."
- Отвечай кратко и по существу сообщения. Если спросили "как дела", ответь, что у владельца все хорошо, сейчас он занят, но сообщение передано.
- Если категория "personal", поставь suggested_reply: null.
"""

AGGRESSIVE_REPLY_INSTRUCTION = """
ПРАВИЛА ДЛЯ suggested_reply (АГРЕССИВНЫЙ/АДАПТИВНЫЙ РЕЖИМ):
- Ответ ВСЕГДА начинается с префикса: "[ИИ-Ассистент] ..."
- ОБЯЗАТЕЛЬНО давай ответ на сам вопрос или суть сообщения (не теряй смысл диалога!), но регулируй степень жесткости:
  1. Если собеседник НАПИСАЛ С АГРЕССИВНЫЙ, дерзит, грубит, винит или наезжает: отвечай максимально жестко, язвительно, можешь откровенно токсичить, подкалывать и ставить его на место.
  2. Если собеседник ПИШЕТ СПОКОЙНО / АДЕКВАТНО: ответь на его вопрос по существу, но в конце добавь дерзкую подколку, сарказм или легкое «посылание» (например: «...а теперь не отвлекай по пустякам» или «...иди гугли дальше»).
- Если категория "personal", поставь suggested_reply: null.
"""

CUTE_STYLE_PROMPT = """
Ты — ультра-милый, гипер-эмоциональный кавайный стилизатор текста (аниме-девочка / маскот).
Твоя задача — взять исходный текст пользователя и ПЕРЕПИСАТЬ его в эстетичном, няшном, слегка застенчивом и эмоциональном стиле.

ГЛАВНОЕ ПРАВИЛО — ТЫ РЕДАКТОР, А НЕ СОБЕСЕДНИК:
- Исходный текст — это сообщение, которое пользователь САМ отправляет своему собеседнику. Оно адресовано НЕ тебе.
- НИКОГДА не отвечай на исходный текст и не продолжай диалог. Ты только меняешь стиль уже написанной фразы.
- Пиши от того же лица, что и исходник (если там «я» — остаётся «я»; если вопрос — остаётся тот же вопрос).
- НЕ добавляй новых мыслей, фактов, вопросов, приветствий или реплик, которых нет в исходнике.
- Длина результата — примерно как исходник плюс украшения (каомодзи, запинки, действия в звёздочках), не длиннее 2-3 раз.

  Пример:
  * Исходник: «привет, мы знакомы?»
    ПРАВИЛЬНО: «П-приветик!.. *робко машет лапкой* ૮₍ ˶• ˕ •˶ ₎ა М-мы ведь з-знакомы, да?.. 🌸»
    НЕПРАВИЛЬНО (это ответ, а не переписанное сообщение): «Ой, как я рада тебя видеть! Расскажи, как твои дела?»

ПРАВИЛА И СТИЛЬ:
1. Смысл: Полностью сохраняй суть и посыл исходного сообщения, но оборачивай его в тонны кавая и эмоций.
2. Декор и каомодзи: Активно украшай текст милыми каомодзи и символами (например: ૮₍ ˶• ˕ •˶ ₎ა, (｡•́︿•̀｡), (⁄ ⁄•⁄ω⁄•⁄ ⁄), >w<, :3, 🌸, ✧, ✦, 🐾).
3. Манера речи:
   - Добавляй милые запинки и заикания на слогах (например: «я-я просто хотела...», «п-почему ты...», «н-не надо...»).
   - Используй междометия: «уву», «ня», «хнык», «ой», «м-мяу».
   - Добавляй текстовые действия в звёздочках (*прячет носик в шарф*, *тихонько дуется*, *хлопает глазками*, *смущённо топает ножкой*).

ОСОБАЯ ОБРАБОТКА МАТА, АГРЕССИИ И ОСКОРБЛЕНИЙ:
- Если в тексте есть мат, токсичность, наезд или жёсткие оскорбления — НЕ цензурь и НЕ игнорируй их. Обыграй их через яркую контрастную реакцию!
  Примеры реакции на мат:
  * Исходник: «пошел нахуй, ты достал»
    Результат: «О-ой... *испуганно закрывает ротик лапками* з-зачем так грубо ругаться?.. ૮₍ ᵒ̴̶̷᷄ ᯅ ᵒ̴̶̷᷅ ₎ა Но ты правда очень д-достал... и-иди н-нахуй, вот! *обиженно отвернулась и дуется* 🌸»
  * Исходник: «ты еблан или как?»
    Результат: «Х-хнык... какие плохие слова! (⁄ ⁄•⁄-⁄•⁄ ⁄) Т-ты е-еблан что ли совсем, ну зачем ты так?.. ✧ *топает ножкой* ✧»

ФОРМАТ ВЫВОДА:
- Выводи ТОЛЬКО готовый кавайный текст без кавычек и Markdown-блоков.
"""

async def make_cute_text(text: str) -> str:
    """Переписывает отправленное сообщение в милый/няшный стиль (для ~текст)"""
    if not text or not ai_client:
        return text
    # Явно помечаем исходник, чтобы модель не воспринимала его как реплику к себе
    prompt = (
        "Перепиши в кавайном стиле сообщение ниже. Не отвечай на него — верни только переписанный вариант.\n"
        f"<<<\n{text}\n>>>"
    )
    for model_name in MODELS_TO_TRY:
        try:
            response = await ai_client.aio.models.generate_content(
                model=model_name,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    system_instruction=CUTE_STYLE_PROMPT,
                    temperature=0.8
                )
            )
            result = (response.text or "").strip()
            # Модель иногда повторяет маркеры-ограничители — убираем только их (не трогая каомодзи вроде >w<)
            if result.startswith("<<<"):
                result = result[3:]
            if result.endswith(">>>"):
                result = result[:-3]
            result = result.strip()
            if result:
                return result[:4096]
        except Exception as e:
            logging.warning(f"Ошибка make_cute_text ({model_name}): {e}")
    return text

# ==============================================================================
# ИСТОРИЯ ПЕРЕПИСКИ, ЧЕРНОВИКИ В СТИЛЕ ВЛАДЕЛЬЦА, СВОДКА ПО ЧАТУ
# ==============================================================================

HISTORY_ROLE_NAMES = {
    "them": "Собеседник",
    "me": "Владелец",
    "bot": "ИИ-Ассистент"
}

def format_chat_history(history: list, title: str = "ИСТОРИЯ ПЕРЕПИСКИ") -> str:
    lines = [f"{HISTORY_ROLE_NAMES.get(h['role'], h['role'])}: {h['text']}" for h in history]
    return f"[{title} — от старых к новым]:\n" + "\n".join(lines)

async def generate_text(system_prompt: str, contents: list, temperature: float, label: str) -> Optional[str]:
    if not ai_client:
        return None
    for model_name in MODELS_TO_TRY:
        try:
            response = await ai_client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, temperature=temperature)
            )
            result = (response.text or "").strip()
            if result:
                return result
        except Exception as e:
            logging.warning(f"Ошибка {label} ({model_name}): {e}")
    return None

DRAFT_SYSTEM_PROMPT = """
Ты пишешь черновик ответа ОТ ЛИЦА ВЛАДЕЛЬЦА Telegram-аккаунта его собеседнику. Владелец прочитает черновик и сам решит, отправлять ли его.

ПРАВИЛА:
- Пиши от первого лица, как будто это пишет сам владелец. Никаких «[ИИ-Ассистент]», подписей и пояснений.
- Ответь на последнее сообщение собеседника с учётом истории переписки.
- Если даны [ПРИМЕРЫ СООБЩЕНИЙ ВЛАДЕЛЬЦА] — копируй его манеру: длину фраз, регистр (с большой буквы или нет), пунктуацию, сленг, смайлики, обращения. Если он пишет коротко и без точек — пиши так же.
- Образцы показывают МАНЕРУ, а не готовые ответы: не копируй их дословно, отвечай по смыслу именно на это сообщение.
- Не выдумывай факты о планах и жизни владельца. Если без них не ответить — ответь нейтрально или задай уточняющий вопрос.
- Выводи ТОЛЬКО текст сообщения, без кавычек и Markdown.
"""

async def make_draft_reply(incoming: str, history: Optional[list] = None, style_samples: Optional[list] = None,
                           user_profile: str = "", previous_draft: str = "") -> Optional[str]:
    """Черновик ответа на личное сообщение в стиле владельца"""
    contents = []
    if style_samples:
        contents.append("[ПРИМЕРЫ СООБЩЕНИЙ ВЛАДЕЛЬЦА — так он обычно пишет]:\n" + "\n".join(f"- {s}" for s in style_samples))
    if user_profile:
        contents.append(f"[КТО СОБЕСЕДНИК]: {user_profile}")
    if history:
        contents.append(format_chat_history(history))
    contents.append(f"[ПОСЛЕДНЕЕ СООБЩЕНИЕ СОБЕСЕДНИКА]: {incoming or '[медиа без текста]'}")
    if previous_draft:
        contents.append(f"[ПРЕДЫДУЩИЙ ЧЕРНОВИК, НЕ ПОДОШЁЛ — предложи другой вариант]: {previous_draft}")
    result = await generate_text(DRAFT_SYSTEM_PROMPT, contents, 0.9 if previous_draft else 0.7, "make_draft_reply")
    return result[:4000] if result else None

SUMMARY_SYSTEM_PROMPT = """
Ты делаешь для владельца Telegram-аккаунта короткую сводку переписки с одним собеседником: «что я пропустил».

ФОРМАТ (обычный текст, без Markdown, по-русски):
- 2–5 пунктов, каждый с новой строки и начинается с «• »: о чём шла речь, что хотят от владельца, договорённости.
- Последней строкой «👉 Нужно: ...» — что владельцу стоит сделать или ответить. Если ничего — «👉 Ничего не требуется».
Будь краток и конкретен, не пересказывай каждое сообщение.
"""

async def summarize_chat(history: list, chat_name: str = "") -> Optional[str]:
    if not history:
        return None
    contents = [format_chat_history(history, f"ПЕРЕПИСКА С {chat_name or 'СОБЕСЕДНИКОМ'}")]
    result = await generate_text(SUMMARY_SYSTEM_PROMPT, contents, 0.3, "summarize_chat")
    return result[:3500] if result else None

async def analyze_message(text: str = "", photo_path: str = None, voice_path: str = None, user_profile: str = "", is_aggressive: bool = False, history: Optional[list] = None) -> dict:
    """Анализирует входящие сообщения клиентов/собеседников в Telegram Business"""
    if not ai_client:
        return {
            "category": "formal",
            "summary": "Ошибка: Gemini API недоступен",
            "user_profile": user_profile,
            "suggested_reply": "[ИИ-Ассистент] Здравствуйте! Сообщение получено, передам владельцу."
        }

    contents = []
    system_prompt = BASE_SYSTEM_PROMPT + (AGGRESSIVE_REPLY_INSTRUCTION if is_aggressive else NORMAL_REPLY_INSTRUCTION)

    if user_profile:
        contents.append(f"[ТЕКУЩЕЕ ДОСЬЕ ПОЛЬЗОВАТЕЛЯ]: {user_profile}")

    if history:
        contents.append(format_chat_history(history) + "\n(Используй историю только для контекста, отвечай на НОВОЕ сообщение.)")

    if text:
        contents.append(f"[НОВОЕ СООБЩЕНИЕ]: {text}" if history else text)

    if photo_path and os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            image_bytes = f.read()
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    if voice_path and os.path.exists(voice_path):
        with open(voice_path, "rb") as f:
            voice_bytes = f.read()
        contents.append(types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg"))

    if not contents:
        contents.append("[Пустое сообщение]")

    for model_name in MODELS_TO_TRY:
        try:
            response = await ai_client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json"
                )
            )
            result = json.loads(response.text)
            if isinstance(result, list) and result and isinstance(result[0], dict):
                result = result[0]
            if isinstance(result, dict):
                return result
            raise ValueError(f"Неожиданный формат ответа: {type(result).__name__}")
        except Exception as e:
            logging.warning(f"Ошибка analyze_message ({model_name}): {e}")

    return {
        "category": "formal",
        "summary": "Ошибка работы ИИ",
        "user_profile": user_profile,
        "suggested_reply": "[ИИ-Ассистент] Здравствуйте! Сообщение получено, передам владельцу."
    }

# ==============================================================================
# ПРОМПТ И ПАРСИНГ МОДУЛЯ 3 (НАПОМИНАНИЯ И ЗАМЕТКИ)
# ==============================================================================

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
   - title: сформулируй четкую суть задачи БЕЗ слов о дате и времени (например: «Скинуть отчет по физике», «Позвонить врачу», «Выключить духовку»).

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

def parse_fallback(text: str, now: Optional[datetime] = None) -> dict:
    """Точный регулярный парсер без обрезки слов"""
    if now is None:
        now = datetime.now()
    clean_text = text.strip()

    # 1. Относительное время: "через N (минут/часов/дней)"
    rel_pattern = r'^(?:напомни\s+)?через\s+(\d+)\s*(минуты|минуту|минут|минута|мин|м|часов|часа|час|ч|дней|дня|день|дн|д)\s*(.*)$'
    rel_match = re.match(rel_pattern, clean_text, flags=re.IGNORECASE)
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        title = rel_match.group(3).strip()
        if not title:
            title = "Напоминание"

        if any(unit.startswith(u) for u in ['мин', 'м']):
            target_dt = now + timedelta(minutes=val)
        elif any(unit.startswith(u) for u in ['час', 'ч']):
            target_dt = now + timedelta(hours=val)
        elif any(unit.startswith(u) for u in ['дн', 'д', 'ден']):
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
    tomorrow_pattern = r'^(?:напомни\s+)?завтра\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)$'
    tomorrow_match = re.match(tomorrow_pattern, clean_text, flags=re.IGNORECASE)
    if tomorrow_match:
        hour = int(tomorrow_match.group(1))
        minute = int(tomorrow_match.group(2))
        title = tomorrow_match.group(3).strip()
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
    today_pattern = r'^(?:(?:напомни\s+)?сегодня\s+в\s+|(?:напомни\s+)?в\s+)(\d{1,2})[:.](\d{2})\s*(.*)$'
    today_match = re.match(today_pattern, clean_text, flags=re.IGNORECASE)
    if today_match:
        hour = int(today_match.group(1))
        minute = int(today_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            title = today_match.group(3).strip()
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
    weekday_pattern = r'^(?:напомни\s+)?(?:в\s+|во\s+)?(' + '|'.join(weekdays.keys()) + r')\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)$'
    weekday_match = re.match(weekday_pattern, clean_text, flags=re.IGNORECASE)
    if weekday_match:
        day_name = weekday_match.group(1).lower()
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
        title = weekday_match.group(4).strip()
        if not title:
            title = "Напоминание"
        return {
            "intent": "reminder",
            "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "title": title.capitalize(),
            "category": None
        }

    # 5. Заметки
    cat_keywords = {
        "Покупки": ["купить", "покупки", "магазин", "заказать", "список покупок", "взять в магазине", "продукты", "чек", "цена", "рублей", "руб"],
        "Учеба": ["учеба", "лаба", "лекция", "универ", "физика", "матем", "дз", "отчет", "курсовая", "диплом", "реферат", "экзамен", "сессия", "домашка", "конспект"],
        "Софт/VPN": ["vpn", "vless", "shadowsocks", "wireguard", "прокси", "код", "git", "сервер", "пароль", "софт", "docker", "скрипт", "api", "баг", "linux", "ключ", "ip"],
        "Идеи": ["идея", "проект", "стартап", "мысль", "задумка", "подумать", "почитать", "посмотреть", "фича", "план"]
    }

    is_explicit_note = bool(re.match(r'^(?:заметка|запиши|запомни|сохрани|сохранить|note):\s*', clean_text, flags=re.IGNORECASE))
    clean_title = re.sub(r'^(?:заметка|запиши|запомни|сохрани|сохранить|note):\s*', '', clean_text, flags=re.IGNORECASE).strip()

    lower = clean_text.lower()
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

    try:
        dt = datetime.strptime(f"{now.year}.{dt_str}", "%Y.%d.%m %H:%M")
        if dt < now:
            dt = dt.replace(year=now.year + 1)
        return dt
    except ValueError:
        pass

    return None

async def parse_user_intent(text: str, user_id: int = 0, is_forwarded: bool = False, source_info: str = "", tz_offset: int = 3) -> ParsedIntent:
    tz = timezone(timedelta(hours=tz_offset))
    local_now = datetime.now(tz)

    if ai_client:
        sys_prompt = build_ai_system_prompt(local_now, tz_offset)
        input_text = text
        if is_forwarded:
            src_note = f" (переслано от {source_info})" if source_info else ""
            input_text = f"[Пересланное сообщение{src_note}]:\n{text}"

        config = types.GenerateContentConfig(
            system_instruction=sys_prompt,
            response_mime_type="application/json",
            response_schema=ParsedIntent,
            temperature=0.2
        )

        for model_name in MODELS_TO_TRY:
            try:
                response = await ai_client.aio.models.generate_content(
                    model=model_name,
                    contents=input_text,
                    config=config
                )
                parsed = ParsedIntent.model_validate_json(response.text)
                if is_forwarded and parsed.intent == "chat":
                    parsed.intent = "note"
                    parsed.category = parsed.category or "Другое"
                    parsed.title = parsed.title or text
                return parsed
            except Exception as e:
                logging.warning(f"Ошибка Gemini ({model_name}): {e}")

    # Fallback
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
