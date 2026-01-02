#!/usr/bin/env python3
"""
Real-world stress test for uringcore using working features.
Tests: task creation, cancellation, futures, timers, and concurrent execution.
"""
import asyncio
import time
import sys

# Must set policy before any asyncio usage
import uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())


async def worker(worker_id: int, results: dict, stop_event: asyncio.Event):
    """Worker that simulates processing work items."""
    while not stop_event.is_set():
        # Simulate work
        fut = asyncio.get_event_loop().create_future()
        asyncio.get_event_loop().call_soon(fut.set_result, worker_id)
        await fut
        results["futures_resolved"] += 1
        
        # Create and await a microtask
        async def micro():
            await asyncio.sleep(0)
        await asyncio.create_task(micro())
        results["tasks_created"] += 1
        
        # Small delay
        await asyncio.sleep(0.001)
        results["sleeps_completed"] += 1


async def cancellation_stress(results: dict, count: int = 100):
    """Test cancellation path under stress."""
    for _ in range(count):
        async def to_cancel():
            try:
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                pass
        
        t = asyncio.create_task(to_cancel())
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        results["cancellations"] += 1


async def gather_stress(results: dict, iterations: int = 50):
    """Test gather under stress."""
    for _ in range(iterations):
        async def noop():
            pass
        await asyncio.gather(*[noop() for _ in range(100)])
        results["gather_batches"] += 1


async def run_stress_test(duration_seconds: int = 60, num_workers: int = 10):
    """Run the stress test."""
    print(f"=== uringcore Coroutine Stress Test ===")
    print(f"Duration: {duration_seconds}s | Workers: {num_workers}")
    print()
    
    results = {
        "futures_resolved": 0,
        "tasks_created": 0,
        "sleeps_completed": 0,
        "cancellations": 0,
        "gather_batches": 0,
    }
    stop_event = asyncio.Event()
    
    # Start workers
    worker_tasks = [
        asyncio.create_task(worker(i, results, stop_event))
        for i in range(num_workers)
    ]
    
    # Run stress functions
    cancel_task = asyncio.create_task(cancellation_stress(results))
    gather_task = asyncio.create_task(gather_stress(results))
    
    # Run for duration
    start_time = time.time()
    
    while time.time() - start_time < duration_seconds:
        await asyncio.sleep(1.0)
        elapsed = time.time() - start_time
        ops_per_sec = sum(results.values()) / elapsed
        print(f"  [{int(elapsed):3d}s] Total ops: {sum(results.values()):,} | OPS: {ops_per_sec:,.0f}")
    
    # Stop workers
    stop_event.set()
    for t in worker_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    
    # Wait for other tasks
    for t in [cancel_task, gather_task]:
        if not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    
    # Final stats
    total_time = time.time() - start_time
    total_ops = sum(results.values())
    ops_per_sec = total_ops / total_time
    
    print()
    print("=== Results ===")
    print(f"Duration:          {total_time:.1f}s")
    print(f"Total Operations:  {total_ops:,}")
    print(f"OPS:               {ops_per_sec:,.0f}")
    print()
    print(f"Futures Resolved:  {results['futures_resolved']:,}")
    print(f"Tasks Created:     {results['tasks_created']:,}")
    print(f"Sleeps Completed:  {results['sleeps_completed']:,}")
    print(f"Cancellations:     {results['cancellations']:,}")
    print(f"Gather Batches:    {results['gather_batches']:,}")
    print()
    
    # Verify success
    if total_ops < 1000:
        print("❌ FAILED: Too few operations completed")
        return False
    
    print("✅ PASSED: Coroutine stress test")
    return True


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    success = asyncio.run(run_stress_test(duration_seconds=duration))
    sys.exit(0 if success else 1)
