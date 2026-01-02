use std::collections::HashMap;
use std::os::unix::io::RawFd;

/// Manages registered file descriptors for `IORING_REGISTER_FILES`.
///
/// Maps `RawFd` to a fixed index `u32`.
/// Handles allocation of free indices and tracking of registered files.
#[derive(Debug)]
pub struct FixedFdTable {
    /// Mapping from `RawFd` to Fixed Index
    index_map: HashMap<RawFd, u32>,
    /// The actual array of FDs (sparse, -1 for empty)
    /// This mirrors the kernel's registered files array.
    files: Vec<RawFd>,
    /// Stack of free indices for reuse
    free_indices: Vec<u32>,
}

impl FixedFdTable {
    /// Create a new table with a given capacity.
    ///
    /// The capacity determines the initial size of the registered files array.
    #[must_use]
    pub fn new(capacity: u32) -> Self {
        let cap = capacity as usize;
        let mut files = Vec::with_capacity(cap);
        // Initialize with -1 (meaning no file)
        files.resize(cap, -1);

        // All indices are initially free, pushing in reverse order so 0 is popped first
        let mut free_indices = Vec::with_capacity(cap);
        for i in (0..capacity).rev() {
            free_indices.push(i);
        }
        Self {
            index_map: HashMap::new(),
            files,
            free_indices,
        }
    }

    /// Initialize from a slice of FDs (e.g. from `register_fds`).
    /// Assumes indices 0..len are mapped to these FDs.
    #[must_use]
    pub fn init_from_slice(capacity: u32, fds: &[RawFd]) -> Self {
        let cap = capacity as usize;
        let mut files = Vec::with_capacity(cap);
        files.resize(cap, -1);

        let mut index_map = HashMap::new();
        // Populate with slice content
        for (i, &fd) in fds.iter().enumerate() {
            if i < cap {
                files[i] = fd;
                if fd != -1 {
                    index_map.insert(fd, i as u32);
                }
            }
        }

        // Rebuild free indices
        let mut free_indices = Vec::new();
        for i in (0..capacity).rev() {
            if i as usize >= fds.len() || fds[i as usize] == -1 {
                free_indices.push(i);
            }
        }

        Self {
            index_map,
            files,
            free_indices,
        }
    }

    /// Update the table state after a successful registration.
    ///
    /// This should be called logic-side.
    /// The actual kernel `register_files` call must happen elsewhere.
    pub fn insert(&mut self, fd: RawFd) -> Option<u32> {
        if self.index_map.contains_key(&fd) {
            return self.index_map.get(&fd).copied();
        }

        // Get a free index
        let idx = self.free_indices.pop()?;

        // Store mapping
        self.index_map.insert(fd, idx);
        self.files[idx as usize] = fd;

        Some(idx)
    }

    /// Remove a file from the table logic.
    pub fn remove(&mut self, fd: RawFd) -> Option<u32> {
        if let Some(idx) = self.index_map.remove(&fd) {
            self.files[idx as usize] = -1;
            self.free_indices.push(idx);
            Some(idx)
        } else {
            None
        }
    }

    /// Get fixed index for an FD.
    #[must_use]
    pub fn get_index(&self, fd: RawFd) -> Option<u32> {
        self.index_map.get(&fd).copied()
    }

    /// Get the full files vector (for initial registration).
    #[must_use]
    pub fn as_vec(&self) -> &Vec<RawFd> {
        &self.files
    }
}
