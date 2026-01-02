//! `io_uring` ring wrapper with SQPOLL support and eventfd signaling.
//!
//! This module provides the core `io_uring` functionality including:
//! - Ring initialization with SQPOLL fallback
//! - eventfd integration for Python event loop wake-up
//! - Completion queue draining
//! - Buffer registration

// Intentional casts for user_data encoding
#![allow(clippy::cast_sign_loss)]
// Ring.ring is intentional naming
#![allow(clippy::struct_field_names)]

use io_uring::{opcode, types, IoUring, Submitter};
use nix::sys::eventfd::{EfdFlags, EventFd};
use std::os::unix::io::{AsRawFd, RawFd};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use crate::buffer::BufferPool;
use crate::error::{Error, Result};

/// Default ring size (number of SQ entries)
pub const DEFAULT_RING_SIZE: u32 = 4096;

/// SQPOLL idle timeout in milliseconds
const SQPOLL_IDLE_MS: u32 = 1000;

/// Flag indicating buffer was selected from the buffer ring
const IORING_CQE_F_BUFFER: u32 = 1 << 0;

/// Completion queue entry wrapper for Python consumption.
#[derive(Debug, Clone)]
pub struct CompletionEntry {
    /// User data (encodes fd and operation type)
    pub user_data: u64,
    /// Result of the operation (bytes transferred or error)
    pub result: i32,
    /// Flags from the completion
    pub flags: u32,
    /// Buffer index if applicable
    pub buffer_index: Option<u16>,
}

impl CompletionEntry {
    /// Extract the file descriptor from `user_data`.
    #[must_use]
    pub const fn fd(&self) -> i32 {
        (self.user_data & 0xFFFF_FFFF) as i32
    }

    /// Extract the operation type from `user_data`.
    #[must_use]
    pub const fn op_type(&self) -> OpType {
        OpType::from_u8(((self.user_data >> 32) & 0xFF) as u8)
    }

    /// Extract the generation ID from `user_data`.
    #[must_use]
    pub const fn generation(&self) -> u16 {
        ((self.user_data >> 48) & 0xFFFF) as u16
    }

    /// Check if this is an error result.
    #[must_use]
    pub const fn is_error(&self) -> bool {
        self.result < 0
    }

    /// Check if this is EOF (zero bytes read).
    #[must_use]
    pub const fn is_eof(&self) -> bool {
        self.result == 0
    }
}

/// Operation types encoded in `user_data`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum OpType {
    /// Receive operation
    Recv = 0,
    /// Send operation
    Send = 1,
    /// Accept operation
    Accept = 2,
    /// Connect operation
    Connect = 3,
    /// Close operation
    Close = 4,
    /// Timeout operation
    Timeout = 5,
    /// SOTA: Multishot receive (kernel 5.19+)
    RecvMulti = 6,
    /// SOTA: Zero-copy send (kernel 6.0+)
    SendZC = 7,
    /// Unknown operation
    Unknown = 255,
}

impl OpType {
    /// Convert from u8.
    #[must_use]
    pub const fn from_u8(v: u8) -> Self {
        match v {
            0 => Self::Recv,
            1 => Self::Send,
            2 => Self::Accept,
            3 => Self::Connect,
            4 => Self::Close,
            5 => Self::Timeout,
            6 => Self::RecvMulti,
            7 => Self::SendZC,
            _ => Self::Unknown,
        }
    }
}

/// Encode `user_data` from fd, operation type, and generation.
#[must_use]
pub const fn encode_user_data(fd: i32, op_type: OpType, generation: u16) -> u64 {
    let fd_part = (fd as u32) as u64;
    let op_part = (op_type as u64) << 32;
    let gen_part = (generation as u64) << 48;
    fd_part | op_part | gen_part
}

/// Extract buffer ID from CQE flags (buffer ID is in upper 16 bits).
#[must_use]
const fn cqe_buffer_id(flags: u32) -> u16 {
    (flags >> 16) as u16
}

