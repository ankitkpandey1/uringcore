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
//! Copyright (c) 2024 Ankit Kumar Pandey <itsankitkp@gmail.com>
//! Licensed under the MIT License.

#![warn(clippy::all, clippy::pedantic, clippy::nursery)]
#![allow(clippy::module_name_repetitions)]
#![allow(clippy::missing_errors_doc)]
#![allow(clippy::missing_panics_doc)]
// PyO3 methods need PyResult even for infallible operations
#![allow(clippy::unnecessary_wraps)]
// PyO3 getters can't be const
#![allow(clippy::missing_const_for_fn)]

pub mod buffer;
pub mod error;
pub mod ring;
pub mod state;

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;

use buffer::BufferPool;
use ring::{OpType, Ring};
use state::{FDStateManager, SocketType};

use parking_lot::Mutex;
use std::collections::HashMap;

/// The main uringcore engine exposed to Python.
#[pyclass]
pub struct UringCore {
    /// The `io_uring` ring
    ring: Ring,
    /// Buffer pool for zero-copy I/O
    buffer_pool: Arc<BufferPool>,
    /// FD state manager
    fd_states: FDStateManager,
    /// Inflight recv buffers: fd -> `buffer_index` (for completion data extraction)
    inflight_recv_buffers: Mutex<HashMap<i32, u16>>,
}

#[pymethods]
impl UringCore {
    /// Create a new `UringCore` instance.
    ///
    /// # Arguments
    ///
    /// * `ring_size` - Size of the submission queue (default: 4096)
    /// * `buffer_size` - Size of each buffer in bytes (default: 64KB)
    /// * `buffer_count` - Number of buffers to allocate (default: 1024)
    /// * `try_sqpoll` - Whether to try SQPOLL mode (default: true)
    #[new]
    #[pyo3(signature = (ring_size=None, buffer_size=None, buffer_count=None, try_sqpoll=None))]
    fn new(
        ring_size: Option<u32>,
        buffer_size: Option<usize>,
        buffer_count: Option<usize>,
        try_sqpoll: Option<bool>,
    ) -> PyResult<Self> {
        let ring_size = ring_size.unwrap_or(ring::DEFAULT_RING_SIZE);
        let buffer_size = buffer_size.unwrap_or(buffer::DEFAULT_BUFFER_SIZE);
        let buffer_count = buffer_count.unwrap_or(buffer::DEFAULT_BUFFER_COUNT);
        let try_sqpoll = try_sqpoll.unwrap_or(true);

        let buffer_pool = Arc::new(
            BufferPool::new(buffer_size, buffer_count)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?,
        );

        let mut ring = Ring::new(ring_size, try_sqpoll)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Register buffers with the ring
        ring.register_buffers(Arc::clone(&buffer_pool))
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(Self {
            ring,
            buffer_pool,
            fd_states: FDStateManager::new(),
            inflight_recv_buffers: Mutex::new(HashMap::new()),
        })
    }

    /// Get the eventfd file descriptor for polling.
    #[getter]
    fn event_fd(&self) -> i32 {
        self.ring.event_fd()
    }

    /// Check if SQPOLL mode is enabled.
    #[getter]
    fn sqpoll_enabled(&self) -> bool {
        self.ring.sqpoll_enabled()
    }

    /// Get the current generation ID.
    #[getter]
    fn generation_id(&self) -> u64 {
        self.ring.generation_id()
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
        // Return any pending buffers to the pool
        if let Some(buffers) = self.fd_states.unregister(fd) {
            let gen_id = self.buffer_pool.generation_id();
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
        self.ring.check_fork()
    }

    /// Submit pending operations to the kernel.
    fn submit(&self) -> PyResult<usize> {
        self.ring
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Drain the eventfd (call after waking up).
    fn drain_eventfd(&self) -> PyResult<()> {
        self.ring
            .drain_eventfd()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    /// Drain completions from the ring.
    ///
    /// Returns a list of tuples: (fd, operation type, result, data).
    #[allow(clippy::cast_sign_loss)]
    fn drain_completions(&mut self, py: Python<'_>) -> PyResult<Vec<PyObject>> {
        let completions = self.ring.drain_completions();
        let mut results = Vec::with_capacity(completions.len());

        for cqe in completions {
            let op_type = match cqe.op_type() {
                OpType::Recv => "recv",
                OpType::Send => "send",
                OpType::Accept => "accept",
                OpType::Connect => "connect",
                OpType::Close => "close",
                OpType::Timeout => "timeout",
                OpType::Unknown => "unknown",
            };

            // Create result tuple
            let tuple = if let Some(buf_idx) = cqe.buffer_index {
                // RecvMulti case (not currently used)
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
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());
                    (cqe.fd(), op_type, cqe.result, py.None()).into_pyobject(py)?
                }
            } else if cqe.op_type() == OpType::Recv {
                // Recv with inflight buffer tracking
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
            .signal()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }

    // =========================================================================
    // io_uring Submission Methods (Pure Async I/O)
    // =========================================================================

    /// Submit a receive operation for a file descriptor.
    ///
    /// Acquires a buffer from the pool and submits a recv operation.
    /// The completion will be delivered via `drain_completions()`.
    fn submit_recv(&mut self, fd: i32) -> PyResult<()> {
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

        // Track inflight
        self.fd_states
            .with_state_mut(fd, state::FDState::on_submit)
            .map_err(|e| {
                // Release buffer on error
                self.buffer_pool
                    .release(buf_idx, self.buffer_pool.generation_id());
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
            })?;

        // Submit to ring
        let gen = self.ring.generation_u16();
        unsafe {
            self.ring
                .prep_recv(fd, buf_ptr, buf_len, buf_idx, gen)
                .map_err(|e| {
                    // Release buffer on error
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());
                    PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
                })?;
        }

        // Track inflight buffer for this FD (for completion data extraction)
        self.inflight_recv_buffers.lock().insert(fd, buf_idx);

        // Flush to kernel
        self.ring
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a send operation for a file descriptor.
    ///
    /// The data is copied to a buffer and submitted to `io_uring`.
    fn submit_send(&mut self, fd: i32, data: &[u8]) -> PyResult<()> {
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

        // Submit to ring
        let gen = self.ring.generation_u16();
        unsafe {
            self.ring.prep_send(fd, buf_ptr, len, gen).map_err(|e| {
                // Release buffer on error
                self.buffer_pool
                    .release(buf_idx, self.buffer_pool.generation_id());
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
            })?;
        }

        // Flush to kernel
        self.ring
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit an accept operation for a listening socket.
    ///
    /// Uses `ACCEPT_MULTI` for efficient connection handling.
    fn submit_accept(&mut self, fd: i32) -> PyResult<()> {
        let gen = self.ring.generation_u16();
        self.ring
            .prep_accept(fd, gen)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Flush to kernel
        self.ring
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Submit a close operation for a file descriptor.
    fn submit_close(&mut self, fd: i32) -> PyResult<()> {
        let gen = self.ring.generation_u16();
        self.ring
            .prep_close(fd, gen)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        // Flush to kernel
        self.ring
            .submit()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        Ok(())
    }

    /// Shutdown the engine.
    fn shutdown(&mut self) {
        self.ring.shutdown();
    }
}

/// A Python module implemented in Rust.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<UringCore>()?;

    // Add version info
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("__author__", "Ankit Kumar Pandey <itsankitkp@gmail.com>")?;

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
