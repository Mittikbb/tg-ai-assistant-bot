import asyncio
import time
from unittest.mock import AsyncMock, patch

async def mock_make_cute_text(text):
    await asyncio.sleep(0.5) # Simulate network delay
    return text + " ~nya"

background_tasks = set()

async def format_and_edit_task(clean_text):
    try:
        cute_text = await mock_make_cute_text(clean_text)
    except Exception as e:
        pass

async def handle_original():
    start = time.time()
    clean_text = "hello"
    try:
        cute_text = await mock_make_cute_text(clean_text)
    except Exception as e:
        pass
    return time.time() - start

async def handle_optimized():
    start = time.time()
    clean_text = "hello"
    task = asyncio.create_task(format_and_edit_task(clean_text))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return time.time() - start

async def main():
    original_time = await handle_original()
    optimized_time = await handle_optimized()
    print(f"Original Time: {original_time:.4f}s")
    print(f"Optimized Time: {optimized_time:.4f}s")

asyncio.run(main())
