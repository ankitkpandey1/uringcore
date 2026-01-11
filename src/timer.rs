use parking_lot::Mutex;
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

#[derive(Debug)]
struct TimerEntry {
    expiration: f64,
    handle: Py<PyAny>,
}

impl PartialEq for TimerEntry {
    fn eq(&self, other: &Self) -> bool {
        self.expiration == other.expiration
    }
}

impl Eq for TimerEntry {}

impl PartialOrd for TimerEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

// Reverse ordering for Min-Heap behavior
impl Ord for TimerEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        // We want the smallest expiration to be greater (popped first)
        other
            .expiration
            .partial_cmp(&self.expiration)
            .unwrap_or(Ordering::Equal)
    }
}

pub struct TimerHeap {
    heap: Mutex<BinaryHeap<TimerEntry>>,
}

impl Default for TimerHeap {
    fn default() -> Self {
        Self::new()
    }
}

impl TimerHeap {
    #[must_use]
    pub fn new() -> Self {
        Self {
            heap: Mutex::new(BinaryHeap::new()),
        }
    }

    pub fn push(&self, expiration: f64, handle: Py<PyAny>) {
        self.heap.lock().push(TimerEntry { expiration, handle });
    }

    pub fn pop_expired(&self, now: f64) -> Vec<Py<PyAny>> {
        let mut heap = self.heap.lock();
        let mut expired = Vec::new();
        while let Some(top) = heap.peek() {
            if top.expiration <= now {
                if let Some(entry) = heap.pop() {
                    expired.push(entry.handle);
                }
            } else {
                break;
            }
        }
        expired
    }

    #[must_use]
    pub fn next_expiration(&self) -> Option<f64> {
        self.heap.lock().peek().map(|entry| entry.expiration)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.heap.lock().len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.heap.lock().is_empty()
    }
}
