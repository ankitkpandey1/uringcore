
import asyncio
import uringcore
import time
import sys

async def worker(n):
    # Simulate I/O and cpu mix
    print(f"Worker {n} starting")
    await asyncio.sleep(0.1)
    
    # Do some busy work that causes many syscalls if straced (e.g. time)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.1:
        pass
        
    print(f"Worker {n} finished")
    return n

async def main():
    print("Starting tasks...")
    # Launch enough tasks to create noise
    tasks = [worker(i) for i in range(10)]
    results = await asyncio.gather(*tasks)
    print(f"Finished: {results}")

if __name__ == "__main__":
    event_loop = uringcore.new_event_loop(buffer_count=256)
    try:
        event_loop.run_until_complete(main())
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        event_loop.close()
