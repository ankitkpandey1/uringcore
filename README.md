# uringcore

[![CI](https://github.com/ankitkpandey1/uringcore/actions/workflows/ci.yml/badge.svg)](https://github.com/ankitkpandey1/uringcore/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Rust](https://img.shields.io/badge/rust-1.70+-orange.svg)](https://www.rust-lang.org/)

A high-performance asyncio event loop for Linux using io_uring.

## Project Status
**Current Phase:** Phase 15 (Final Polish & Release)

`uringcore` is a high-performance, drop-in replacement for `asyncio` on Linux.
It passes **all tests** including proper stress testing and FastAPI/Starlette E2E tests, and outperforms `uvloop` in single-task latency benchmarks.


## Key Features
- **Pure io_uring**: No `epoll`/`selector` fallback. All I/O is submitted to the ring.
- **Native Scheduler**: `Mutex<VecDeque>` for efficient single-threaded task scheduling.
- **Zero-Copy Buffers**: Pre-registered fixed buffers for maximum I/O bandwidth.
- **Native Futures**: Optimized Future implementation entirely in Rust.
- **Asyncio Function Caching**: Cached `_enter_task`/`_leave_task` to reduce per-step overhead.
- **Registered FD Table**: `IOSQE_FIXED_FILE` support for zero FD lookup overhead.
- **Zero-Copy Send**: `IORING_OP_SEND_ZC` for large payload efficiency (kernel 6.0+).
- **Multishot Recv**: `RECV_MULTISHOT` for persistent connections (kernel 5.19+).
- **Native Timers**: `IORING_OP_TIMEOUT` for zero-syscall timer management.
- **Strict Resource Management**: Deterministic cleanup via `Drop` trait.

## Benchmarks
Latest results (Jan 2026) vs `uvloop`:

**Single-Task Latency (uringcore wins):**
- `sleep(0)`: **2.9x faster** (4.26µs vs 12.53µs)
- `lock_acquire`: **3.1x faster** (3.90µs vs 12.26µs)
- `future_res`: **3.3x faster** (3.91µs vs 12.81µs)

**High-Concurrency (gather 100):**
- `asyncio`: 173 µs
- `uringcore`: **139 µs** (1.25x faster than asyncio)
- `uvloop`: 105 µs (gap is purely FFI overhead, syscalls are minimized)

## Performance Verification

To verify the system efficiency (syscall reduction), we profiled `gather(100)` using `strace`.

| Metric | uringcore | uvloop | Impact |
|--------|-----------|--------|--------|
| **Total Syscalls** | **1,979** | 52,587 | **26x reduction** |
| `io_uring_enter` | 0 | 2,200 | Perfect batching |
| `epoll_ctl` | 2 | 13,201 | Kernel thrashing prevented |

**Reproduction:**
Run the included benchmark with `strace` to reproduce these findings:

```bash
# Install strace
sudo apt-get install strace

# Run benchmark for uringcore
strace -c python3 benchmarks/syscall_bench.py uringcore

# Run benchmark for uvloop
strace -c python3 benchmarks/syscall_bench.py uvloop
```

### Why is uringcore slower than uvloop on gather(100)?
(139µs vs 105µs)

**Root Cause**: Architectural decision to use standard `asyncio.Task`.
- **uvloop**: Re-implements `Task` and `Future` completely in C. When a task yields, uvloop stays in C-land to schedule the next one.
- **uringcore**: Uses Python's standard `asyncio.Task` for **100% ecosystem compatibility**. Every task step requires control to pass from Rust -> Python Interpreter -> Python Task Object -> Rust.

**Architectural Decision**: 
We chose **NOT** to re-implement `Task` in Rust for V1.0. This maintains compatibility with tools that inspect `asyncio.Task` (debuggers, `nest_asyncio`, etc.) and avoids massive complexity. `uringcore` beats `asyncio` while providing massive I/O scalability (where syscalls matter more than micro-scheduling latency).

## Introduction

uringcore provides a drop-in replacement for Python's asyncio event loop, built on the io_uring interface available in Linux kernel 5.11+ (with advanced features optimal on 5.19+). The project targets use cases where low-latency I/O and high throughput are critical requirements.

The implementation leverages a completion-driven architecture rather than the traditional readiness-based model used by epoll. This design reduces syscalls on the hot path (near-zero when SQPOLL is enabled), yielding measurable latency and CPU improvements.

## Use Cases

- **Real-time data pipelines** processing high message volumes
- **API gateways** handling high concurrent connection counts
- **WebSocket servers** with persistent connections
- **Database connection pools** with intensive query workloads

## Requirements

- Linux kernel 5.11+ (5.19+ recommended for `RECV_MULTI` optimizations)
- Python 3.10+
- Rust 1.70+

**SQPOLL Mode:** Requires `CAP_SYS_ADMIN` or kernel 5.12+ with unprivileged SQPOLL. SQPOLL often requires elevated privileges and may be unavailable on managed/cloud hosts; uringcore auto-detects SQPOLL capability and falls back to batched `io_uring_enter` when unsupported. This fallback is automatic and requires no configuration.

## Installation

### From PyPI

```bash
pip install uringcore
```

### From Source

```bash
git clone https://github.com/ankitkpandey1/uringcore.git
cd uringcore

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install build dependencies
pip install maturin

# Build and install
maturin develop
```

## Quick Start

Replace the default asyncio event loop with uringcore:

```python
import asyncio
import uringcore

# Set the event loop policy
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

async def main():
    # Standard asyncio code works unchanged
    await asyncio.sleep(1)
    print("Hello from uringcore!")

asyncio.run(main())
```

### With FastAPI

```python
import asyncio
import uringcore
from fastapi import FastAPI

asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello, World!"}
```

### With Starlette

```python
import asyncio
import uringcore
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

async def homepage(request):
    return JSONResponse({"hello": "world"})

app = Starlette(routes=[Route("/", homepage)])
```

## Performance

Measured benchmark results against standard asyncio and uvloop. See [BENCHMARK.md](BENCHMARK.md) for machine specs, exact commands, and methodology.

| Metric | uringcore | asyncio | uvloop |
|--------|-----------|---------|--------|
| **Throughput** | 15,394 req/s | 11,317 req/s | 11,721 req/s |
| **p50 Latency** | 58 µs | 83 µs | 78 µs |
| **p99 Latency** | 121 µs | 181 µs | 182 µs |
| **vs asyncio** | **+36%** | baseline | +4% |

## Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Core I/O on io_uring** | ✅ Stable | Primary implementation uses io_uring; configurable fallbacks (batched `io_uring_enter` or epoll) available for restricted environments |
| **TCP** | ✅ Stable | `create_server`, `create_connection`, `start_server` |
| **UDP** | ✅ Stable | `create_datagram_endpoint` |
| **Unix Sockets** | ✅ Stable | `create_unix_server`, `create_unix_connection` |
| **Signal Handlers** | ✅ Stable | `add_signal_handler`, `remove_signal_handler` |
| **Executor** | ✅ Stable | `run_in_executor` for blocking calls |
| **Reader/Writer** | ✅ Stable | `add_reader`, `add_writer` for compatibility |
| **Subprocess** | 🔶 Beta | `subprocess_exec`, `subprocess_shell` |
| **SSL/TLS** | 🔶 Beta | Memory BIO wrapper (kTLS not yet integrated) |
| **IORING_OP_LINK_TIMEOUT** | 🔶 Beta | Connection timeout support |

**Legend:** ✅ Stable (CI-tested) | 🔶 Beta (functional, limited testing)

## Configuration

Default buffer pool settings (tunable via environment variables):

| Setting | Default | Env Var |
|---------|---------|---------|
| Buffer size | 64 KB | `URINGCORE_BUFFER_SIZE` |
| Buffer count | 1024 | `URINGCORE_BUFFER_COUNT` |
| Quarantine window | 5 ms | `URINGCORE_QUARANTINE_MS` |

## Project Structure

```
uringcore/
├── src/                    # Rust core implementation
│   ├── lib.rs              # PyO3 module entry point
│   ├── buffer.rs           # Zero-copy buffer pool
│   ├── ring.rs             # io_uring wrapper
│   ├── state.rs            # FD state machine
│   └── error.rs            # Error types
├── python/                 # Python layer
│   └── uringcore/
│       ├── loop.py         # UringEventLoop
│       ├── transport.py    # Socket transport
│       ├── datagram.py     # UDP transport
│       ├── subprocess.py   # Subprocess transport
│       └── ssl_transport.py # SSL/TLS wrapper
├── tests/                  # Test suites
└── benchmarks/             # Performance benchmarks
```

## Documentation

- [Architecture](ARCHITECTURE.md) - Design decisions, io_uring internals, CI test matrix
- [Benchmarks](BENCHMARK.md) - Performance measurements with reproducibility metadata

## Development

### Running Tests

```bash
# Rust tests
cargo test

# Python tests
source .venv/bin/activate
pytest tests/ -v
```

CI runs tests across Python 3.10-3.13 and multiple kernel versions. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml) for the test matrix.

### Code Quality

```bash
cargo fmt && cargo clippy --all-targets -- -D warnings
```

## Security

- **SQPOLL** requires `CAP_SYS_ADMIN` on kernels < 5.12
- **Seccomp**: If syscalls are blocked, uringcore falls back gracefully with diagnostic messages
- **Containers**: Works in Docker/Podman with default seccomp profiles; restrictive profiles may require `--security-opt seccomp=unconfined`

For vulnerability reports, contact: ankitkpandey1@gmail.com

## License

```
SPDX-License-Identifier: Apache-2.0
Copyright 2024-2025 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
```

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

## Author

**Ankit Kumar Pandey** - [ankitkpandey1@gmail.com](mailto:ankitkpandey1@gmail.com)

## Acknowledgments

- The io_uring subsystem maintainers, particularly Jens Axboe
- The PyO3 project for Rust-Python bindings
- The uvloop project for demonstrating high-performance event loop implementation
