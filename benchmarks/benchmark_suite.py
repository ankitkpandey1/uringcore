"""Benchmark suite comparing uringcore vs uvloop performance.

This module provides standardized benchmarks for measuring event loop
performance across different workloads.
"""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# Try to import uvloop for comparison
try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False


@dataclass
class BenchmarkResult:
    """Container for benchmark results."""
    name: str
    loop_type: str
    iterations: int
    total_time_ms: float
    avg_time_ms: float
    min_time_ms: float
    max_time_ms: float
    std_dev_ms: float
    ops_per_sec: float


@dataclass 
class BenchmarkSuite:
    """Collection of benchmark results."""
    timestamp: str
    python_version: str
    results: list[BenchmarkResult]


def measure_time(func, iterations: int = 1000) -> BenchmarkResult:
    """Measure execution time of an async function."""
    times = []
    
    for _ in range(iterations):
        start = time.perf_counter()
        asyncio.get_event_loop().run_until_complete(func())
        end = time.perf_counter()
        times.append((end - start) * 1000)  # Convert to ms
    
    total = sum(times)
    avg = statistics.mean(times)
    
    return BenchmarkResult(
        name=func.__name__,
        loop_type="",  # Set by caller
        iterations=iterations,
        total_time_ms=total,
        avg_time_ms=avg,
        min_time_ms=min(times),
        max_time_ms=max(times),
        std_dev_ms=statistics.stdev(times) if len(times) > 1 else 0,
        ops_per_sec=iterations / (total / 1000)
    )


# Benchmark workloads

async def bench_sleep_zero():
    """Minimal async overhead - sleep(0)."""
    await asyncio.sleep(0)


async def bench_create_task():
    """Task creation overhead."""
    async def noop():
        pass
    task = asyncio.create_task(noop())
    await task


async def bench_gather_10():
    """Gather 10 concurrent tasks."""
    async def noop():
        pass
    await asyncio.gather(*[noop() for _ in range(10)])


async def bench_gather_100():
    """Gather 100 concurrent tasks."""
    async def noop():
        pass
    await asyncio.gather(*[noop() for _ in range(100)])


async def bench_queue_operations():
    """Queue put/get operations."""
    queue = asyncio.Queue()
    await queue.put(1)
    await queue.get()


async def bench_event_set_wait():
    """Event set and wait."""
    event = asyncio.Event()
    event.set()
    await asyncio.wait_for(event.wait(), timeout=1.0)


async def bench_lock_acquire():
    """Lock acquire/release."""
    lock = asyncio.Lock()
    async with lock:
        pass


async def bench_future_set_result():
    """Future creation and resolution."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    future.set_result(42)
    await future


BENCHMARKS = [
    (bench_sleep_zero, 10000),
    (bench_create_task, 5000),
    (bench_gather_10, 1000),
    (bench_gather_100, 500),
    (bench_queue_operations, 5000),
    (bench_event_set_wait, 5000),
    (bench_lock_acquire, 5000),
    (bench_future_set_result, 10000),
]


def run_benchmark_suite(loop_type: str, set_loop_func) -> list[BenchmarkResult]:
    """Run all benchmarks with a specific event loop."""
    results = []
    
    for bench_func, iterations in BENCHMARKS:
        # Set up the loop
        set_loop_func()
        
        result = measure_time(bench_func, iterations)
        result.loop_type = loop_type
        results.append(result)
        
        # Clean up
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass
    
    return results


def run_all_benchmarks() -> dict:
    """Run benchmarks for all available event loops."""
    import sys
    from datetime import datetime
    
    all_results = {
        "timestamp": datetime.now().isoformat(),
        "python_version": sys.version,
        "loops": {}
    }
    
    # Standard asyncio
    def set_asyncio():
        asyncio.set_event_loop(asyncio.new_event_loop())
    
    print("Running asyncio benchmarks...")
    asyncio_results = run_benchmark_suite("asyncio", set_asyncio)
    all_results["loops"]["asyncio"] = [asdict(r) for r in asyncio_results]
    
    # uvloop if available
    if UVLOOP_AVAILABLE:
        def set_uvloop():
            loop = uvloop.new_event_loop()
            asyncio.set_event_loop(loop)
        
        print("Running uvloop benchmarks...")
        uvloop_results = run_benchmark_suite("uvloop", set_uvloop)
        all_results["loops"]["uvloop"] = [asdict(r) for r in uvloop_results]
    
    # uringcore
    try:
        from uringcore import EventLoopPolicy
        
        def set_uringcore():
            policy = EventLoopPolicy()
            asyncio.set_event_loop(policy.new_event_loop())
        
        print("Running uringcore benchmarks...")
        uringcore_results = run_benchmark_suite("uringcore", set_uringcore)
        all_results["loops"]["uringcore"] = [asdict(r) for r in uringcore_results]
    except Exception as e:
        print(f"uringcore not available: {e}")
    
    return all_results


def save_results(results: dict, output_dir: Path = None):
    """Save benchmark results to JSON file."""
    if output_dir is None:
        output_dir = Path(__file__).parent / "results"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = results["timestamp"].replace(":", "-").replace(".", "-")
    filename = output_dir / f"benchmark_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"Results saved to {filename}")
    return filename


def print_comparison(results: dict):
    """Print benchmark comparison table."""
    loops = list(results["loops"].keys())
    
    if not loops:
        print("No results to compare")
        return
    
    # Get benchmark names from first loop
    first_loop = loops[0]
    benchmarks = [r["name"] for r in results["loops"][first_loop]]
    
    # Print header
    header = f"{'Benchmark':<25}"
    for loop in loops:
        header += f" | {loop:>12}"
    print(header)
    print("-" * len(header))
    
    # Print each benchmark
    for bench_name in benchmarks:
        row = f"{bench_name:<25}"
        times = {}
        
        for loop in loops:
            loop_results = results["loops"][loop]
            for r in loop_results:
                if r["name"] == bench_name:
                    times[loop] = r["avg_time_ms"]
                    break
        
        # Find baseline (asyncio)
        baseline = times.get("asyncio", 1.0)
        
        for loop in loops:
            t = times.get(loop, 0)
            speedup = baseline / t if t > 0 else 0
            row += f" | {t:>8.3f}ms"
        
        print(row)
    
    # Print speedup comparison
    if len(loops) > 1 and "asyncio" in loops:
        print("\nSpeedup vs asyncio:")
        for loop in loops:
            if loop == "asyncio":
                continue
            
            speedups = []
            for bench_name in benchmarks:
                asyncio_time = None
                loop_time = None
                
                for r in results["loops"]["asyncio"]:
                    if r["name"] == bench_name:
                        asyncio_time = r["avg_time_ms"]
                        break
                
                for r in results["loops"][loop]:
                    if r["name"] == bench_name:
                        loop_time = r["avg_time_ms"]
                        break
                
                if asyncio_time and loop_time:
                    speedups.append(asyncio_time / loop_time)
            
            avg_speedup = statistics.mean(speedups) if speedups else 0
            print(f"  {loop}: {avg_speedup:.2f}x average speedup")


if __name__ == "__main__":
    print("=" * 60)
    print("uringcore Benchmark Suite")
    print("=" * 60)
    
    results = run_all_benchmarks()
    save_results(results)
    
    print("\n" + "=" * 60)
    print("Results Comparison")
    print("=" * 60 + "\n")
    
    print_comparison(results)
