"""Subprocess transport and protocol support for uringcore."""

import asyncio
import os
import signal
import subprocess
from typing import Any, Optional, Tuple, Callable


class SubprocessTransport(asyncio.SubprocessTransport):
    """Subprocess transport using add_reader for pipe I/O."""

    def __init__(self, loop, protocol, proc: subprocess.Popen):
        """Initialize subprocess transport.
        
        Args:
            loop: The UringEventLoop
            protocol: SubprocessProtocol instance
            proc: The Popen process object
        """
        self._loop = loop
        self._protocol = protocol
        self._proc = proc
        self._pid = proc.pid
        self._returncode = None
        self._closed = False
        
        # Pipe transports: fd -> ReadPipeTransport/WritePipeTransport
        self._pipes = {}
        
        # Set up stdin (write pipe)
        if proc.stdin is not None:
            self._pipes[0] = WriteSubprocessPipeTransport(
                loop, proc.stdin, protocol, 0
            )
        
        # Set up stdout (read pipe)
        if proc.stdout is not None:
            self._pipes[1] = ReadSubprocessPipeTransport(
                loop, proc.stdout, protocol, 1
            )
        
        # Set up stderr (read pipe)
        if proc.stderr is not None:
            self._pipes[2] = ReadSubprocessPipeTransport(
                loop, proc.stderr, protocol, 2
            )
        
        # Start monitoring process exit
        self._start_exit_waiter()

    def _start_exit_waiter(self):
        """Start a thread to wait for process exit."""
        import threading
        
        def wait_for_exit():
            returncode = self._proc.wait()
            self._loop.call_soon_threadsafe(
                self._process_exited, returncode
            )
        
        thread = threading.Thread(target=wait_for_exit, daemon=True)
        thread.start()

    def _process_exited(self, returncode):
        """Called when the process exits."""
        self._returncode = returncode
        
        # Drain any remaining data from read pipes before closing
        for fd_num, pipe_transport in list(self._pipes.items()):
            if isinstance(pipe_transport, ReadSubprocessPipeTransport):
                # Read all remaining data
                try:
                    while True:
                        data = os.read(pipe_transport._pipe.fileno(), 65536)
                        if not data:
                            break
                        self._protocol.pipe_data_received(fd_num, data)
                except (OSError, BlockingIOError):
                    pass
        
        # Close all pipes
        for pipe in self._pipes.values():
            pipe.close()
        
        # Notify protocol
        try:
            self._protocol.process_exited()
        except Exception:
            pass

    def get_pid(self):
        """Return the subprocess process ID."""
        return self._pid

    def get_returncode(self):
        """Return the subprocess return code or None."""
        return self._returncode

    def get_pipe_transport(self, fd):
        """Return the transport for the pipe with file descriptor fd."""
        return self._pipes.get(fd)

    def send_signal(self, signal_num):
        """Send a signal to the subprocess."""
        self._proc.send_signal(signal_num)

    def terminate(self):
        """Terminate the subprocess."""
        self._proc.terminate()

    def kill(self):
        """Kill the subprocess."""
        self._proc.kill()

    def close(self):
        """Close the transport."""
        if self._closed:
            return
        self._closed = True
        
        for pipe in self._pipes.values():
            pipe.close()
        
        if self._returncode is None:
            self.terminate()

    def is_closing(self):
        """Return True if the transport is closing."""
        return self._closed

    def get_extra_info(self, name, default=None):
        """Get extra info."""
        if name == "subprocess":
            return self._proc
        return default


class ReadSubprocessPipeTransport(asyncio.ReadTransport):
    """Read transport for subprocess stdout/stderr."""

    def __init__(self, loop, pipe, protocol, fd):
        self._loop = loop
        self._pipe = pipe
        self._protocol = protocol
        self._fd = fd
        self._closing = False
        
        # Set non-blocking
        os.set_blocking(pipe.fileno(), False)
        
        # Start reading
        self._loop.add_reader(pipe.fileno(), self._read_ready)

    def _read_ready(self):
        """Called when pipe is readable."""
        try:
            data = os.read(self._pipe.fileno(), 65536)
            if data:
                self._protocol.pipe_data_received(self._fd, data)
            else:
                # EOF
                self._loop.remove_reader(self._pipe.fileno())
                self._protocol.pipe_connection_lost(self._fd, None)
        except OSError as exc:
            self._loop.remove_reader(self._pipe.fileno())
            self._protocol.pipe_connection_lost(self._fd, exc)

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True
        self._loop.remove_reader(self._pipe.fileno())
        self._pipe.close()

    def is_closing(self):
        return self._closing

    def pause_reading(self):
        self._loop.remove_reader(self._pipe.fileno())

    def resume_reading(self):
        self._loop.add_reader(self._pipe.fileno(), self._read_ready)

    def get_extra_info(self, name, default=None):
        if name == "pipe":
            return self._pipe
        return default


class WriteSubprocessPipeTransport(asyncio.WriteTransport):
    """Write transport for subprocess stdin."""

    def __init__(self, loop, pipe, protocol, fd):
        self._loop = loop
        self._pipe = pipe
        self._protocol = protocol
        self._fd = fd
        self._closing = False
        self._buffer = bytearray()
        
        # Set non-blocking - may fail if pipe is already closed
        try:
            os.set_blocking(pipe.fileno(), False)
        except (OSError, ValueError):
            self._closing = True

    def write(self, data):
        """Write data to the pipe."""
        if self._closing:
            return
        
        self._buffer.extend(data)
        self._loop.add_writer(self._pipe.fileno(), self._write_ready)

    def _write_ready(self):
        """Called when pipe is writable."""
        if not self._buffer:
            self._loop.remove_writer(self._pipe.fileno())
            return
        
        try:
            n = os.write(self._pipe.fileno(), self._buffer)
            del self._buffer[:n]
            
            if not self._buffer:
                self._loop.remove_writer(self._pipe.fileno())
                if self._closing:
                    self._pipe.close()
        except BlockingIOError:
            pass
        except OSError as exc:
            self._loop.remove_writer(self._pipe.fileno())
            self._protocol.pipe_connection_lost(self._fd, exc)

    def write_eof(self):
        """Close the write end."""
        self.close()

    def can_write_eof(self):
        return True

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True
        
        if not self._buffer:
            self._pipe.close()
        # Otherwise, will close after buffer is flushed

    def is_closing(self):
        return self._closing

    def abort(self):
        self._buffer.clear()
        self._loop.remove_writer(self._pipe.fileno())
        self._pipe.close()
        self._closing = True

    def get_write_buffer_size(self):
        return len(self._buffer)

    def set_write_buffer_limits(self, high=None, low=None):
        pass

    def get_write_buffer_limits(self):
        return (0, 0)

    def get_extra_info(self, name, default=None):
        if name == "pipe":
            return self._pipe
        return default
