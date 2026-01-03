
import asyncio
import time
import cProfile
import pstats
import io

async def worker(idx):
    # Simulate some CPU work and I/O
    counter = 0
    for _ in range(100):
        counter += 1
        await asyncio.sleep(0)  # Yield to scheduler
    return counter

async def main():
    start = time.monotonic()
    tasks = [worker(i) for i in range(5000)]
    await asyncio.gather(*tasks)
    end = time.monotonic()
    print(f"Time: {end - start:.4f}s")

if __name__ == "__main__":
    import uringcore
    
    # Enable debug mode if useful
    # uringcore.UringEventLoop().set_debug(True)
    
    pr = cProfile.Profile()
    pr.enable()
    
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
    
    pr.disable()
    s = io.StringIO()
    sortby = 'cumulative'
    ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
    ps.print_stats(30)
    print(s.getvalue())
