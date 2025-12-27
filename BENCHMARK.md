# uringcore Performance Benchmarks

## Overview

This document presents performance measurements comparing uringcore, uvloop, and standard asyncio. All values represent actual measurements from the benchmark suite using realistic workloads.

## Methodology

All benchmarks execute on Linux systems with kernel 5.11+. Each test runs multiple iterations with warmup, and garbage collection is disabled during measurement to reduce variance.

### Test Environment

- **Platform**: Linux x86_64
- **Python**: 3.13.3
- **Kernel**: 5.15+
- **Measurement**: `time.perf_counter_ns()` with GC disabled

## Results

### Server Workloads (Throughput)

Realistic TCP echo and HTTP-like server workloads with concurrent clients (10 clients).

| Benchmark | asyncio | uvloop | uringcore | Speedup (vs asyncio) |
|-----------|---------|--------|-----------|----------------------|
| **Echo Server (64B)** | 8,015 req/s | 4,850 req/s | **12,773 req/s** | **1.59x** |
| Echo Server (1KB) | 12,346 req/s | 4,766 req/s | 12,350 req/s | 1.00x |
| HTTP Server | 12,253 req/s | 4,908 req/s | 11,745 req/s | 0.96x |

### Server Workloads (Latency P99)

Lower is better.

| Benchmark | asyncio | uvloop | uringcore |
|-----------|---------|--------|-----------|
| **Echo Server (64B)** | 3,186 µs | 3,705 µs | **2,720 µs** |
| Echo Server (1KB) | 2,198 µs | 3,324 µs | **1,654 µs** |
| HTTP Server | 2,960 µs | 3,128 µs | **2,072 µs** |

### Async Primitives (Microseconds per op)

Lower is better.

| Benchmark | asyncio | uvloop | uringcore |
|-----------|---------|--------|-----------|
| sleep(0) | 4.84 | 11.74 | **4.73** |
| create_task | 6.09 | 12.89 | 6.11 |
| queue_put_get | 4.43 | 12.77 | 4.88 |
| future_result | 4.02 | 10.63 | **3.97** |

## Analysis

**uringcore** demonstrates a **1.6x throughput improvement** and **significantly lower P99 latency** for small packet workloads compared to standard asyncio. This performance gain is achieved through a hybrid event loop architecture that combines:

1. **io_uring Polling**: Efficient handling of high-concurrency event signaling.
2. **Selector Fallback**: Robust support for standard socket operations.
3. **Zero-Overhead Primitives**: Pure async operations match standard asyncio speed, avoiding the overhead seen in some alternative loops for simple tasks.

The hybrid architecture ensures that uringcore is both **faster** for high-frequency I/O and **fully compatible** with the existing asyncio ecosystem.

## Running Benchmarks

```bash
cd benchmarks
pip install matplotlib uvloop
python benchmark_suite.py   # Primitives
python server_benchmark.py  # Server workloads
```

## References

1. Axboe, J. (2019). "Efficient IO with io_uring". Kernel.org Documentation.
2. MagicStack Inc. "uvloop: Ultra fast asyncio event loop". GitHub Repository.
3. Python Software Foundation. "asyncio — Asynchronous I/O". Python Documentation.
