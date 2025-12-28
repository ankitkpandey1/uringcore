//! Buffer pool management for zero-copy I/O operations.
//!
//! This module provides a pre-allocated, page-aligned buffer pool that is
//! registered with `io_uring` for zero-copy operations. Buffers are wrapped
//! in `PyCapsule` for safe handoff to Python with automatic return on GC.

// Lock ordering is intentional and correct
#![allow(clippy::significant_drop_tightening)]

use parking_lot::Mutex;
use std::collections::VecDeque;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use crate::error::{Error, Result};

/// Default buffer size (64KB - optimal for network I/O)
pub const DEFAULT_BUFFER_SIZE: usize = 64 * 1024;

/// Default number of buffers in the pool (enough for high concurrency)
pub const DEFAULT_BUFFER_COUNT: usize = 1024;

/// Quarantine duration before buffer reuse (reduced for high throughput)
const QUARANTINE_DURATION: Duration = Duration::from_millis(1);

/// A reference to a buffer in the pool with offset tracking for partial reads.
#[derive(Debug)]
pub struct BufferRef {
    /// Index in the buffer pool
    pub index: u16,
    /// Number of valid bytes in this buffer
    pub len: usize,
    /// Current read offset for partial consumption
    pub offset: usize,
    /// Generation ID for fork-safety validation
    pub generation_id: u64,
}

impl BufferRef {
    /// Create a new buffer reference.
    #[must_use]
    pub const fn new(index: u16, len: usize, generation_id: u64) -> Self {
        Self {
            index,
            len,
            offset: 0,
            generation_id,
        }
    }

    /// Returns the remaining unread bytes.
    #[must_use]
    pub const fn remaining(&self) -> usize {
        self.len.saturating_sub(self.offset)
    }

    /// Returns true if all data has been consumed.
    #[must_use]
    pub const fn is_consumed(&self) -> bool {
        self.offset >= self.len
    }

    /// Advance the offset by the given amount.
    pub fn advance(&mut self, amount: usize) {
        self.offset = self.offset.saturating_add(amount).min(self.len);
    }
}

/// Entry in the quarantine queue for buffer safety.
struct QuarantineEntry {
    index: u16,
    release_time: Instant,
}

/// Pre-allocated buffer pool registered with `io_uring`.
///
/// Buffers are page-aligned and mlock'd to prevent swapping.
/// The pool manages buffer lifecycle with quarantine for GC safety.
pub struct BufferPool {
    /// Base pointer to the mmap'd region
    base: NonNull<u8>,
    /// Size of each buffer
    buffer_size: usize,
    /// Total number of buffers
    buffer_count: usize,
    /// Total allocated size
    total_size: usize,
    /// Free buffer indices available for use
    free_list: Mutex<VecDeque<u16>>,
    /// Buffers in quarantine waiting to be reused
    quarantine: Mutex<VecDeque<QuarantineEntry>>,
    /// Current generation ID (incremented on fork)
    generation_id: AtomicU64,
}

// SAFETY: The buffer pool uses interior mutability via Mutex and atomic operations.
// The mmap'd memory is only accessed through controlled methods.
unsafe impl Send for BufferPool {}
unsafe impl Sync for BufferPool {}

