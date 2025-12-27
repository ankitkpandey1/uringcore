#!/usr/bin/env python3
"""uringcore Server Benchmark Suite - Scientific Rigor Edition.

This benchmark measures realistic server performance with:
- Exact system information for reproducibility
- Proper warmup to eliminate JIT bias
- Randomized test ordering
- Long I/O workloads
- High concurrency tests

Copyright (c) 2024 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import gc
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import json

try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False


# =============================================================================
# System Information
# =============================================================================

def get_system_info() -> Dict[str, str]:
    """Collect exact system information for reproducibility."""
    info = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }
    
    # Kernel version
    try:
        info["kernel"] = platform.release()
        # Get more detailed kernel info
        result = subprocess.run(["uname", "-a"], capture_output=True, text=True)
        info["uname"] = result.stdout.strip()
    except Exception:
        pass
    
    # CPU info
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":")[1].strip()
                    break
        info["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    
    # io_uring support
    try:
        import uringcore
        core = uringcore.UringCore()
        info["io_uring_available"] = True
        info["sqpoll_enabled"] = core.sqpoll_enabled
        info["uringcore_version"] = uringcore.__version__
        core.shutdown()
    except Exception as e:
        info["io_uring_available"] = False
        info["io_uring_error"] = str(e)
    
    return info


def print_system_info(info: Dict[str, str]):
    """Print system information."""
    print("=" * 70)
    print("SYSTEM INFORMATION")
    print("=" * 70)
    print(f"Timestamp:     {info.get('timestamp', 'N/A')}")
    print(f"Python:        {info.get('python_version', 'N/A').split()[0]}")
    print(f"Kernel:        {info.get('kernel', 'N/A')}")
    print(f"CPU:           {info.get('cpu_model', 'N/A')}")
    print(f"CPU Count:     {info.get('cpu_count', 'N/A')}")
    print(f"io_uring:      {'Available' if info.get('io_uring_available') else 'Not Available'}")
    print(f"SQPOLL:        {'Enabled' if info.get('sqpoll_enabled') else 'Disabled'}")
    print(f"uringcore:     {info.get('uringcore_version', 'N/A')}")
    print("=" * 70)


# =============================================================================
# Benchmark Results
# =============================================================================

@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    name: str
    loop_type: str
    requests_per_sec: float
    latency_p50_us: float
    latency_p99_us: float
    latency_mean_us: float
    total_requests: int
    duration_seconds: float
    errors: int = 0


# =============================================================================
# Echo Server and Client
# =============================================================================

async def run_echo_server(port: int) -> asyncio.AbstractServer:
    """Start a simple echo server."""
    async def handle_client(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    
    server = await asyncio.start_server(handle_client, '127.0.0.1', port)
    return server


def echo_client_sync(port: int, num_requests: int, payload: bytes) -> List[float]:
    """Synchronous echo client measuring per-request latency."""
    latencies = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    
    try:
        sock.connect(('127.0.0.1', port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        for _ in range(num_requests):
            start = time.perf_counter_ns()
            sock.sendall(payload)
            received = b''
            while len(received) < len(payload):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                received += chunk
            end = time.perf_counter_ns()
            
            if len(received) == len(payload):
                latencies.append((end - start) / 1000)  # ns -> µs
    except Exception:
        pass
    finally:
        sock.close()
    
    return latencies


async def benchmark_echo_server(
    loop_type: str,
    num_clients: int,
    requests_per_client: int,
    payload_size: int,
    warmup_requests: int = 50,
) -> BenchmarkResult:
    """Benchmark echo server with specified parameters."""
    port = random.randint(30000, 40000)
    payload = b'x' * payload_size
    
    # Start server
    server = await run_echo_server(port)
    await asyncio.sleep(0.05)  # Let server settle
    
    # Warmup phase (critical for eliminating JIT bias)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=2) as executor:
        warmup_futures = [
            loop.run_in_executor(executor, echo_client_sync, port, warmup_requests, payload)
            for _ in range(2)
        ]
        await asyncio.gather(*warmup_futures)
    
    # Actual benchmark
    gc.disable()
    all_latencies = []
    start_time = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [
            loop.run_in_executor(executor, echo_client_sync, port, requests_per_client, payload)
            for _ in range(num_clients)
        ]
        results = await asyncio.gather(*futures)
        for res in results:
            all_latencies.extend(res)
    
    end_time = time.perf_counter()
    gc.enable()
    
    server.close()
    await server.wait_closed()
    
    duration = end_time - start_time
    total_requests = len(all_latencies)
    
    if total_requests == 0:
        return BenchmarkResult(
            name=f"echo_{payload_size}b",
            loop_type=loop_type,
            requests_per_sec=0,
            latency_p50_us=0,
            latency_p99_us=0,
            latency_mean_us=0,
            total_requests=0,
            duration_seconds=duration,
            errors=num_clients * requests_per_client,
        )
    
    sorted_latencies = sorted(all_latencies)
    
    return BenchmarkResult(
        name=f"echo_{payload_size}b",
        loop_type=loop_type,
        requests_per_sec=total_requests / duration,
        latency_p50_us=sorted_latencies[len(sorted_latencies) // 2],
        latency_p99_us=sorted_latencies[int(len(sorted_latencies) * 0.99)],
        latency_mean_us=statistics.mean(all_latencies),
        total_requests=total_requests,
        duration_seconds=duration,
        errors=num_clients * requests_per_client - total_requests,
    )


# =============================================================================
# Long I/O Benchmark (10MB transfer)
# =============================================================================

async def benchmark_large_transfer(
    loop_type: str,
    transfer_size_mb: int = 10,
    num_transfers: int = 5,
) -> BenchmarkResult:
    """Benchmark large data transfers."""
    port = random.randint(40000, 50000)
    data_size = transfer_size_mb * 1024 * 1024
    payload = b'x' * data_size
    
    # Simple echo server for large data
    server = await run_echo_server(port)
    await asyncio.sleep(0.05)
    
    gc.disable()
    latencies = []
    errors = 0
    start_time = time.perf_counter()
    
    for _ in range(num_transfers):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)  # Longer timeout for large transfers
        
        try:
            sock.connect(('127.0.0.1', port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            
            transfer_start = time.perf_counter_ns()
            
            # Send all data
            sock.sendall(payload)
            
            # Receive all data
            received = b''
            while len(received) < len(payload):
                chunk = sock.recv(1024 * 1024)
                if not chunk:
                    break
                received += chunk
            
            transfer_end = time.perf_counter_ns()
            
            if len(received) == len(payload):
                latencies.append((transfer_end - transfer_start) / 1_000_000)  # ms
            else:
                errors += 1
        except Exception:
            errors += 1
        finally:
            sock.close()
    
    end_time = time.perf_counter()
    gc.enable()
    
    server.close()
    await server.wait_closed()
    
    duration = end_time - start_time
    successful = len(latencies)
    
    if successful == 0:
        return BenchmarkResult(
            name=f"transfer_{transfer_size_mb}mb",
            loop_type=loop_type,
            requests_per_sec=0,
            latency_p50_us=0,
            latency_p99_us=0,
            latency_mean_us=0,
            total_requests=0,
            duration_seconds=duration,
            errors=errors,
        )
    
    sorted_latencies = sorted(latencies)
    throughput_mbps = (successful * transfer_size_mb) / duration
    
    return BenchmarkResult(
        name=f"transfer_{transfer_size_mb}mb",
        loop_type=loop_type,
        requests_per_sec=throughput_mbps,  # MB/s for this benchmark
        latency_p50_us=sorted_latencies[len(sorted_latencies) // 2] * 1000,  # ms -> µs
        latency_p99_us=sorted_latencies[-1] * 1000 if sorted_latencies else 0,
        latency_mean_us=statistics.mean(latencies) * 1000 if latencies else 0,
        total_requests=successful,
        duration_seconds=duration,
        errors=errors,
    )


# =============================================================================
# High Concurrency Benchmark
# =============================================================================

async def benchmark_high_concurrency(
    loop_type: str,
    num_connections: int = 100,
    requests_per_connection: int = 10,
) -> BenchmarkResult:
    """Benchmark with many concurrent connections."""
    return await benchmark_echo_server(
        loop_type=loop_type,
        num_clients=num_connections,
        requests_per_client=requests_per_connection,
        payload_size=64,
        warmup_requests=10,
    )


# =============================================================================
# Benchmark Runner
# =============================================================================

def run_benchmarks_with_loop(loop_type: str, loop_factory: Callable) -> List[BenchmarkResult]:
    """Run all benchmarks with a specific loop type."""
    results = []
    
    loop = loop_factory()
    asyncio.set_event_loop(loop)
    
    try:
        # Echo 64B
        result = loop.run_until_complete(
            benchmark_echo_server(loop_type, num_clients=10, requests_per_client=100, payload_size=64)
        )
        results.append(result)
        print(f"  {result.name}: {result.requests_per_sec:.0f} req/s, p99={result.latency_p99_us:.0f}µs")
        
        # Echo 1KB
        result = loop.run_until_complete(
            benchmark_echo_server(loop_type, num_clients=10, requests_per_client=100, payload_size=1024)
        )
        results.append(result)
        print(f"  {result.name}: {result.requests_per_sec:.0f} req/s, p99={result.latency_p99_us:.0f}µs")
        
        # Large transfer 10MB
        result = loop.run_until_complete(
            benchmark_large_transfer(loop_type, transfer_size_mb=10, num_transfers=3)
        )
        results.append(result)
        print(f"  {result.name}: {result.requests_per_sec:.1f} MB/s, p99={result.latency_p99_us/1000:.0f}ms")
        
        # High concurrency
        result = loop.run_until_complete(
            benchmark_high_concurrency(loop_type, num_connections=50, requests_per_connection=20)
        )
        result.name = "concurrent_50"
        results.append(result)
        print(f"  {result.name}: {result.requests_per_sec:.0f} req/s, p99={result.latency_p99_us:.0f}µs")
        
    finally:
        loop.close()
    
    return results


def run_all_benchmarks() -> Dict:
    """Run benchmarks for all available loop types in randomized order."""
    system_info = get_system_info()
    print_system_info(system_info)
    
    results = {
        "system_info": system_info,
        "benchmarks": {},
    }
    
    # Define loop types
    loop_configs = [
        ("asyncio", asyncio.new_event_loop),
    ]
    
    if UVLOOP_AVAILABLE:
        loop_configs.append(("uvloop", uvloop.new_event_loop))
    
    try:
        import uringcore
        loop_configs.append(("uringcore", lambda: uringcore.EventLoopPolicy().new_event_loop()))
    except ImportError:
        pass
    
    # Randomize order to eliminate systematic bias
    random.shuffle(loop_configs)
    print(f"\nBenchmark order: {[c[0] for c in loop_configs]}")
    
    for loop_type, loop_factory in loop_configs:
        print(f"\n[{loop_type}] Running benchmarks...")
        try:
            benchmark_results = run_benchmarks_with_loop(loop_type, loop_factory)
            results["benchmarks"][loop_type] = [asdict(r) for r in benchmark_results]
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error running {loop_type}: {e}")
    
    return results


def print_comparison(results: Dict):
    """Print comparison table."""
    benchmarks = results.get("benchmarks", {})
    if not benchmarks:
        return
    
    loops = list(benchmarks.keys())
    
    # Get all benchmark names
    all_names = set()
    for loop_results in benchmarks.values():
        for r in loop_results:
            all_names.add(r["name"])
    
    print("\n" + "=" * 80)
    print("BENCHMARK COMPARISON")
    print("=" * 80)
    
    # Throughput table
    print("\nThroughput (higher is better):")
    header = "Benchmark".ljust(20) + "".join(f" | {l:>12}" for l in loops)
    print(header)
    print("-" * len(header))
    
    for name in sorted(all_names):
        row = name.ljust(20)
        for loop in loops:
            loop_results = benchmarks.get(loop, [])
            result = next((r for r in loop_results if r["name"] == name), None)
            if result:
                if "transfer" in name:
                    row += f" | {result['requests_per_sec']:>10.1f}/s"
                else:
                    row += f" | {result['requests_per_sec']:>10.0f}/s"
            else:
                row += " |        N/A"
        print(row)
    
    # Latency table
    print("\nP99 Latency (lower is better):")
    header = "Benchmark".ljust(20) + "".join(f" | {l:>12}" for l in loops)
    print(header)
    print("-" * len(header))
    
    for name in sorted(all_names):
        row = name.ljust(20)
        for loop in loops:
            loop_results = benchmarks.get(loop, [])
            result = next((r for r in loop_results if r["name"] == name), None)
            if result:
                if "transfer" in name:
                    row += f" | {result['latency_p99_us']/1000:>10.0f}ms"
                else:
                    row += f" | {result['latency_p99_us']:>10.0f}µs"
            else:
                row += " |        N/A"
        print(row)
    
    print("=" * 80)


def main():
    """Main entry point."""
    print("=" * 70)
    print("uringcore Server Benchmark Suite - Scientific Rigor Edition")
    print("=" * 70)
    
    results = run_all_benchmarks()
    print_comparison(results)
    
    # Save results
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = results_dir / f"benchmark_{timestamp}.json"
    
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to {results_file}")
    print("\nBenchmark complete!")


if __name__ == "__main__":
    main()
