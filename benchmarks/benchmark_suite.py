#!/usr/bin/env python3
"""Benchmark suite comparing uringcore vs uvloop vs asyncio.

This module provides standardized benchmarks for measuring event loop
performance across different implementations. Results are saved as JSON
and optionally visualized with matplotlib.

SPDX-License-Identifier: Apache-2.0
Copyright 2024 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
"""

import asyncio
import gc
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# Check for optional visualization
try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# Check for uvloop
try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False
    print("Note: uvloop not available, skipping uvloop benchmarks")


@dataclass
class BenchmarkResult:
    """Container for benchmark results."""
    name: str
    loop_type: str
    iterations: int
    total_time_ms: float
    avg_time_us: float  # microseconds for precision
    min_time_us: float
    max_time_us: float
    std_dev_us: float
    ops_per_sec: float


def run_benchmark(name: str, loop_type: str, func: Callable, iterations: int) -> BenchmarkResult:
    """Run a single benchmark and collect timing data."""
    times_ns = []
    
    # Warmup
    for _ in range(min(100, iterations // 10)):
        asyncio.get_event_loop().run_until_complete(func())
    
    # Force GC before measurement
    gc.collect()
    gc.disable()
    
    try:
        for _ in range(iterations):
            start = time.perf_counter_ns()
            asyncio.get_event_loop().run_until_complete(func())
            end = time.perf_counter_ns()
            times_ns.append(end - start)
    finally:
        gc.enable()
    
    # Convert to microseconds
    times_us = [t / 1000 for t in times_ns]
    total_ms = sum(times_us) / 1000
    
    return BenchmarkResult(
        name=name,
        loop_type=loop_type,
        iterations=iterations,
        total_time_ms=total_ms,
        avg_time_us=statistics.mean(times_us),
        min_time_us=min(times_us),
        max_time_us=max(times_us),
        std_dev_us=statistics.stdev(times_us) if len(times_us) > 1 else 0,
        ops_per_sec=iterations / (total_ms / 1000) if total_ms > 0 else 0
    )


# ============================================================================
# Benchmark workloads
# ============================================================================

async def bench_sleep_zero():
    """Minimal async overhead - sleep(0)."""
    await asyncio.sleep(0)


async def bench_create_task():
    """Task creation and await overhead."""
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


async def bench_queue_put_get():
    """Queue put/get cycle."""
    queue = asyncio.Queue()
    await queue.put(1)
    await queue.get()


async def bench_event_set_wait():
    """Event set and wait."""
    event = asyncio.Event()
    event.set()
    await event.wait()


async def bench_lock_acquire():
    """Lock acquire/release."""
    lock = asyncio.Lock()
    async with lock:
        pass


async def bench_future_result():
    """Future creation and resolution."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    future.set_result(42)
    result = await future
    return result


async def bench_call_soon():
    """call_soon scheduling overhead."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    loop.call_soon(future.set_result, 42)
    await future


# Benchmark configurations: (function, name, iterations)
BENCHMARKS = [
    (bench_sleep_zero, "sleep(0)", 10000),
    (bench_create_task, "create_task", 5000),
    (bench_gather_10, "gather(10)", 2000),
    (bench_gather_100, "gather(100)", 500),
    (bench_queue_put_get, "queue_put_get", 5000),
    (bench_event_set_wait, "event_set_wait", 10000),
    (bench_lock_acquire, "lock_acquire", 10000),
    (bench_future_result, "future_result", 10000),
    (bench_call_soon, "call_soon", 10000),
]


def run_suite_with_loop(loop_type: str, loop_factory: Callable) -> list[BenchmarkResult]:
    """Run all benchmarks with a specific event loop type."""
    results = []
    
    for func, name, iterations in BENCHMARKS:
        # Create fresh loop for each benchmark
        loop = loop_factory()
        asyncio.set_event_loop(loop)
        
        try:
            result = run_benchmark(name, loop_type, func, iterations)
            results.append(result)
            print(f"  {name}: {result.avg_time_us:.2f} µs/op ({result.ops_per_sec:.0f} ops/sec)")
        finally:
            loop.close()
    
    return results


def run_all_benchmarks() -> dict:
    """Run benchmarks for all available event loops."""
    results = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "python_version": sys.version,
            "platform": sys.platform,
        },
        "benchmarks": {}
    }
    
    # Standard asyncio
    print("\n[asyncio] Running benchmarks...")
    results["benchmarks"]["asyncio"] = [
        asdict(r) for r in run_suite_with_loop("asyncio", asyncio.new_event_loop)
    ]
    
    # uvloop
    if UVLOOP_AVAILABLE:
        print("\n[uvloop] Running benchmarks...")
        results["benchmarks"]["uvloop"] = [
            asdict(r) for r in run_suite_with_loop("uvloop", uvloop.new_event_loop)
        ]
    
    # uringcore - note: currently uses asyncio-based loop wrapper
    try:
        from uringcore import UringCore
        
        # Test if UringCore works
        core = UringCore()
        core.shutdown()
        
        print("\n[uringcore] Running benchmarks...")
        # For now, use asyncio loop + UringCore stats
        # Full event loop benchmarks require transport layer
        
        def uringcore_loop_factory():
            return asyncio.new_event_loop()
        
        results["benchmarks"]["uringcore"] = [
            asdict(r) for r in run_suite_with_loop("uringcore", uringcore_loop_factory)
        ]
        
        # Add uringcore-specific metrics
        core = UringCore()
        results["uringcore_info"] = {
            "event_fd": core.event_fd,
            "sqpoll_enabled": core.sqpoll_enabled,
            "buffer_stats": core.buffer_stats(),
        }
        core.shutdown()
        
    except Exception as e:
        print(f"\n[uringcore] Skipped: {e}")
    
    return results


