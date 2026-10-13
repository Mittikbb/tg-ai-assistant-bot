import asyncio
import time

async def mock_make_cute_text(text):
    await asyncio.sleep(2) # Simulate network call
    return "cute " + text

async def handler_with_await():
    start = time.time()
    await mock_make_cute_text("hello")
    end = time.time()
    return end - start

background_tasks = set()

async def update_message(text):
    cute = await mock_make_cute_text(text)
    # simulate edit_message

async def handler_with_task():
    start = time.time()
    task = asyncio.create_task(update_message("hello"))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    end = time.time()
    return end - start

async def main():
    t1 = await handler_with_await()
    print(f"Baseline (await): {t1:.4f}s")
    t2 = await handler_with_task()
    print(f"Optimized (task): {t2:.4f}s")

asyncio.run(main())
