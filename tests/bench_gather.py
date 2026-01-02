
import asyncio
import time
import uringcore

async def bench_gather_100():
    async def noop():
        pass
    await asyncio.gather(*[noop() for _ in range(100)])

async def main():
    # Warmup
    for _ in range(10):
        await bench_gather_100()
    
    # Benchmark
    iterations = 500
    start = time.perf_counter()
    for _ in range(iterations):
        await bench_gather_100()
    elapsed = time.perf_counter() - start
    
    us_per_op = (elapsed / iterations) * 1_000_000
    print(f"gather(100): {us_per_op:.2f} µs/op ({iterations/elapsed:.0f} ops/sec)")

if __name__ == "__main__":
    # Test with uringcore
    print("[uringcore]")
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
    
    # Reset to default
    asyncio.set_event_loop_policy(None)
    
    # Test with uvloop
    import uvloop
    print("[uvloop]")
    uvloop.install()
    asyncio.run(main())
