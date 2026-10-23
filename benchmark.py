import asyncio
import time
import os
import sys

# mock env vars so we can load the bot
os.environ["BOT_TOKEN"] = "123456789:AABBCCDDEEFF"
os.environ["MY_TELEGRAM_ID"] = "1"
os.environ["GEMINI_API_KEY"] = "mock"

# we must monkeypatch ai_brain to mock the generate_content so we can benchmark the delay.
import ai_brain
from unittest.mock import AsyncMock

async def mock_generate_content(*args, **kwargs):
    await asyncio.sleep(2.0) # simulate 2 seconds API delay
    class Response:
        text = "mock cute text"
    return Response()

ai_brain.ai_client = AsyncMock()
ai_brain.ai_client.aio.models.generate_content = mock_generate_content

import main

async def run_benchmark():
    class MockMessage:
        def __init__(self):
            self.from_user = type('User', (object,), {'id': 1, 'first_name': 'Test'})()
            self.chat = type('Chat', (object,), {'id': 1})()
            self.text = "~ hello"
            self.caption = ""
            self.message_id = 42
            self.business_connection_id = "test_conn"
            self.voice = None
            self.photo = None

    # Mock bot edit_message_text
    main.bot = AsyncMock()
    main.bot.edit_message_text = AsyncMock()
    main.bot.send_message = AsyncMock()
    main.MY_ID = 1

    msg = MockMessage()

    start = time.time()
    await main.handle_business_message(msg)
    end = time.time()

    elapsed = end - start
    print(f"Elapsed time: {elapsed:.2f} seconds")

asyncio.run(run_benchmark())
