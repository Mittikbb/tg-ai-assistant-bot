import asyncio
import time
from aiogram import Bot, Dispatcher
from aiogram.types import Message, User, Chat, Update
import main

def dummy_make_cute_text(text):
    time.sleep(0.5) # Simulate SYNCHRONOUS API latency
    return text + " cute!"

async def measure_latency():
    max_latency = 0
    start = time.time()
    for _ in range(100):
        t = time.time()
        await asyncio.sleep(0)
        latency = time.time() - t
        if latency > max_latency:
            max_latency = latency
        if time.time() - start > 0.6:
            break
    print(f"Max event loop latency: {max_latency:.4f} seconds")

async def benchmark():
    # Mock bot and make_cute_text
    class MockBot:
        async def edit_message_text(self, *args, **kwargs):
            pass
        async def send_message(self, *args, **kwargs):
            pass

    main.bot = MockBot()
    main.make_cute_text = dummy_make_cute_text
    main.MY_ID = 123

    # Create fake message
    msg = Message(
        message_id=1,
        date=int(time.time()),
        chat=Chat(id=456, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Test"),
        text="~hello",
        business_connection_id="abc"
    )

    t1 = asyncio.create_task(main.handle_business_message(msg))
    t2 = asyncio.create_task(measure_latency())

    await asyncio.gather(t1, t2)

if __name__ == "__main__":
    import os
    os.environ["BOT_TOKEN"] = "123:test"
    asyncio.run(benchmark())
