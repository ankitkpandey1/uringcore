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
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### BufferPool

The `BufferPool` manages pre-allocated, page-aligned memory regions registered with io_uring for zero-copy I/O operations.

**Design Decisions:**

- **mmap Allocation**: Direct memory mapping bypasses userspace allocator overhead and ensures page alignment required by io_uring buffer registration.

- **mlock**: Pinning buffers in physical memory prevents page faults during I/O operations, reducing latency variance.

- **Quarantine Mechanism**: Released buffers enter a 5ms quarantine period before reuse. This design prevents race conditions between Python's garbage collector and io_uring's asynchronous buffer access.

- **Generation ID**: Each buffer carries a generation identifier. After process fork, the generation increments, invalidating all pre-fork buffers and preventing use-after-fork corruption.

### Ring

The `Ring` component wraps the io_uring instance and manages kernel communication.

**Design Decisions:**

- **SQPOLL Mode with Fallback**: When available (`CAP_SYS_ADMIN` or kernel 5.12+), SQPOLL enables the kernel to poll the submission queue autonomously, eliminating `io_uring_enter` syscalls on the submission path. The implementation gracefully degrades to batched submission when SQPOLL is unavailable.

- **eventfd Integration**: Rather than polling the completion queue, an eventfd signals when completions are available. The Python event loop waits on this single file descriptor, maintaining compatibility with asyncio's selector-based architecture while receiving io_uring completions.

- **User Data Encoding**: Each submission encodes the file descriptor, operation type, and generation ID into the 64-bit user_data field. This approach eliminates lookup tables when processing completions.

### FDStateManager

Per-file-descriptor state tracking enables sophisticated flow control and resource management.

**Design Decisions:**

- **Credit-Based Backpressure**: Each FD maintains a credit budget limiting concurrent in-flight operations. This mechanism prevents buffer exhaustion under high load and provides the foundation for flow control.

- **Sovereign State Machine**: Each FD operates independently with its own buffer queue and inflight count. This design isolates failures and enables fine-grained resource management.

- **Generation Validation**: State operations validate the current generation ID, rejecting operations from stale (pre-fork) contexts.

## Completion-Driven Virtual Readiness

Traditional asyncio event loops operate on readiness notifications: the selector indicates when a file descriptor can perform I/O without blocking, then the application issues the actual I/O syscall.

uringcore inverts this model:

1. **Submission Phase**: The application submits I/O requests to io_uring's submission queue. No data transfer occurs yet.

2. **Kernel Processing**: The kernel executes I/O operations asynchronously, independent of the application.

3. **Completion Phase**: Completed operations appear in the completion queue. The application receives actual data, not merely permission to read.

This inversion eliminates the syscall between readiness and I/O, reducing latency and CPU overhead.

## Fork Safety

Unix fork semantics present challenges for io_uring. A forked child inheriting the parent's io_uring instance risks corruption, as kernel-side state references the parent's address space.

The implementation detects fork through PID comparison:

1. **Detection**: Before each operation, the current PID is compared against the stored original PID.

2. **Teardown**: Upon fork detection, all Ring resources are released and the buffer pool's generation ID increments.

3. **Reinitialization**: The child process creates fresh io_uring instances, ensuring isolation from the parent.

## Memory Model

### Buffer Lifecycle

```
┌─────────┐     ┌──────────┐     ┌────────────┐     ┌─────────────┐
│  Free   │────▶│ In-Flight│────▶│  Completed │────▶│ Quarantine  │
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

## Performance Characteristics

| Aspect | Traditional (epoll) | io_uring |
|--------|---------------------|----------|
| Readiness Check | 1 syscall | 0 syscalls |
| Data Transfer | 1 syscall | 0 syscalls (pre-submitted) |
| Buffer Allocation | Per-operation | Pre-allocated pool |
| Completion Notification | Per-FD poll | Batched via eventfd |

## Limitations and Trade-offs

1. **Linux Only**: io_uring is a Linux-specific interface. Cross-platform compatibility requires alternative implementations.

2. **Kernel Version**: Full functionality requires kernel 5.11+. Older kernels lack necessary io_uring features.

3. **Memory Footprint**: Pre-allocated buffer pools consume memory regardless of actual usage. Applications should tune pool sizes appropriately.

4. **Complexity**: The completion-driven model differs from traditional callback patterns, potentially complicating debugging.

## References

1. Axboe, J. "io_uring" (2019). Linux Kernel Documentation.
2. Python Software Foundation. "asyncio — Asynchronous I/O". Python 3 Documentation.
3. Love, R. "Linux Kernel Development" (2010). Addison-Wesley Professional.
