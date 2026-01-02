# uringcore Architecture

## Design Philosophy

The uringcore project implements a completion-driven event loop for Python's asyncio framework. Unlike traditional event loops that rely on readiness notification (epoll/kqueue), this implementation leverages the Linux io_uring interface to receive completion events directly from the kernel.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Python Application                         │
│                    (asyncio coroutines)                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     UringEventLoop                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Scheduler  │  │  Timer Heap  │  │  Transport Registry    │ │
│  └─────────────┘  └──────────────┘  └────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UringCore (Rust/PyO3)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │  BufferPool  │  │    Ring      │  │   FDStateManager      │ │
│  │  (mmap/mlock)│  │  (io_uring)  │  │   (per-FD state)      │ │
│  └──────────────┘  └──────────────┘  └───────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Linux Kernel (5.11+)                         │
│                      io_uring subsystem                         │
│        (Advanced features like RECV_MULTI optimal on 5.19+)     │
└─────────────────────────────────────────────────────────────────┘
```

---

## io_uring vs epoll: The Fundamental Difference

### The Readiness Model (epoll)

Traditional asyncio uses epoll, which follows a **readiness notification** model:

```
Application                    Kernel
    │                            │
    │──── epoll_wait() ─────────>│  (1) Wait for readiness
    │<─── "fd 5 is readable" ────│  (2) Kernel says "you CAN read"
    │──── read(fd=5) ───────────>│  (3) Actually read data (SYSCALL!)
    │<─── data ─────────────────│  (4) Receive data
```

**Problem**: Two syscalls per I/O operation - one to check readiness, one to transfer data.

### The Completion Model (io_uring)

io_uring uses a **completion notification** model:

```
Application                    Kernel
    │                            │
    │──── submit(recv, fd=5) ───>│  (1) Submit request to SQ
    │     [continue other work]  │  (2) Kernel does I/O async
    │<─── completion + data ─────│  (3) CQ contains actual data
```

**Advantage**: Near-zero syscalls on the hot path when SQPOLL is enabled; otherwise batched `io_uring_enter` submissions occur.

### Why This Matters for Performance

| Operation | epoll | io_uring |
|-----------|-------|----------|
| Check readiness | 1 syscall | 0 (SQPOLL) / batched |
| Data transfer | 1 syscall | 0 (pre-submitted) |
| Context switches | 2 per I/O | near-zero (kernel async) |
| CPU cache pollution | High | Low |

### The Submission Queue (SQ) and Completion Queue (CQ)

```
User Space                          Kernel Space
┌───────────────┐                 ┌───────────────┐
│  Application  │                 │    Kernel     │
│               │                 │               │
│  ┌─────────┐  │   shared mmap   │  ┌─────────┐  │
│  │   SQ    │──┼────────────────>│  │   SQ    │  │
│  │ (submit)│  │                 │  │ (poll)  │  │
│  └─────────┘  │                 │  └─────────┘  │
│               │                 │       │       │
│  ┌─────────┐  │                 │       ▼       │
│  │   CQ    │<─┼────────────────┤│   I/O ops    │  │
│  │ (poll)  │  │   shared mmap   │  ┌─────────┐  │
│  └─────────┘  │                 │  │   CQ    │  │
│               │                 │  │ (write) │  │
└───────────────┘                 │  └─────────┘  │
                                  └───────────────┘
