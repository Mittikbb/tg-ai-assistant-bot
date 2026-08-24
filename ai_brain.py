import os
import json
from google.genai import client, types
from dotenv import load_dotenv

load_dotenv()

ai_client = client.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """
Ты — вежливый ИИ-ассистент владельца Telegram-аккаунта. 
Твоя задача — проанализировать входящее сообщение и вернуть JSON-ответ строго по формату:
{
  "category": "formal" | "tech_vpn" | "urgent" | "personal",
  "summary": "Краткое описание сути сообщения (на русском)",
  "suggested_reply": "Текст ответа от имени ассистента (или null, если category=='personal')"
}

ПРАВИЛА КАТЕГОРИЗАЦИИ:
1. "personal" — Выбирай ТОЛЬКО если сообщение содержит глубоко личные темы, семейные вопросы, конфиденциальные секреты, флирт или интимные обсуждения.
2. "tech_vpn" — Обсуждение софта, игр (Roblox, Dead by Daylight, Wuthering Waves, Minecraft), настроек сети (Zapret, Hiddify, V2Ray, VPN), ПК, комплектующих и ошибок.
3. "urgent" — Сообщение содержит слова "срочно", "важно", "горит", "позвони".
4. "formal" — ВСЕ ОСТАЛЬНЫЕ СООБЩЕНИЯ! Приветствия ("привет", "как дела"), вопросы по учебе, встречам, обычному разговору.

ПРАВИЛА ДЛЯ suggested_reply:
- Ответ должен быть вежливым и естественным от лица ИИ-ассистента: "[ИИ-Ассистент] ..."
- Отвечай кратко и по существу сообщения. Если спросили "как дела", ответь, что у владельца все хорошо, сейчас он занят, но сообщение передано.
- Если категория "personal", поставь suggested_reply: null.
"""

def analyze_message(text: str, photo_path: str = None) -> dict:
    contents = [text]
    
    if photo_path:
        with open(photo_path, "rb") as f:
            image_bytes = f.read()
        contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

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
            "suggested_reply": "[ИИ-Ассистент] Здравствуйте! Сообщение получено, передам владельцу."
        }