/// Check if CQE has a buffer selected.
#[must_use]
const fn cqe_has_buffer(flags: u32) -> bool {
    flags & IORING_CQE_F_BUFFER != 0
}

/// `io_uring` ring wrapper.
pub struct Ring {
    /// The `io_uring` instance
    ring: IoUring,
    /// eventfd for signaling Python
    event_fd: EventFd,
    /// Whether SQPOLL is enabled
    sqpoll_enabled: bool,
    /// Current generation ID (low 16 bits used in `user_data`)
    generation_id: AtomicU64,
    /// Original PID for fork detection
    original_pid: u32,
    /// Whether the ring is active
    is_active: AtomicBool,
    /// Buffer pool reference for registered buffers
    buffer_pool: Option<Arc<BufferPool>>,
    /// SOTA: Registered FD table (IOSQE_FIXED_FILE)
    registered_fds: Option<Vec<RawFd>>,
    /// SOTA: Provided buffer ring group ID
    provided_buf_group_id: Option<u16>,
}

impl Ring {
    /// Create a new ring with SQPOLL if available.
    ///
    /// # Errors
    ///
    /// Returns an error if ring or eventfd creation fails.
    pub fn new(ring_size: u32, try_sqpoll: bool) -> Result<Self> {
        let (ring, sqpoll_enabled) = Self::create_ring(ring_size, try_sqpoll)?;

        // Create eventfd for signaling Python
        let event_fd =
            EventFd::from_value_and_flags(0, EfdFlags::EFD_NONBLOCK | EfdFlags::EFD_CLOEXEC)
                .map_err(|e| Error::EventFd(e.to_string()))?;

        // Register eventfd with io_uring so completions signal it
        ring.submitter()
            .register_eventfd(event_fd.as_raw_fd())
            .map_err(|e| Error::EventFd(format!("Failed to register eventfd: {e}")))?;

        Ok(Self {
            ring,
            event_fd,
            sqpoll_enabled,
            generation_id: AtomicU64::new(1),
            original_pid: std::process::id(),
            is_active: AtomicBool::new(true),
            buffer_pool: None,
            registered_fds: None,
            provided_buf_group_id: None,
        })
    }

    /// Create ring with SQPOLL fallback.
    fn create_ring(ring_size: u32, try_sqpoll: bool) -> Result<(IoUring, bool)> {
        if try_sqpoll {
            // Try SQPOLL first
            match IoUring::builder()
                .setup_sqpoll(SQPOLL_IDLE_MS)
                .setup_cqsize(ring_size * 2) // CQ larger than SQ
                .build(ring_size)
            {
                Ok(ring) => {
                    tracing::info!("io_uring initialized with SQPOLL");
                    return Ok((ring, true));
                }
                Err(e) => {
                    tracing::warn!(
                        "SQPOLL not available ({}), falling back to batched submissions",
                        e
                    );
                }
            }
        }

        // Fallback to regular io_uring
        let ring = IoUring::builder()
            .setup_cqsize(ring_size * 2)
            .build(ring_size)
            .map_err(|e| Error::RingInit(e.to_string()))?;

        tracing::info!("io_uring initialized without SQPOLL");
        Ok((ring, false))
    }

    /// Create a ring with default settings.
    ///
    /// # Errors
    ///
    /// Returns an error if ring creation fails.
    pub fn with_defaults() -> Result<Self> {
        Self::new(DEFAULT_RING_SIZE, true)
    }

    /// Get the eventfd for Python to poll on.
    #[must_use]
    pub fn event_fd(&self) -> RawFd {
        self.event_fd.as_raw_fd()
    }

    /// Check if SQPOLL is enabled.
    #[must_use]
    pub const fn sqpoll_enabled(&self) -> bool {
        self.sqpoll_enabled
    }

