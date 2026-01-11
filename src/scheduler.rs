use parking_lot::Mutex;
use pyo3::prelude::*;
use std::collections::VecDeque;
use std::sync::Arc;

/// A mutex-protected ready queue for Python tasks using `VecDeque`.
///
/// This optimized implementation reduces allocation and improves cache locality
/// for single-threaded asyncio workloads compared to channel-based solutions.
#[derive(Clone)]
pub struct Scheduler {
    queue: Arc<Mutex<VecDeque<Py<PyAny>>>>,
}

impl Default for Scheduler {
    fn default() -> Self {
        Self::new()
    }
}

impl Scheduler {
    #[must_use]
    pub fn new() -> Self {
        Self {
            queue: Arc::new(Mutex::new(VecDeque::with_capacity(256))),
        }
    }

    /// Push a task to the ready queue.
    pub fn push(&self, handle: Py<PyAny>) {
        self.queue.lock().push_back(handle);
    }

    /// Pop a task from the ready queue.
    #[must_use]
    pub fn pop(&self) -> Option<Py<PyAny>> {
        self.queue.lock().pop_front()
    }

    /// Check if the queue is empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.queue.lock().is_empty()
    }

    /// Get the number of pending tasks.
    #[must_use]
    pub fn len(&self) -> usize {
        self.queue.lock().len()
    }

    /// Drain all items from the queue efficiently.
    /// This swaps the underlying queue with a new empty one to minimize lock hold time.
    #[must_use]
    pub fn drain(&self) -> VecDeque<Py<PyAny>> {
        let mut queue = self.queue.lock();
        if queue.is_empty() {
            return VecDeque::new();
        }

        let count = queue.len();
        let mut new_queue = VecDeque::with_capacity(count);
        std::mem::swap(&mut *queue, &mut new_queue);
        new_queue
    }

    /// Clear all items from the queue.
    pub fn clear(&self) {
        self.queue.lock().clear();
    }
}
