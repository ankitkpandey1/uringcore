#!/usr/bin/env python3
"""Realistic server benchmarks comparing event loop implementations.

This module benchmarks actual network I/O performance using:
1. TCP echo server (raw sockets)
2. HTTP server simulation

SPDX-License-Identifier: Apache-2.0
Copyright 2024 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
"""

import asyncio
import gc
import json
import socket
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

# Check for uvloop
try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False


@dataclass
class ServerBenchmarkResult:
    """Container for server benchmark results."""
    name: str
    loop_type: str
    total_requests: int
    duration_sec: float
    requests_per_sec: float
    avg_latency_us: float
    min_latency_us: float
    max_latency_us: float
    p50_latency_us: float
    p99_latency_us: float


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ============================================================================
# TCP Echo Server Benchmark
# ============================================================================

async def echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single echo connection."""
    try:
        while True:
            data = await reader.read(1024)
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


async def run_echo_server(port: int, ready_event: asyncio.Event):
    """Run echo server and signal when ready."""
    server = await asyncio.start_server(echo_handler, '127.0.0.1', port)
    ready_event.set()
    async with server:
        await server.serve_forever()


def echo_client_sync(port: int, num_requests: int, payload: bytes) -> list[float]:
    """Synchronous echo client that measures latency."""
    latencies = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)  # 2 second timeout
    try:
        sock.connect(('127.0.0.1', port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        for _ in range(num_requests):
            start = time.perf_counter_ns()
            sock.sendall(payload)
            received = b''
            while len(received) < len(payload):
                try:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    received += chunk
                except socket.timeout:
                    break
            end = time.perf_counter_ns()
            latencies.append((end - start) / 1000)  # Convert to µs
    except Exception:
        pass
    finally:
        sock.close()
    
    return latencies


async def benchmark_echo_server(
    loop_type: str,
    num_clients: int = 10,
    requests_per_client: int = 1000,
    payload_size: int = 64
) -> ServerBenchmarkResult:
    """Benchmark echo server with multiple concurrent clients."""
    port = find_free_port()
    payload = b'x' * payload_size
    
    # Start server
    ready_event = asyncio.Event()
    server_task = asyncio.create_task(run_echo_server(port, ready_event))
    await ready_event.wait()
    
    # Wait for server to be fully ready
    await asyncio.sleep(0.1)
    
    # Run clients in thread pool without blocking the loop
    all_latencies = []
    start_time = time.perf_counter()
    loop = asyncio.get_running_loop()
    
    with ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [
            loop.run_in_executor(executor, echo_client_sync, port, requests_per_client, payload)
            for _ in range(num_clients)
        ]
        results = await asyncio.gather(*futures)
        for res in results:
            all_latencies.extend(res)
    
    end_time = time.perf_counter()
    duration = end_time - start_time
    
    # Stop server
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    
    # Calculate statistics
    total_requests = len(all_latencies)
    sorted_latencies = sorted(all_latencies)
    
    return ServerBenchmarkResult(
        name=f"echo_server_{payload_size}b",
        loop_type=loop_type,
        total_requests=total_requests,
        duration_sec=duration,
        requests_per_sec=total_requests / duration,
        avg_latency_us=statistics.mean(all_latencies),
        min_latency_us=min(all_latencies),
        max_latency_us=max(all_latencies),
        p50_latency_us=sorted_latencies[int(len(sorted_latencies) * 0.50)],
        p99_latency_us=sorted_latencies[int(len(sorted_latencies) * 0.99)],
    )


# ============================================================================
# HTTP-like Request Benchmark
# ============================================================================

HTTP_RESPONSE = b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\nHello, World!'
HTTP_REQUEST = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'


async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle HTTP-like requests."""
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            # Simple HTTP response
            writer.write(HTTP_RESPONSE)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_http_server(port: int, ready_event: asyncio.Event):
    """Run HTTP-like server."""
    server = await asyncio.start_server(http_handler, '127.0.0.1', port)
    ready_event.set()
    async with server:
        await server.serve_forever()