```

Both queues are memory-mapped, so no copying is needed between user and kernel space.

---

## Core Components

### BufferPool

The `BufferPool` manages pre-allocated, page-aligned memory regions registered with io_uring for zero-copy I/O operations.

**Implementation Details:**

```rust
pub struct BufferPool {
    memory: *mut u8,           // mmap'd memory region
    buffer_size: usize,        // Size of each buffer (64KB default)
    buffer_count: u16,         // Number of buffers (1024 default)
    free_list: Vec<u16>,       // Available buffer indices
    quarantine: VecDeque<(u16, Instant)>,  // Recently freed buffers
    generation: AtomicU64,     // Fork detection
}
```

**Design Decisions:**

- **mmap Allocation**: Direct memory mapping bypasses userspace allocator overhead and ensures page alignment required by io_uring buffer registration.

- **mlock**: Pinning buffers in physical memory prevents page faults during I/O operations, reducing latency variance.

- **Quarantine Mechanism**: Released buffers enter a 5ms quarantine period before reuse. This design prevents race conditions between Python's garbage collector and io_uring's asynchronous buffer access.

- **Generation ID**: Each buffer carries a generation identifier. After process fork, the generation increments, invalidating all pre-fork buffers and preventing use-after-fork corruption.

### Ring

The `Ring` component wraps the io_uring instance and manages kernel communication.

**Key Operations:**

```rust
impl Ring {
    pub fn prep_recv(&mut self, fd: RawFd, buf: *mut u8, len: u32, gen: u16);
    pub fn prep_send(&mut self, fd: RawFd, buf: *const u8, len: u32, gen: u16);
    pub fn prep_accept(&mut self, fd: RawFd, gen: u16);
    pub fn prep_connect_with_timeout(&mut self, fd: RawFd, addr: *const sockaddr, 
                                      addr_len: socklen_t, timeout_ms: u64, gen: u16);
    pub fn submit(&mut self) -> Result<u32>;
    pub fn drain_completions(&mut self) -> Vec<CompletionEntry>;
}
```

**Design Decisions:**

- **SQPOLL Mode with Fallback**: When available (`CAP_SYS_ADMIN` or kernel 5.12+), SQPOLL enables the kernel to poll the submission queue autonomously, eliminating `io_uring_enter` syscalls on the submission path. The implementation gracefully degrades to batched submission when SQPOLL is unavailable.

- **eventfd Integration**: Rather than Python polling the completion queue, the Rust core drains CQEs and signals completion availability to Python by writing to an `eventfd` that the Python loop monitors. **Note:** The kernel does not write to this eventfd; the Rust core is solely responsible for signaling. The Python event loop waits on this single file descriptor, maintaining compatibility with asyncio's selector-based architecture while receiving io_uring completions. Selectors are retained only as a compatibility surface; they are not authoritative for correctness — the Rust completion stream is the source of truth.

- **RECV_MULTI Kernel Maturity**: Advanced features such as `RECV_MULTI` reach best performance and stability on kernel versions 5.19+ (and 6.x); earlier kernels may provide reduced functionality.

- **User Data Encoding**: Each submission encodes the file descriptor, operation type, and generation ID into the 64-bit user_data field:

```rust
// user_data layout (64 bits):
// [63:48] generation (16 bits)
// [47:40] op_type (8 bits)  
// [39:32] reserved (8 bits)
// [31:0]  fd (32 bits)

pub const fn encode_user_data(fd: i32, op_type: OpType, generation: u16) -> u64 {
    let fd_part = (fd as u32) as u64;
    let op_part = (op_type as u64) << 32;
    let gen_part = (generation as u64) << 48;
    fd_part | op_part | gen_part
}
```

### FDStateManager

Per-file-descriptor state tracking enables sophisticated flow control and resource management.

**State Machine:**

```
                 ┌──────────┐
                 │  Idle    │
                 └────┬─────┘
                      │ submit()
                      ▼
            ┌─────────────────┐
            │    In-Flight    │
            │  (credits > 0)  │
            └────────┬────────┘
                     │ completion
                     ▼
              ┌─────────────┐
              │  Completed  │──── rearm ────┐
              └─────────────┘               │
                     ▲                      │
                     └──────────────────────┘
```

**Design Decisions:**

- **Credit-Based Backpressure**: Each FD maintains a credit budget limiting concurrent in-flight operations. This mechanism prevents buffer exhaustion under high load and provides the foundation for flow control.

- **Sovereign State Machine**: Each FD operates independently with its own buffer queue and inflight count. This design isolates failures and enables fine-grained resource management.

- **Generation Validation**: State operations validate the current generation ID, rejecting operations from stale (pre-fork) contexts. `generation_id` must be validated on every CQE, every buffer return, and before any Python callback; mismatches are dropped and logged.

**Per-FD FIFO Invariant:** Data is delivered to Python in strict per-FD FIFO order. Partial-consumption uses `pending_offset` to preserve ordering across buffer boundaries.

---

## Building a Rust-Python Hybrid with PyO3

### Why Rust for the Core?

| Requirement | Python | Rust |
|-------------|--------|------|
| Memory safety | GC (unpredictable) | Compile-time (zero-cost) |
| System calls | ctypes/cffi (overhead) | Native (zero overhead) |
| Unsafe operations | Possible but hard | Explicit and auditable |
| Performance | Interpreted | Native code |

### PyO3 Integration Architecture

```
Python                           Rust (via PyO3)
┌─────────────────┐             ┌─────────────────┐
│  UringEventLoop │             │   UringCore     │
│  (loop.py)      │────────────>│   #[pyclass]    │
│                 │  calls       │                 │
│  - _run_once()  │             │  - submit_recv  │
│  - _process_    │<────────────│  - submit_send  │
│    completions()│  returns     │  - drain_compl. │
└─────────────────┘  PyObject   └─────────────────┘
```

**Key PyO3 Patterns Used:**

```rust
#[pyclass]
pub struct UringCore {
    ring: Ring,
    buffer_pool: BufferPool,
    fd_states: FDStateManager,
    inflight_recv_buffers: Mutex<HashMap<i32, u16>>,
}

