#!/usr/bin/env python3
"""Benchmark suite comparing uringcore vs uvloop vs asyncio.

This module provides standardized benchmarks for measuring event loop
performance across different implementations. Results are saved as JSON
and optionally visualized with matplotlib.

SPDX-License-Identifier: Apache-2.0
Copyright 2025 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
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


# Additional benchmarks

async def bench_sleep_sequential():
    """Sequential sleep(0)."""
    for _ in range(10):
        await asyncio.sleep(0)

async def bench_sleep_concurrent_100():
    """100 concurrent sleep(0)."""
    async def noop():
        await asyncio.sleep(0)
    await asyncio.gather(*[noop() for _ in range(100)])

async def bench_semaphore_acquire():
    """Semaphore acquire/release."""
    sem = asyncio.Semaphore(1)
    async with sem:
        pass

async def bench_condition_notify():
    """Condition wait/notify."""
    cond = asyncio.Condition()
    async def waiter():
        async with cond:
            await cond.wait()
    
    async def notifier():
        async with cond:
            cond.notify()
            
    t = asyncio.create_task(waiter())
    # Ensure waiter is waiting
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await notifier()
    await t

async def bench_context_vars():
    """ContextVar propagation overhead."""
    import contextvars
    var = contextvars.ContextVar("bench", default=0)
    var.set(1)
    async def get_val():
        return var.get()
    await asyncio.create_task(get_val())

async def bench_call_later():
    """call_later scheduling overhead."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    loop.call_later(0.000001, future.set_result, 42)
    await future

async def bench_cancel_task():
    """Task cancellation overhead."""
    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
    t = asyncio.create_task(forever())
    await asyncio.sleep(0)
    t.cancel()
    await t

async def bench_shield_overhead():
    """asyncio.shield overhead."""
    async def noop():
        pass
    await asyncio.shield(noop())

async def bench_wait_for_overhead():
    """asyncio.wait_for overhead."""
    async def noop():
        pass
    await asyncio.wait_for(noop(), timeout=10)

async def bench_deep_recursion():
    """Deep recursion/stack chain."""
    async def recursive(n):
        if n <= 0:
            return
        await recursive(n - 1)
    await recursive(20)

async def bench_exception_overhead():
    """Exception propagation overhead."""
    async def raiser():
        raise ValueError("test")
    try:
        await raiser()
    except ValueError:
        pass

async def bench_socketpair_overhead():
    """Socketpair send/recv overhead (simulated networking)."""
    import socket
    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)
    
    loop = asyncio.get_event_loop()
    
    async def sender():
        await loop.sock_sendall(wsock, b"x")
        
    async def receiver():
        await loop.sock_recv(rsock, 1)
        
    await asyncio.gather(sender(), receiver())
    
    rsock.close()
    wsock.close()