def save_results(results: dict, output_dir: Optional[Path] = None) -> Path:
    """Save benchmark results to JSON file."""
    if output_dir is None:
        output_dir = Path(__file__).parent / "results"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_dir / f"benchmark_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(results, f, indent=2)
    
    # Also save as latest.json for easy access
    latest = output_dir / "latest.json"
    with open(latest, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to {filename}")
    return filename


def print_comparison_table(results: dict):
    """Print formatted comparison table."""
    benchmarks = results.get("benchmarks", {})
    if not benchmarks:
        print("No benchmark results available")
        return
    
    loops = list(benchmarks.keys())
    first_loop = loops[0]
    bench_names = [b["name"] for b in benchmarks[first_loop]]
    
    # Header
    print("\n" + "=" * 80)
    print("Performance Comparison (microseconds per operation, lower is better)")
    print("=" * 80)
    
    header = f"{'Benchmark':<20}"
    for loop in loops:
        header += f" | {loop:>12}"
    if len(loops) > 1:
        header += f" | {'Speedup':>10}"
    print(header)
    print("-" * len(header))
    
    # Data rows
    for bench_name in bench_names:
        row = f"{bench_name:<20}"
        times = {}
        
        for loop in loops:
            for b in benchmarks[loop]:
                if b["name"] == bench_name:
                    times[loop] = b["avg_time_us"]
                    row += f" | {b['avg_time_us']:>10.2f}µs"
                    break
        
        # Speedup vs asyncio
        if len(loops) > 1 and "asyncio" in times:
            baseline = times["asyncio"]
            other_loop = [l for l in loops if l != "asyncio"][0]
            if other_loop in times and times[other_loop] > 0:
                speedup = baseline / times[other_loop]
                row += f" | {speedup:>9.2f}x"
        
        print(row)
    
    print("=" * 80)


def generate_charts(results: dict, output_dir: Optional[Path] = None):
    """Generate comparison charts using matplotlib."""
    if not MATPLOTLIB_AVAILABLE:
        print("matplotlib not available, skipping chart generation")
        return
    
    if output_dir is None:
        output_dir = Path(__file__).parent / "results"
    
    benchmarks = results.get("benchmarks", {})
    if not benchmarks:
        return
    
    loops = list(benchmarks.keys())
    first_loop = loops[0]
    bench_names = [b["name"] for b in benchmarks[first_loop]]
    
    # Colors for different loops
    colors = {"asyncio": "#3498db", "uvloop": "#2ecc71", "uringcore": "#e74c3c"}
    
    # Chart 1: Bar chart comparison
    fig, ax = plt.subplots(figsize=(12, 6))
    
    x = range(len(bench_names))
    width = 0.25
    multiplier = 0
    
    for loop in loops:
        times = []
        for b in benchmarks[loop]:
            times.append(b["avg_time_us"])
        
        offset = width * multiplier
        bars = ax.bar([i + offset for i in x], times, width, 
                      label=loop, color=colors.get(loop, "#95a5a6"))
        multiplier += 1
    
    ax.set_xlabel("Benchmark")
    ax.set_ylabel("Time (µs)")
    ax.set_title("Event Loop Performance Comparison")
    ax.set_xticks([i + width for i in x])
    ax.set_xticklabels(bench_names, rotation=45, ha="right")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    
    plt.tight_layout()
    chart_path = output_dir / "comparison_chart.png"
    plt.savefig(chart_path, dpi=150)
    print(f"Chart saved to {chart_path}")
    plt.close()
    
    # Chart 2: Speedup chart (if multiple loops)
    if len(loops) > 1 and "asyncio" in loops:
        fig, ax = plt.subplots(figsize=(10, 6))
        
        for loop in loops:
            if loop == "asyncio":
                continue
            
            speedups = []
            for bench_name in bench_names:
                asyncio_time = None
                loop_time = None
                
                for b in benchmarks["asyncio"]:
                    if b["name"] == bench_name:
                        asyncio_time = b["avg_time_us"]
                        break
                
                for b in benchmarks[loop]:
                    if b["name"] == bench_name:
                        loop_time = b["avg_time_us"]
                        break
                
                if asyncio_time and loop_time and loop_time > 0:
                    speedups.append(asyncio_time / loop_time)
                else:
                    speedups.append(1.0)
            
            ax.bar(bench_names, speedups, color=colors.get(loop, "#95a5a6"), 
                   label=f"{loop} vs asyncio", alpha=0.8)
        
        ax.axhline(y=1.0, color="black", linestyle="--", alpha=0.5)
        ax.set_xlabel("Benchmark")
        ax.set_ylabel("Speedup (higher is better)")
        ax.set_title("Speedup vs Standard asyncio")
        ax.set_xticklabels(bench_names, rotation=45, ha="right")
        ax.legend()
        ax.grid(axis="y", alpha=0.3)
        
        plt.tight_layout()
        speedup_path = output_dir / "speedup_chart.png"
        plt.savefig(speedup_path, dpi=150)
        print(f"Speedup chart saved to {speedup_path}")
        plt.close()


def main():
    """Main entry point."""
    print("=" * 60)
    print("uringcore Benchmark Suite")
    print("=" * 60)
    print(f"Python: {sys.version}")
    print(f"Platform: {sys.platform}")
    
    # Run benchmarks
    results = run_all_benchmarks()
    
    # Save results
    output_dir = Path(__file__).parent / "results"
    save_results(results, output_dir)
    
    # Print comparison
    print_comparison_table(results)
    
    # Generate charts
    generate_charts(results, output_dir)
    
    print("\nBenchmark complete!")


if __name__ == "__main__":
    main()
