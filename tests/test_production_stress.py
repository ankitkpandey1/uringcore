"""Production-grade stress test with memory leak detection.

Simulates a real-world API workload with:
- CRUD operations with varying I/O waits
- Thousands of concurrent requests
- Long-running duration (configurable up to 15+ minutes)
- Continuous metrics collection
- Memory leak detection

Usage:
    # Quick test (1 minute)
    pytest tests/test_production_stress.py -v -k quick
    
    # Full stress test (10 minutes)
    pytest tests/test_production_stress.py -v -k full
    
    # Extended test (15 minutes)
    python tests/test_production_stress.py --duration=900

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import gc
import os
import random
import resource
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pytest

import uringcore
from uringcore import get_metrics


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class StressConfig:
    """Configuration for stress test."""
    duration_seconds: int = 60  # Total test duration
    concurrent_clients: int = 50  # Number of concurrent clients
    operations_per_client: int = 100  # Operations per client before reconnect
    min_io_delay_ms: float = 1  # Minimum simulated I/O delay
    max_io_delay_ms: float = 50  # Maximum simulated I/O delay
    payload_size_min: int = 64  # Minimum payload size
    payload_size_max: int = 4096  # Maximum payload size
    sample_interval_seconds: float = 1.0  # Metrics sampling interval
    memory_growth_threshold_mb: float = 50  # Max allowed memory growth


# ============================================================================
# Metrics Collection
# ============================================================================

@dataclass
class MetricsSample:
    """Single metrics sample."""
    timestamp: float
    memory_rss_mb: float
    memory_vms_mb: float
    buffers_total: int
    buffers_free: int
    buffers_in_use: int
    fd_count: int
    fd_inflight: int
    operations_completed: int
    errors: int
    latency_avg_ms: float


@dataclass
class StressResult:
    """Results from stress test."""
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    samples: List[MetricsSample] = field(default_factory=list)
    memory_leak_detected: bool = False
    start_memory_mb: float = 0
    end_memory_mb: float = 0
    peak_memory_mb: float = 0


def get_memory_usage_mb() -> tuple:
    """Get current memory usage (RSS, VMS) in MB."""
    try:
        with open('/proc/self/status', 'r') as f:
            rss = vms = 0
            for line in f:
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) / 1024
                elif line.startswith('VmSize:'):
                    vms = int(line.split()[1]) / 1024
            return rss, vms
    except:
        # Fallback using resource module
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_maxrss / 1024, 0


# ============================================================================
# Simulated CRUD Operations
# ============================================================================

class CRUDSimulator:
    """Simulates database CRUD operations with varying I/O wait times."""
    
    OPERATIONS = ['CREATE', 'READ', 'UPDATE', 'DELETE', 'QUERY']
    WEIGHTS = [0.2, 0.4, 0.2, 0.1, 0.1]  # Read-heavy workload
    
    def __init__(self, config: StressConfig):
        self.config = config
        self.data_store: Dict[str, bytes] = {}
    
    async def execute_operation(self) -> tuple:
        """Execute a random CRUD operation with simulated I/O delay."""
        op = random.choices(self.OPERATIONS, weights=self.WEIGHTS)[0]
        
        # Simulated I/O delay (database latency)
        delay = random.uniform(
            self.config.min_io_delay_ms / 1000,
            self.config.max_io_delay_ms / 1000
        )
        await asyncio.sleep(delay)
        
        # Generate payload
        payload_size = random.randint(
            self.config.payload_size_min,
            self.config.payload_size_max
        )
        
        key = f"key_{random.randint(0, 1000)}"
        
        if op == 'CREATE':
            data = os.urandom(payload_size)
            self.data_store[key] = data
            return op, len(data), data
        elif op == 'READ':
            data = self.data_store.get(key, b"NOT_FOUND")
            return op, len(data), data
        elif op == 'UPDATE':
            data = os.urandom(payload_size)
            self.data_store[key] = data
            return op, len(data), data
        elif op == 'DELETE':
            self.data_store.pop(key, None)
            return op, 0, b"DELETED"
        else:  # QUERY
            results = list(self.data_store.values())[:10]
            data = b"".join(results)
            return op, len(data), data


# ============================================================================
# Stress Test Server
# ============================================================================

class StressServer:
    """Stress test server simulating a database-backed API."""
    
    def __init__(self, config: StressConfig):
        self.config = config
        self.crud = CRUDSimulator(config)
        self.server = None
        self.port = 0
        self.operation_count = 0
        self.error_count = 0
    
    async def handle_client(self, reader, writer):
        """Handle client connection with CRUD operations."""
        try:
            while True:
                # Read request
                data = await reader.read(4096)
                if not data:
                    break
                
                # Execute CRUD operation
                op, size, result = await self.crud.execute_operation()
                self.operation_count += 1
                
                # Send response
                response = f"{op}:{size}:".encode() + result[:1024]
                writer.write(response)
                await writer.drain()
        except Exception as e:
            self.error_count += 1
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
    
    async def start(self):
        """Start the stress test server."""
        self.server = await asyncio.start_server(
            self.handle_client,
            '127.0.0.1',
            0,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port
    
    async def stop(self):
        """Stop the server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()


