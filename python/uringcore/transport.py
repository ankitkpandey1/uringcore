"""UringSocketTransport: Pure io_uring socket transport.

This module provides asyncio-compatible Transport implementation
that uses io_uring for all I/O operations.
"""

import asyncio


class UringSocketTransport(asyncio.Transport):
    """Socket transport using io_uring for I/O."""

    def __init__(self, loop, fd: int, protocol, sock=None):
        """Initialize the transport.

        Args:
            loop: The UringEventLoop instance
            fd: File descriptor for the socket
            protocol: The protocol instance
            sock: Optional socket object (for cleanup)
        """
        self._loop = loop
        self._fd = fd
        self._protocol = protocol
        self._sock = sock
        self._closing = False
        self._closed = False
        self._write_buffer = bytearray()
        self._write_buffer_size = 0
        self._paused = False
        self._recv_pending = False  # Track if recv is in flight
        self._high_water = 64 * 1024  # 64KB
        self._low_water = 16 * 1024  # 16KB

    def get_extra_info(self, name, default=None):
        """Get transport extra info."""
        if name == "socket":
            return self._sock
        if name == "peername":
            try:
                return self._sock.getpeername() if self._sock else None
            except Exception:
                return None
        if name == "sockname":
            try:
                return self._sock.getsockname() if self._sock else None
            except Exception:
                return None
        return default

    def is_closing(self):
        """Return True if the transport is closing or closed."""
        return self._closing or self._closed

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True

        if self._sock:
            self._sock.close()
        else:
             # Submit close via io_uring
             self._loop._core.submit_close(self._fd)

    def is_reading(self):
        """Return True if the transport is receiving."""
        return not self._paused and not self._closing

    def pause_reading(self):
        """Pause the receiving end."""
        if self._closing or self._paused:
            return
        self._paused = True
        self._loop._core.pause_reading(self._fd)

    def resume_reading(self):
        """Resume the receiving end."""
        if self._closing:
            return
        if self._paused:
            self._paused = False
            self._loop._core.resume_reading(self._fd)
        # Always rearm receive when resuming or starting
        self._rearm_recv()

    def _rearm_recv(self):
        """Submit a receive operation."""
        if self._closing or self._paused or self._recv_pending:
            return
            
        try:
            self._recv_pending = True
            fut = self._loop.create_future()
            fut.add_done_callback(self._on_recv_complete)
            self._loop._io_futures[(self._fd, "recv")] = fut
            self._loop._core.submit_recv(self._fd, fut)
        except Exception as exc:
            self._recv_pending = False
            self._error_received(exc)

    def _on_recv_complete(self, fut):
        """Handle receive completion."""
        self._recv_pending = False
        if self._closing:
            return
            
        try:
            exc = fut.exception()
            if exc:
                self._error_received_exc(exc)
                return

            data = fut.result()
            if data:
                self._data_received(data)
                # Auto-rearm if not paused/closed
                if not self._paused and not self._closing:
                    self._rearm_recv()
            else:
                self._eof_received()
        except Exception as exc:
            self._error_received_exc(exc)

    def set_write_buffer_limits(self, high=None, low=None):
        """Set the high- and low-water limits for write flow control."""
        if high is not None:
            self._high_water = high
        if low is not None:
            self._low_water = low

    def get_write_buffer_size(self):
        """Return the current size of the write buffer."""
        return self._write_buffer_size

    def get_write_buffer_limits(self):
        """Get the high and low water marks."""
        return (self._low_water, self._high_water)

    def write(self, data):
        """Write data to the transport."""
        if self._closing:
            return

        if not data:
            return

        # Submit directly via io_uring with future
        try:
            fut = self._loop.create_future()
            fut.add_done_callback(self._on_write_complete)
            self._loop._core.submit_send(self._fd, bytes(data), fut)
            self._write_buffer_size += len(data)

            # Check high water mark
            if self._write_buffer_size >= self._high_water:
                self._protocol.pause_writing()
        except Exception as exc:
            self._error_received_exc(exc)

    def _on_write_complete(self, fut):
        """Handle write completion."""
        if self._closing:
            return

        try:
            exc = fut.exception()
            if exc:
                self._error_received_exc(exc)
                return
            
            result = fut.result()
            self._send_completed(result)
        except Exception as exc:
             self._error_received_exc(exc)

    def writelines(self, list_of_data):
        """Write a list of data to the transport."""
        for data in list_of_data:
            self.write(data)

    def write_eof(self):
        """Close the write end after flushing buffered data."""
        # TCP half-close via shutdown
        if self._sock:
            try:
                import socket

                self._sock.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def can_write_eof(self):
        """Return True if the transport supports write_eof."""
        return True

    def abort(self):
        """Close the transport immediately."""
        self._force_close(None)

    def _force_close(self, exc):
        """Force close the transport."""
        if self._closed:
            return
        self._closed = True
        self._closing = True

        # Close socket directly
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

        # Notify protocol
        self._loop.call_soon(self._call_connection_lost, exc)

    def _call_connection_lost(self, exc):
        """Call connection_lost on the protocol."""
        try:
            self._protocol.connection_lost(exc)
        except Exception:
            pass

    # =========================================================================
    # Completion handlers (called by event loop)
    # =========================================================================

    def _data_received(self, data: bytes):
        """Called when data is received."""
        try:
            self._protocol.data_received(data)
        except Exception as exc:
            self._loop.call_exception_handler(
                {
                    "message": "Exception in data_received callback",
                    "exception": exc,
                    "transport": self,
                    "protocol": self._protocol,
                }
            )

    def _eof_received(self):
        """Called when EOF is received."""
        try:
            keep_open = self._protocol.eof_received()
            if not keep_open:
                self.close()
        except Exception as exc:
            self._loop.call_exception_handler(
                {
                    "message": "Exception in eof_received callback",
                    "exception": exc,
                    "transport": self,
                    "protocol": self._protocol,
                }
            )
            self.close()

    def _error_received(self, error):
        """Called when an error occurs (int or exception)."""
        if isinstance(error, int):
            import os
            exc = OSError(error, os.strerror(abs(error)))
        else:
            exc = error
        self._force_close(exc)
        
    def _error_received_exc(self, exc):
        """Helper for exception objects."""
        self._force_close(exc)

    def _send_completed(self, result: int):
        """Called when a send completes."""
        if result < 0:
            # Send error (should be handled by exception logic usually, but result code path)
            # With future result, result is bytes written (positive).
            # If error, exception is raised.
            # So result should be >= 0.
            return

        # Reduce buffer size
        self._write_buffer_size = max(0, self._write_buffer_size - result)

        # Check low water mark
        if self._write_buffer_size <= self._low_water:
            try:
                self._protocol.resume_writing()
            except Exception:
                pass