def http_client_sync(port: int, num_requests: int) -> list[float]:
    """Synchronous HTTP client."""
    latencies = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(('127.0.0.1', port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        for _ in range(num_requests):
            start = time.perf_counter_ns()
            sock.sendall(HTTP_REQUEST)
            received = b''
            while b'\r\n\r\n' not in received or len(received) < len(HTTP_RESPONSE):
                try:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    received += chunk
                    if len(received) >= len(HTTP_RESPONSE):
                        break
                except socket.timeout:
                    break
            end = time.perf_counter_ns()
            latencies.append((end - start) / 1000)
    except Exception:
        pass
    finally:
        sock.close()
    
    return latencies


async def benchmark_http_server(
    loop_type: str,
    num_clients: int = 10,
    requests_per_client: int = 1000
) -> ServerBenchmarkResult:
    """Benchmark HTTP-like server."""
    port = find_free_port()
    
    ready_event = asyncio.Event()
    server_task = asyncio.create_task(run_http_server(port, ready_event))
    await ready_event.wait()
    await asyncio.sleep(0.1)
    
    all_latencies = []
    start_time = time.perf_counter()
    loop = asyncio.get_running_loop()
    
    with ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [
            loop.run_in_executor(executor, http_client_sync, port, requests_per_client)
            for _ in range(num_clients)
        ]
        results = await asyncio.gather(*futures)
        for res in results:
            all_latencies.extend(res)
    
    end_time = time.perf_counter()
    duration = end_time - start_time
    
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    
    total_requests = len(all_latencies)
    sorted_latencies = sorted(all_latencies)
    
    return ServerBenchmarkResult(
        name="http_server",
        loop_type=loop_type,
        total_requests=total_requests,
        duration_sec=duration,
        requests_per_sec=total_requests / duration,
        avg_latency_us=statistics.mean(all_latencies),
        min_latency_us=min(all_latencies),
        max_latency_us=max(all_latencies),
        p50_latency_us=sorted_latencies[int(len(sorted_latencies) * 0.50)],
        p99_latency_us=sorted_latencies[int(len(sorted_latencies) * 0.99)],
    )


# ============================================================================
# Benchmark Runner
# ============================================================================

def run_server_benchmarks_with_loop(
    loop_type: str,
    loop_factory: Callable
) -> list[ServerBenchmarkResult]:
    """Run all server benchmarks with a specific event loop."""
    results = []
    
    # Echo server benchmarks
    for payload_size in [64, 1024]:
        loop = loop_factory()
        asyncio.set_event_loop(loop)
        gc.collect()
        
        try:
            result = loop.run_until_complete(
                benchmark_echo_server(
                    loop_type,
                    num_clients=10,
                    requests_per_client=50,
                    payload_size=payload_size
                )
            )
            results.append(result)
            print(f"  {result.name}: {result.requests_per_sec:.0f} req/s, "
                  f"p99={result.p99_latency_us:.0f}µs")
        finally:
            loop.close()
    
    # HTTP server benchmark
    loop = loop_factory()
    asyncio.set_event_loop(loop)
    gc.collect()
    
    try:
        result = loop.run_until_complete(
            benchmark_http_server(loop_type, num_clients=10, requests_per_client=50)
        )
        results.append(result)
        print(f"  {result.name}: {result.requests_per_sec:.0f} req/s, "
              f"p99={result.p99_latency_us:.0f}µs")
    finally:
        loop.close()
    
    return results


def run_all_server_benchmarks() -> dict:
    """Run server benchmarks for all available event loops."""
    results = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "python_version": sys.version,
            "platform": sys.platform,
            "benchmark_type": "server",
        },
        "benchmarks": {}
    }
    
    # Standard asyncio
    print("\n[asyncio] Running server benchmarks...")
    results["benchmarks"]["asyncio"] = [
        asdict(r) for r in run_server_benchmarks_with_loop("asyncio", asyncio.new_event_loop)
    ]
    
    # uvloop
    if UVLOOP_AVAILABLE:
        print("\n[uvloop] Running server benchmarks...")
        results["benchmarks"]["uvloop"] = [
            asdict(r) for r in run_server_benchmarks_with_loop("uvloop", uvloop.new_event_loop)
        ]
    
    # uringcore
    try:
        import uringcore
        print("\n[uringcore] Running server benchmarks...")
        results["benchmarks"]["uringcore"] = [
            asdict(r) for r in run_server_benchmarks_with_loop(
                "uringcore", 
                lambda: uringcore.EventLoopPolicy().new_event_loop()
            )
        ]
    except ImportError as e:
        print(f"Skipping uringcore: {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error running uringcore: {e}")
    
    return results


def print_server_comparison(results: dict):
    """Print server benchmark comparison."""
    benchmarks = results.get("benchmarks", {})
    if not benchmarks:
        return
    
    loops = list(benchmarks.keys())
    first_loop = loops[0]
    bench_names = [b["name"] for b in benchmarks[first_loop]]
    
    print("\n" + "=" * 90)
    print("Server Performance Comparison")
    print("=" * 90)
    
    # Requests per second
    print("\nRequests per Second (higher is better):")
    header = f"{'Benchmark':<20}"
    for loop in loops:
        header += f" | {loop:>12}"
    print(header)
    print("-" * len(header))
    
    for bench_name in bench_names:
        row = f"{bench_name:<20}"
        for loop in loops:
            for b in benchmarks[loop]:
                if b["name"] == bench_name:
                    row += f" | {b['requests_per_sec']:>10.0f}/s"
                    break
        print(row)
    
    # P99 Latency
    print("\nP99 Latency (lower is better):")
    header = f"{'Benchmark':<20}"
    for loop in loops:
        header += f" | {loop:>12}"
    print(header)
    print("-" * len(header))
    
    for bench_name in bench_names:
        row = f"{bench_name:<20}"
        for loop in loops:
            for b in benchmarks[loop]:
                if b["name"] == bench_name:
                    row += f" | {b['p99_latency_us']:>10.0f}µs"
                    break
        print(row)
    
    print("=" * 90)


def save_server_results(results: dict, output_dir: Path = None) -> Path:
    """Save server benchmark results."""
    if output_dir is None:
        output_dir = Path(__file__).parent / "results"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = output_dir / f"server_benchmark_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(results, f, indent=2)
    
    latest = output_dir / "server_latest.json"
    with open(latest, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to {filename}")
    return filename


def main():
    """Main entry point."""
    print("=" * 60)
    print("uringcore Server Benchmark Suite")
    print("=" * 60)
    print(f"Python: {sys.version}")
    print(f"Platform: {sys.platform}")
    
    results = run_all_server_benchmarks()
    
    output_dir = Path(__file__).parent / "results"
    save_server_results(results, output_dir)
    
    print_server_comparison(results)
    
    print("\nServer benchmark complete!")


if __name__ == "__main__":
    main()
