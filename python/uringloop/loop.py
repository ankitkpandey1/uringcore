"""UringEventLoop: Completion-driven asyncio event loop using io_uring.

This module implements the asyncio event loop that drains completions
from the Rust io_uring engine via eventfd signaling.
"""

import asyncio
import collections
import heapq
import selectors
import time
from typing import Any, Callable, Optional

from uringloop._core import UringCore


class UringEventLoop(asyncio.AbstractEventLoop):
    """Completion-driven event loop using io_uring.

    This event loop waits on an eventfd signaled by the Rust io_uring
    completion queue handler, rather than using a traditional selector.
    """

    def __init__(self):
        """Initialize the event loop."""
        self._closed = False
        self._stopping = False
        self._running = False
        
        # Initialize the Rust core
        self._core = UringCore()
        
        # Ready callbacks queue
        self._ready: collections.deque = collections.deque()
        
        # Scheduled callbacks (heap of (time, handle))
        self._scheduled: list = []
        
        # File descriptor to transport mapping
        self._transports: dict[int, Any] = {}
        
        # Use a selector for the eventfd
        self._selector = selectors.DefaultSelector()
        self._selector.register(
            self._core.event_fd,
            selectors.EVENT_READ,
            self._process_completions,
        )
        
        # Thread safety
        self._thread_id: Optional[int] = None
        
        # Exception handler
        self._exception_handler: Optional[Callable] = None
        
        # Debug mode
        self._debug = False

    def _check_closed(self):
        """Check if the loop is closed and raise if so."""
        if self._closed:
            raise RuntimeError("Event loop is closed")

    def _check_running(self):
        """Check if the loop is already running."""
        if self._running:
            raise RuntimeError("This event loop is already running")

    # =========================================================================
    # Running and stopping the event loop
    # =========================================================================

    def run_forever(self):
        """Run the event loop until stop() is called."""
        self._check_closed()
        self._check_running()
        
        self._running = True
        self._thread_id = None  # Would be set to threading.get_ident()
        
        try:
            while not self._stopping:
                self._run_once()
        finally:
            self._stopping = False
            self._running = False
            self._thread_id = None

    def run_until_complete(self, future):
        """Run until the future is complete."""
        self._check_closed()
        self._check_running()
        
        future = asyncio.ensure_future(future, loop=self)
        future.add_done_callback(lambda _: self.stop())
        
        try:
            self.run_forever()
        except Exception:
            if not future.done():
                future.cancel()
            raise
        
        if not future.done():
            raise RuntimeError("Event loop stopped before Future completed")
        
        return future.result()

    def stop(self):
        """Stop the event loop."""
        self._stopping = True

    def is_running(self):
        """Return True if the event loop is running."""
        return self._running

    def is_closed(self):
        """Return True if the event loop is closed."""
        return self._closed

    def close(self):
        """Close the event loop."""
        if self._running:
            raise RuntimeError("Cannot close a running event loop")
        if self._closed:
            return
        
        self._selector.unregister(self._core.event_fd)
        self._selector.close()
        self._core.shutdown()
        self._closed = True

    # =========================================================================
    # Internal: Running one iteration
    # =========================================================================

    def _run_once(self):
        """Run one iteration of the event loop."""
        # Calculate timeout based on scheduled callbacks
        timeout = self._calculate_timeout()
        
        # Wait for events
        events = self._selector.select(timeout)
        
        # Process any events (which will call _process_completions)
        for key, _ in events:
            callback = key.data
            callback()
        
        # Process scheduled callbacks
        self._process_scheduled()
        
        # Process ready callbacks
        self._process_ready()

    def _calculate_timeout(self) -> Optional[float]:
        """Calculate the timeout for the next select call."""
        if self._stopping:
            return 0.0
        
        if self._ready:
            return 0.0
        
        if self._scheduled:
            now = time.monotonic()
            next_time = self._scheduled[0][0]
            timeout = max(0.0, next_time - now)
            return timeout
        
        # Default timeout
        return 1.0

    def _process_completions(self):
        """Process completions from the io_uring ring."""
        # Drain the eventfd
        self._core.drain_eventfd()
        
        # Get completions from the Rust core
        completions = self._core.drain_completions()
        
        for fd, op_type, result, data in completions:
            transport = self._transports.get(fd)
            if transport is not None:
                transport._process_completion(op_type, result, data)

    def _process_scheduled(self):
        """Process scheduled callbacks that are due."""
        now = time.monotonic()
        
        while self._scheduled and self._scheduled[0][0] <= now:
            _, handle = heapq.heappop(self._scheduled)
            if not handle._cancelled:
                self._ready.append(handle)

    def _process_ready(self):
        """Process ready callbacks."""
        while self._ready:
            handle = self._ready.popleft()
            if not handle._cancelled:
                handle._run()

    # =========================================================================
    # Callback scheduling
    # =========================================================================

    def call_soon(self, callback, *args, context=None):
        """Schedule a callback to be called soon."""
        self._check_closed()
        handle = asyncio.Handle(callback, args, self, context)
        self._ready.append(handle)
        return handle

    def call_soon_threadsafe(self, callback, *args, context=None):
        """Schedule a callback to be called from another thread."""
        handle = self.call_soon(callback, *args, context=context)
        # Signal the eventfd to wake up the loop
        self._core.signal()
        return handle

    def call_later(self, delay, callback, *args, context=None):
        """Schedule a callback to be called after delay seconds."""
        self._check_closed()
        when = time.monotonic() + delay
        return self.call_at(when, callback, *args, context=context)

    def call_at(self, when, callback, *args, context=None):
        """Schedule a callback to be called at a specific time."""
        self._check_closed()
        handle = asyncio.TimerHandle(when, callback, args, self, context)
        heapq.heappush(self._scheduled, (when, handle))
        return handle

    # =========================================================================
    # Time
    # =========================================================================

    def time(self):
        """Return the current time."""
        return time.monotonic()

    # =========================================================================
    # Future/Task creation
    # =========================================================================

    def create_future(self):
        """Create a Future attached to this loop."""
        return asyncio.Future(loop=self)

    def create_task(self, coro, *, name=None, context=None):
        """Create a Task from a coroutine."""
        self._check_closed()
        task = asyncio.Task(coro, loop=self, name=name, context=context)
        return task

    # =========================================================================
    # Debug
    # =========================================================================

    def get_debug(self):
        """Return the debug mode setting."""
        return self._debug

    def set_debug(self, enabled):
        """Set the debug mode."""
        self._debug = enabled

    # =========================================================================
    # Exception handling
    # =========================================================================

    def set_exception_handler(self, handler):
        """Set the exception handler."""
        self._exception_handler = handler

    def get_exception_handler(self):
        """Get the exception handler."""
        return self._exception_handler

    def default_exception_handler(self, context):
        """Default exception handler."""
        message = context.get("message", "Unhandled exception")
        exception = context.get("exception")
        
        if exception is not None:
            import traceback
            exc_info = (type(exception), exception, exception.__traceback__)
            tb = "".join(traceback.format_exception(*exc_info))
            print(f"{message}\n{tb}")
        else:
            print(message)

    def call_exception_handler(self, context):
        """Call the exception handler."""
        if self._exception_handler is not None:
            self._exception_handler(self, context)
        else:
            self.default_exception_handler(context)

    # =========================================================================
    # FD operations (for io_uring integration)
    # =========================================================================

    def register_fd(self, fd: int, socket_type: str = "tcp"):
        """Register a file descriptor with the io_uring engine."""
        self._core.register_fd(fd, socket_type)

    def unregister_fd(self, fd: int):
        """Unregister a file descriptor."""
        self._core.unregister_fd(fd)
        self._transports.pop(fd, None)

    def pause_reading(self, fd: int):
        """Pause reading for a file descriptor."""
        self._core.pause_reading(fd)

    def resume_reading(self, fd: int):
        """Resume reading for a file descriptor."""
        self._core.resume_reading(fd)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_buffer_stats(self):
        """Get buffer pool statistics."""
        return self._core.buffer_stats()

    def get_fd_stats(self):
        """Get FD state statistics."""
        return self._core.fd_stats()
