import asyncio
import time
from aiogram.types import Message, User, Chat
from unittest.mock import AsyncMock, patch

async def run_benchmark():
    # Import the handler
    from main import handle_business_message, MY_ID, bot

    # Mock message
    msg = AsyncMock(spec=Message)
    msg.text = "~Hello world"
    msg.caption = None
    msg.from_user = User(id=MY_ID, is_bot=False, first_name="Test")
    msg.chat = Chat(id=123, type="private", full_name="John")
    msg.sender_business_bot = None
    msg.business_connection_id = "conn123"
    msg.message_id = 456

    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    start_time = time.time()
    with patch("main.make_cute_text", new_callable=AsyncMock) as mock_make_cute_text:
        # Simulate network delay of 2 seconds
        async def delayed_cute(*args):
            await asyncio.sleep(2)
            return "cute text"
        mock_make_cute_text.side_effect = delayed_cute

        await handle_business_message(msg)

    duration = time.time() - start_time
    print(f"Handler execution time: {duration:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
