use parking_lot::Mutex;
use pyo3::prelude::*;
use std::collections::VecDeque;
use std::sync::Arc;

/// A thread-safe ready queue for Python tasks.
/// Stores PyObject references (handles).
#[derive(Clone)]
pub struct Scheduler {
    ready: Arc<Mutex<VecDeque<PyObject>>>,
}

impl Scheduler {
    pub fn new() -> Self {
        Self {
            ready: Arc::new(Mutex::new(VecDeque::with_capacity(1024))),
        }
    }

    /// Push a task to the ready queue.
    pub fn push(&self, handle: PyObject) {
        self.ready.lock().push_back(handle);
    }

    /// Pop a task from the ready queue.
    pub fn pop(&self) -> Option<PyObject> {
        self.ready.lock().pop_front()
    }

    /// Check if the queue is empty.
    pub fn is_empty(&self) -> bool {
        self.ready.lock().is_empty()
    }

    /// Get the number of pending tasks.
    pub fn len(&self) -> usize {
        self.ready.lock().len()
    }
}
