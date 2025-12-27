# uringcore Performance Benchmarks

## Overview

This document presents performance measurements comparing uringcore, uvloop, and standard asyncio. All values represent actual measurements from the benchmark suite.

## Methodology

All benchmarks execute on Linux systems with kernel 5.11+. Each test runs multiple iterations with warmup, and garbage collection is disabled during measurement to reduce variance.

### Test Environment

- **Platform**: Linux x86_64
- **Python**: 3.13.3
- **Measurement**: `time.perf_counter_ns()` with GC disabled

### Benchmark Categories

| Category | Description | Iterations |
|----------|-------------|------------|
| sleep(0) | Minimal async context switch | 10,000 |
| create_task | Task creation and await | 5,000 |
| gather(10) | 10 concurrent tasks | 2,000 |
| gather(100) | 100 concurrent tasks | 500 |
| queue_put_get | asyncio.Queue cycle | 5,000 |
| event_set_wait | Event synchronization | 10,000 |
| lock_acquire | asyncio.Lock cycle | 10,000 |
| future_result | Future resolution | 10,000 |
| call_soon | Callback scheduling | 10,000 |

## Results

### Measured Performance (µs per operation)

| Benchmark | asyncio | uvloop | uringcore |
|-----------|---------|--------|-----------|
| sleep(0) | 4.84 | 11.74 | 4.73 |
| create_task | 6.09 | 12.89 | 6.11 |
| gather(10) | 22.59 | 24.08 | 22.85 |
| gather(100) | 162.51 | 115.76 | 169.05 |
| queue_put_get | 4.43 | 12.77 | 4.88 |
| event_set_wait | 3.77 | 11.68 | 4.09 |
| lock_acquire | 3.95 | 11.01 | 4.22 |
| future_result | 4.02 | 10.63 | 3.97 |
| call_soon | 6.05 | 11.33 | 5.74 |

### Operations per Second

| Benchmark | asyncio | uvloop | uringcore |
|-----------|---------|--------|-----------|
| sleep(0) | 206,612 | 85,179 | 211,526 |
| create_task | 164,204 | 77,580 | 163,772 |
| gather(10) | 44,269 | 41,534 | 43,762 |
| gather(100) | 6,153 | 8,639 | 5,915 |
| queue_put_get | 225,734 | 78,326 | 204,812 |
| event_set_wait | 265,252 | 85,588 | 244,249 |
| lock_acquire | 253,165 | 90,817 | 237,238 |
| future_result | 248,756 | 94,108 | 252,119 |
| call_soon | 165,289 | 88,231 | 174,182 |

## Analysis

### Observations

1. **Pure Async Primitives**: For Python async primitives (tasks, futures, events, locks), asyncio and uringcore perform similarly. These operations are handled in Python, not by the event loop's I/O engine.

2. **uvloop Overhead**: uvloop shows higher latency for pure async primitives. uvloop is optimized for network I/O, where its libuv backend provides significant benefits over epoll.

3. **gather(100) Exception**: uvloop performs better on gather(100), likely due to its optimized task scheduling in libuv.

### Where io_uring Provides Benefit

The uringcore architecture provides performance improvements for:

- **Network I/O**: Batched submissions and completions reduce syscalls
- **High Concurrency**: Completion queue eliminates per-operation overhead  
- **Zero-Copy**: Pre-registered buffers eliminate allocation/copy

These benefits are realized when the full network transport layer is integrated.

## Running Benchmarks

```bash
cd benchmarks
pip install matplotlib uvloop  # Optional dependencies
python benchmark_suite.py
```

### Output Files

- `results/latest.json` - Most recent results
- `results/comparison_chart.png` - Bar chart comparison
- `results/speedup_chart.png` - Speedup vs asyncio

## References

1. Axboe, J. (2019). "Efficient IO with io_uring". Kernel.org Documentation.
2. MagicStack Inc. "uvloop: Ultra fast asyncio event loop". GitHub Repository.
3. Python Software Foundation. "asyncio — Asynchronous I/O". Python Documentation.
