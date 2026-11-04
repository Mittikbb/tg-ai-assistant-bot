import time
from db import get_cursor, init_db

def run_benchmark(iterations=1000):
    start = time.time()
    for _ in range(iterations):
        with get_cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    end = time.time()
    print(f"Time for {iterations} queries: {end - start:.4f} seconds")

if __name__ == "__main__":
    init_db()
    run_benchmark()
