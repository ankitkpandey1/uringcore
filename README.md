# uringloop

[![CI](https://github.com/ankitkpandey1/uringcore/actions/workflows/ci.yml/badge.svg)](https://github.com/ankitkpandey1/uringcore/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Rust](https://img.shields.io/badge/rust-1.70+-orange.svg)](https://www.rust-lang.org/)

A high-performance asyncio event loop for Linux using io_uring.

## Introduction

uringloop provides a drop-in replacement for Python's asyncio event loop, built on the io_uring interface available in Linux kernel 5.11+. The project targets use cases where low-latency I/O and high throughput are critical requirements.

The implementation leverages a completion-driven architecture rather than the traditional readiness-based model used by epoll. This design eliminates syscalls from the hot path, resulting in measurable performance improvements for network-intensive applications.

## Use Cases

- **High-frequency trading systems** requiring sub-millisecond latency
- **Real-time data pipelines** processing millions of messages per second
- **API gateways** handling high concurrent connection counts
- **WebSocket servers** with persistent connections
- **Database connection pools** with intensive query workloads

## Requirements

- Linux kernel 5.11+ (for `IORING_OP_PROVIDE_BUFFERS`)
- Python 3.10+
- Rust 1.70+

Optional:
- SQPOLL mode requires `CAP_SYS_ADMIN` or kernel 5.12+ with unprivileged SQPOLL

## Installation

### From PyPI (when published)

```bash
pip install uringloop
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

Replace the default asyncio event loop with uringloop:

```python
import asyncio
import uringloop

# Set the event loop policy
asyncio.set_event_loop_policy(uringloop.EventLoopPolicy())

async def main():
    # Standard asyncio code works unchanged
    await asyncio.sleep(1)
    print("Hello from uringloop!")

asyncio.run(main())
```

### With FastAPI

```python
import asyncio
import uringloop
from fastapi import FastAPI

asyncio.set_event_loop_policy(uringloop.EventLoopPolicy())

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Powered by uringloop"}
```

### With Starlette

```python
import asyncio
import uringloop
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

asyncio.set_event_loop_policy(uringloop.EventLoopPolicy())

async def homepage(request):
    return JSONResponse({"hello": "world"})

app = Starlette(routes=[Route("/", homepage)])
```

## Performance

Benchmark comparisons against uvloop and standard asyncio are available in [BENCHMARK.md](BENCHMARK.md).

Summary results:
- **3.8x** average speedup over asyncio
- **Near-zero** syscalls on the data path
- **Sub-millisecond** p99 latency under load

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
│   └── uringloop/
│       ├── __init__.py
│       ├── loop.py         # UringEventLoop
│       └── policy.py       # EventLoopPolicy
├── tests/                  # Test suites
│   ├── test_basic.py
│   └── e2e/
│       ├── starlette/
│       └── fastapi/
└── benchmarks/             # Performance benchmarks
```

## Documentation

- [Architecture](ARCHITECTURE.md) - Design decisions and system overview
- [Benchmarks](BENCHMARK.md) - Performance measurements and analysis

## Development

### Running Tests

```bash
# Rust tests
cargo test

# Python tests
source .venv/bin/activate
pytest tests/ -v

# All tests including e2e
pytest tests/ tests/e2e/ -v
```

### Code Quality

```bash
# Rust formatting and linting
cargo fmt
cargo clippy --all-targets -- -D warnings

# Python linting (if ruff/black installed)
ruff check .
```

## License

```
SPDX-License-Identifier: Apache-2.0
Copyright 2024 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
```

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

## Author

**Ankit Kumar Pandey** - [ankitkpandey1@gmail.com](mailto:ankitkpandey1@gmail.com)

## Acknowledgments

- The io_uring subsystem maintainers, particularly Jens Axboe
- The PyO3 project for Rust-Python bindings
- The uvloop project for demonstrating high-performance event loop implementation
