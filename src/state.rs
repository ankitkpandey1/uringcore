//! Sovereign FD State Machine for completion-driven I/O.
//!
//! Each file descriptor is tracked by an [`FDState`] struct that maintains:
//! - Inflight operation count
//! - Buffer queue for pre-filled data
//! - Credit budget for backpressure
//! - Generation ID for fork-safety
//! - Pending offset for FIFO partial consumption

// These casts are intentional and safe for our use case
#![allow(clippy::cast_possible_truncation)]
// Lock scope is correct - we need the lock held while accessing state
#![allow(clippy::significant_drop_tightening)]

use parking_lot::RwLock;
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::buffer::BufferRef;
use crate::error::{Error, Result};

/// Default credit budget per FD (max inflight + queued)
pub const DEFAULT_CREDIT_BUDGET: u32 = 64;

/// State for a single file descriptor.
#[derive(Debug)]
pub struct FDState {
    /// Active operations currently in the kernel ring
    pub inflight_count: u32,
    /// Pre-filled data buffers waiting for Python consumption
    pub buffer_queue: VecDeque<BufferRef>,
    /// Maximum allowed (inflight + queued) for backpressure
    pub credit_budget: u32,
    /// Generation ID for fork-safety validation
    pub generation_id: u64,
    /// Whether reading is paused (from transport layer)
    pub is_paused: bool,
    /// Socket type info for proper handling
    pub socket_type: SocketType,
}

/// Type of socket for specialized handling.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocketType {
    /// TCP stream socket
    TcpStream,
    /// TCP listener socket
    TcpListener,
    /// UDP socket
    Udp,
    /// Unix stream socket
    UnixStream,
    /// Unix listener socket
    UnixListener,
    /// Pipe or other FD
    Other,
}

impl FDState {
    /// Create a new FD state.
    #[must_use]
    pub fn new(generation_id: u64, socket_type: SocketType) -> Self {
        Self {
            inflight_count: 0,
            buffer_queue: VecDeque::new(),
            credit_budget: DEFAULT_CREDIT_BUDGET,
            generation_id,
            is_paused: false,
            socket_type,
        }
    }

    /// Check if we have credit available for new operations.
    #[must_use]
    pub fn has_credit(&self) -> bool {
        let used = self.inflight_count + self.buffer_queue.len() as u32;
        used < self.credit_budget
    }

    /// Get available credit for new submissions.
    #[must_use]
    pub fn available_credit(&self) -> u32 {
        let used = self.inflight_count + self.buffer_queue.len() as u32;
        self.credit_budget.saturating_sub(used)
    }

    /// Check if this FD should accept new receive submissions.
    #[must_use]
    pub fn should_submit_recv(&self) -> bool {
        !self.is_paused && self.has_credit()
    }

    /// Record that an operation was submitted.
    pub fn on_submit(&mut self) {
        self.inflight_count = self.inflight_count.saturating_add(1);
    }

    /// Record that an operation completed with data.
    pub fn on_completion(&mut self, buffer: BufferRef) {
        self.inflight_count = self.inflight_count.saturating_sub(1);
        self.buffer_queue.push_back(buffer);
    }

    /// Record that an operation completed without data (error or close).
    pub fn on_completion_empty(&mut self) {
        self.inflight_count = self.inflight_count.saturating_sub(1);
    }

    /// Take the next buffer from the queue for Python consumption.
    pub fn take_buffer(&mut self) -> Option<BufferRef> {
        self.buffer_queue.pop_front()
    }

    /// Peek at the next buffer without removing it.
    #[must_use]
    pub fn peek_buffer(&self) -> Option<&BufferRef> {
        self.buffer_queue.front()
    }

    /// Get a mutable reference to the front buffer for partial consumption.
    pub fn peek_buffer_mut(&mut self) -> Option<&mut BufferRef> {
        self.buffer_queue.front_mut()
    }

    /// Pause reading (called from transport layer).
    pub fn pause(&mut self) {
        self.is_paused = true;
    }

    /// Resume reading (called from transport layer).
    pub fn resume(&mut self) {
        self.is_paused = false;
    }

    /// Check if this state matches the current generation.
    #[must_use]
    pub fn is_valid_generation(&self, current_gen: u64) -> bool {
        self.generation_id == current_gen
    }
}

/// Manager for all FD states.
pub struct FDStateManager {
    /// Map of FD to its state
    states: RwLock<HashMap<i32, FDState>>,
    /// Current generation ID
    generation_id: AtomicU64,
}

impl FDStateManager {
    /// Create a new FD state manager.
    #[must_use]
    pub fn new() -> Self {
        Self {
            states: RwLock::new(HashMap::new()),
            generation_id: AtomicU64::new(1),
        }
    }

    /// Get the current generation ID.
    #[must_use]
    pub fn generation_id(&self) -> u64 {
        self.generation_id.load(Ordering::SeqCst)
    }

