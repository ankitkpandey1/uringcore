"""UringEventLoop: Completion-driven asyncio event loop using io_uring."""
import asyncio
import selectors
import warnings
from typing import Any, Dict, Optional
from uringcore._core import UringCore

class UringEventLoop(asyncio.SelectorEventLoop):
    """Event loop using io_uring for completions with selector fallback."""
    
    def __init__(self, selector: Optional[selectors.BaseSelector] = None):
        super().__init__(selector)
        
        # Initialize Rust core
        self._core = UringCore()
        
        # Register eventfd to wake up loop on completions
        self.add_reader(self._core.event_fd, self._process_completions)
        
        # Mapping for uring-aware transports (fd -> transport)
        self._uring_transports: Dict[int, Any] = {}

    def _process_completions(self):
        """Process completions from the io_uring ring."""
        # Drain the eventfd signal
        self._core.drain_eventfd()
        
        # Process all available completions
        for fd, op_type, result, data in self._core.drain_completions():
            transport = self._uring_transports.get(fd)
            if transport is not None:
                if hasattr(transport, '_process_completion'):
                    transport._process_completion(op_type, result, data)

    def close(self):
        """Close the event loop."""
        if not self.is_closed():
            try:
                self.remove_reader(self._core.event_fd)
                self._core.shutdown()
            except Exception:
                pass
        super().close()

    # =========================================================================
    # io_uring Integration (Stub/Future Use)
    # =========================================================================

    def register_fd(self, fd: int, socket_type: str = "tcp"):
        """Register a file descriptor with the io_uring engine."""
        self._core.register_fd(fd, socket_type)

    def unregister_fd(self, fd: int):
        """Unregister a file descriptor."""
        self._core.unregister_fd(fd)
        self._uring_transports.pop(fd, None)
        
    def get_buffer_stats(self):
        """Get buffer pool statistics."""
        return self._core.buffer_stats()

    def get_fd_stats(self):
        """Get FD state statistics."""
        return self._core.fd_stats()
