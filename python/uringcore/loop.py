"""UringEventLoop: Pure io_uring asyncio event loop.

This module implements a completion-driven event loop that uses io_uring
exclusively for I/O operations. No selector fallback.
"""

import asyncio
import collections
import heapq
import os
import select
import socket
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from uringcore._core import UringCore


class UringEventLoop(asyncio.AbstractEventLoop):
    """Pure io_uring event loop with no selector fallback.
    
    All I/O operations go through the io_uring submission queue.
    Completions are delivered via eventfd signaling.
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
        self._scheduled: List[Tuple[float, asyncio.TimerHandle]] = []
        
        # Transport registry: fd -> transport
        self._transports: Dict[int, Any] = {}
        
        # Server registry: fd -> (server, protocol_factory)
        self._servers: Dict[int, Tuple[Any, Callable]] = {}
        
        # Pending send buffers: fd -> list of (data, future)
        self._pending_sends: Dict[int, List[Tuple[bytes, asyncio.Future]]] = {}
        
        # Thread safety
        self._thread_id: Optional[int] = None
        
        # Exception handler
        self._exception_handler: Optional[Callable] = None
        
        # Debug mode
        self._debug = False
        
        # epoll for eventfd and reader/writer callbacks
        self._epoll = select.epoll()
        self._epoll.register(self._core.event_fd, select.EPOLLIN)
        
        # Reader/writer callbacks: fd -> (callback, args)
        self._readers: Dict[int, Tuple[Callable, tuple]] = {}
        self._writers: Dict[int, Tuple[Callable, tuple]] = {}
        
        # Signal handlers: signum -> (callback, args)
        self._signal_handlers: Dict[int, Tuple[Callable, tuple]] = {}

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
        self._thread_id = None
        
        # Set this loop as the running loop for asyncio compatibility
        old_loop = asyncio._get_running_loop()
        try:
            asyncio._set_running_loop(self)
            while not self._stopping:
                self._run_once()
        finally:
            asyncio._set_running_loop(old_loop)
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
        
        # Cleanup default executor
        if hasattr(self, '_default_executor') and self._default_executor is not None:
            self._default_executor.shutdown(wait=False)
            self._default_executor = None
        
        self._epoll.unregister(self._core.event_fd)
        self._epoll.close()
        self._core.shutdown()
        self._closed = True

    async def shutdown_asyncgens(self):
        """Shutdown all active asynchronous generators."""
        # No-op: we don't track async generators yet
        pass

    async def shutdown_default_executor(self, wait=True):
        """Shutdown the default executor."""
        if hasattr(self, '_default_executor') and self._default_executor is not None:
            self._default_executor.shutdown(wait=wait)
            self._default_executor = None

    # =========================================================================
    # Internal: Running one iteration
    # =========================================================================

    def _run_once(self):
        """Run one iteration of the event loop."""
        timeout = self._calculate_timeout()
        
        # Wait for epoll events (eventfd + reader/writer FDs)
        events = self._epoll.poll(timeout)
        
        # Process events
        for fd, event_mask in events:
            if fd == self._core.event_fd:
                # io_uring completion signal
                self._core.drain_eventfd()
                self._process_completions()
            else:
                # Reader/writer callback
                if event_mask & select.EPOLLIN and fd in self._readers:
                    callback, args = self._readers[fd]
                    self._ready.append(asyncio.Handle(callback, args, self))
                if event_mask & select.EPOLLOUT and fd in self._writers:
                    callback, args = self._writers[fd]
                    self._ready.append(asyncio.Handle(callback, args, self))
        
        # Process scheduled callbacks
        self._process_scheduled()
        
        # Process ready callbacks
        self._process_ready()

    def _calculate_timeout(self) -> float:
        """Calculate the timeout for the next poll."""
        if self._stopping:
            return 0.0
        
        if self._ready:
            return 0.0
        
        if self._scheduled:
            now = time.monotonic()
            next_time = self._scheduled[0][0]
            timeout = max(0.0, next_time - now)
            return min(timeout, 0.01)  # Cap at 10ms for responsiveness
        
        return 0.01  # 10ms default for fast io_uring responsiveness

    def _process_completions(self):
        """Process completions from the io_uring ring."""
        completions = self._core.drain_completions()
        
        for fd, op_type, result, data in completions:
            if op_type == "recv":
                self._handle_recv_completion(fd, result, data)
            elif op_type == "send":
                self._handle_send_completion(fd, result)
            elif op_type == "accept":
                self._handle_accept_completion(fd, result)
            elif op_type == "close":
                self._handle_close_completion(fd, result)

    def _handle_recv_completion(self, fd: int, result: int, data: Optional[bytes]):
        """Handle a receive completion."""
        transport = self._transports.get(fd)
        if transport is None:
            return
        
        if result > 0 and data:
            # Data received - deliver to protocol
            transport._data_received(data)
            # Rearm receive
            self._core.submit_recv(fd)
        elif result == 0:
            # EOF
            transport._eof_received()
        else:
            # Error
            transport._error_received(result)

    def _handle_send_completion(self, fd: int, result: int):
        """Handle a send completion."""
        transport = self._transports.get(fd)
        if transport is None:
            return
        
        transport._send_completed(result)

    def _handle_accept_completion(self, fd: int, result: int):
        """Handle an accept completion."""
        server_info = self._servers.get(fd)
        if server_info is None:
            return
        
        server, protocol_factory = server_info
        
        if result >= 0:
            # New connection accepted
            client_fd = result
            self._create_transport_for_accepted(client_fd, protocol_factory)
            # Rearm accept
            self._core.submit_accept(fd)
        # On error, don't rearm (server closed or fatal error)

    def _handle_close_completion(self, fd: int, result: int):
        """Handle a close completion."""
        self._transports.pop(fd, None)
        self._core.unregister_fd(fd)

    def _create_transport_for_accepted(self, fd: int, protocol_factory: Callable):
        """Create transport and protocol for an accepted connection."""
        # Set non-blocking
        os.set_blocking(fd, False)
        
        # Create protocol
        protocol = protocol_factory()
        
        # Create transport
        from uringcore.transport import UringSocketTransport
        transport = UringSocketTransport(self, fd, protocol)
        self._transports[fd] = transport
        
        # Notify protocol
        protocol.connection_made(transport)
        
        # Start receiving
        self._core.register_fd(fd, "tcp")
        self._core.submit_recv(fd)

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

    def _timer_handle_cancelled(self, handle):
        """Called when a timer handle is cancelled."""
        # No-op: cancelled handles are filtered during processing
        pass

    # =========================================================================
    # Time
    # =========================================================================

    def time(self):
        """Return the current time."""
        return time.monotonic()

    # =========================================================================
    # File descriptor callbacks (add_reader/add_writer)
    # =========================================================================

    def add_reader(self, fd, callback, *args):
        """Start watching a file descriptor for read availability."""
        self._check_closed()
        if hasattr(fd, 'fileno'):
            fd = fd.fileno()
        
        # Remove existing reader if any
        self._remove_reader_no_check(fd)
        
        # Register with epoll for reading
        try:
            mask = select.EPOLLIN
            if fd in self._writers:
                mask |= select.EPOLLOUT
                self._epoll.modify(fd, mask)
            else:
                self._epoll.register(fd, mask)
        except FileExistsError:
            self._epoll.modify(fd, mask)
        
        self._readers[fd] = (callback, args)

    def remove_reader(self, fd) -> bool:
        """Stop watching a file descriptor for read availability."""
        if hasattr(fd, 'fileno'):
            fd = fd.fileno()
        return self._remove_reader_no_check(fd)

    def _remove_reader_no_check(self, fd) -> bool:
        """Internal: remove reader without closed check."""
        if fd not in self._readers:
            return False
        
        del self._readers[fd]
        
        # Update epoll registration
        if fd in self._writers:
            try:
                self._epoll.modify(fd, select.EPOLLOUT)
            except (FileNotFoundError, OSError):
                pass
        else:
            try:
                self._epoll.unregister(fd)
            except (FileNotFoundError, OSError):
                pass
        
        return True

    def add_writer(self, fd, callback, *args):
        """Start watching a file descriptor for write availability."""
        self._check_closed()
        if hasattr(fd, 'fileno'):
            fd = fd.fileno()
        
        # Remove existing writer if any
        self._remove_writer_no_check(fd)
        
        # Register with epoll for writing
        try:
            mask = select.EPOLLOUT
            if fd in self._readers:
                mask |= select.EPOLLIN
                self._epoll.modify(fd, mask)
            else:
                self._epoll.register(fd, mask)
        except FileExistsError:
            self._epoll.modify(fd, mask)
        
        self._writers[fd] = (callback, args)

    def remove_writer(self, fd) -> bool:
        """Stop watching a file descriptor for write availability."""
        if hasattr(fd, 'fileno'):
            fd = fd.fileno()
        return self._remove_writer_no_check(fd)

    def _remove_writer_no_check(self, fd) -> bool:
        """Internal: remove writer without closed check."""
        if fd not in self._writers:
            return False
        
        del self._writers[fd]
        
        # Update epoll registration
        if fd in self._readers:
            try:
                self._epoll.modify(fd, select.EPOLLIN)
            except (FileNotFoundError, OSError):
                pass
        else:
            try:
                self._epoll.unregister(fd)
            except (FileNotFoundError, OSError):
                pass
        
        return True

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
    # Executor support
    # =========================================================================

    def run_in_executor(self, executor, func, *args):
        """Run a function in an executor."""
        self._check_closed()
        
        if executor is None:
            executor = self._get_default_executor()
        
        future = executor.submit(func, *args)
        
        # Wrap in asyncio Future
        loop_future = self.create_future()
        
        def on_done(f):
            if self._closed:
                return  # Silently ignore if loop is closed
            try:
                result = f.result()
                self.call_soon_threadsafe(loop_future.set_result, result)
            except Exception as e:
                try:
                    self.call_soon_threadsafe(loop_future.set_exception, e)
                except RuntimeError:
                    pass  # Loop closed, ignore
        
        future.add_done_callback(on_done)
        return loop_future

    def _get_default_executor(self):
        """Get or create the default executor."""
        if not hasattr(self, '_default_executor') or self._default_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._default_executor = ThreadPoolExecutor()
        return self._default_executor

    def set_default_executor(self, executor):
        """Set the default executor."""
        self._default_executor = executor

    # =========================================================================
    # Server creation (Pure io_uring)
    # =========================================================================

    async def create_server(
        self,
        protocol_factory,
        host=None,
        port=None,
        *,
        family=socket.AF_UNSPEC,
        flags=socket.AI_PASSIVE,
        sock=None,
        backlog=100,
        ssl=None,
        reuse_address=None,
        reuse_port=None,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
        start_serving=True,
    ):
        """Create a TCP server using io_uring accept."""
        if ssl is not None:
            raise NotImplementedError("SSL not yet supported")
        
        if sock is not None:
            sockets = [sock]
        else:
            sockets = []
            infos = socket.getaddrinfo(
                host, port, family, socket.SOCK_STREAM, 0, flags
            )
            for af, socktype, proto, canonname, sa in infos:
                try:
                    sock = socket.socket(af, socktype, proto)
                except OSError:
                    continue
                
                sockets.append(sock)
                
                if reuse_address:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if reuse_port:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                
                sock.setblocking(False)
                sock.bind(sa)
                sock.listen(backlog)
        
        # Create server object
        from uringcore.server import UringServer
        server = UringServer(self, sockets, protocol_factory)
        
        # Register with io_uring
        for s in sockets:
            fd = s.fileno()
            self._core.register_fd(fd, "tcp_listener")
            self._servers[fd] = (server, protocol_factory)
            if start_serving:
                self._core.submit_accept(fd)
        
        return server

    # =========================================================================
    # UDP Datagram Endpoint
    # =========================================================================

    async def create_datagram_endpoint(
        self,
        protocol_factory,
        local_addr=None,
        remote_addr=None,
        *,
        family=0,
        proto=0,
        flags=0,
        reuse_port=None,
        allow_broadcast=None,
        sock=None,
    ):
        """Create a datagram (UDP) endpoint.
        
        Returns (transport, protocol) tuple.
        """
        self._check_closed()
        
        if sock is not None:
            # Use provided socket
            if local_addr or remote_addr:
                raise ValueError("socket and host/port cannot both be specified")
        else:
            # Create socket based on addresses
            if family == 0:
                family = socket.AF_INET
            
            sock = socket.socket(family, socket.SOCK_DGRAM, proto)
            sock.setblocking(False)
            
            if reuse_port:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            if allow_broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            if local_addr:
                sock.bind(local_addr)
            
            if remote_addr:
                sock.connect(remote_addr)
        
        # Create protocol and transport
        protocol = protocol_factory()
        
        from uringcore.datagram import UringDatagramTransport
        transport = UringDatagramTransport(self, sock, protocol, remote_addr)
        
        # Notify protocol
        protocol.connection_made(transport)
        
        return transport, protocol

    # =========================================================================
    # Unix Sockets
    # =========================================================================

    async def create_unix_connection(
        self,
        protocol_factory,
        path=None,
        *,
        ssl=None,
        sock=None,
        server_hostname=None,
        ssl_handshake_timeout=None,
    ):
        """Create a Unix socket connection.
        
        Returns (transport, protocol) tuple.
        """
        self._check_closed()
        
        if ssl is not None:
            raise NotImplementedError("SSL not yet supported for Unix sockets")
        
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                sock.connect(path)
            except BlockingIOError:
                pass  # Connection in progress - will complete async
        
        # Wait for connection using add_writer
        connected = self.create_future()
        
        def on_connected():
            self.remove_writer(sock.fileno())
            # Check for connection error
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                connected.set_exception(OSError(err, "Connect failed"))
            else:
                connected.set_result(None)
        
        self.add_writer(sock.fileno(), on_connected)
        await connected
        
        # Create transport and protocol
        protocol = protocol_factory()
        
        from uringcore.transport import UringSocketTransport
        transport = UringSocketTransport(self, sock.fileno(), protocol, sock)
        self._transports[sock.fileno()] = transport
        
        protocol.connection_made(transport)
        
        self._core.register_fd(sock.fileno(), "tcp")
        self._core.submit_recv(sock.fileno())
        
        return transport, protocol

    async def create_unix_server(
        self,
        protocol_factory,
        path=None,
        *,
        sock=None,
        backlog=100,
        ssl=None,
        ssl_handshake_timeout=None,
        start_serving=True,
    ):
        """Create a Unix socket server.
        
        Returns a Server object.
        """
        self._check_closed()
        
        if ssl is not None:
            raise NotImplementedError("SSL not yet supported for Unix sockets")
        
        import os
        
        if sock is not None:
            sockets = [sock]
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            
            # Remove existing socket file if it exists
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            
            sock.bind(path)
            sock.listen(backlog)
            sockets = [sock]
        
        # Create server object
        from uringcore.server import UringServer
        server = UringServer(self, sockets, protocol_factory)
        
        # Register with io_uring
        for s in sockets:
            fd = s.fileno()
            self._core.register_fd(fd, "unix_listener")
            self._servers[fd] = (server, protocol_factory)
            if start_serving:
                self._core.submit_accept(fd)
        
        return server

    # =========================================================================
    # Client connection (Pure io_uring)
    # =========================================================================

    async def create_connection(
        self,
        protocol_factory,
        host=None,
        port=None,
        *,
        ssl=None,
        family=0,
        proto=0,
        flags=0,
        sock=None,
        local_addr=None,
        server_hostname=None,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
        happy_eyeballs_delay=None,
        interleave=None,
    ):
        """Create a connection using io_uring."""
        if ssl is not None:
            raise NotImplementedError("SSL not yet supported")
        
        if sock is None:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
            if not infos:
                raise OSError(f"getaddrinfo({host!r}) failed")
            
            af, socktype, proto, canonname, sa = infos[0]
            sock = socket.socket(af, socktype, proto)
            sock.setblocking(False)
            
            # Perform connect (non-blocking)
            try:
                sock.connect(sa)
            except BlockingIOError:
                pass  # Expected for non-blocking
        
        fd = sock.fileno()
        
        # Create protocol
        protocol = protocol_factory()
        
        # Create transport
        from uringcore.transport import UringSocketTransport
        transport = UringSocketTransport(self, fd, protocol, sock=sock)
        self._transports[fd] = transport
        
        # Register and start receiving
        self._core.register_fd(fd, "tcp")
        protocol.connection_made(transport)
        self._core.submit_recv(fd)
        
        return transport, protocol

    # =========================================================================
    # Subprocess
    # =========================================================================

    async def subprocess_exec(
        self,
        protocol_factory,
        *args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs
    ):
        """Execute a subprocess.
        
        Returns (transport, protocol) tuple.
        """
        self._check_closed()
        
        import subprocess as sp
        
        proc = sp.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **kwargs
        )
        
        protocol = protocol_factory()
        
        from uringcore.subprocess import SubprocessTransport
        transport = SubprocessTransport(self, protocol, proc)
        
        # Notify protocol
        protocol.connection_made(transport)
        
        return transport, protocol

    async def subprocess_shell(
        self,
        protocol_factory,
        cmd,
        *,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs
    ):
        """Execute a shell command.
        
        Returns (transport, protocol) tuple.
        """
        self._check_closed()
        
        import subprocess as sp
        
        proc = sp.Popen(
            cmd,
            shell=True,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **kwargs
        )
        
        protocol = protocol_factory()
        
        from uringcore.subprocess import SubprocessTransport
        transport = SubprocessTransport(self, protocol, proc)
        
        # Notify protocol
        protocol.connection_made(transport)
        
        return transport, protocol

    # =========================================================================
    # Socket operations (Pure io_uring)
    # =========================================================================

    async def sock_recv(self, sock, nbytes):
        """Receive data from the socket using io_uring."""
        fd = sock.fileno()
        fut = self.create_future()
        
        # Store future for completion handler
        if fd not in self._transports:
            self._core.register_fd(fd, "tcp")
        
        # Submit receive and wait for completion
        self._core.submit_recv(fd)
        
        # This is a simplified implementation
        # Real implementation would track futures per-fd
        return await fut

    async def sock_sendall(self, sock, data):
        """Send data to the socket using io_uring."""
        fd = sock.fileno()
        
        if fd not in self._transports:
            self._core.register_fd(fd, "tcp")
        
        self._core.submit_send(fd, data)

    async def sock_connect(self, sock, address):
        """Connect socket to address."""
        sock.setblocking(False)
        try:
            sock.connect(address)
        except BlockingIOError:
            pass
        # For now, we rely on non-blocking connect completion

    async def sock_accept(self, sock):
        """Accept a connection on a socket."""
        fd = sock.fileno()
        self._core.submit_accept(fd)
        # Simplified - real implementation would await the accept completion

    # =========================================================================
    # Debug and exception handling
    # =========================================================================

    def get_debug(self):
        """Return the debug mode setting."""
        return self._debug

    def set_debug(self, enabled):
        """Set the debug mode."""
        self._debug = enabled

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
    # Signal Handlers
    # =========================================================================

    def add_signal_handler(self, sig, callback, *args):
        """Add a handler for a signal.
        
        Args:
            sig: Signal number (e.g., signal.SIGINT)
            callback: Callback function
            *args: Arguments to pass to callback
        """
        import signal as signal_module
        
        self._check_closed()
        
        if sig == signal_module.SIGKILL or sig == signal_module.SIGSTOP:
            raise RuntimeError(f"Cannot register handler for signal {sig}")
        
        def _signal_handler(signum, frame):
            self.call_soon_threadsafe(callback, *args)
        
        # Store old handler and set new one
        self._signal_handlers[sig] = (callback, args)
        signal_module.signal(sig, _signal_handler)

    def remove_signal_handler(self, sig) -> bool:
        """Remove a handler for a signal.
        
        Args:
            sig: Signal number
            
        Returns:
            True if handler was removed, False if not present
        """
        import signal as signal_module
        
        if sig not in self._signal_handlers:
            return False
        
        del self._signal_handlers[sig]
        signal_module.signal(sig, signal_module.SIG_DFL)
        return True

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_buffer_stats(self):
        """Get buffer pool statistics."""
        return self._core.buffer_stats()

    def get_fd_stats(self):
        """Get FD state statistics."""
        return self._core.fd_stats()