# ============================================================================
# Stress Test Client
# ============================================================================

class StressClient:
    """Stress test client."""
    
    def __init__(self, client_id: int, port: int, config: StressConfig, result: StressResult):
        self.client_id = client_id
        self.port = port
        self.config = config
        self.result = result
    
    async def run(self, stop_event: asyncio.Event):
        """Run client operations until stopped."""
        while not stop_event.is_set():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', self.port),
                    timeout=5.0
                )
                
                try:
                    for _ in range(self.config.operations_per_client):
                        if stop_event.is_set():
                            break
                        
                        # Generate random request payload
                        payload_size = random.randint(
                            self.config.payload_size_min,
                            self.config.payload_size_max
                        )
                        payload = os.urandom(payload_size)
                        
                        start_time = time.perf_counter()
                        
                        try:
                            writer.write(payload)
                            await writer.drain()
                            
                            response = await asyncio.wait_for(
                                reader.read(4096),
                                timeout=5.0
                            )
                            
                            end_time = time.perf_counter()
                            latency_ms = (end_time - start_time) * 1000
                            
                            self.result.total_operations += 1
                            self.result.successful_operations += 1
                            self.result.total_bytes_sent += payload_size
                            self.result.total_bytes_received += len(response)
                            self.result.latencies_ms.append(latency_ms)
                            
                        except Exception as e:
                            self.result.total_operations += 1
                            self.result.failed_operations += 1
                            break
                            
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except:
                        pass
                        
            except Exception as e:
                await asyncio.sleep(0.1)  # Backoff on connection error


# ============================================================================
# Metrics Collector
# ============================================================================

class StressMetricsCollector:
    """Collects metrics during stress test."""
    
    def __init__(self, loop, result: StressResult, config: StressConfig):
        self.loop = loop
        self.result = result
        self.config = config
        self.running = False
    
    async def collect_loop(self, stop_event: asyncio.Event):
        """Collect metrics until stopped."""
        self.running = True
        
        while not stop_event.is_set():
            try:
                await asyncio.sleep(self.config.sample_interval_seconds)
                
                rss, vms = get_memory_usage_mb()
                
                # Get uringcore metrics
                metrics = get_metrics(self.loop)
                
                sample = MetricsSample(
                    timestamp=time.time(),
                    memory_rss_mb=rss,
                    memory_vms_mb=vms,
                    buffers_total=metrics.buffers_total if metrics else 0,
                    buffers_free=metrics.buffers_free if metrics else 0,
                    buffers_in_use=metrics.buffers_in_use if metrics else 0,
                    fd_count=metrics.fd_count if metrics else 0,
                    fd_inflight=metrics.fd_inflight if metrics else 0,
                    operations_completed=self.result.successful_operations,
                    errors=self.result.failed_operations,
                    latency_avg_ms=sum(self.result.latencies_ms[-100:]) / max(len(self.result.latencies_ms[-100:]), 1),
                )
                
                self.result.samples.append(sample)
                self.result.peak_memory_mb = max(self.result.peak_memory_mb, rss)
                
            except Exception as e:
                pass
        
        self.running = False


# ============================================================================
# Main Stress Test
# ============================================================================

async def run_stress_test(config: StressConfig) -> StressResult:
    """Run the complete stress test."""
    result = StressResult()
    
    # Record starting memory
    gc.collect()
    result.start_memory_mb, _ = get_memory_usage_mb()
    result.peak_memory_mb = result.start_memory_mb
    
    # Get event loop
    loop = asyncio.get_event_loop()
    
    # Start server
    server = StressServer(config)
    port = await server.start()
    
    # Create stop event
    stop_event = asyncio.Event()
    
    # Start metrics collector
    collector = StressMetricsCollector(loop, result, config)
    collector_task = asyncio.create_task(collector.collect_loop(stop_event))
    
    # Start clients
    clients = [
        StressClient(i, port, config, result)
        for i in range(config.concurrent_clients)
    ]
    
    client_tasks = [
        asyncio.create_task(client.run(stop_event))
        for client in clients
    ]
    
    # Run for specified duration
    await asyncio.sleep(config.duration_seconds)
    
    # Signal stop
    stop_event.set()
    
    # Wait for tasks to complete
    await asyncio.gather(*client_tasks, return_exceptions=True)
    await collector_task
    
    # Stop server
    await server.stop()
    
    # Final metrics
    gc.collect()
    result.end_memory_mb, _ = get_memory_usage_mb()
    
    # Check for memory leak
    memory_growth = result.end_memory_mb - result.start_memory_mb
    if memory_growth > config.memory_growth_threshold_mb:
        result.memory_leak_detected = True
    
    return result


