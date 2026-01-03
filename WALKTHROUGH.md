# uringcore Code Walkthrough

This document provides a detailed walkthrough of the uringcore codebase, explaining how data flows from the Python entry point through the Rust core to the Linux kernel's io_uring subsystem.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Entry Point: Setting the Policy](#entry-point-setting-the-policy)
3. [Event Loop Initialization](#event-loop-initialization)
4. [The Main Loop: _run_once()](#the-main-loop-_run_once)
5. [Submitting I/O Operations](#submitting-io-operations)
6. [Processing Completions](#processing-completions)
7. [TCP Server Flow: create_server()](#tcp-server-flow-create_server)
8. [TCP Client Flow: create_connection()](#tcp-client-flow-create_connection)
9. [Buffer Management](#buffer-management)
10. [Code File Reference](#code-file-reference)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Python Application                             │
│                         asyncio.run(main())                              │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    uringcore.EventLoopPolicy                             │
│                     python/uringcore/policy.py                           │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ creates
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       UringEventLoop                                     │
│                    python/uringcore/loop.py                              │
│  ┌───────────────┐ ┌──────────────┐ ┌───────────────┐                   │
│  │ _ready queue  │ │ _scheduled   │ │ _transports   │                   │
│  │ (DEPRECATED)  │ │ (heap)       │ │ (fd→transport)│                   │
│  └───────────────┘ └──────────────┘ └───────────────┘                   │
│                                     │                                    │
│                           ┌─────────┴─────────┐                          │
│                           │     UringCore     │  PyO3 boundary           │
│                           │    (Rust FFI)     │                          │
│                           └─────────┬─────────┘                          │
└─────────────────────────────────────┼───────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         UringCore (Rust)                                 │
│                          src/lib.rs                                      │
│  ┌───────────────┐ ┌──────────────┐ ┌───────────────┐                   │
│  │  BufferPool   │ │     Ring     │ │ FDStateManager│                   │
│  │  src/buffer.rs│ │  src/ring.rs │ │  src/state.rs │                   │
│  └───────────────┘ └──────────────┘ └───────────────┘                   │
│  ┌────────────────┐                                                      │
│  │   Scheduler    │                                                      │
│  │ src/scheduler.rs│                                                      │
│  └────────────────┘                                                      │
│                           │                                              │
│                    ┌──────┴──────┐                                       │
│                    │  io_uring   │                                       │
│                    │ (io-uring   │                                       │
│                    │   crate)    │                                       │
│                    └──────┬──────┘                                       │
└───────────────────────────┼─────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Linux Kernel (5.11+)                                │
│                      io_uring subsystem                                  │
│  ┌─────────────────────────┐    ┌─────────────────────────┐             │
│  │   Submission Queue (SQ) │    │   Completion Queue (CQ) │             │
│  │   (mmap shared memory)  │    │   (mmap shared memory)  │             │
│  └─────────────────────────┘    └─────────────────────────┘             │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Entry Point: Setting the Policy

**File:** `python/uringcore/__init__.py`

When a user writes:

```python
import asyncio
import uringcore

asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
asyncio.run(main())
```

The flow is:

1. `uringcore.EventLoopPolicy()` is instantiated
2. `asyncio.run()` calls `policy.new_event_loop()`
3. This creates a `UringEventLoop` instance

**File:** `python/uringcore/policy.py`

```python
class EventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return UringEventLoop()
```

---

## Event Loop Initialization

**File:** `python/uringcore/loop.py` → `UringEventLoop.__init__`

```python
def __init__(self):
    self._closed = False
    self._running = False
    
    # 1. Create the Rust core (this creates the io_uring ring)
    self._core = UringCore()
    
    # 2. Callback queues
    self._ready = collections.deque()      # Ready-to-run callbacks
    self._scheduled = []                   # Timer heap
    
    # 3. Transport registries
    self._transports = {}                  # fd → transport
    self._servers = {}                     # fd → (server, protocol_factory)
    
    # 4. epoll for eventfd signaling
    self._epoll = select.epoll()
    self._epoll.register(self._core.event_fd, select.EPOLLIN)
```

### Rust Core Initialization

**File:** `src/lib.rs` → `UringCore::new`

```rust
#[pymethods]
impl UringCore {
    #[new]
    fn new(...) -> PyResult<Self> {
        // ... (ring/buffer pool/fd state init)

        // 5. Create Scheduler (Mutex-protected ready queue)
        let scheduler = Scheduler::new();
        
        Ok(Self { ring, buffer_pool, fd_states, scheduler, ... })
    }
}
```

### Ring Creation

**File:** `src/ring.rs` → `Ring::new`

```rust
pub fn new(ring_size: u32, try_sqpoll: bool) -> Result<Self> {
    // 1. Try SQPOLL mode first
    let (ring, sqpoll_enabled) = Self::create_ring(ring_size, try_sqpoll)?;
    
    // 2. Create eventfd for Python signaling
    let event_fd = nix::sys::eventfd::eventfd(0, EfdFlags::EFD_NONBLOCK)?;
    
    // 3. Register eventfd with io_uring for CQE notifications
    ring.submitter().register_eventfd(event_fd)?;
    
    Ok(Self { ring, event_fd, sqpoll_enabled, ... })
}
```

---

## The Main Loop: _run_once()

**File:** `python/uringcore/loop.py` → `UringEventLoop._run_once`

This is the heart of the event loop. Each iteration:

```python
def _run_once(self):
    # 1. Calculate timeout from scheduled callbacks
    timeout = self._calculate_timeout()
    
    # 2. Wait for io_uring completions via epoll on eventfd
    events = self._epoll.poll(timeout)
    
    # 3. Process events
    for fd, event_mask in events:
        if fd == self._core.event_fd:
            # io_uring has completions ready
            self._core.drain_eventfd()     # Clear the eventfd
            self._process_completions()    # Process CQEs
        else:
            # add_reader/add_writer callback
            self._dispatch_callbacks(fd, event_mask)
    
    # 4. Run scheduled callbacks (timers)
    self._process_scheduled()
    
    # 5. Run ready callbacks
    self._process_ready()
```

### Flow Diagram

```
                    ┌──────────────────┐
                    │   _run_once()    │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
    ┌─────────────────┐ ┌─────────┐ ┌───────────────┐
    │ _calculate_     │ │ epoll.  │ │ _process_     │
    │ timeout()       │ │ poll()  │ │ scheduled()   │
    └─────────────────┘ └────┬────┘ └───────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
    ┌─────────────────────┐     ┌─────────────────────┐
    │ eventfd readable?   │     │ reader/writer fd?   │
    │                     │     │                     │
    │ └→ drain_eventfd()  │     │ └→ dispatch callback│
    │ └→ _process_        │     │                     │
    │    completions()    │     │                     │
    └─────────────────────┘     └─────────────────────┘
              │
              ▼
    ┌─────────────────────┐
    │  _process_ready()   │
    │  (run all callbacks)│
    └─────────────────────┘
```

---

## Submitting I/O Operations

### Example: Submitting a Receive

**Flow:** Python transport → Python loop → Rust UringCore → Rust Ring → Kernel SQ

```
┌──────────────────────────────────────────────────────────────────┐
│  UringSocketTransport.__init__(loop, sock, protocol)            │
│                                                                  │
│  1. self._loop._core.register_fd(fd, "tcp")                     │
│  2. self._loop._core.submit_recv(fd)   ←─────────────────────┐  │
│  3. self._loop._core.submit()                                │  │
└───────────────────┬──────────────────────────────────────────│──┘
                    │ PyO3 FFI                                 │
                    ▼                                          │
┌──────────────────────────────────────────────────────────────│──┐
│  UringCore::submit_recv(fd)  [src/lib.rs]                    │  │
│                                                              │  │
│  1. buf_idx = buffer_pool.acquire()?     // Get buffer       │  │
│  2. buf_ptr = buffer_pool.get_buffer_ptr(buf_idx)            │  │
│  3. inflight_recv_buffers.insert(fd, buf_idx)                │  │
│  4. ring.prep_recv(fd, buf_ptr, buf_len, generation)  ───────┘  │
└───────────────────┬─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  Ring::prep_recv(fd, buf, len, generation)  [src/ring.rs]       │
│                                                                  │
│  1. user_data = encode_user_data(fd, OpType::Recv, generation)  │
│                                                                  │
│     user_data layout (64 bits):                                  │
│     ┌────────────┬──────────┬──────────┬────────────────────┐   │
│     │ gen (16)   │ op (8)   │ rsv (8)  │     fd (32)        │   │
│     └────────────┴──────────┴──────────┴────────────────────┘   │
│                                                                  │
│  2. entry = opcode::Recv::new(Fd(fd), buf, len)                 │
│            .build()                                              │
│            .user_data(user_data)                                 │
│                                                                  │
│  3. sq.push(&entry)   // Add to submission queue                 │
└───────────────────┬─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Linux Kernel io_uring                         │
│                                                                  │
│  Submission Queue (SQ) - shared memory:                          │
│  ┌────────┬────────┬────────┬────────┬────────┐                 │
│  │ SQE 0  │ SQE 1  │ SQE 2  │  ...   │ SQE N  │                 │
│  │(recv)  │(send)  │(accept)│        │        │                 │
│  └────────┴────────┴────────┴────────┴────────┘                 │
│                                                                  │
│  When SQPOLL: Kernel polls SQ automatically                      │
│  Otherwise:   io_uring_enter() syscall flushes SQ                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Processing Completions

When the kernel completes I/O, it writes to the Completion Queue (CQ) and signals the eventfd.

**Flow:** Kernel CQ → Rust drain_completions → Python _process_completions → Transport callback

```
┌─────────────────────────────────────────────────────────────────┐
│                    Linux Kernel io_uring                         │
│                                                                  │
│  Completion Queue (CQ) - shared memory:                          │
│  ┌────────────┬────────────┬────────────────────────────────┐   │
│  │  CQE.result│  CQE.flags │      CQE.user_data             │   │
│  │  (bytes    │  (buffer   │  (fd | op_type | generation)   │   │
│  │   read)    │   flags)   │                                │   │
│  └────────────┴────────────┴────────────────────────────────┘   │
│                                                                  │
│  Kernel writes to eventfd to signal Python                       │
└───────────────────┬─────────────────────────────────────────────┘
                    │ eventfd signaled
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  UringEventLoop._run_once()                                      │
│                                                                  │
│  events = self._epoll.poll()                                     │
│  if fd == self._core.event_fd:                                   │
│      self._core.drain_eventfd()                                  │
│      self._process_completions()                                 │
└───────────────────┬─────────────────────────────────────────────┘
                    │ PyO3 FFI
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  UringCore::drain_completions()  [src/lib.rs]                    │
│                                                                  │
│  for cqe in ring.completions():                                  │
│      fd = cqe.user_data & 0xFFFFFFFF                            │
│      op_type = (cqe.user_data >> 32) & 0xFF                      │
│      generation = (cqe.user_data >> 48) & 0xFFFF                 │
│                                                                  │
│      if op_type == Recv:                                         │
│          buf_idx = inflight_recv_buffers.remove(fd)              │
│          data = buffer_pool.get_buffer_slice(buf_idx, cqe.result)│
│          return (fd, "recv", result, PyBytes(data))              │
│                                                                  │
│      buffer_pool.release(buf_idx)  // Return buffer to pool      │
└───────────────────┬─────────────────────────────────────────────┘
                    │ returns list of (fd, op, result, data)
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  UringEventLoop._process_completions()                           │
│                                                                  │
│  for (fd, op, result, data) in self._core.drain_completions():   │
│      if op == "recv":                                            │
│          self._handle_recv_completion(fd, result, data)          │
│      elif op == "send":                                          │
│          self._handle_send_completion(fd, result)                │
│      elif op == "accept":                                        │
│          self._handle_accept_completion(fd, result)              │
└───────────────────┬─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  _handle_recv_completion(fd, result, data)                       │
│                                                                  │
│  transport = self._transports[fd]                                │
│  if result > 0:                                                  │
│      transport._protocol.data_received(data)   // User callback  │
│      self._core.submit_recv(fd)                // Re-arm recv    │
│  elif result == 0:                                               │
│      transport._protocol.eof_received()        // EOF            │
└─────────────────────────────────────────────────────────────────┘
```

---

## TCP Server Flow: create_server()

```
┌─────────────────────────────────────────────────────────────────┐
│  await loop.create_server(MyProtocol, host, port)                │
│                                                                  │
│  1. sock = socket.socket(AF_INET, SOCK_STREAM)                   │
│  2. sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)                 │
│  3. sock.bind((host, port))                                      │
│  4. sock.listen(backlog)                                         │
│  5. sock.setblocking(False)                                      │
│                                                                  │
│  6. self._core.register_fd(sock.fileno(), "tcp")                 │
│  7. self._core.submit_accept(sock.fileno())   // Start accepting │
│  8. self._servers[fd] = (server, protocol_factory)               │
└───────────────────┬─────────────────────────────────────────────┘
                    │
                    ▼ (when client connects)
┌─────────────────────────────────────────────────────────────────┐
│  _handle_accept_completion(server_fd, client_fd)                 │
│                                                                  │
│  if client_fd > 0:                                               │
│      # Create transport + protocol for new connection            │
│      protocol = protocol_factory()                               │
│      transport = UringSocketTransport(self, client_sock, prot)   │
│      protocol.connection_made(transport)                         │
│                                                                  │
│      # Start receiving on client fd                              │
│      self._core.submit_recv(client_fd)                           │
│                                                                  │
│      # Re-arm accept on server fd                                │
│      self._core.submit_accept(server_fd)                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## TCP Client Flow: create_connection()

```
┌─────────────────────────────────────────────────────────────────┐
│  await loop.create_connection(MyProtocol, host, port)            │
│                                                                  │
│  1. sock = socket.socket(AF_INET, SOCK_STREAM)                   │
│  2. sock.setblocking(False)                                      │
│  3. sock.connect((host, port))   // Returns immediately (EINPROG)│
│                                                                  │
│  4. self._core.register_fd(sock.fileno(), "tcp")                 │
│  5. Wait for connect completion via add_writer()                 │
│  6. Create transport + protocol                                  │
│  7. Start receiving: self._core.submit_recv(fd)                  │
│                                                                  │
│  return (transport, protocol)                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Buffer Management

**File:** `src/buffer.rs`

```
┌─────────────────────────────────────────────────────────────────┐
│                       BufferPool                                 │
│                                                                  │
│  mmap allocation (page-aligned, mlock'd):                        │
│  ┌────────┬────────┬────────┬────────┬────────┬────────┐        │
│  │ buf 0  │ buf 1  │ buf 2  │ buf 3  │  ...   │ buf N  │        │
│  │ 64KB   │ 64KB   │ 64KB   │ 64KB   │        │ 64KB   │        │
│  └────────┴────────┴────────┴────────┴────────┴────────┘        │
│                                                                  │
│  free_list: [0, 1, 2, 3, ...]     // Available buffers          │
│  quarantine: [(5, timestamp)]      // Recently released          │
│                                                                  │
│  Lifecycle:                                                      │
│  ┌──────┐   acquire()   ┌──────────┐   completion   ┌──────────┐│
│  │ Free │ ────────────→ │ In-Flight│ ─────────────→ │ Completed││
│  │ List │               │ (kernel) │               │ (Python)  ││
│  └──────┘               └──────────┘               └──────────┘│
│      ▲                                                    │      │
│      │                  ┌──────────┐    5ms delay         │      │
│      └──────────────────│Quarantine│◀────────────────────┘      │
│      recycle()          └──────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

---


---

## Scheduler Implementation

**File:** `src/scheduler.rs`

The Scheduler is a Rust-side component that manages the queue of ready-to-run Python tasks.

**Design:** `Mutex<VecDeque<PyObject>>`

While a lock-free queue (like `crossbeam-channel`) is standard for multi-threaded work stealing, `asyncio` is fundamentally single-threaded. Through benchmarking, we found that a simple `Mutex` protecting a `VecDeque` outperforms atomic channels because:
1.  **Allocation Reuse**: `VecDeque` reuses its capacity, avoiding per-push memory allocation.
2.  **Cache Locality**: Contiguous memory access is faster than linked-list nodes.
3.  **Low Contention**: The lock is only disputed when `loop.call_soon_threadsafe` pushes from another thread, which is rare in typical asyncio apps.

**Batch Processing:**

To minimize lock overhead, `run_tick` drains the queue in a single batch:

```rust
pub fn drain(&self) -> VecDeque<PyObject> {
    let mut queue = self.queue.lock();
    if queue.is_empty() {
        return VecDeque::new();
    }

    // Optimization: Swap with empty queue to release lock immediately
    let count = queue.len();
    let mut new_queue = VecDeque::with_capacity(count);
    std::mem::swap(&mut *queue, &mut new_queue);
    new_queue
}
```

---

## Code File Reference

| File | Purpose |
|------|---------|
| `python/uringcore/__init__.py` | Package entry point, exports |
| `python/uringcore/policy.py` | EventLoopPolicy for asyncio |
| `python/uringcore/loop.py` | UringEventLoop implementation |
| `python/uringcore/transport.py` | Socket transport (TCP/Unix) |
| `python/uringcore/datagram.py` | UDP transport |
| `python/uringcore/subprocess.py` | Subprocess transport |
| `src/lib.rs` | UringCore PyO3 class |
| `src/ring.rs` | io_uring Ring wrapper |
| `src/buffer.rs` | Zero-copy buffer pool |
| `src/scheduler.rs` | Task ready queue |
| `src/state.rs` | Per-FD state machine |
| `src/error.rs` | Error types |

---

## Key Function Reference

### Python Layer

| Function | File | Purpose |
|----------|------|---------|
| `UringEventLoop.__init__` | loop.py:27 | Initialize loop and Rust core |
| `UringEventLoop._run_once` | loop.py:169 | Single loop iteration |
| `UringEventLoop._process_completions` | loop.py:213 | Handle CQEs |
| `UringEventLoop.create_server` | loop.py:685 | Create TCP server |
| `UringEventLoop.create_connection` | loop.py:560 | Create TCP client |

### Rust Layer

| Function | File | Purpose |
|----------|------|---------|
| `UringCore::new` | lib.rs:77 | Create io_uring instance |
| `UringCore::submit_recv` | lib.rs:314 | Submit recv to SQ |
| `UringCore::drain_completions` | lib.rs:199 | Drain CQ |
| `Ring::prep_recv` | ring.rs:361 | Prepare recv SQE |
| `Ring::prep_send` | ring.rs:390 | Prepare send SQE |
| `BufferPool::acquire` | buffer.rs:180 | Get buffer from pool |
| `BufferPool::release` | buffer.rs:200 | Return buffer to quarantine |

---

## Summary

1. **User sets policy** → `EventLoopPolicy` creates `UringEventLoop`
2. **Loop initializes** → Creates `UringCore` (Rust) → Creates `Ring` (io_uring) → Creates `BufferPool`
3. **Loop runs** → `_run_once()` polls eventfd via epoll
4. **I/O submitted** → Python → `submit_recv/send` → Rust `prep_*` → SQ entry
5. **Kernel completes** → Writes to CQ → Signals eventfd
6. **Completions processed** → Rust `drain_completions` → Python callbacks → Protocol methods
