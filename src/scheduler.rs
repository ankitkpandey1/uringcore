use crossbeam_channel::{unbounded, Receiver, Sender};
use pyo3::prelude::*;

/// A lock-free ready queue for Python tasks using crossbeam MPSC channel.
/// This eliminates mutex contention in high-concurrency scenarios like gather(100).
#[derive(Clone)]
pub struct Scheduler {
    sender: Sender<PyObject>,
    receiver: Receiver<PyObject>,
}

impl Default for Scheduler {
    fn default() -> Self {
        Self::new()
    }
}

impl Scheduler {
    #[must_use]
    pub fn new() -> Self {
        let (sender, receiver) = unbounded();
        Self { sender, receiver }
    }

    /// Push a task to the ready queue (lock-free).
    pub fn push(&self, handle: PyObject) {
        // unbounded channel never blocks on send
        let _ = self.sender.send(handle);
    }

    /// Pop a task from the ready queue.
    #[must_use]
    pub fn pop(&self) -> Option<PyObject> {
        self.receiver.try_recv().ok()
    }

    /// Check if the queue is empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.receiver.is_empty()
    }

    /// Get the number of pending tasks.
    #[must_use]
    pub fn len(&self) -> usize {
        self.receiver.len()
    }

    /// Drain all items from the queue efficiently (lock-free iteration).
    #[must_use]
    pub fn drain(&self) -> Vec<PyObject> {
        self.receiver.try_iter().collect()
    }

    /// Clear all items from the queue.
    pub fn clear(&self) {
        // Drain and drop all items
        for _ in self.receiver.try_iter() {}
    }
}
