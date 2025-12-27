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

/// The main uringcore engine exposed to Python.
#[pyclass]
pub struct UringCore {
    /// The `io_uring` ring
    ring: Ring,
    /// Buffer pool for zero-copy I/O
    buffer_pool: Arc<BufferPool>,
    /// FD state manager
    fd_states: FDStateManager,
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
                if cqe.result > 0 {
                    // Get buffer data as bytes
                    let data = unsafe {
                        self.buffer_pool
                            .get_buffer_slice(buf_idx, cqe.result as usize)
                    };
                    let py_bytes = PyBytes::new(py, data);

                    // Return buffer to pool
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
                    // Error or EOF, still return buffer
                    self.buffer_pool
                        .release(buf_idx, self.buffer_pool.generation_id());
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