#[pymethods]
impl UringCore {
    #[new]
    fn new() -> PyResult<Self> { ... }
    
    fn submit_recv(&mut self, fd: i32) -> PyResult<()> { ... }
    
    fn drain_completions<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<PyObject>> {
        // Returns list of (fd, op_type, result, data) tuples
    }
}
```

**GIL Considerations:**

- All I/O operations are submitted without holding the GIL
- The GIL is only held when converting data to Python objects
- This allows other Python threads to run during I/O

### Python Layer Responsibilities

```python
class UringEventLoop(asyncio.AbstractEventLoop):
    def _run_once(self):
        # 1. Calculate timeout from scheduled callbacks
        timeout = self._calculate_timeout()
        
        # 2. Wait for io_uring completions via epoll on eventfd
        events = self._epoll.poll(timeout)
        
        # 3. Process completions
        for fd, event_mask in events:
            if fd == self._core.event_fd:
                self._core.drain_eventfd()
                self._process_completions()
            else:
                # Reader/writer callbacks (for 3rd party compat)
                self._dispatch_callbacks(fd, event_mask)
        
        # 4. Run scheduled callbacks
        self._run_scheduled()
        
        # 5. Run ready callbacks
        self._run_ready()
```

---

## Comparative Performance Study

### Measurement Methodology

```python
# Benchmark: TCP echo server
# Payload: 64 bytes
# Iterations: 500 requests
# Warmup: 100 iterations

async def benchmark():
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    
    for _ in range(500):
        start = time.perf_counter_ns()
        writer.write(payload)
        await writer.drain()
        await reader.read(64)
        end = time.perf_counter_ns()
        latencies.append(end - start)
```

### Performance Breakdown

| Component | asyncio + epoll | uringcore |
|-----------|-----------------|-----------|
| Readiness check syscall | ~1000ns | 0ns |
| I/O syscall | ~1000ns | 0ns (pre-submitted) |
| Buffer allocation | ~500ns | 0ns (pre-allocated) |
| Context switch | ~2000ns | ~500ns (batched) |
| **Total overhead** | **~4500ns** | **~500ns** |

### Results

| Metric | asyncio | uringcore | Improvement |
|--------|---------|-----------|-------------|
| Throughput | 11,317 req/s | 15,394 req/s | **+36%** |
| p50 latency | 83 µs | 58 µs | **-30%** |
| p99 latency | 181 µs | 121 µs | **-33%** |

### Reproducibility Metadata

| Parameter | Value |
|-----------|-------|
| **CPU** | AMD Ryzen 7 9700X 8-Core @ 3.80 GHz |
| **Cores used** | 1 (single-threaded benchmark) |
| **Kernel** | 6.6.87.2-microsoft-standard-WSL2 |
| **Python** | 3.13.3 |
| **Payload** | 64 bytes |
| **Iterations** | 500 requests (after 100 warmup) |
| **Client** | Single TCP connection, sequential requests |
| **Flags** | TCP_NODELAY enabled, GC disabled during measurement |
| **Command** | `python benchmarks/server_benchmark.py` |

---

## Completion-Driven Virtual Readiness

Traditional asyncio event loops operate on readiness notifications: the selector indicates when a file descriptor can perform I/O without blocking, then the application issues the actual I/O syscall.

uringcore inverts this model:

1. **Submission Phase**: The application submits I/O requests to io_uring's submission queue. No data transfer occurs yet.

2. **Kernel Processing**: The kernel executes I/O operations asynchronously, independent of the application.

3. **Completion Phase**: Completed operations appear in the completion queue. The application receives actual data, not merely permission to read.

This inversion eliminates the syscall between readiness and I/O, reducing latency and CPU overhead.

---

## Fork Safety

Unix fork semantics present challenges for io_uring. A forked child inheriting the parent's io_uring instance risks corruption, as kernel-side state references the parent's address space.

The implementation detects fork through PID comparison:

1. **Detection**: Before each operation, the current PID is compared against the stored original PID.

2. **Teardown**: Upon fork detection, all Ring resources are released and the buffer pool's generation ID increments.

3. **Reinitialization**: The child process creates fresh io_uring instances, ensuring isolation from the parent.

---

## Memory Model

### Buffer Lifecycle

```
┌─────────┐     ┌──────────┐     ┌────────────┐     ┌─────────────┐
│  Free   │────>│ In-Flight│────>│  Completed │────>│ Quarantine  │
│  List   │     │ (kernel) │     │  (Python)  │     │  (5ms wait) │
└─────────┘     └──────────┘     └────────────┘     └─────────────┘
     ▲                                                      │
     └──────────────────────────────────────────────────────┘
