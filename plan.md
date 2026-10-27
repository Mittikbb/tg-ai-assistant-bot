1. Add a `background_tasks = set()` global variable in `main.py` if it doesn't already exist.
2. In `main.py`, when processing the `~` prefix in `handle_business_message` (lines 654-670), we are calling `await make_cute_text(clean_text)`. This is a network call that can block the processing of other events if not offloaded.
3. Replace the inline blocking call with `asyncio.create_task()` to process the formatting in the background.
4. Define a helper async function inside or outside the handler to do the actual network call and editing.
5. Make sure to keep a strong reference to the background task using `background_tasks.add(task)` and `task.add_done_callback(background_tasks.discard)`.
