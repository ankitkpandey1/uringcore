"""UringDatagramTransport: UDP transport using io_uring / add_reader."""

import asyncio
import socket
from typing import Any, Optional, Tuple


class UringDatagramTransport(asyncio.DatagramTransport):
    """UDP transport for uringcore event loop."""

    def __init__(self, loop, sock: socket.socket, protocol, address=None):
        """Initialize the transport.
        
        Args:
            loop: The UringEventLoop instance
            sock: UDP socket
            protocol: The datagram protocol instance
            address: Remote address for connected sockets
        """
        self._loop = loop
        self._sock = sock
        self._protocol = protocol
        self._address = address
        self._closing = False
        self._closed = False
        self._buffer = []
        
        # Set non-blocking
        sock.setblocking(False)
        
        # Start receiving via add_reader
        self._loop.add_reader(sock.fileno(), self._read_ready)

    def _read_ready(self):
        """Called when socket is readable."""
        if self._closing:
            return
        
        try:
            data, addr = self._sock.recvfrom(65536)
            self._protocol.datagram_received(data, addr)
        except BlockingIOError:
            pass
        except Exception as exc:
            self._protocol.error_received(exc)

    def sendto(self, data, addr=None):
        """Send data to the given address."""
        if self._closing:
            return
        
        try:
            if addr is None:
                addr = self._address
            if addr:
                self._sock.sendto(data, addr)
            else:
                self._sock.send(data)
        except BlockingIOError:
            # TODO: buffer and use add_writer
            pass
        except Exception as exc:
            self._protocol.error_received(exc)

    def get_extra_info(self, name, default=None):
        """Get transport extra info."""
        if name == "socket":
            return self._sock
        if name == "peername":
            return self._address
        if name == "sockname":
            try:
                return self._sock.getsockname()
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
        
        self._loop.remove_reader(self._sock.fileno())
        self._sock.close()
        self._closed = True
        
        self._loop.call_soon(self._call_connection_lost, None)

    def _call_connection_lost(self, exc):
        """Call connection_lost on the protocol."""
        try:
            self._protocol.connection_lost(exc)
        except Exception:
            pass

    def abort(self):
        """Close the transport immediately."""
        self.close()

    def get_write_buffer_size(self):
        """Return the current size of the write buffer."""
        return 0

    def get_write_buffer_limits(self):
        """Get the high and low water marks."""
        return (0, 0)

    def set_write_buffer_limits(self, high=None, low=None):
        """Set the high- and low-water limits for write flow control."""
        pass
