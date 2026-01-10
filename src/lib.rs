//! uringcore: Completion-driven asyncio event loop using `io_uring`.
//!
//! This crate provides a 1:1 drop-in replacement for uvloop built on `io_uring`
//! with Completion-Driven Virtual Readiness (CDVR).
//!
//! # Architecture
//!
//! - **Rust Core**: Owns the `io_uring` instance and pre-registered `BufferPool`
//! - **Python Layer**: Consumes "Completions," not "Readiness"
//! - **No Selector**: The asyncio loop waits on eventfd, not a Selector
//!
//! # Features
//!
//! - Zero-copy buffer handoff via `PyCapsule`
//! - SQPOLL mode with automatic fallback
//! - Fork-safe with generation ID validation
//! - Credit-based backpressure
//!
//! # Example
//!
//! ```python
//! import asyncio
//! import uringcore
//!
//! asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
//!
//! async def main():
//!     # Your async code here
//!     pass
//!
//! asyncio.run(main())
//! ```
//!
//! Copyright (c) 2025 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
//! Licensed under the MIT License.

#![warn(clippy::all, clippy::pedantic, clippy::nursery)]
#![allow(clippy::module_name_repetitions)]
#![allow(clippy::missing_errors_doc)]
#![allow(clippy::missing_panics_doc)]
// PyO3 methods need PyResult even for infallible operations
#![allow(clippy::unnecessary_wraps)]
// PyO3 getters can't be const
#![allow(clippy::missing_const_for_fn)]
// PyO3 methods require owned types for Python interop
#![allow(clippy::needless_pass_by_value)]
// Complex types are acceptable for callback storage
#![allow(clippy::type_complexity)]
// Drop timing is acceptable in our async context
#![allow(clippy::significant_drop_tightening)]
// match is clearer than map_or_else for error handling
#![allow(clippy::option_if_let_else)]
// PyO3 methods need self even if unused
// PyO3 methods need self even if unused
#![allow(clippy::unused_self)]
// Allow too many lines in PyO3 wrapper functions
#![allow(clippy::too_many_lines)]
// Allow cast truncation as we explicitly handle buffer sizes < 4GB
#![allow(clippy::cast_possible_truncation)]
// Allow sign loss for benign casts (e.g. fd)
#![allow(clippy::cast_sign_loss)]
// Allow precision loss for benign casts (e.g. timestamp)
#![allow(clippy::cast_precision_loss)]
// Allow collapsible if-else for readability in error handling
#![allow(clippy::collapsible_else_if)]
// PyO3 naming conventions often trigger these (e.g. bid vs bgid)
#![allow(clippy::similar_names)]

pub mod buf_ring;
pub mod buffer;
pub mod error;
pub mod fixed_fd;
pub mod future;
pub mod handle;
pub mod ring;
pub mod scheduler;
pub mod state;
// pub mod task; // Removed in favor of asyncio.Task implementation
pub mod timer;

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::os::fd::RawFd;
use std::sync::Arc;

use buffer::BufferPool;
use handle::UringHandle;
use ring::{OpType, Ring};
use scheduler::Scheduler;
use state::{FDStateManager, SocketType};
use timer::TimerHeap;

/// State for an in-flight `recvmsg` operation.
/// Must be heap-allocated and kept alive until completion.
struct RecvMsgState {
    pub msghdr: libc::msghdr,
    pub iovec: libc::iovec,
    pub addr: libc::sockaddr_storage,
}

/// State for an in-flight `sendmsg` operation.
/// Must be heap-allocated and kept alive until completion.
struct SendMsgState {
    pub msghdr: libc::msghdr,
    pub iovec: libc::iovec,
    pub addr: libc::sockaddr_storage,
    // We need to keep the data alive too if it's not copied into a kernel buffer immediately.
    // For io_uring sendmsg, the iovec points to the data.
    // If we pass bytes from Python, we typically need to ensure they stay valid.
    // However, for typical send operations, we might copy the data into a buffer we own
    // or rely on PyBytes being immortal if we hold a reference (but we can't easily hold Py<PyBytes> in a raw struct without GIL).
    //
    // A better approach for `submit_sendto` is to allocate a buffer from our `BufferPool` (or a separate `Vec<u8>`)
    // and copy the data there, OR hold the PyObject.
    // Given our BufferPool is for 'recv' mainly (fixed size chunks), for send we might just want to use a `Vec<u8>` or `Box<[u8]>`.
    //
    // To keep it simple and safe: We will own the data in this state.
    pub data: Vec<u8>,
}

// Safety: The raw pointers in msghdr/iovec refer to fields within the struct itself
// or the pinned buffer from BufferPool.
unsafe impl Send for RecvMsgState {}
unsafe impl Sync for RecvMsgState {}

unsafe impl Send for SendMsgState {}
unsafe impl Sync for SendMsgState {}

use parking_lot::Mutex;
use std::collections::HashMap;

/// The main uringcore engine exposed to Python.
#[pyclass(module = "uringcore")]
pub struct UringCore {
    /// The `io_uring` ring (wrapped in Mutex for interior mutability)
    ring: Mutex<Ring>,
    /// Buffer pool for zero-copy I/O
    buffer_pool: Arc<BufferPool>,
    /// FD state manager
    fd_states: FDStateManager,
    /// Inflight recv buffers: fd -> `buffer_index` (for completion data extraction)
    inflight_recv_buffers: Mutex<HashMap<i32, u16>>,
    /// Timer heap for scheduled callbacks
    timers: TimerHeap,
    /// Task scheduler for Python callbacks
    scheduler: Scheduler,
    /// Future map for Native Completion (FD -> Future)
    futures: Mutex<HashMap<i32, PyObject>>,
    /// Provided Buffer Ring
    /// Register a file descriptor for fixed file optimization.
    pbuf_ring: Option<Arc<buf_ring::PBufRing>>,
    /// Reader callbacks: fd -> (callback, args)
    readers: Mutex<HashMap<i32, (PyObject, PyObject)>>,
    /// Writer callbacks: fd -> (callback, args)
    writers: Mutex<HashMap<i32, (PyObject, PyObject)>>,
    /// Inflight recvmsg states: fd -> Box<RecvMsgState>
    recvmsg_states: Mutex<HashMap<i32, Box<RecvMsgState>>>,
    /// Inflight sendmsg states: fd -> Box<SendMsgState>
    sendmsg_states: Mutex<HashMap<i32, Box<SendMsgState>>>,
    /// Stopping flag for `run_until_stopped`
    stopping: std::sync::atomic::AtomicBool,
}

