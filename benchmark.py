import asyncio
import time

async def simulate_make_cute_text():
    await asyncio.sleep(2) # Simulate API call
    return "Cute text"

async def test_baseline():
    start_time = time.time()
    cute_text = await simulate_make_cute_text()
    if cute_text:
        await asyncio.sleep(0.1) # Simulate bot.edit_message_text
    end_time = time.time()
    return end_time - start_time

async def test_optimized():
    background_tasks = set()
    start_time = time.time()

    async def format_and_edit():
        cute_text = await simulate_make_cute_text()
        if cute_text:
            await asyncio.sleep(0.1)

    task = asyncio.create_task(format_and_edit())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    end_time = time.time()

    await task
    return end_time - start_time

async def main():
    baseline = await test_baseline()
    optimized = await test_optimized()
    print(f"Baseline (blocking handler): {baseline:.4f}s")
    print(f"Optimized (non-blocking handler): {optimized:.4f}s")

if __name__ == "__main__":
    asyncio.run(main())
