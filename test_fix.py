import asyncio
from unittest.mock import AsyncMock, patch
import time

async def main():
    # Setup global background_tasks
    import main as m
    m.background_tasks = set()

    # Mock bot methods
    m.bot.edit_message_caption = AsyncMock()
    m.bot.edit_message_text = AsyncMock()
    m.bot.send_message = AsyncMock()

    # Mock message
    from aiogram.types import Message, User, Chat
    msg = AsyncMock(spec=Message)
    msg.text = "~Hello world"
    msg.caption = None
    msg.from_user = User(id=m.MY_ID, is_bot=False, first_name="Test")
    msg.chat = Chat(id=123, type="private", full_name="John")
    msg.sender_business_bot = None
    msg.business_connection_id = "conn123"
    msg.message_id = 456

    # Replace the handler code
    # We will just patch the handler in memory if possible, but actually let's just patch main.py directly
    pass

if __name__ == "__main__":
    pass
