import asyncio
import time

async def make_cute_text_mock(text):
    await asyncio.sleep(2)
    return "cute " + text

async def edit_message_mock():
    pass

async def test_baseline():
    start = time.time()
    cute_text = await make_cute_text_mock("hello")
    if cute_text:
        await edit_message_mock()
    end = time.time()
    return end - start

background_tasks = set()
async def process_cute_text_task(text):
    cute_text = await make_cute_text_mock(text)
    if cute_text:
        await edit_message_mock()

async def test_optimized():
    start = time.time()
    task = asyncio.create_task(process_cute_text_task("hello"))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    end = time.time()
    return end - start

async def main():
    t1 = await test_baseline()
    t2 = await test_optimized()
    print(f"Baseline: {t1:.4f}s")
    print(f"Optimized: {t2:.4f}s")

asyncio.run(main())