    /// Increment generation ID (called after fork).
    pub fn increment_generation(&self) -> u64 {
        self.generation_id.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// Register a new FD.
    pub fn register(&self, fd: i32, socket_type: SocketType) {
        let gen_id = self.generation_id.load(Ordering::SeqCst);
        self.states
            .write()
            .insert(fd, FDState::new(gen_id, socket_type));
    }

    /// Unregister an FD and return any pending buffers.
    pub fn unregister(&self, fd: i32) -> Option<Vec<BufferRef>> {
        self.states
            .write()
            .remove(&fd)
            .map(|state| state.buffer_queue.into_iter().collect())
    }

    /// Execute a function with read access to an FD state.
    pub fn with_state<F, R>(&self, fd: i32, f: F) -> Result<R>
    where
        F: FnOnce(&FDState) -> R,
    {
        let states = self.states.read();
        let state = states
            .get(&fd)
            .ok_or_else(|| Error::Fd(format!("FD {fd} not registered")))?;

        // Validate generation
        let current_gen = self.generation_id.load(Ordering::SeqCst);
        if !state.is_valid_generation(current_gen) {
            return Err(Error::InvalidGeneration {
                expected: current_gen,
                got: state.generation_id,
            });
        }

        Ok(f(state))
    }

    /// Execute a function with write access to an FD state.
    pub fn with_state_mut<F, R>(&self, fd: i32, f: F) -> Result<R>
    where
        F: FnOnce(&mut FDState) -> R,
    {
        let mut states = self.states.write();
        let state = states
            .get_mut(&fd)
            .ok_or_else(|| Error::Fd(format!("FD {fd} not registered")))?;

        // Validate generation
        let current_gen = self.generation_id.load(Ordering::SeqCst);
        if !state.is_valid_generation(current_gen) {
            return Err(Error::InvalidGeneration {
                expected: current_gen,
                got: state.generation_id,
            });
        }

        Ok(f(state))
    }

    /// Check if an FD should accept new receive submissions.
    pub fn should_submit_recv(&self, fd: i32) -> bool {
        self.states
            .read()
            .get(&fd)
            .is_some_and(FDState::should_submit_recv)
    }

    /// Pause reading for an FD.
    pub fn pause_reading(&self, fd: i32) -> Result<()> {
        self.with_state_mut(fd, FDState::pause)
    }

    /// Resume reading for an FD.
    pub fn resume_reading(&self, fd: i32) -> Result<()> {
        self.with_state_mut(fd, FDState::resume)
    }

    /// Invalidate all states (called after fork detection).
    pub fn invalidate_all(&self) -> Vec<(i32, Vec<BufferRef>)> {
        let mut states = self.states.write();
        let result: Vec<_> = states
            .drain()
            .map(|(fd, state)| {
                let buffers: Vec<_> = state.buffer_queue.into_iter().collect();
                (fd, buffers)
            })
            .collect();
        result
    }

    /// Get statistics for all FDs.
    #[must_use]
    pub fn stats(&self) -> FDStateStats {
        let states = self.states.read();
        let mut total_inflight = 0u64;
        let mut total_queued = 0u64;
        let mut paused_count = 0usize;

        for state in states.values() {
            total_inflight += u64::from(state.inflight_count);
            total_queued += state.buffer_queue.len() as u64;
            if state.is_paused {
                paused_count += 1;
            }
        }

        FDStateStats {
            fd_count: states.len(),
            total_inflight,
            total_queued,
            paused_count,
        }
    }
}

impl Default for FDStateManager {
    fn default() -> Self {
        Self::new()
    }
}

/// Statistics for FD state manager.
#[derive(Debug, Clone, Copy)]
pub struct FDStateStats {
    /// Number of registered FDs
    pub fd_count: usize,
    /// Total inflight operations across all FDs
    pub total_inflight: u64,
    /// Total queued buffers across all FDs
    pub total_queued: u64,
    /// Number of paused FDs
    pub paused_count: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fd_state_credit() {
        let mut state = FDState::new(1, SocketType::TcpStream);
        assert!(state.has_credit());
        assert_eq!(state.available_credit(), DEFAULT_CREDIT_BUDGET);

        // Simulate submissions
        for _ in 0..DEFAULT_CREDIT_BUDGET {
            state.on_submit();
        }

        assert!(!state.has_credit());
        assert_eq!(state.available_credit(), 0);
    }

    #[test]
    fn test_fd_state_pause_resume() {
        let mut state = FDState::new(1, SocketType::TcpStream);
        assert!(state.should_submit_recv());

        state.pause();
        assert!(!state.should_submit_recv());

        state.resume();
        assert!(state.should_submit_recv());
    }

    #[test]
    fn test_fd_state_manager() {
        let manager = FDStateManager::new();

        manager.register(5, SocketType::TcpStream);

        let result = manager.with_state(5, FDState::has_credit);
        assert!(result.is_ok());
        assert!(result.unwrap());

        let result = manager.with_state(999, FDState::has_credit);
        assert!(result.is_err());
    }

    #[test]
    fn test_generation_validation() {
        let manager = FDStateManager::new();
        manager.register(5, SocketType::TcpStream);

        // Should work with current generation
        assert!(manager.with_state(5, |_| ()).is_ok());

        // Increment generation and register new FD
        manager.increment_generation();

        // Old FD should now fail validation
        assert!(manager.with_state(5, |_| ()).is_err());
    }
}
