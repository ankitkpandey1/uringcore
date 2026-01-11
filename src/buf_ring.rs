use std::alloc::{Layout, alloc_zeroed, dealloc};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU16, Ordering};

/// Kernel-compatible IO uring buffer ring header.
/// matches `struct io_uring_buf_ring` from kernel headers.
#[repr(C)]
struct io_uring_buf_ring_header {
    resv1: u64,
    resv2: u32,
    resv3: u16,
    tail: AtomicU16,
}

/// IO uring buffer entry.
/// matches `struct io_uring_buf` from kernel headers.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct io_uring_buf {
    pub addr: u64,
    pub len: u32,
    pub bid: u16,
    pub resv: u16,
}

/// Manages a Provided Buffer Ring (`PBufRing`) shared with the kernel.
pub struct PBufRing {
    ptr: NonNull<u8>,
    layout: Layout,
    #[allow(dead_code)]
    ring_entries: u16,
    mask: u16,
    bgid: u16,
}

impl PBufRing {
    /// Create a new Provided Buffer Ring.
    ///
    /// `ring_entries` must be a power of 2.
    pub fn new(ring_entries: u16, bgid: u16) -> std::io::Result<Self> {
        if !ring_entries.is_power_of_two() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "ring_entries must be a power of 2",
            ));
        }

        // Layout: Header + Entries
        // The header is 16 bytes.
        // Each entry is 16 bytes.
        let header_size = std::mem::size_of::<io_uring_buf_ring_header>();
        let entries_size = std::mem::size_of::<io_uring_buf>() * ring_entries as usize;
        let total_size = header_size + entries_size;

        // Use page alignment (4096) to be safe and efficient
        let layout = Layout::from_size_align(total_size, 4096)
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::OutOfMemory, "Invalid layout"))?;

        let ptr = unsafe {
            let p = alloc_zeroed(layout);
            if p.is_null() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::OutOfMemory,
                    "Failed to allocate ring memory",
                ));
            }
            NonNull::new_unchecked(p)
        };

        let ring = Self {
            ptr,
            layout,
            ring_entries,
            mask: ring_entries - 1,
            bgid,
        };

        // Initialize tail to 0 (already zeroed by alloc_zeroed, but being explicit doesn't hurt)
        // unsynchronized access is fine here as we haven't shared it yet

        Ok(ring)
    }

    /// Update the ring with new buffers.
    ///
    /// Writes `count` buffers starting at the current tail.
    /// Advances the tail and makes it visible to the kernel.
    ///
    /// `buffers` is a closure that returns specific buffer info (addr, len, bid) for the i-th slot.
    pub fn add_buffers<F>(&self, count: u16, mut get_buf: F)
    where
        F: FnMut(u16) -> (u64, u32, u16),
    {
        unsafe {
            // Pointer alignment is guaranteed by mmap (page aligned)
            #[allow(clippy::cast_ptr_alignment)]
            #[allow(clippy::ptr_as_ptr)]
            let header = self.ptr.as_ptr() as *mut io_uring_buf_ring_header;
            let tail = (*header).tail.load(Ordering::Relaxed);
            #[allow(clippy::cast_ptr_alignment)]
            let buf_base = self
                .ptr
                .as_ptr()
                .add(std::mem::size_of::<io_uring_buf_ring_header>())
                .cast::<io_uring_buf>();

            for i in 0..count {
                let idx = (tail.wrapping_add(i)) & self.mask;
                let (addr, len, bid) = get_buf(i);

                let buf_ptr = buf_base.add(idx as usize);
                (*buf_ptr).addr = addr;
                (*buf_ptr).len = len;
                (*buf_ptr).bid = bid;
            }

            // Commit tail update with Release ordering so kernel sees the writes
            (*header)
                .tail
                .store(tail.wrapping_add(count), Ordering::Release);
        }
    }

    /// Get the memory address of the ring for registration.
    #[must_use]
    pub fn as_ptr(&self) -> *mut u8 {
        self.ptr.as_ptr()
    }

    /// Get the Buffer Group ID.
    #[must_use]
    pub fn bgid(&self) -> u16 {
        self.bgid
    }
}

unsafe impl Send for PBufRing {}
unsafe impl Sync for PBufRing {}

impl Drop for PBufRing {
    fn drop(&mut self) {
        unsafe {
            dealloc(self.ptr.as_ptr(), self.layout);
        }
    }
}