#[pymethods]
impl UringCore {
    /// Create a new `UringCore` instance.
    ///
    /// # Arguments
    ///
    /// * `buffer_count` - Number of buffers to allocate (default: 1024)
    /// * `ring_size` - Size of the submission queue (default: 4096)
    /// * `try_sqpoll` - Whether to try SQPOLL mode (default: true)
    #[new]
    #[pyo3(signature = (buffer_size=8192, buffer_count=4096, ring_size=4096, try_sqpoll=true))]
    fn new(
        buffer_size: usize,
        buffer_count: usize,
        ring_size: u32,
        try_sqpoll: bool,
    ) -> PyResult<Self> {
        let mut ring = Ring::new(ring_size, try_sqpoll)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Initialize buffer pool (mmap)
        let pool = Arc::new(
            BufferPool::new(buffer_size, buffer_count)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyMemoryError, _>(e.to_string()))?,
        );

        // Register buffers with io_uring (Fixed Buffers)
        ring.register_buffers(pool.clone())
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Try to set up Provided Buffer Ring if supported
        let mut pbuf_ring = None;
        // Use BGID 1 for the default group
        let bgid = 1;
        // Ring entries must be power of 2. Round up buffer_count to next power of 2.
        let pbuf_entries = buffer_count.next_power_of_two() as u16;

        // Attempt to create and register PBufRing
        if let Ok(pr) = buf_ring::PBufRing::new(pbuf_entries, bgid) {
            // Unsafe: Getting pointers for registration
            let addr = pr.as_ptr() as u64;

            // Try registration
            // SAFETY: addr is valid execution of PBufRing::new ensured valid layout
            if unsafe { ring.register_pbuf_ring(addr, pbuf_entries, bgid).is_ok() } {
                tracing::info!(
                    "PBufRing registered with {} entries (BGID {})",
                    pbuf_entries,
                    bgid
                );

                // Populate the ring with buffers from the pool
                // We map buffer index 0..buffer_count to the ring
                // The BufferPool owns the memory, PBufRing just indexes it for the kernel

                let pool_ref = &pool; // borrow for closure

                pr.add_buffers(buffer_count as u16, |i| {
                    // SAFETY: i < buffer_count is guaranteed by loop bound
                    let addr = unsafe { pool_ref.get_buffer_ptr(i) } as u64;
                    let len = buffer_size as u32; // pool.buffer_size()
                    let bid = i; // Buffer ID matches pool index
                    (addr, len, bid)
                });

                pbuf_ring = Some(Arc::new(pr));
            } else {
                tracing::debug!("PBufRing registration failed (kernel too old?), skipping.");
            }
        }