    /// Get the current generation ID.
    #[must_use]
    pub fn generation_id(&self) -> u64 {
        self.generation_id.load(Ordering::SeqCst)
    }

    /// Get the low 16 bits of generation for `user_data` encoding.
    #[must_use]
    pub fn generation_u16(&self) -> u16 {
        (self.generation_id.load(Ordering::SeqCst) & 0xFFFF) as u16
    }

    /// Check for fork and return true if detected.
    #[must_use]
    pub fn check_fork(&self) -> bool {
        std::process::id() != self.original_pid
    }

    /// Register buffer pool with the ring.
    ///
    /// # Errors
    ///
    /// Returns an error if registration fails.
    pub fn register_buffers(&mut self, pool: Arc<BufferPool>) -> Result<()> {
        let iovecs = pool.as_iovecs();

        // SAFETY: The iovecs point to valid mmap'd memory owned by BufferPool
        unsafe {
            self.ring
                .submitter()
                .register_buffers(&iovecs)
                .map_err(|e| {
                    if e.raw_os_error() == Some(12) {
                        Error::RingOp(
                            "register_buffers failed: Cannot allocate memory (ENOMEM). \
                             This usually means the RLIMIT_MEMLOCK is too low. \
                             Try increasing it with 'ulimit -l 65536' or editing /etc/security/limits.conf. \
                             Original error: 12".to_string()
                        )
                    } else {
                        Error::RingOp(format!("register_buffers failed: {e}"))
                    }
                })?;
        }

        self.buffer_pool = Some(pool);
        Ok(())
    }

    /// Signal the eventfd to wake up Python.
    pub fn signal(&self) -> Result<()> {
        self.event_fd
            .write(1)
            .map_err(|e| Error::EventFd(e.to_string()))?;
        Ok(())
    }

    /// Drain the eventfd (call after Python wakes up).
    pub fn drain_eventfd(&self) -> Result<()> {
        // Non-blocking read, ignore EAGAIN
        let _ = self.event_fd.read();
        Ok(())
    }

