use parking_lot::Mutex;
use pyo3::prelude::*;
use std::collections::VecDeque;

/// A thread-safe queue for scheduled Python tasks.
pub struct Scheduler {
    /// Queue of (handle, context) tuples
    /// Ideally the handle itself contains context, but for now just PyObject handle
    ready_queue: Mutex<VecDeque<PyObject>>,
}

impl Scheduler {
    pub fn new() -> Self {
        Self {
            ready_queue: Mutex::new(VecDeque::new()),
        }
    }

    /// Push a Python handle to the ready queue.
    pub fn push(&self, handle: PyObject) {
        self.ready_queue.lock().push_back(handle);
    }

    /// Pop a batch of handles to run.
    /// limiting batch size ensures we don't starve I/O polling indefinitely.
    pub fn pop_batch(&self, limit: usize) -> Vec<PyObject> {
        let mut queue = self.ready_queue.lock();
        let count = queue.len().min(limit);
        let mut batch = Vec::with_capacity(count);
        for _ in 0..count {
            if let Some(handle) = queue.pop_front() {
                batch.push(handle);
            }
        }
        batch
    }
    
    pub fn len(&self) -> usize {
        self.ready_queue.lock().len()
    }
    
    pub fn is_empty(&self) -> bool {
        self.ready_queue.lock().is_empty()
    }
}