impl BufferPool {
    /// Create a new buffer pool with the specified size and count.
    ///
    /// # Errors
    ///
    /// Returns an error if mmap or mlock fails.
    pub fn new(buffer_size: usize, buffer_count: usize) -> Result<Self> {
        let total_size = buffer_size
            .checked_mul(buffer_count)
            .ok_or_else(|| Error::BufferAllocation("size overflow".into()))?;

        // Allocate page-aligned memory using mmap
        let base = unsafe {
            let ptr = libc::mmap(
                std::ptr::null_mut(),
                total_size,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_POPULATE,
                -1,
                0,
            );

            if ptr == libc::MAP_FAILED {
                return Err(Error::BufferAllocation(format!(
                    "mmap failed: {}",
                    std::io::Error::last_os_error()
                )));
            }

            // Try to lock memory (non-fatal if it fails)
            let _ = libc::mlock(ptr, total_size);

            NonNull::new(ptr.cast::<u8>())
                .ok_or_else(|| Error::BufferAllocation("mmap returned null".into()))?
        };

        // Initialize free list with all buffer indices
        #[allow(clippy::cast_possible_truncation)]
        let free_list: VecDeque<u16> = (0..buffer_count as u16).collect();

        Ok(Self {
            base,
            buffer_size,
            buffer_count,
            total_size,
            free_list: Mutex::new(free_list),
            quarantine: Mutex::new(VecDeque::new()),
            generation_id: AtomicU64::new(1),
        })
    }

    /// Create a buffer pool with default settings.
    ///
    /// # Errors
    ///
    /// Returns an error if allocation fails.
    pub fn with_defaults() -> Result<Self> {
        Self::new(DEFAULT_BUFFER_SIZE, DEFAULT_BUFFER_COUNT)
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

    /// Acquire a buffer from the pool.
    ///
    /// Returns None if no buffers are available.
    pub fn acquire(&self) -> Option<u16> {
        // First, try to reclaim quarantined buffers
        self.reclaim_quarantined();

        self.free_list.lock().pop_front()
    }

    /// Return a buffer to the pool (goes through quarantine).
    pub fn release(&self, index: u16, generation_id: u64) {
        // Validate generation ID to prevent use-after-fork
        if generation_id != self.generation_id.load(Ordering::SeqCst) {
            tracing::warn!(
                "Dropping buffer {} with stale generation {} (current: {})",
                index,
                generation_id,
                self.generation_id.load(Ordering::SeqCst)
            );
            return;
        }

        self.quarantine.lock().push_back(QuarantineEntry {
            index,
            release_time: Instant::now(),
        });
    }

    /// Reclaim buffers that have completed their quarantine period.
    fn reclaim_quarantined(&self) {
        let now = Instant::now();
        let mut quarantine = self.quarantine.lock();
        let mut free_list = self.free_list.lock();

        while let Some(entry) = quarantine.front() {
            if now.duration_since(entry.release_time) >= QUARANTINE_DURATION {
                if let Some(entry) = quarantine.pop_front() {
                    free_list.push_back(entry.index);
                }
            } else {
                break;
            }
        }
    }

    /// Get a pointer to a specific buffer by index.
    ///
    /// # Safety
    ///
    /// The caller must ensure the index is valid and the buffer is currently owned.
    #[must_use]
    pub unsafe fn get_buffer_ptr(&self, index: u16) -> *mut u8 {
        debug_assert!((index as usize) < self.buffer_count);
        self.base.as_ptr().add(index as usize * self.buffer_size)
    }

    /// Get a slice view of a buffer.
    ///
    /// # Safety
    ///
    /// The caller must ensure the index is valid, the buffer is owned,
    /// and len does not exceed the buffer size.
    #[must_use]
    pub unsafe fn get_buffer_slice(&self, index: u16, len: usize) -> &[u8] {
        let ptr = self.get_buffer_ptr(index);
        std::slice::from_raw_parts(ptr, len.min(self.buffer_size))
    }

    /// Get a mutable slice view of a buffer for writing.
    ///
    /// # Safety
    ///
    /// The caller must ensure the index is valid, the buffer is owned,
    /// and len does not exceed the buffer size.
    #[must_use]
    #[allow(clippy::mut_from_ref)] // Intentional: raw pointer to mutable slice for FFI
    pub unsafe fn get_buffer_slice_mut(&self, index: u16, len: usize) -> &mut [u8] {
        let ptr = self.get_buffer_ptr(index).cast::<u8>();
        std::slice::from_raw_parts_mut(ptr, len.min(self.buffer_size))
    }

    /// Get the size of each buffer.
    #[must_use]
    pub const fn buffer_size(&self) -> usize {
        self.buffer_size
    }

    /// Get the total number of buffers.
    #[must_use]
    pub const fn buffer_count(&self) -> usize {
        self.buffer_count
    }

    /// Get the base pointer for `io_uring` registration.
    #[must_use]
    pub fn base_ptr(&self) -> *mut u8 {
        self.base.as_ptr()
    }

    /// Get the iovec array for `io_uring` buffer registration.
    #[must_use]
    #[allow(clippy::cast_possible_truncation)]
    pub fn as_iovecs(&self) -> Vec<libc::iovec> {
        (0..self.buffer_count)
            .map(|i| libc::iovec {
                iov_base: unsafe { self.get_buffer_ptr(i as u16).cast() },
                iov_len: self.buffer_size,
            })
            .collect()
    }

    /// Get statistics about buffer pool usage.
    #[must_use]
    pub fn stats(&self) -> BufferPoolStats {
        let free_count = self.free_list.lock().len();
        let quarantine_count = self.quarantine.lock().len();
        BufferPoolStats {
            total: self.buffer_count,
            free: free_count,
            quarantined: quarantine_count,
            in_use: self.buffer_count - free_count - quarantine_count,
        }
    }
}

impl Drop for BufferPool {
    fn drop(&mut self) {
        unsafe {
            let _ = libc::munlock(self.base.as_ptr().cast(), self.total_size);
            let _ = libc::munmap(self.base.as_ptr().cast(), self.total_size);
        }
    }
}

/// Statistics about buffer pool usage.
#[derive(Debug, Clone, Copy)]
pub struct BufferPoolStats {
    /// Total number of buffers in the pool
    pub total: usize,
    /// Number of free buffers available
    pub free: usize,
    /// Number of buffers in quarantine
    pub quarantined: usize,
    /// Number of buffers currently in use
    pub in_use: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_buffer_pool_creation() {
        let pool = BufferPool::new(4096, 16).unwrap();
        assert_eq!(pool.buffer_count(), 16);
        assert_eq!(pool.buffer_size(), 4096);
    }

