
import os
import asyncio
from typing import Any

class ReadPipeTransport(asyncio.ReadTransport):
    """Read transport for pipes."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        pipe: Any,
        protocol: asyncio.BaseProtocol,
        extra=None,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._pipe = pipe
        self._protocol = protocol
        self._closing = False
        self._paused = False
        self._extra = extra or {}

        # Set non-blocking
        os.set_blocking(pipe.fileno(), False)

        # Start reading
        self._loop.add_reader(pipe.fileno(), self._read_ready)
        self._loop.call_soon(self._protocol.connection_made, self)

    def _read_ready(self) -> None:
        """Called when pipe is readable."""
        if self._paused or self._closing:
            return
            
        try:
            data = os.read(self._pipe.fileno(), 65536)
            if data:
                self._protocol.data_received(data)
            else:
                # EOF
                self._loop.remove_reader(self._pipe.fileno())
                if self._protocol.eof_received():
                   # Keep open if protocol requests
                   pass
                else:
                   self.close()
        except OSError as exc:
            self._loop.remove_reader(self._pipe.fileno())
            self._protocol.connection_lost(exc)

    def pause_reading(self) -> None:
        if self._closing or self._paused:
            return
        self._paused = True
        self._loop.remove_reader(self._pipe.fileno())

    def resume_reading(self) -> None:
        if self._closing or not self._paused:
            return
        self._paused = False
        self._loop.add_reader(self._pipe.fileno(), self._read_ready)

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True
        self._loop.remove_reader(self._pipe.fileno())
        self._pipe.close()
        self._protocol.connection_lost(None)

    def is_closing(self):
        return self._closing

    def get_extra_info(self, name, default=None):
        if name in self._extra:
            return self._extra[name]
        if name == 'pipe':
            return self._pipe
        return default

class WritePipeTransport(asyncio.WriteTransport):
    """Write transport for pipes."""

    def __init__(self, loop, pipe, protocol, waiter=None, extra=None):
        self._loop = loop
        self._pipe = pipe
        self._protocol = protocol
        self._closing = False
        self._buffer = bytearray()
        self._high_water = 64 * 1024
        self._low_water = 16 * 1024
        self._extra = extra or {}
        
        # Set non-blocking
        try:
            os.set_blocking(pipe.fileno(), False)
        except (OSError, ValueError):
             self._closing = True

        self._loop.call_soon(self._protocol.connection_made, self)
        
        if waiter is not None:
             self._loop.call_soon(lambda: waiter.set_result(None) if not waiter.done() else None)

    def write(self, data):
        """Write data to the pipe."""
        if self._closing:
            return
        if not data:
            return

        self._buffer.extend(data)
        self._loop.add_writer(self._pipe.fileno(), self._write_ready)
        
        # Flow control
        if len(self._buffer) > self._high_water:
             try:
                 self._protocol.pause_writing()
             except Exception:
                 pass

    def _write_ready(self):
        """Called when pipe is writable."""
        if not self._buffer:
            self._loop.remove_writer(self._pipe.fileno())
            return

        try:
            n = os.write(self._pipe.fileno(), self._buffer)
            del self._buffer[:n]

            if len(self._buffer) < self._low_water:
                 try:
                     self._protocol.resume_writing()
                 except Exception:
                     pass

            if not self._buffer:
                self._loop.remove_writer(self._pipe.fileno())
                if self._closing:
                    self._pipe.close()
                    self._protocol.connection_lost(None)
                    
        except BlockingIOError:
            pass
        except OSError as exc:
            self._loop.remove_writer(self._pipe.fileno())
            self._protocol.connection_lost(exc)

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True

        if not self._buffer:
            self._pipe.close()
            self._protocol.connection_lost(None)
        # Otherwise wait for buffer drain

    def is_closing(self):
        return self._closing

    def abort(self):
         self._buffer.clear()
         self._loop.remove_writer(self._pipe.fileno())
         self._pipe.close()
         self._closing = True
         self._protocol.connection_lost(None)
    
    def get_write_buffer_size(self):
        return len(self._buffer)
    
    def set_write_buffer_limits(self, high=None, low=None):
        if high is not None: self._high_water = high
        if low is not None: self._low_water = low
    
    def get_write_buffer_limits(self):
        return (self._low_water, self._high_water)
    
    def get_extra_info(self, name, default=None):
        if name in self._extra:
            return self._extra[name]
        if name == 'pipe':
            return self._pipe
        return default