def print_report(result: StressResult, config: StressConfig):
    """Print stress test report."""
    print("\n" + "=" * 70)
    print("STRESS TEST REPORT")
    print("=" * 70)
    
    print(f"\nDuration: {config.duration_seconds}s")
    print(f"Concurrent Clients: {config.concurrent_clients}")
    
    print("\n--- Operations ---")
    print(f"Total Operations: {result.total_operations:,}")
    print(f"Successful: {result.successful_operations:,}")
    print(f"Failed: {result.failed_operations:,}")
    print(f"Throughput: {result.total_operations / config.duration_seconds:.1f} ops/s")
    
    print("\n--- Data Transfer ---")
    print(f"Bytes Sent: {result.total_bytes_sent / 1024 / 1024:.2f} MB")
    print(f"Bytes Received: {result.total_bytes_received / 1024 / 1024:.2f} MB")
    
    if result.latencies_ms:
        latencies = sorted(result.latencies_ms)
        print("\n--- Latency ---")
        print(f"Min: {latencies[0]:.2f} ms")
        print(f"p50: {latencies[len(latencies)//2]:.2f} ms")
        print(f"p99: {latencies[int(len(latencies)*0.99)]:.2f} ms")
        print(f"Max: {latencies[-1]:.2f} ms")
    
    print("\n--- Memory ---")
    print(f"Start: {result.start_memory_mb:.1f} MB")
    print(f"End: {result.end_memory_mb:.1f} MB")
    print(f"Peak: {result.peak_memory_mb:.1f} MB")
    print(f"Growth: {result.end_memory_mb - result.start_memory_mb:.1f} MB")
    print(f"Leak Detected: {'YES ⚠️' if result.memory_leak_detected else 'NO ✅'}")
    
    if result.samples:
        print("\n--- Buffer Pool (Final) ---")
        last = result.samples[-1]
        print(f"Total Buffers: {last.buffers_total}")
        print(f"Free Buffers: {last.buffers_free}")
        print(f"In-Use Buffers: {last.buffers_in_use}")
        print(f"FD Count: {last.fd_count}")
    
    print("\n" + "=" * 70)


# ============================================================================
# Pytest Tests
# ============================================================================

@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


class TestProductionStress:
    """Production stress tests."""

    @pytest.mark.asyncio
    async def test_quick_stress(self, event_loop):
        """Quick 30-second stress test."""
        config = StressConfig(
            duration_seconds=30,
            concurrent_clients=20,
            operations_per_client=50,
        )
        
        result = await run_stress_test(config)
        print_report(result, config)
        
        # Assertions
        assert result.successful_operations > 0, "Should complete some operations"
        assert not result.memory_leak_detected, f"Memory leak: grew {result.end_memory_mb - result.start_memory_mb:.1f} MB"
        assert result.failed_operations < result.successful_operations * 0.1, "Error rate too high"

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_full_stress(self, event_loop):
        """Full 10-minute stress test."""
        config = StressConfig(
            duration_seconds=600,  # 10 minutes
            concurrent_clients=50,
            operations_per_client=100,
        )
        
        result = await run_stress_test(config)
        print_report(result, config)
        
        assert not result.memory_leak_detected
        assert result.successful_operations > 10000


# ============================================================================
# CLI Entry Point
# ============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Production Stress Test")
    parser.add_argument("--duration", type=int, default=60, help="Duration in seconds")
    parser.add_argument("--clients", type=int, default=50, help="Concurrent clients")
    parser.add_argument("--ops", type=int, default=100, help="Operations per client")
    args = parser.parse_args()
    
    config = StressConfig(
        duration_seconds=args.duration,
        concurrent_clients=args.clients,
        operations_per_client=args.ops,
    )
    
    print(f"Starting stress test: {args.duration}s, {args.clients} clients")
    
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    
    result = asyncio.run(run_stress_test(config))
    print_report(result, config)
    
    sys.exit(1 if result.memory_leak_detected else 0)
