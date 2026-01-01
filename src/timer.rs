use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

#[derive(Debug)]
struct TimerEntry {
    expiration: f64,
    handle: PyObject,
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
        other.expiration.partial_cmp(&self.expiration).unwrap_or(Ordering::Equal)
    }
}

pub struct TimerHeap {
    heap: BinaryHeap<TimerEntry>,
}

impl TimerHeap {
    pub fn new() -> Self {
        Self {
            heap: BinaryHeap::new(),
        }
    }

    pub fn push(&mut self, expiration: f64, handle: PyObject) {
        self.heap.push(TimerEntry { expiration, handle });
    }

    pub fn pop_expired(&mut self, now: f64) -> Vec<PyObject> {
        let mut expired = Vec::new();
        while let Some(top) = self.heap.peek() {
            if top.expiration <= now {
                if let Some(entry) = self.heap.pop() {
                    expired.push(entry.handle);
                }
            } else {
                break;
            }
        }
        expired
    }

    pub fn next_expiration(&self) -> Option<f64> {
        self.heap.peek().map(|entry| entry.expiration)
    }

    pub fn len(&self) -> usize {
        self.heap.len()
    }
}
