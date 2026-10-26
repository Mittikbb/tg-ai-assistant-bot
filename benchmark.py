import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

async def make_cute_text(text):
    await asyncio.sleep(2) # Simulate network delay
    return text + " ~nya"

async def test_current():
    start = time.time()
    # current code
    clean_text = "hello"
    cute_text = await make_cute_text(clean_text)
    # simulate remaining code
    end = time.time()
    return end - start

background_tasks = set()

async def offloaded(clean_text):
    cute_text = await make_cute_text(clean_text)

async def test_optimized():
    start = time.time()
    clean_text = "hello"
    task = asyncio.create_task(offloaded(clean_text))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    end = time.time()
    return end - start

async def main():
    print(f"Current: {await test_current():.4f}s")
    print(f"Optimized: {await test_optimized():.4f}s")

asyncio.run(main())
