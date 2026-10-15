import asyncio
import time
from aiogram import Bot
from unittest.mock import AsyncMock

async def main():
    import main as bot_main
    bot_main.bot = AsyncMock()
    bot_main.bot.edit_message_text = AsyncMock()

    # Create mock message
    msg = AsyncMock()
    msg.text = "~hello"
    msg.chat.id = 123
    msg.from_user.id = bot_main.MY_ID
    msg.message_id = 456
    msg.business_connection_id = "abc"

    # Before we do anything, let's mock make_cute_text to simulate network delay
    import ai_brain
    async def mock_make_cute_text(text):
        await asyncio.sleep(0.5)
        return "cute " + text
    ai_brain.make_cute_text = mock_make_cute_text

    bot_main.make_cute_text = mock_make_cute_text

    start = time.time()
    await bot_main.handle_business_message(msg)
    end = time.time()

    print(f"Time taken: {end - start:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