    #[test]
    fn test_buffer_acquire_release() {
        let pool = BufferPool::new(4096, 4).unwrap();
        let gen_id = pool.generation_id();

        // Acquire all 4 buffers
        let idx1 = pool.acquire().unwrap();
        let idx2 = pool.acquire().unwrap();
        let idx3 = pool.acquire().unwrap();
        let idx4 = pool.acquire().unwrap();
        assert_ne!(idx1, idx2);
        assert_ne!(idx2, idx3);
        assert_ne!(idx3, idx4);

        // No more buffers available
        assert!(pool.acquire().is_none());

        // Release one and wait for quarantine
        pool.release(idx1, gen_id);
        std::thread::sleep(Duration::from_millis(10));

        // Now we should be able to acquire one again
        let idx5 = pool.acquire().unwrap();
        assert_eq!(idx5, idx1); // Should get the released buffer back
    }

    #[test]
    fn test_buffer_ref_partial_consumption() {
        let mut buf_ref = BufferRef::new(0, 100, 1);
        assert_eq!(buf_ref.remaining(), 100);
        assert!(!buf_ref.is_consumed());

        buf_ref.advance(30);
        assert_eq!(buf_ref.remaining(), 70);
        assert_eq!(buf_ref.offset, 30);

        buf_ref.advance(70);
        assert_eq!(buf_ref.remaining(), 0);
        assert!(buf_ref.is_consumed());
    }

    #[test]
    fn test_generation_id() {
        let pool = BufferPool::new(4096, 4).unwrap();
        let gen1 = pool.generation_id();
        let gen2 = pool.increment_generation();
        assert_eq!(gen2, gen1 + 1);
        assert_eq!(pool.generation_id(), gen2);
    }
}