# Benchmark configurations: (function, name, iterations)
BENCHMARKS = [
    (bench_sleep_zero, "sleep(0)", 10000),
    (bench_create_task, "create_task", 5000),
    (bench_gather_10, "gather(10)", 2000),
    (bench_gather_100, "gather(100)", 500),
    (bench_queue_put_get, "queue_put", 5000), # Renamed for brevity
    (bench_event_set_wait, "event_wait", 5000),
    (bench_lock_acquire, "lock_acquire", 10000),
    (bench_future_result, "future_res", 10000),
    (bench_call_soon, "call_soon", 10000),
    # New benchmarks
    (bench_sleep_sequential, "sleep_seq_10", 2000),
    (bench_sleep_concurrent_100, "sleep_conc_100", 200),
    (bench_semaphore_acquire, "semaphore", 10000),
    (bench_condition_notify, "condition", 2000),
    (bench_context_vars, "context_vars", 5000),
    (bench_call_later, "call_later", 5000),
    (bench_cancel_task, "task_cancel", 2000),
    (bench_shield_overhead, "shield", 5000),
    (bench_wait_for_overhead, "wait_for", 5000),
    (bench_deep_recursion, "recursion_20", 2000),
    (bench_exception_overhead, "exception", 10000),
    (bench_socketpair_overhead, "sock_pair", 2000),
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
            gc.collect() # Ensure resources are freed before next loop creation
    
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
        
        # Check env for override or use defaults for high-throughput sock_pair benchmark
        buffer_count = int(os.environ.get("URINGCORE_BUFFER_COUNT", 4096))
        buffer_size = int(os.environ.get("URINGCORE_BUFFER_SIZE", 8192))
        
        # Initialize core (will raise helpful error if ENOMEM)
        core = UringCore(buffer_count=buffer_count, buffer_size=buffer_size)
        core.shutdown()
        
        print("\n[uringcore] Running benchmarks...")
        # For now, use asyncio loop + UringCore stats
        # Full event loop benchmarks require transport layer
        
        def uringcore_loop_factory():
             from uringcore import new_event_loop
             return new_event_loop(buffer_count=buffer_count, buffer_size=buffer_size)
        
        results["benchmarks"]["uringcore"] = [
            asdict(r) for r in run_suite_with_loop("uringcore", uringcore_loop_factory)
        ]
        
        # Add uringcore-specific metrics
        core = UringCore(buffer_count=buffer_count, buffer_size=buffer_size)
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


# Check for Plotly
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    print("Note: plotly not available, skipping interactive charts")


def generate_plotly_charts(results: dict, output_dir: Optional[Path] = None):
    """Generate interactive charts using Plotly."""
    if not PLOTLY_AVAILABLE:
        return

    if output_dir is None:
        output_dir = Path(__file__).parent / "results"

    benchmarks = results.get("benchmarks", {})
    if not benchmarks:
        return

    loops = list(benchmarks.keys())
    first_loop = loops[0]
    bench_names = [b["name"] for b in benchmarks[first_loop]]
    
    # Define colors
    colors = {"asyncio": "#3498db", "uvloop": "#2ecc71", "uringcore": "#e74c3c"}

    # Create subplot figure
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("Operation Latency (lower is better)", "Speedup vs asyncio (higher is better)"),
        vertical_spacing=0.15
    )

    # 1. Latency Bar Chart
    for loop in loops:
        times = []
        for b in benchmarks[loop]:
            times.append(b["avg_time_us"])
        
        fig.add_trace(
            go.Bar(name=loop, x=bench_names, y=times, marker_color=colors.get(loop, "gray")),
            row=1, col=1
        )

    # 2. Speedup Chart (if comparison possible)
    if len(loops) > 1 and "asyncio" in loops:
        for loop in loops:
            if loop == "asyncio":
                continue
            
            speedups = []
            for bench_name in bench_names:
                asyncio_time = next((b["avg_time_us"] for b in benchmarks["asyncio"] if b["name"] == bench_name), None)
                loop_time = next((b["avg_time_us"] for b in benchmarks[loop] if b["name"] == bench_name), None)
                
                if asyncio_time and loop_time and loop_time > 0:
                    speedups.append(asyncio_time / loop_time)
                else:
                    speedups.append(1.0)
            
            fig.add_trace(
                go.Bar(
                    name=f"{loop} speedup", 
                    x=bench_names, 
                    y=speedups, 
                    marker_color=colors.get(loop, "gray"),
                    showlegend=True
                ),
                row=2, col=1
            )
        
        # Add baseline line
        fig.add_shape(
            type="line", line=dict(dash="dash", width=1, color="gray"),
            x0=-0.5, x1=len(bench_names)-0.5, y0=1, y1=1,
            row=2, col=1
        )

    # Update layout
    fig.update_layout(
        title_text=f"Event Loop Performance: uringcore vs others ({sys.platform})",
        height=900,
        showlegend=True,
        barmode='group',
        template="plotly_white"
    )
    
    # Update axes
    fig.update_yaxes(title_text="Time (µs)", row=1, col=1)
    fig.update_yaxes(title_text="Speedup Factor (x)", row=2, col=1)
    fig.update_xaxes(tickangle=45, row=2, col=1)

    # Save to HTML
    html_path = output_dir / "benchmark_report.html"
    fig.write_html(str(html_path))
    print(f"Interactive report saved to {html_path}")

    # Save to PNG (for BENCHMARK.md)
    try:
        png_path = output_dir / "benchmark_chart.png"
        fig.write_image(str(png_path), scale=2)
        print(f"Static chart saved to {png_path}")
    except Exception as e:
        print(f"Failed to save static chart (requires kaleido): {e}")


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
    generate_plotly_charts(results, output_dir)
    
    print("\nBenchmark complete!")


if __name__ == "__main__":
    main()
