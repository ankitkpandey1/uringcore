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
use std::sync::Arc;

use buffer::BufferPool;
use handle::UringHandle;
use ring::{OpType, Ring};
use scheduler::Scheduler;
use state::{FDStateManager, SocketType};
use timer::TimerHeap;

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
    /// Provided Buffer Ring (SOTA)
    pbuf_ring: Option<Arc<buf_ring::PBufRing>>,
    /// Reader callbacks: fd -> (callback, args)
    readers: Mutex<HashMap<i32, (PyObject, PyObject)>>,
    /// Writer callbacks: fd -> (callback, args)
    writers: Mutex<HashMap<i32, (PyObject, PyObject)>>,
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

        // Try to set up Provided Buffer Ring (SOTA Phase 7) if supported
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

    /// Register a file descriptor for fixed file optimization (SOTA Phase 8).
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

                if matches!(op_type, OpType::Recv) {
                    let buf_idx_opt = self.inflight_recv_buffers.lock().remove(&fd);
                    if let Some(buf_idx) = buf_idx_opt {
                        if result > 0 {
                            // Extract data
                            let len = result as usize;
                            // Correct safety: get buffer slice, copy to Python bytes
                            unsafe {
                                let slice = self.buffer_pool.get_buffer_slice(buf_idx, len);
                                data_bytes = Some(pyo3::types::PyBytes::new(py, slice).into());
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