```

### Zero-Copy Path

For receive operations:
1. Kernel writes directly into registered buffers
2. Completion provides buffer index and length
3. Python receives a memoryview over the buffer (no copy)
4. Buffer returns to pool after Python processing

For send operations:
Write-side zero-copy is opportunistic and restricted: only provably immutable buffers (immutable `bytes` or read-only `mmap`) may be submitted zero-copy. All other write buffers must be copied into Rust-owned pinned memory before submission.

### PyCapsule Lifetime Semantics

PyCapsule destructors must run on the Python loop thread (GIL). If a destructor can run on another thread, the destructor must enqueue a `return_buffer(buf_id)` call that the loop drains under GIL to safely recycle the buffer.

---

## Advanced Features

### IORING_OP_LINK_TIMEOUT

Connection timeouts are implemented using linked operations:

```rust
// Submit connect with linked timeout
ring.prep_connect_with_timeout(fd, addr, addr_len, 5000, gen);

// Internally creates two linked SQEs:
// 1. Connect operation with IOSQE_IO_LINK flag
// 2. LinkTimeout operation (cancels connect if it takes too long)
```

### Supported asyncio APIs

| Category | Methods |
|----------|---------|
| **TCP** | `create_server`, `create_connection`, `start_server` |
| **UDP** | `create_datagram_endpoint` |
| **Unix** | `create_unix_server`, `create_unix_connection` |
| **Subprocess** | `subprocess_exec`, `subprocess_shell` |
| **Signals** | `add_signal_handler`, `remove_signal_handler` |
| **Callbacks** | `add_reader`, `add_writer`, `call_soon`, `call_later`, `call_at` |
| **Executor** | `run_in_executor` |

---

## Performance Characteristics

| Aspect | Traditional (epoll) | io_uring |
|--------|---------------------|----------|
| Readiness Check | 1 syscall | 0 (SQPOLL) / batched |
| Data Transfer | 1 syscall | 0 (pre-submitted) |
| Buffer Allocation | Per-operation | Pre-allocated pool |
| Completion Notification | Per-FD poll | Batched via eventfd |

---

## Limitations and Trade-offs

1. **Linux Only**: io_uring is a Linux-specific interface. Cross-platform compatibility requires alternative implementations.

2. **Kernel Version**: Full functionality requires kernel 5.11+. Older kernels lack necessary io_uring features.

3. **Memory Footprint**: Pre-allocated buffer pools consume memory regardless of actual usage. Applications should tune pool sizes appropriately.

4. **Complexity**: The completion-driven model differs from traditional callback patterns, potentially complicating debugging.

5. **Seccomp / Container Restrictions**: If required syscalls or registrations fail under seccomp or container restrictions, uringcore auto-degrades to batched `io_uring_enter` mode or falls back to an epoll-based path (configurable). Failures are surfaced with actionable diagnostics listing missing syscalls.

---

## Observability

Expose runtime metrics (inflight buffers, queue lengths, completion latency, buffer reuse rate) and a dynamic throttle API for operators.

---

## Native Task Scheduling (Phase 3)

`uringcore` moves the scheduling logic entirely to Rust to reduce Python overhead.

### Components

1.  **UringTask**: A PyObject wrapping the coroutine. It implements a `_step(value, exc)` method (similar to `_run` in asyncio).
2.  **Scheduler**: A Rust `Mutex<VecDeque<PyObject>>` that stores tasks ready to run.
3.  **run_tick**: The main loop iteration logic in Rust that drains the scheduler queue and executes tasks.

**Optimization**:
- `call_soon` pushes directly to the Rust queue.
- `run_tick` consumes the queue in a single lock acquisition (batch drain).
- Tasks are executed without crossing the language boundary for queue management.

## Native Futures (Phase 5)

Traditional `asyncio.Future` is implemented in Python (with a C accelerator). `uringcore` implements `UringFuture` entirely in Rust (`#[pyclass]`).

