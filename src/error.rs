//! Error types for uringcore.

use thiserror::Error;

/// Result type alias for uringcore operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors that can occur in uringcore operations.
#[derive(Error, Debug)]
pub enum Error {
    /// Buffer allocation failed
    #[error("Buffer allocation failed: {0}")]
    BufferAllocation(String),

    /// `io_uring` initialization failed
    #[error("`io_uring` initialization failed: {0}")]
    RingInit(String),

    /// `io_uring` operation failed
    #[error("`io_uring` operation failed: {0}")]
    RingOp(String),

    /// eventfd operation failed
    #[error("eventfd operation failed: {0}")]
    EventFd(String),

    /// File descriptor error
    #[error("File descriptor error: {0}")]
    Fd(String),

    /// Fork detected - ring must be reinitialized
    #[error("Fork detected, ring must be reinitialized")]
    ForkDetected,

    /// No buffers available
    #[error("No buffers available in pool")]
    NoBuffersAvailable,

    /// Credit budget exhausted (backpressure)
    #[error("Credit budget exhausted for fd {0}")]
    CreditExhausted(i32),

    /// Invalid generation ID
    #[error("Invalid generation ID: expected {expected}, got {got}")]
    InvalidGeneration { expected: u64, got: u64 },

    /// SQPOLL not available
    #[error("SQPOLL not available: {0}")]
    SqpollUnavailable(String),

    /// I/O error
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// Nix error
    #[error("System error: {0}")]
    Nix(#[from] nix::Error),
}