        Ok(Self {
            ring: Mutex::new(ring),
            buffer_pool: pool,
            fd_states: FDStateManager::new(),
            inflight_recv_buffers: Mutex::new(HashMap::new()),
            timers: TimerHeap::new(),
            scheduler: Scheduler::new(),
            futures: Mutex::new(HashMap::new()),
            pbuf_ring,
            readers: Mutex::new(HashMap::new()),
            writers: Mutex::new(HashMap::new()),
            recvmsg_states: Mutex::new(HashMap::new()),
            sendmsg_states: Mutex::new(HashMap::new()),
            stopping: std::sync::atomic::AtomicBool::new(false),
        })
    }

    /// Get the eventfd file descriptor for polling.
    #[getter]
    fn event_fd(&self) -> i32 {
        self.ring.lock().event_fd()
    }

    /// Check if SQPOLL mode is enabled.
    #[getter]
    fn sqpoll_enabled(&self) -> bool {
        self.ring.lock().sqpoll_enabled()
    }

    /// Get the current generation ID.
    #[getter]
    fn generation_id(&self) -> u64 {
        self.ring.lock().generation_id()
    }

    /// Register a file descriptor for I/O operations.
    ///
    /// # Arguments
    ///
    /// * `fd` - The file descriptor to register
    /// * `socket_type` - Type of socket ("tcp", "udp", "unix", "other")
    fn register_fd(&self, fd: i32, socket_type: &str) {
        let st = match socket_type {
            "tcp" => SocketType::TcpStream,
            "tcp_listener" => SocketType::TcpListener,
            "udp" => SocketType::Udp,
            "unix" => SocketType::UnixStream,
            "unix_listener" => SocketType::UnixListener,
            _ => SocketType::Other,
        };

        self.fd_states.register(fd, st);
    }

    /// Unregister a file descriptor.
    fn unregister_fd(&self, fd: i32) {
        let gen_id = self.buffer_pool.generation_id();

        // 1. Release any inflight recv buffer for this FD
        // This fixes the buffer leak when closing a socket with pending recv
        let buf_idx_opt = self.inflight_recv_buffers.lock().remove(&fd);
        if let Some(buf_idx) = buf_idx_opt {
            self.buffer_pool.release(buf_idx, gen_id);
        }

        // 2. Return any pending buffers from FD state to the pool
        if let Some(buffers) = self.fd_states.unregister(fd) {
            for buf in buffers {
                self.buffer_pool.release(buf.index, gen_id);
            }
        }
    }

    /// Pause reading for a file descriptor.
    fn pause_reading(&self, fd: i32) -> PyResult<()> {
        self.fd_states
            .pause_reading(fd)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Resume reading for a file descriptor.
    fn resume_reading(&self, fd: i32) -> PyResult<()> {
        self.fd_states
            .resume_reading(fd)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Check if fork has been detected.
    fn check_fork(&self) -> bool {
        self.ring.lock().check_fork()
    }

    /// Submit pending operations to the kernel.
    fn submit(&self) -> PyResult<usize> {
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Drain the eventfd (call after waking up).
    fn drain_eventfd(&self) -> PyResult<()> {
        self.ring
            .lock()
            .drain_eventfd()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Drain completions from the ring.
    ///
    /// Returns a list of tuples: (fd, operation type, result, data).
    #[allow(clippy::cast_sign_loss)]
    fn drain_completions(&self, py: Python<'_>) -> PyResult<Vec<PyObject>> {
        let completions = self.ring.lock().drain_completions();
        let mut results = Vec::with_capacity(completions.len());

        // Track buffers to recycle to PBufRing
        // We use a small local vector to batch updates
        let mut recycled_pbuf_ids = Vec::new();

        for cqe in completions {
            let op_type = match cqe.op_type() {
                OpType::Recv => "recv",
                OpType::Send => "send",
                OpType::Accept => "accept",
                OpType::Connect => "connect",
                OpType::Close => "close",
                OpType::Timeout => "timeout",
                OpType::RecvMulti => "recv_multi",
                OpType::SendZC => "send_zc",
                OpType::AcceptMulti => "accept_multi",
                OpType::RecvMsg => "recvmsg",
                OpType::SendMsg => "sendmsg",
                OpType::Unknown => "unknown",
            };

            // Create result tuple
            let tuple = if let Some(buf_idx) = cqe.buffer_index {
                // RecvMulti uses provided buffers (PBufRing)
                // If the operation was RecvMulti, we need to track it for replenishment
                // Note: buffer_index is provided by the kernel for RecvMulti

                let is_multishot = cqe.op_type() == OpType::RecvMulti;

                if cqe.result > 0 {
                    let data = unsafe {
                        self.buffer_pool
                            .get_buffer_slice(buf_idx, cqe.result as usize)
                    };
                    let py_bytes = PyBytes::new(py, data);

                    // Release from pool (mark as free/consumable)
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());

                    // If it was a provided buffer, queue for replenishment
                    if is_multishot {
                        recycled_pbuf_ids.push(buf_idx);
                    }

                    (
                        cqe.fd(),
                        op_type,
                        cqe.result,
                        Some(py_bytes.into_pyobject(py)?),
                    )
                        .into_pyobject(py)?
                } else {
                    // Error or EOF
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());

                    // Even on error, if a buffer was picked, we should recycle it?
                    // Usually if result <= 0, no buffer is consumed "data-wise",
                    // but the kernel might have Selected it.
                    // However, for RecvMulti, if result < 0, typically no buffer is used unless partial?
                    // But if buffer_index IS set, then a buffer WAS selected.
                    if is_multishot {
                        recycled_pbuf_ids.push(buf_idx);
                    }

                    (cqe.fd(), op_type, cqe.result, py.None()).into_pyobject(py)?
                }
            } else if cqe.op_type() == OpType::Recv {
                // Recv with inflight buffer tracking (Standard non-multishot recv)
                // Decrement inflight count to allow recv rearm
                let _ = self
                    .fd_states
                    .with_state_mut(cqe.fd(), state::FDState::on_completion_empty);

                // Extract buffer index - lock is dropped before processing
                let buf_idx_opt = self.inflight_recv_buffers.lock().remove(&cqe.fd());
                if let Some(buf_idx) = buf_idx_opt {
                    if cqe.result > 0 {
                        let data = unsafe {
                            self.buffer_pool
                                .get_buffer_slice(buf_idx, cqe.result as usize)
                        };
                        let py_bytes = PyBytes::new(py, data);
                        self.buffer_pool
                            .release(buf_idx, self.buffer_pool.generation_id());
                        (
                            cqe.fd(),
                            op_type,
                            cqe.result,
                            Some(py_bytes.into_pyobject(py)?),
                        )
                            .into_pyobject(py)?
                    } else {
                        // Error or EOF
                        self.buffer_pool
                            .release(buf_idx, self.buffer_pool.generation_id());
                        (cqe.fd(), op_type, cqe.result, py.None()).into_pyobject(py)?
                    }
                } else {
                    // No buffer tracked (shouldn't happen)
                    (cqe.fd(), op_type, cqe.result, py.None()).into_pyobject(py)?
                }
            } else {
                (cqe.fd(), op_type, cqe.result, py.None()).into_pyobject(py)?
            };

            results.push(tuple.into());
        }

        // Replenish PBufRing if we have recycled buffers and a ring is active
        if !recycled_pbuf_ids.is_empty() {
            if let Some(ref pr) = self.pbuf_ring {
                let pool_ref = &self.buffer_pool;
                let buf_size = pool_ref.buffer_size() as u32;

                pr.add_buffers(recycled_pbuf_ids.len() as u16, |i| {
                    let bid = recycled_pbuf_ids[i as usize];
                    // Re-register the buffer with the kernel ring
                    let addr = unsafe { pool_ref.get_buffer_ptr(bid) } as u64;
                    (addr, buf_size, bid)
                });
            }
        }

        Ok(results)
    }

    /// Get buffer pool statistics: (total, free, quarantined, in-use).
    fn buffer_stats(&self) -> (usize, usize, usize, usize) {
        let stats = self.buffer_pool.stats();
        (stats.total, stats.free, stats.quarantined, stats.in_use)
    }

    /// Get FD state statistics: (count, inflight, queued, paused).
    fn fd_stats(&self) -> (usize, u64, u64, usize) {
        let stats = self.fd_states.stats();
        (
            stats.fd_count,
            stats.total_inflight,
            stats.total_queued,
            stats.paused_count,
        )
    }

    /// Signal the eventfd (for testing).
    fn signal(&self) -> PyResult<()> {
        self.ring
            .lock()
            .signal()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Register a file descriptor for fixed file optimization.
    ///
    /// Returns the fixed index.
    fn register_file(&self, fd: i32) -> PyResult<u32> {
        self.ring
            .lock()
            .register_file(fd)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Unregister a file descriptor.
    fn unregister_file(&self, fd: i32) -> PyResult<()> {
        self.ring
            .lock()
            .unregister_file(fd)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    // =========================================================================
    // io_uring Submission Methods (Pure Async I/O)
    // =========================================================================

    /// Submit a receive operation for a file descriptor.
    ///
    /// Acquires a buffer from the pool and submits a recv operation.
    /// The completion will be delivered via `run_tick` completion processing.
    fn submit_recv(&self, fd: i32, future: PyObject) -> PyResult<()> {
        // Check if FD should accept new submissions
        if !self.fd_states.should_submit_recv(fd) {
            return Ok(()); // Backpressure or paused
        }

        // Acquire a buffer from the pool
        let buf_idx = self.buffer_pool.acquire().ok_or_else(|| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("No buffers available for recv")
        })?;

        // Get buffer pointer and size
        let buf_ptr = unsafe { self.buffer_pool.get_buffer_ptr(buf_idx).cast::<u8>() };
        // Buffer size is 64KB which fits in u32
        #[allow(clippy::cast_possible_truncation)]
        let buf_len = self.buffer_pool.buffer_size() as u32;

        self.fd_states
            .with_state_mut(fd, state::FDState::on_submit)
            .map_err(|e| {
                // Release buffer on error
                self.buffer_pool
                    .release(buf_idx, self.buffer_pool.generation_id());
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
            })?;

        // Phase 4: Store future
        self.futures.lock().insert(fd, future);

        // Submit to ring
        let gen = self.ring.lock().generation_u16();
        unsafe {
            self.ring
                .lock()
                .prep_recv(fd, buf_ptr, buf_len, buf_idx, gen)
                .map_err(|e| {
                    // Release buffer on error
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());
                    self.futures.lock().remove(&fd);
                    PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
                })?;
        }

        // Track inflight buffer for this FD (for completion data extraction)
        // IMPORTANT: If there's already a buffer tracked for this FD (fd reuse case),
        // we must release it first to prevent memory leak
        {
            let mut inflight = self.inflight_recv_buffers.lock();
            if let Some(old_buf_idx) = inflight.insert(fd, buf_idx) {
                // Release the old buffer that was overwritten
                self.buffer_pool
                    .release(old_buf_idx, self.buffer_pool.generation_id());
            }
        }

        // Flush to kernel
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a recvfrom (recvmsg) operation for a file descriptor.
    fn submit_recvfrom(&self, fd: i32, future: PyObject) -> PyResult<()> {
        // Check backpressure/paused
        if !self.fd_states.should_submit_recv(fd) {
            return Ok(());
        }

        // Acquire buffer
        let buf_idx = self.buffer_pool.acquire().ok_or_else(|| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("No buffers available for recvfrom")
        })?;

        // Get buffer pointer and size
        let buf_ptr = unsafe {
            self.buffer_pool
                .get_buffer_ptr(buf_idx)
                .cast::<libc::c_void>()
        };
        // Buffer size is 64KB which fits in u32
        #[allow(clippy::cast_possible_truncation)]
        let buf_len = self.buffer_pool.buffer_size() as u32;

        self.fd_states
            .with_state_mut(fd, state::FDState::on_submit)
            .map_err(|e| {
                self.buffer_pool
                    .release(buf_idx, self.buffer_pool.generation_id());
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
            })?;

        self.futures.lock().insert(fd, future);

        // Prepare RecvMsgState
        let mut state = Box::new(RecvMsgState {
            msghdr: unsafe { std::mem::zeroed() },
            iovec: libc::iovec {
                iov_base: buf_ptr,
                iov_len: buf_len as usize,
            },
            addr: unsafe { std::mem::zeroed() },
        });

        // Setup msghdr
        state.msghdr.msg_name = std::ptr::addr_of_mut!(state.addr).cast::<libc::c_void>();
        state.msghdr.msg_namelen = std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
        state.msghdr.msg_iov = std::ptr::addr_of_mut!(state.iovec);
        state.msghdr.msg_iovlen = 1;

        // Submit to ring
        let gen = self.ring.lock().generation_u16();
        unsafe {
            let res = self.ring.lock().prep_recvmsg(
                fd,
                std::ptr::addr_of_mut!(state.msghdr),
                buf_idx,
                gen,
            );
            if let Err(e) = res {
                self.buffer_pool
                    .release(buf_idx, self.buffer_pool.generation_id());
                self.futures.lock().remove(&fd);
                return Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>((
                    format!("prep_recvmsg failed: {e}"),
                )));
            }
        }

        // Store state to keep it alive
        self.recvmsg_states.lock().insert(fd, state);

        // Track inflight buffer
        {
            let mut inflight = self.inflight_recv_buffers.lock();
            if let Some(old_buf_idx) = inflight.insert(fd, buf_idx) {
                self.buffer_pool
                    .release(old_buf_idx, self.buffer_pool.generation_id());
            }
        }

        // Flush
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a `sendmsg` operation for `sock_sendto`.
    ///
    /// Copies data into a heap-allocated state to ensure validity during the async operation.
    fn submit_sendto(
        &self,
        py: Python,
        fd: i32,
        data: &Bound<'_, PyBytes>,
        addr: PyObject,
        future: PyObject,
    ) -> PyResult<()> {
        let fd = fd as RawFd;
        let data_bytes = data.as_bytes().to_vec();
        let len = data_bytes.len();

        let addr_storage: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
        let mut state = Box::new(SendMsgState {
            msghdr: unsafe { std::mem::zeroed() },
            iovec: unsafe { std::mem::zeroed() },
            addr: addr_storage,
            data: data_bytes,
        });

        // Address handling
        // If it's a tuple, it's IPv4/IPv6. If it's str/bytes, it's UNIX.

        if let Ok(addr_tuple) = addr.downcast_bound::<pyo3::types::PyTuple>(py) {
            if addr_tuple.len() == 2 {
                let host = addr_tuple.get_item(0)?.extract::<String>()?;
                let port = addr_tuple.get_item(1)?.extract::<u16>()?;

                let ip: std::net::Ipv4Addr = host.parse().map_err(|e| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Invalid IPv4 address: {e}"
                    ))
                })?;
                let sockaddr_in = libc::sockaddr_in {
                    sin_family: libc::AF_INET as libc::sa_family_t,
                    sin_port: u16::to_be(port),
                    sin_addr: libc::in_addr {
                        s_addr: u32::to_be(u32::from(ip)),
                    },
                    sin_zero: [0; 8],
                };
                unsafe {
                    std::ptr::copy_nonoverlapping(
                        std::ptr::addr_of!(sockaddr_in),
                        std::ptr::addr_of_mut!(state.addr).cast::<libc::sockaddr_in>(),
                        1,
                    );
                }
            } else if addr_tuple.len() == 4 {
                let host = addr_tuple.get_item(0)?.extract::<String>()?;
                let port = addr_tuple.get_item(1)?.extract::<u16>()?;
                let flowinfo = addr_tuple.get_item(2)?.extract::<u32>()?;
                let scope_id = addr_tuple.get_item(3)?.extract::<u32>()?;

                let ip: std::net::Ipv6Addr = host.parse().map_err(|e| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Invalid IPv6 address: {e}"
                    ))
                })?;

                let sockaddr_in6 = libc::sockaddr_in6 {
                    sin6_family: libc::AF_INET6 as libc::sa_family_t,
                    sin6_port: u16::to_be(port),
                    sin6_flowinfo: flowinfo,
                    sin6_addr: libc::in6_addr {
                        s6_addr: ip.octets(),
                    },
                    sin6_scope_id: scope_id,
                };
                unsafe {
                    std::ptr::copy_nonoverlapping(
                        std::ptr::addr_of!(sockaddr_in6),
                        std::ptr::addr_of_mut!(state.addr).cast::<libc::sockaddr_in6>(),
                        1,
                    );
                }
            } else {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "Address tuple must be length 2 (IPv4) or 4 (IPv6)",
                ));
            }
        } else if let Ok(path) = addr.downcast_bound::<pyo3::types::PyString>(py) {
            // AF_UNIX via string path
            let path_str = path.to_str()?;
            let path_bytes = path_str.as_bytes();

            let max_len = std::mem::size_of::<libc::sockaddr_un>()
                - std::mem::offset_of!(libc::sockaddr_un, sun_path);

            if path_bytes.len() >= max_len {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "UNIX socket path too long",
                ));
            }

            let mut sun: libc::sockaddr_un = unsafe { std::mem::zeroed() };
            sun.sun_family = libc::AF_UNIX as libc::sa_family_t;

            unsafe {
                std::ptr::copy_nonoverlapping(
                    path_bytes.as_ptr().cast::<i8>(),
                    sun.sun_path.as_mut_ptr(),
                    path_bytes.len(),
                );
            }

            unsafe {
                std::ptr::copy_nonoverlapping(
                    std::ptr::addr_of!(sun),
                    std::ptr::addr_of_mut!(state.addr).cast::<libc::sockaddr_un>(),
                    1,
                );
            }
        } else if let Ok(path_bytes) = addr.downcast_bound::<pyo3::types::PyBytes>(py) {
            // AF_UNIX via bytes (e.g. abstract namespace)
            let bytes = path_bytes.as_bytes();
            let max_len = std::mem::size_of::<libc::sockaddr_un>()
                - std::mem::offset_of!(libc::sockaddr_un, sun_path);

            if bytes.len() >= max_len {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "UNIX socket path too long",
                ));
            }

            let mut sun: libc::sockaddr_un = unsafe { std::mem::zeroed() };
            sun.sun_family = libc::AF_UNIX as libc::sa_family_t;

            unsafe {
                std::ptr::copy_nonoverlapping(
                    bytes.as_ptr().cast::<i8>(),
                    sun.sun_path.as_mut_ptr(),
                    bytes.len(),
                );
            }

            unsafe {
                std::ptr::copy_nonoverlapping(
                    std::ptr::addr_of!(sun),
                    std::ptr::addr_of_mut!(state.addr).cast::<libc::sockaddr_un>(),
                    1,
                );
            }
        } else {
            return Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                "Address must be a tuple (IPv4/6) or string/bytes (UNIX)",
            ));
        }

        // Setup SendMsg
        state.iovec.iov_base = state.data.as_mut_ptr().cast::<libc::c_void>();
        state.iovec.iov_len = len;

        state.msghdr.msg_name = std::ptr::addr_of_mut!(state.addr).cast::<libc::c_void>();
        // Safety: state.addr is a valid sockaddr_storage, aligned, and initialized.
        // Accessing ss_family is safe because it's a field in the C struct layout which we assume matches libc.
        // Actually, in Rust `libc::sockaddr_storage` fields are public, so reading them is safe.
        // The previous unsafe block was flagging this.
        let family = state.addr.ss_family;
        state.msghdr.msg_namelen = if family == libc::AF_INET as libc::sa_family_t {
            std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t
        } else if family == libc::AF_INET6 as libc::sa_family_t {
            std::mem::size_of::<libc::sockaddr_in6>() as libc::socklen_t
        } else if family == libc::AF_UNIX as libc::sa_family_t {
            // Calculate length: family field + path length + null terminator?
            // Logic typically: offsetof(sun_path) + path_len + 1 (if not abstract)
            // But simpler to just use sizeof(sockaddr_un) roughly or exact
            // For abstract, it's exact length.
            // Let's use max size for safety in sendto, kernel handles it?
            // Ideally we calculate exact.
            // But we don't have the length handy here easily without re-measuring string.
            // Wait, we can assume full size for sockaddr_un in storage?
            // Actually, for sendmsg, msg_namelen should be the actual length associated with the address.
            // Let's just use sizeof(sockaddr_un) for now as it contains zero-padding which is safe.
            std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t
        } else {
            // Should not happen
            std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t
        };

        state.msghdr.msg_iov = std::ptr::addr_of_mut!(state.iovec);
        state.msghdr.msg_iovlen = 1;

        let gen = self.ring.lock().generation_u16();
        unsafe {
            let res =
                self.ring
                    .lock()
                    .prep_sendmsg(fd, std::ptr::addr_of_mut!(state.msghdr), 0, gen);
            if let Err(e) = res {
                return Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!(
                    "prep_sendmsg failed: {e}"
                )));
            }
        }

        self.sendmsg_states.lock().insert(fd, state);

        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        self.futures.lock().insert(fd, future);
        Ok(())
    }

    /// Submit a multishot receive operation using provided buffers.
    ///
    /// Requires Provided Buffer Ring (Phase 7).
    fn submit_recv_multishot(&self, fd: i32, future: PyObject) -> PyResult<()> {
        let bgid = if let Some(ref pr) = self.pbuf_ring {
            pr.bgid()
        } else {
            return Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(
                "Provided Buffer Ring not available (kernel < 5.19 or not initialized)",
            ));
        };

        let gen = self.ring.lock().generation_u16();

        // Register future
        self.futures.lock().insert(fd, future);
        // Note: No specific buffer_index to track inflight, kernel provides it on completion.

        self.ring
            .lock()
            .prep_recv_multishot(fd, bgid, gen)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Submit immediately
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a send operation for a file descriptor.
    ///
    /// The data is copied to a buffer and submitted to `io_uring`.
    fn submit_send(&self, fd: i32, data: &[u8], future: PyObject) -> PyResult<()> {
        // Acquire a buffer from the pool
        let buf_idx = self.buffer_pool.acquire().ok_or_else(|| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("No buffers available")
        })?;

        // Copy data to buffer
        let buf_slice = unsafe { self.buffer_pool.get_buffer_slice_mut(buf_idx, data.len()) };
        buf_slice.copy_from_slice(data);

        // Get buffer pointer
        let buf_ptr = buf_slice.as_ptr();
        // Data length is limited by buffer size (64KB) which fits in u32
        #[allow(clippy::cast_possible_truncation)]
        let len = data.len() as u32;

        self.futures.lock().insert(fd, future);

        // Submit to ring
        let gen = self.ring.lock().generation_u16();
        unsafe {
            self.ring
                .lock()
                .prep_send(fd, buf_ptr, len, gen)
                .map_err(|e| {
                    // Release buffer on error
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());
                    self.futures.lock().remove(&fd);
                    PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
                })?;
        }

        // Flush to kernel
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit an accept operation for a listening socket.
    ///
    /// Uses `ACCEPT_MULTI` for efficient connection handling.
    /// Submit an accept operation for a listening socket.
    ///
    /// Uses `ACCEPT_MULTI` for efficient connection handling.
    fn submit_accept(&self, fd: i32, future: PyObject) -> PyResult<()> {
        let gen = self.ring.lock().generation_u16();

        self.futures.lock().insert(fd, future);

        self.ring.lock().prep_accept(fd, gen).map_err(|e| {
            self.futures.lock().remove(&fd);
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
        })?;

        // Flush to kernel
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a multishot accept operation.
    #[pyo3(signature = (fd))]
    fn submit_accept_multishot(&self, fd: i32) -> PyResult<()> {
        let gen = self.ring.lock().generation_u16();

        // Note: We don't track a future for multishot accept because it
        // produces a stream of events. The Python loop handles dispatch.

        // We use insert to mark that we accept completions for this FD
        // but we don't store a Python future because one doesn't exist yet.
        // We can store None? No, futures map expects PyObject.
        // Actually, we probably don't need to put anything in futures map if
        // run_tick logic works for AcceptMulti (OpType::AcceptMulti).
        // run_tick just returns (fd, op, res, data). It doesn't NEED to find a future.
        // It tries to resolve it if found, but if not found, it still adds to results list.
        // So we just submit and let run_tick return the event.

        self.ring
            .lock()
            .prep_accept_multishot(fd, gen)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a close operation for a file descriptor.
    fn submit_close(&self, fd: i32) -> PyResult<()> {
        let gen = self.ring.lock().generation_u16();
        self.ring
            .lock()
            .prep_close(fd, gen)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Flush to kernel
        self.ring
            .lock()
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Shutdown the engine.
    fn shutdown(&self) {
        self.ring.lock().shutdown();
    }

    // =========================================================================
    // Timer Methods
    // =========================================================================

    /// Push a timer to the heap.
    fn push_timer(&self, expiration: f64, handle: PyObject) {
        self.timers.push(expiration, handle);
    }

    /// Pop all expired timers.
    fn pop_expired(&self, now: f64) -> Vec<PyObject> {
        self.timers.pop_expired(now)
    }

    /// Get the expiration time of the next timer.
    fn next_expiration(&self) -> Option<f64> {
        self.timers.next_expiration()
    }

    // =========================================================================
    // Scheduling Methods
    // =========================================================================

    /// Push a handle to the ready queue.
    #[allow(clippy::needless_pass_by_value)]
    fn push_task(&self, handle: PyObject) {
        self.scheduler.push(handle);
    }

    /// Get the number of ready handles.
    fn ready_len(&self) -> usize {
        self.scheduler.len()
    }

    /// Run one tick of the event loop.
    ///
    /// This method:
    /// 1. Checks for expired timers -> moves to ready queue
    /// 2. Submits/Polls I/O -> processes completions (callbacks)
    /// 3. Executes ready tasks
    #[pyo3(signature = (timeout=None))]
    #[allow(unused_variables)]
    fn run_tick(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Vec<PyObject>> {
        let mut results = Vec::new();

        // 1. Process Timers (Native)
        let _n_timers = {
            let mut ts = libc::timespec {
                tv_sec: 0,
                tv_nsec: 0,
            };
            unsafe {
                libc::clock_gettime(libc::CLOCK_MONOTONIC, &raw mut ts);
            }
            let now = ts.tv_sec as f64 + (ts.tv_nsec as f64 / 1_000_000_000.0);

            let expired = self.timers.pop_expired(now);
            let count = expired.len();
            for handle in expired {
                self.scheduler.push(handle);
            }
            count
        };

        // 2. Submit pending I/O and process completions
        let completions = {
            let mut ring = self.ring.lock();
            ring.submit()
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            ring.drain_completions()
        };

        if !completions.is_empty() {
            // Intermediate storage for Phase 1 processing
            struct CompletionItem {
                fd: i32,
                result: i32,
                op_type: OpType,
                data_bytes: Option<PyObject>,
            }

            let mut items = Vec::with_capacity(completions.len());
            let mut fds_to_resolve = Vec::with_capacity(completions.len());

            // Phase 1: Buffer management and data extraction (No futures lock)
            for cqe in completions {
                let fd = cqe.fd();
                let result = cqe.result;
                let op_type = cqe.op_type();

                // Handle buffer release for recv / data extraction
                let mut data_bytes: Option<PyObject> = None;

                if matches!(op_type, OpType::RecvMulti) {
                    if let Some(buf_idx) = cqe.buffer_index {
                        if result > 0 {
                            let len = result as usize;
                            unsafe {
                                let slice = self.buffer_pool.get_buffer_slice(buf_idx, len);
                                data_bytes = Some(pyo3::types::PyBytes::new(py, slice).into());
                            }
                        }
                    }
                }

                if matches!(op_type, OpType::SendMsg) {
                    // Cleanup SendMsg state
                    self.sendmsg_states.lock().remove(&fd);
                    data_bytes = Some(result.into_pyobject(py)?.into());
                } else if matches!(op_type, OpType::Recv) || matches!(op_type, OpType::RecvMsg) {
                    let buf_idx_opt = self.inflight_recv_buffers.lock().remove(&fd);
                    if let Some(buf_idx) = buf_idx_opt {
                        // Extract address if RecvMsg

                        // Extract address if RecvMsg
                        let mut addr_tuple: Option<PyObject> = None;

                        if matches!(op_type, OpType::RecvMsg) {
                            let state_opt = self.recvmsg_states.lock().remove(&fd);
                            if let Some(state) = state_opt {
                                if result > 0 {
                                    // Parse sockaddr
                                    // Assuming IPv4/IPv6 for now
                                    // TODO: Handle UNIX paths
                                    let addr_ptr = std::ptr::addr_of!(state.addr);
                                    let sa = unsafe { &*addr_ptr.cast::<libc::sockaddr>() };

                                    if sa.sa_family == libc::AF_INET as libc::sa_family_t {
                                        let sin = unsafe { *addr_ptr.cast::<libc::sockaddr_in>() };
                                        let ip_u32 = u32::from_be(sin.sin_addr.s_addr);
                                        let ip = std::net::Ipv4Addr::from(ip_u32).to_string();
                                        let port = u16::from_be(sin.sin_port);
                                        addr_tuple = Some((ip, port).into_pyobject(py)?.into());
                                    } else if sa.sa_family == libc::AF_INET6 as libc::sa_family_t {
                                        let sin6 =
                                            unsafe { *addr_ptr.cast::<libc::sockaddr_in6>() };
                                        let ip_u128 = u128::from_be_bytes(sin6.sin6_addr.s6_addr);
                                        let ip = std::net::Ipv6Addr::from(ip_u128).to_string();
                                        let port = u16::from_be(sin6.sin6_port);
                                        // IPv6 tuple: (host, port, flowinfo, scopeid)
                                        addr_tuple = Some(
                                            (ip, port, sin6.sin6_flowinfo, sin6.sin6_scope_id)
                                                .into_pyobject(py)?
                                                .into(),
                                        );
                                    } else if sa.sa_family == libc::AF_UNIX as libc::sa_family_t {
                                        let sun = unsafe { *addr_ptr.cast::<libc::sockaddr_un>() };
                                        let path_len = state.msghdr.msg_namelen as usize
                                            - std::mem::offset_of!(libc::sockaddr_un, sun_path);

                                        if path_len > 0 {
                                            // Handle abstract namespace (starts with null byte)
                                            if sun.sun_path[0] == 0 {
                                                let slice = unsafe {
                                                    std::slice::from_raw_parts(
                                                        sun.sun_path.as_ptr().cast::<u8>(),
                                                        path_len,
                                                    )
                                                };
                                                addr_tuple = Some(PyBytes::new(py, slice).into());
                                            } else {
                                                // Regular path, null-terminated C string in sun_path
                                                // But msg_namelen includes the path structure
                                                // Let's just create bytes from sun_path up to null or len
                                                let slice = unsafe {
                                                    std::ffi::CStr::from_ptr(sun.sun_path.as_ptr())
                                                };
                                                let path_str = slice.to_string_lossy().into_owned();
                                                addr_tuple =
                                                    Some(path_str.into_pyobject(py)?.into());
                                            }
                                        } else {
                                            // Unnamed
                                            addr_tuple =
                                                Some(pyo3::types::PyString::new(py, "").into());
                                        }
                                    }
                                }
                            }
                        }

                        if result > 0 {
                            // Extract data
                            let len = result as usize;
                            unsafe {
                                let slice = self.buffer_pool.get_buffer_slice(buf_idx, len);
                                let bytes = pyo3::types::PyBytes::new(py, slice);
                                // If we have an address, we return a tuple (bytes, address) as data
                                if let Some(addr) = addr_tuple {
                                    data_bytes = Some((bytes, addr).into_pyobject(py)?.into());
                                } else {
                                    data_bytes = Some(bytes.into());
                                }
                            }
                        }
                        self.buffer_pool
                            .release(buf_idx, self.buffer_pool.generation_id());
                    }
                }

                items.push(CompletionItem {
                    fd,
                    result,
                    op_type,
                    data_bytes,
                });
                fds_to_resolve.push(fd);
            }

            // Phase 2: Batch remove futures (Single futures lock)
            let mut resolved_futures = HashMap::new();
            if !fds_to_resolve.is_empty() {
                let mut futures_guard = self.futures.lock();
                for fd in fds_to_resolve {
                    if let Some(fut) = futures_guard.remove(&fd) {
                        resolved_futures.insert(fd, fut);
                    }
                }
            }

            // Phase 3: Resolve futures and build results
            for item in items {
                let fd = item.fd;
                let result = item.result;
                let op_type = item.op_type;
                let data_bytes = item.data_bytes;

                // Clone data for return value (Python tuple) BEFORE consuming data_bytes in future resolution
                let data_for_tuple = data_bytes.as_ref().map(|p| p.clone_ref(py));

                // Resolve Future
                let future_opt = resolved_futures.remove(&fd);

                if let Some(future) = future_opt {
                    if result < 0 {
                        // Error
                        let err = PyErr::new::<pyo3::exceptions::PyOSError, _>((
                            -result,
                            std::io::Error::from_raw_os_error(-result).to_string(),
                        ));

                        if let Ok(uring_fut) =
                            future.downcast_bound::<crate::future::UringFuture>(py)
                        {
                            if let Err(e) = uring_fut.borrow().set_exception_fast(
                                py,
                                &self.scheduler,
                                err.into_pyobject(py)?.into(),
                                future,
                            ) {
                                e.print(py);
                            }
                        } else {
                            if let Err(e) = future.call_method1(py, "set_exception", (err,)) {
                                e.print(py);
                            }
                        }
                    } else {
                        // Success
                        if matches!(op_type, OpType::Recv)
                            || matches!(op_type, OpType::RecvMulti)
                            || matches!(op_type, OpType::RecvMsg)
                        {
                            if let Some(bytes) = data_bytes {
                                if let Ok(uring_fut) =
                                    future.downcast_bound::<crate::future::UringFuture>(py)
                                {
                                    if let Err(e) = uring_fut.borrow().set_result_fast(
                                        py,
                                        &self.scheduler,
                                        bytes,
                                        future,
                                    ) {
                                        e.print(py);
                                    }
                                } else {
                                    if let Err(e) = future.call_method1(py, "set_result", (bytes,))
                                    {
                                        e.print(py);
                                    }
                                }
                            } else {
                                let empty = pyo3::types::PyBytes::new(py, &[]);
                                if let Ok(uring_fut) =
                                    future.downcast_bound::<crate::future::UringFuture>(py)
                                {
                                    if let Err(e) = uring_fut.borrow().set_result_fast(
                                        py,
                                        &self.scheduler,
                                        empty.into(),
                                        future,
                                    ) {
                                        e.print(py);
                                    }
                                } else {
                                    if let Err(e) = future.call_method1(py, "set_result", (empty,))
                                    {
                                        e.print(py);
                                    }
                                }
                            }
                        } else {
                            if let Ok(uring_fut) =
                                future.downcast_bound::<crate::future::UringFuture>(py)
                            {
                                if let Err(e) = uring_fut.borrow().set_result_fast(
                                    py,
                                    &self.scheduler,
                                    result.into_pyobject(py)?.into(),
                                    future,
                                ) {
                                    e.print(py);
                                }
                            } else {
                                if let Err(e) = future.call_method1(py, "set_result", (result,)) {
                                    e.print(py);
                                }
                            }
                        }
                    }
                }

                // Add to results list for Python side processing (even if future resolved)
                let data_obj = data_for_tuple.unwrap_or_else(|| py.None());
                let op_str = op_type.as_str();
                let tuple = (fd, op_str, result, data_obj).into_pyobject(py)?;
                results.push(tuple.into());
            }
        }

        // 4. Run ready tasks (BATCH DRAIN for performance)
        let ready_batch = self.scheduler.drain();

        for handle in ready_batch {
            if let Ok(uring_handle) = handle.downcast_bound::<UringHandle>(py) {
                // Execute timer callback
                // asyncio.TimerHandle._run() executes the callback
                if let Err(e) = uring_handle.borrow().execute(py) {
                    e.print(py);
                }
            } else {
                // Should not happen for timers from our own loop
                // but handle generic PyObject just in case
                let is_cancelled = match handle.call_method0(py, "cancelled") {
                    Ok(val) => val.is_truthy(py).unwrap_or(false),
                    Err(_) => false, // If no cancelled method, assume not cancelled
                };

                if !is_cancelled {
                    if let Err(e) = handle.call_method0(py, "_run") {
                        e.print(py);
                    }
                }
            }
        }

        Ok(results)
    }

    // =========================================================================
    // Reader/Writer Management (for Rust-native event loop)
    // =========================================================================

    /// Add a reader callback for a file descriptor.
    fn add_reader(&self, fd: i32, callback: PyObject, args: PyObject) {
        self.readers.lock().insert(fd, (callback, args));
    }

    /// Remove a reader callback.
    fn remove_reader(&self, fd: i32) -> bool {
        self.readers.lock().remove(&fd).is_some()
    }

    /// Add a writer callback for a file descriptor.
    fn add_writer(&self, fd: i32, callback: PyObject, args: PyObject) {
        self.writers.lock().insert(fd, (callback, args));
    }

    /// Remove a writer callback.
    fn remove_writer(&self, fd: i32) -> bool {
        self.writers.lock().remove(&fd).is_some()
    }

    /// Set the stopping flag.
    fn stop(&self) {
        self.stopping
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }

    /// Clear the stopping flag.
    fn clear_stop(&self) {
        self.stopping
            .store(false, std::sync::atomic::Ordering::SeqCst);
    }

    /// Check if stopping.
    fn is_stopping(&self) -> bool {
        self.stopping.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Run the event loop until stopped.
    /// This is the Rust-native event loop that eliminates FFI overhead.
    #[pyo3(signature = (epoll_fd))]
    fn run_until_stopped(&self, py: Python<'_>, epoll_fd: i32) -> PyResult<()> {
        // Clear stopping flag
        self.stopping
            .store(false, std::sync::atomic::Ordering::SeqCst);

        let mut events: [libc::epoll_event; 64] = unsafe { std::mem::zeroed() };
        let eventfd = self.ring.lock().event_fd();

        loop {
            // Check stopping flag
            if self.stopping.load(std::sync::atomic::Ordering::SeqCst) {
                break;
            }

            // Calculate timeout based on next timer
            let timeout_ms = if !self.scheduler.is_empty() {
                0 // Tasks ready, don't block
            } else if let Some(next_exp) = self.timers.next_expiration() {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs_f64();
                let delay = (next_exp - now).max(0.0);
                (delay * 1000.0) as i32
            } else {
                100 // Default timeout ms
            };

            // epoll_wait (release GIL during blocking)
            let nfds = py.allow_threads(|| unsafe {
                libc::epoll_wait(epoll_fd, events.as_mut_ptr(), 64, timeout_ms)
            });

            if nfds < 0 {
                let err = std::io::Error::last_os_error();
                if err.kind() == std::io::ErrorKind::Interrupted {
                    continue; // EINTR, retry
                }
                return Err(PyErr::new::<pyo3::exceptions::PyOSError, _>(
                    err.to_string(),
                ));
            }

            // Process epoll events
            for event in events.iter().take(nfds as usize) {
                let fd = event.u64 as i32;
                let event_mask = event.events;

                if fd == eventfd {
                    // io_uring completion signal
                    let _ = self.ring.lock().drain_eventfd();
                } else {
                    // Reader/writer callbacks
                    let read_mask = (libc::EPOLLIN | libc::EPOLLHUP | libc::EPOLLERR) as u32;
                    if event_mask & read_mask != 0 {
                        let maybe_reader = {
                            let readers = self.readers.lock();
                            readers
                                .get(&fd)
                                .map(|(cb, args)| (cb.clone_ref(py), args.clone_ref(py)))
                        };
                        if let Some((callback, args)) = maybe_reader {
                            if let Ok(args_tuple) = args.downcast_bound::<pyo3::types::PyTuple>(py)
                            {
                                if let Err(e) = callback.call1(py, args_tuple) {
                                    e.print(py);
                                }
                            } else if let Err(e) = callback.call0(py) {
                                e.print(py);
                            }
                        }
                    }
                    if event_mask & libc::EPOLLOUT as u32 != 0 {
                        let maybe_writer = {
                            let writers = self.writers.lock();
                            writers
                                .get(&fd)
                                .map(|(cb, args)| (cb.clone_ref(py), args.clone_ref(py)))
                        };
                        if let Some((callback, args)) = maybe_writer {
                            if let Ok(args_tuple) = args.downcast_bound::<pyo3::types::PyTuple>(py)
                            {
                                if let Err(e) = callback.call1(py, args_tuple) {
                                    e.print(py);
                                }
                            } else if let Err(e) = callback.call0(py) {
                                e.print(py);
                            }
                        }
                    }
                }
            }

            // Run one tick (timers + completions + tasks)
            let _ = self.run_tick(py, None)?;
        }

        Ok(())
    }
}

/// A Python module implemented in Rust.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<UringCore>()?;
    m.add_class::<UringHandle>()?;
    // m.add_class::<task::UringTask>()?; // Removed
    m.add_class::<future::UringFuture>()?;
    m.add_class::<handle::UringHandle>()?;

    // Add version info
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("__author__", "Ankit Kumar Pandey <ankitkpandey1@gmail.com>")?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_buffer_pool_creation() {
        let pool = BufferPool::with_defaults().unwrap();
        assert_eq!(pool.buffer_count(), buffer::DEFAULT_BUFFER_COUNT);
    }
}