### Key Optimizations

1.  **Direct State Access**: Rust code (completion handlers) can set the future's result/exception directly by calculating the memory offset of the state, bypassing Python method calls (`set_result`).
2.  **Inline Callbacks**: `add_done_callback` stores callbacks in a Rust `Vec`, avoiding Python list overhead.
3.  **No Loop Overhead**: `UringFuture` is tightly coupled with `Ring` completions, allowing 0-copy state updates from the completion queue.

## Memory Safety & Resource Management

### The `ENOMEM` Challenge

`io_uring` locks memory pages for registered buffers (`RLIMIT_MEMLOCK`). If the `Ring` is not dropped deterministically, these locks persist, leading to `ENOMEM` on subsequent loop creations (common in test suites).

**Solution**:
The `Ring` struct implements `Drop`, ensuring that `unregister_buffers()` is called whenever the ring is destroyed. This guarantees that locked memory is released back to the OS immediately, independent of Python's Garbage Collector timing.

### Reference Cycles

`UringCore` -> `Scheduler` -> `UringTask` -> `UringCore` (via loop).
To prevent memory leaks from these cycles, `UringCore` implements `shutdown()` (called by `loop.close()`) which explicitly clears the scheduler and futures map, breaking the cycle.

---

## Required CI Tests

(Unchanged)

---

## SOTA 2025 Optimizations

The following state-of-the-art optimizations have been implemented or are available:

### Implemented

| Optimization | Status | Kernel Requirement |
|--------------|--------|-------------------|
| **Asyncio Function Caching** | ✅ Active | N/A |
| **Native Timers** (`IORING_OP_TIMEOUT`) | ✅ Available | 5.4+ |
| **Multishot Recv** (`IORING_OP_RECV` + `RECV_MULTISHOT`) | ✅ Available | 5.19+ |
| **Batch Drain Scheduler** | ✅ Active | N/A |

### Available (Kernel Feature Detection)

| Optimization | API | Kernel Requirement |
|--------------|-----|-------------------|
| **Zero-Copy Send** | `prep_send_zc()` | 6.0+ |
| **Provided Buffer Ring** | `REGISTER_PBUF_RING` | 5.19+ |
| **Registered FDs** | `IOSQE_FIXED_FILE` | 5.1+ |

### Performance Results

| Metric | uringcore | uvloop | Speedup |
|--------|-----------|--------|---------|
| `sleep(0)` | 5.24 µs | 12.20 µs | **2.3x** |
| `create_task` | 8.97 µs | 13.46 µs | **1.5x** |
| `future_res` | 4.48 µs | 12.42 µs | **2.8x** |

---

## Future Work

1. **Registered FD Table**: Use `IORING_REGISTER_FILES` to eliminate per-op FD lookup overhead.
2. **Provided Buffer Ring**: Let kernel select buffers automatically via `REGISTER_PBUF_RING`.
3. **Zero-Copy Send**: Implement `IORING_OP_SEND_ZC` for large payloads (>4KB).
4. **nogil Python 3.13+**: Test and optimize for free-threaded Python.
5. **eBPF Integration**: XDP for packet steering to bypass kernel network stack.

---

## References

1. Axboe, J. "Efficient IO with io_uring" (2019). https://kernel.dk/io_uring.pdf
2. Lord, D. "io_uring and networking in 2023". LWN.net. https://lwn.net/Articles/930536/
3. Python Software Foundation. "asyncio — Asynchronous I/O". Python 3 Documentation.
4. PyO3 Contributors. "PyO3 User Guide". https://pyo3.rs/
5. Love, R. "Linux Kernel Development" (2010). Addison-Wesley Professional.
