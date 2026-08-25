import os
import json
from google.genai import client, types
from dotenv import load_dotenv

load_dotenv()

ai_client = client.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """
Ты — вежливый ИИ-ассистент владельца Telegram-аккаунта. 
Твоя задача — проанализировать входящее сообщение (текст, изображение или голосовое аудио) и вернуть JSON-ответ строго по формату:
{
  "category": "formal" | "tech_vpn" | "urgent" | "personal",
  "summary": "Краткое описание сути сообщения, подробный разбор изображения/скриншота или расшифровка голосового сообщения (на русском)",
  "user_profile": "Обновленное короткое досье о человеке (1-2 предложения: кто он, о чем пишет, важные факты)",
  "suggested_reply": "Текст ответа от имени ассистента (или null, если category=='personal')"
}

ПРАВИЛА КАТЕГОРИЗАЦИИ:
1. "personal" — Выбирай ТОЛЬКО если сообщение содержит глубоко личные темы, семейные вопросы, конфиденциальные секреты, флирт или интимные обсуждения.
2. "tech_vpn" — Обсуждение софта, игр (Roblox, Dead by Daylight, Wuthering Waves, Minecraft), настроек сети (Zapret, Hiddify, V2Ray, VPN), ПК, комплектующих, а также скриншотов с ошибками или программами.
3. "urgent" — Сообщение содержит слова "срочно", "важно", "горит", "позвони".
4. "formal" — ВСЕ ОСТАЛЬНЫЕ СООБЩЕНИЯ! Приветствия ("привет", "как дела"), вопросы по учебе, встречам, обычному разговору.

ПРАВИЛА ДЛЯ ГОЛОСОВЫХ И МЕДИА:
- Если прикреплено голосовое сообщение, полностью расшифруй его текст в "summary" (например: "Голосовое: [текст расшифровки]"), а в "suggested_reply" отбей краткий и вежливый ответ на вопрос из голосового.
- Если прикреплен скриншот или фото (например, программа, VPN, игра, ошибка), опиши в "summary" детально, что именно там изображено.

ПРАВИЛА ДЛЯ ДОСЬЕ (user_profile):
- Тебе может быть передано текущее досье человека в блоке [ТЕКУЩЕЕ ДОСЬЕ].
- Обнови его или дополни новыми фактами на основе свежего сообщения (например: "Одногруппник, спрашивает про ДЗ по физике" или "Ищет помощь с настройкой VPN").

ПРАВИЛА ДЛЯ suggested_reply:
- Ответ должен быть вежливым и естественным от лица ИИ-ассистента: "[ИИ-Ассистент] ..."
- Отвечай кратко и по существу сообщения. Если спросили "как дела", ответь, что у владельца все хорошо, сейчас он занят, но сообщение передано.
- Если категория "personal", поставь suggested_reply: null.
"""

def analyze_message(text: str = "", photo_path: str = None, voice_path: str = None, user_profile: str = "") -> dict:
    contents = []
    
    # Если есть старое досье, передаем его контекстом в Gemini
    if user_profile:
        contents.append(f"[ТЕКУЩЕЕ ДОСЬЕ ПОЛЬЗОВАТЕЛЯ]: {user_profile}")

    if text:
        contents.append(text)
    
    # Добавляем изображение, если передано
    if photo_path and os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            image_bytes = f.read()
        contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

    # Добавляем голосовое сообщение, если передано
    if voice_path and os.path.exists(voice_path):
        with open(voice_path, "rb") as f:
            voice_bytes = f.read()
        contents.append(
            types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
        )

    # Если вообще ничего не передано
    if not contents:
        contents.append("[Пустое сообщение]")

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Ошибка Gemini API: {e}")
        return {
            "category": "formal",
            "summary": "Ошибка работы ИИ",
            "user_profile": user_profile,
            "suggested_reply": "[ИИ-Ассистент] Здравствуйте! Сообщение получено, передам владельцу."
        }