    /// Get the submitter for submitting new operations.
    #[must_use]
    pub fn submitter(&self) -> Submitter<'_> {
        self.ring.submitter()
    }

    /// Submit pending operations to the kernel.
    ///
    /// # Errors
    ///
    /// Returns an error if submission fails.
    /// Submit pending operations to the kernel.
    ///
    /// # Errors
    ///
    /// Returns an error if submission fails.
    pub fn submit(&mut self) -> Result<usize> {
        // Optimization: Don't submit if SQ is empty
        if self.ring.submission().len() == 0 {
            return Ok(0);
        }

        // Always call submit to ensure operations are flushed to kernel
        // Even with SQPOLL, we need io_uring_enter when the kernel thread is idle
        self.ring
            .submitter()
            .submit()
            .map_err(|e| Error::RingOp(format!("submit failed: {e}")))
    }

    /// Submit and wait for at least one completion.
    ///
    /// # Errors
    ///
    /// Returns an error if submission fails.
    pub fn submit_and_wait(&self, want: usize) -> Result<usize> {
        self.ring
            .submitter()
            .submit_and_wait(want)
            .map_err(|e| Error::RingOp(format!("submit_and_wait failed: {e}")))
    }

    /// Drain completions from the CQ.
    ///
    /// Returns a vector of completion entries.
    pub fn drain_completions(&mut self) -> Vec<CompletionEntry> {
        let mut completions = Vec::new();

        // Access completion queue
        let cq = self.ring.completion();

        for cqe in cq {
            let flags = cqe.flags();
            let entry = CompletionEntry {
                user_data: cqe.user_data(),
                result: cqe.result(),
                flags,
                buffer_index: if cqe_has_buffer(flags) {
                    Some(cqe_buffer_id(flags))
                } else {
                    None
                },
            };
            completions.push(entry);
        }

        completions
    }

    /// Get a mutable reference to the SQ for pushing entries.
    pub fn with_sq<F, R>(&mut self, f: F) -> R
    where
        F: FnOnce(&mut io_uring::squeue::SubmissionQueue<'_>) -> R,
    {
        let mut sq = self.ring.submission();
        f(&mut sq)
    }

    /// Prepare a receive operation with a provided buffer.
    ///
    /// # Safety
    ///
    /// The buffer must remain valid until completion.
    pub unsafe fn prep_recv(
        &mut self,
        fd: RawFd,
        buf: *mut u8,
        len: u32,
        _buf_idx: u16,
        generation: u16,
    ) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::Recv, generation);

        // Use regular Recv with provided buffer
        let entry = opcode::Recv::new(types::Fd(fd), buf, len)
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            sq.push(&entry)
                .map_err(|_| Error::RingOp("push failed".into()))
        })
    }

    /// Prepare a send operation.
    ///
    /// # Safety
    ///
    /// The buffer must remain valid until completion.
    pub unsafe fn prep_send(
        &mut self,
        fd: RawFd,
        buf: *const u8,
        len: u32,
        generation: u16,
    ) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::Send, generation);

        let entry = opcode::Send::new(types::Fd(fd), buf, len)
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            sq.push(&entry)
                .map_err(|_| Error::RingOp("push failed".into()))
        })
    }

    /// Prepare an accept operation.
    pub fn prep_accept(&mut self, fd: RawFd, generation: u16) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::Accept, generation);

        // Use regular Accept instead of AcceptMulti for broader kernel compatibility
        let entry = opcode::Accept::new(types::Fd(fd), std::ptr::null_mut(), std::ptr::null_mut())
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            // SAFETY: Accept is safe to push
            unsafe {
                sq.push(&entry)
                    .map_err(|_| Error::RingOp("push failed".into()))
            }
        })
    }

    /// Prepare a close operation.
    pub fn prep_close(&mut self, fd: RawFd, generation: u16) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::Close, generation);

        let entry = opcode::Close::new(types::Fd(fd))
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            // SAFETY: Close is safe to push
            unsafe {
                sq.push(&entry)
                    .map_err(|_| Error::RingOp("push failed".into()))
            }
        })
    }

    /// Prepare a connect operation with timeout.
    ///
    /// This uses `IOSQE_IO_LINK` to link a connect operation with a timeout.
    /// If the connect doesn't complete within `timeout_ms`, it's cancelled.
    pub fn prep_connect_with_timeout(
        &mut self,
        fd: RawFd,
        addr: *const libc::sockaddr,
        addr_len: libc::socklen_t,
        timeout_ms: u64,
        generation: u16,
    ) -> Result<()> {
        let connect_user_data = encode_user_data(fd, OpType::Connect, generation);
        let timeout_user_data = encode_user_data(fd, OpType::Timeout, generation);

        // Create timespec for timeout
        // nsec is always < 1_000_000_000 so u32 cast is safe
        #[allow(clippy::cast_possible_truncation)]
        let ts = types::Timespec::new()
            .sec(timeout_ms / 1000)
            .nsec(((timeout_ms % 1000) * 1_000_000) as u32);

        // Connect operation with IO_LINK flag to link with timeout
        let connect_entry = opcode::Connect::new(types::Fd(fd), addr, addr_len)
            .build()
            .user_data(connect_user_data)
            .flags(io_uring::squeue::Flags::IO_LINK);

        // Link timeout operation - cancels the linked connect if it takes too long
        let timeout_entry = opcode::LinkTimeout::new(&raw const ts)
            .build()
            .user_data(timeout_user_data);

        self.with_sq(|sq| {
            if sq.len() + 2 > sq.capacity() {
                return Err(Error::RingOp("SQ is full for connect+timeout".into()));
            }

            // SAFETY: Connect and LinkTimeout are safe to push
            unsafe {
                sq.push(&connect_entry)
                    .map_err(|_| Error::RingOp("push connect failed".into()))?;
                sq.push(&timeout_entry)
                    .map_err(|_| Error::RingOp("push link_timeout failed".into()))?;
            }
            Ok(())
        })
    }

    // =========================================================================
    // SOTA 2025 OPTIMIZATIONS
    // =========================================================================

    /// Prepare a standalone timeout operation (native timer).
    ///
    /// Returns completion when `deadline_ns` (absolute monotonic time) is reached.
    /// Use `encode_user_data(timer_id, OpType::Timeout, gen)` for user_data.
    pub fn prep_timeout(&mut self, deadline_ns: u64, user_data: u64) -> Result<()> {
        // Convert nanoseconds to timespec
        #[allow(clippy::cast_possible_truncation)]
        let ts = types::Timespec::new()
            .sec((deadline_ns / 1_000_000_000) as i64 as u64)
            .nsec((deadline_ns % 1_000_000_000) as u32);

        let entry = opcode::Timeout::new(&raw const ts)
            .flags(types::TimeoutFlags::ABS) // Absolute timeout
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            unsafe {
                sq.push(&entry)
                    .map_err(|_| Error::RingOp("push timeout failed".into()))
            }
        })
    }

    /// Cancel a pending timeout operation.
    pub fn cancel_timeout(&mut self, user_data: u64) -> Result<()> {
        let entry = opcode::TimeoutRemove::new(user_data)
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            unsafe {
                sq.push(&entry)
                    .map_err(|_| Error::RingOp("push timeout_remove failed".into()))
            }
        })
    }

    /// Prepare multishot receive (kernel 5.19+).
    ///
    /// One submission handles ALL future data on this socket until cancelled.
    /// Completions have CQE_F_MORE flag when more data is expected.
    ///
    /// # Safety
    ///
    /// Requires kernel 5.19+. May fail with EINVAL on older kernels.
    pub fn prep_recv_multishot(
        &mut self,
        fd: RawFd,
        buf_group_id: u16,
        generation: u16,
    ) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::RecvMulti, generation);

        // RecvMulti uses buffer group selection (IOSQE_BUFFER_SELECT)
        let entry = opcode::RecvMulti::new(types::Fd(fd), buf_group_id)
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            unsafe {
                sq.push(&entry)
                    .map_err(|_| Error::RingOp("push recv_multi failed".into()))
            }
        })
    }

    // =========================================================================
    // SOTA 2025: Registered FD Table (IOSQE_FIXED_FILE)
    // =========================================================================

    /// Register file descriptors for IOSQE_FIXED_FILE optimization.
    ///
    /// After registration, use `prep_recv_fixed(fd_index, ...)` instead of raw FDs.
    /// This eliminates per-operation FD lookup overhead.
    pub fn register_fds(&mut self, fds: &[RawFd]) -> Result<()> {
        self.ring
            .submitter()
            .register_files(fds)
            .map_err(|e| Error::RingOp(format!("register_files failed: {e}")))?;
        self.registered_fds = Some(fds.to_vec());
        Ok(())
    }

    /// Unregister previously registered file descriptors.
    pub fn unregister_fds(&mut self) -> Result<()> {
        if self.registered_fds.is_some() {
            self.ring
                .submitter()
                .unregister_files()
                .map_err(|e| Error::RingOp(format!("unregister_files failed: {e}")))?;
            self.registered_fds = None;
        }
        Ok(())
    }

    /// Get the index of a registered FD, or None if not registered.
    pub fn fd_index(&self, fd: RawFd) -> Option<u32> {
        self.registered_fds
            .as_ref()
            .and_then(|fds| fds.iter().position(|&f| f == fd).map(|i| i as u32))
    }

    // =========================================================================
    // SOTA 2025: Zero-Copy Send (SEND_ZC)
    // =========================================================================

    /// Prepare zero-copy send (kernel 6.0+).
    ///
    /// For large payloads, avoids copying data into kernel.
    ///
    /// # Safety
    ///
    /// Buffer must remain valid until IORING_CQE_F_NOTIF completion.
    pub unsafe fn prep_send_zc(
        &mut self,
        fd: RawFd,
        buf: *const u8,
        len: u32,
        generation: u16,
    ) -> Result<()> {
        let user_data = encode_user_data(fd, OpType::SendZC, generation);

        // Use SendZc opcode
        let entry = opcode::SendZc::new(types::Fd(fd), buf, len)
            .build()
            .user_data(user_data);

        self.with_sq(|sq| {
            if sq.is_full() {
                return Err(Error::RingOp("SQ is full".into()));
            }
            sq.push(&entry)
                .map_err(|_| Error::RingOp("push send_zc failed".into()))
        })
    }

    /// Get ring capabilities for feature detection.
    pub fn capabilities(&self) -> RingCapabilities {
        RingCapabilities {
            sqpoll: self.sqpoll_enabled,
            registered_fds: self.registered_fds.is_some(),
            // Note: Full capability detection would require probing the kernel
            multishot_recv: true, // Assume available, will fail gracefully if not
            send_zc: true,        // Assume available, will fail gracefully if not
            provided_buffers: self.provided_buf_group_id.is_some(),
        }
    }

    /// Shutdown the ring.
    pub fn shutdown(&mut self) {
        self.is_active.store(false, Ordering::SeqCst);
        // Explicitly unregister buffers and FDs to release resources
        if self.buffer_pool.is_some() {
            let _ = self.ring.submitter().unregister_buffers();
            self.buffer_pool = None;
        }
        if self.registered_fds.is_some() {
            let _ = self.ring.submitter().unregister_files();
            self.registered_fds = None;
        }
    }
}

