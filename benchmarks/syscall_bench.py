import asyncio
import time
import statistics
import os

try:
    import uvloop
except ImportError:
    uvloop = None

try:
    import uringcore
except ImportError:
    uringcore = None

ITERATIONS = 2000
WARMUP = 200

async def bench_gather_100():
    async def noop():
        pass
    tasks = [noop() for _ in range(100)]
    await asyncio.gather(*tasks)

def run_bench(name, loop_factory):
    print(f"Running {name}...")
    loop = loop_factory()
    asyncio.set_event_loop(loop)
    
    # Warmup
    for _ in range(WARMUP):
        loop.run_until_complete(bench_gather_100())
    
    times = []
    for _ in range(ITERATIONS):
        start = time.perf_counter_ns()
        loop.run_until_complete(bench_gather_100())
        end = time.perf_counter_ns()
        times.append(end - start)
    
    loop.close()
    
    avg_us = statistics.mean(times) / 1000
    print(f"{name}: {avg_us:.2f} µs")
    return avg_us

if __name__ == "__main__":
    import sys
    
    target = None
    if len(sys.argv) > 1:
        target = sys.argv[1]

    print(f"Benchmark: gather(100) | Iterations: {ITERATIONS}")
    print("-" * 40)
    
    res = {}
    
    # Asyncio
    if target is None or target == "asyncio":
        res["asyncio"] = run_bench("asyncio", asyncio.new_event_loop)
    
    # Uvloop
    if uvloop and (target is None or target == "uvloop"):
        res["uvloop"] = run_bench("uvloop", uvloop.new_event_loop)
    
    # Uringcore
    if uringcore and (target is None or target == "uringcore"):
        def uring_factory():
            return uringcore.new_event_loop()
        res["uringcore"] = run_bench("uringcore", uring_factory)
        
    print("-" * 40)
    if "uringcore" in res and "uvloop" in res:
        print(f"uringcore vs uvloop: {res['uvloop'] / res['uringcore']:.2f}x speedup")
    if "uringcore" in res and "asyncio" in res:
        print(f"uringcore vs asyncio: {res['asyncio'] / res['uringcore']:.2f}x speedup")