/// Ring capabilities for feature detection.
#[derive(Debug, Clone, Copy)]
pub struct RingCapabilities {
    /// SQPOLL mode enabled
    pub sqpoll: bool,
    /// Registered FD table active
    pub registered_fds: bool,
    /// Multishot recv available (kernel 5.19+)
    pub multishot_recv: bool,
    /// Zero-copy send available (kernel 6.0+)
    pub send_zc: bool,
    /// Provided buffer ring active
    pub provided_buffers: bool,
}

impl Drop for Ring {
    fn drop(&mut self) {
        // Fallback cleanup if shutdown wasn't called
        if self.buffer_pool.is_some() {
            let _ = self.ring.submitter().unregister_buffers();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_user_data_encoding() {
        let fd = 42i32;
        let op = OpType::Recv;
        let gen = 1u16;

        let user_data = encode_user_data(fd, op, gen);

        let entry = CompletionEntry {
            user_data,
            result: 0,
            flags: 0,
            buffer_index: None,
        };

        assert_eq!(entry.fd(), fd);
        assert_eq!(entry.op_type(), OpType::Recv);
        assert_eq!(entry.generation(), gen);
    }

    #[test]
    fn test_ring_creation() {
        // Skip if io_uring not supported
        if let Ok(ring) = Ring::new(64, false) {
            assert!(!ring.sqpoll_enabled());
            assert!(ring.event_fd() >= 0);
        }
    }

    #[test]
    fn test_fork_detection() {
        if let Ok(ring) = Ring::new(64, false) {
            assert!(!ring.check_fork());
        }
    }

    #[test]
    fn test_buffer_flags() {
        // Test buffer flag detection
        let flags_with_buffer = IORING_CQE_F_BUFFER | (42u32 << 16);
        assert!(cqe_has_buffer(flags_with_buffer));
        assert_eq!(cqe_buffer_id(flags_with_buffer), 42);

        let flags_without_buffer = 0u32;
        assert!(!cqe_has_buffer(flags_without_buffer));
    }
}
