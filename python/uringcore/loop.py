"""UringEventLoop: Pure io_uring asyncio event loop.

This module implements a completion-driven event loop that uses io_uring
exclusively for I/O operations. No selector fallback.
"""

import asyncio
import os
import select
import socket
import subprocess
import time
from typing import (
    Any,
    Callable,
    Optional,
    TypeVar,
    IO,
    cast,
)
from os import PathLike

from uringcore._core import UringCore, UringFuture, UringTask, UringHandle
from uringcore.subprocess import SubprocessTransport

_ProtocolT = TypeVar("_ProtocolT", bound=asyncio.BaseProtocol)


class UringEventLoop(asyncio.AbstractEventLoop):
    """Pure io_uring event loop with no selector fallback.

    All I/O operations go through the io_uring submission queue.
    Completions are delivered via eventfd signaling.
    """

    def __init__(self, **kwargs):
        """Initialize the event loop."""
        self._closed = False
        self._stopping = False
        self._running = False
        self._task_factory = None

        # Support environment variable configuration for buffer settings
        # URINGCORE_BUFFER_COUNT: Number of buffers (default: 512)
        # URINGCORE_BUFFER_SIZE: Size of each buffer in bytes (default: 32768)
        import os

        env_buffer_count = os.environ.get("URINGCORE_BUFFER_COUNT")
        env_buffer_size = os.environ.get("URINGCORE_BUFFER_SIZE")

        if env_buffer_count is not None:
            kwargs.setdefault("buffer_count", int(env_buffer_count))
        else:
            kwargs.setdefault("buffer_count", 512)

        if env_buffer_size is not None:
            kwargs.setdefault("buffer_size", int(env_buffer_size))
        else:
            kwargs.setdefault("buffer_size", 32768)

        # Initialize the Rust core
        try:
            self._core = UringCore(**kwargs)
        except RuntimeError as e:
            # Check for ENOMEM / OS error 12
            msg = str(e)
            if "os error 12" in msg or "Cannot allocate memory" in msg:
                raise RuntimeError(
                    f"Failed to initialize io_uring with {kwargs['buffer_count']}x{kwargs['buffer_size']} buffers: {e}.\n"
                    "This is typically due to low RLIMIT_MEMLOCK limits.\n"
                    "Please increase your memlock limit (e.g., 'ulimit -l 65536' or higher).\n"
                    "On WSL/Docker, you may need to configure /etc/security/limits.conf."
                ) from e
            raise

        # Ready callbacks now managed by UringCore (Rust)
        # self._ready = collections.deque()

        # Transport registry: fd -> transport
        self._transports: dict[int, Any] = {}

        # Server registry: fd -> (server, protocol_factory)
        self._servers: dict[int, tuple[Any, Callable[..., Any]]] = {}

        # Pending send buffers: fd -> list of (data, future)
        self._pending_sends: dict[int, list[tuple[bytes, asyncio.Future[Any]]]] = {}

        # Thread safety
        self._thread_id: Optional[int] = None

        # Exception handler
        self._exception_handler: Optional[Callable[[Any, dict[str, Any]], None]] = None

        # Debug mode
        self._debug = False

        # epoll for eventfd and reader/writer callbacks
        self._epoll = select.epoll()
        self._epoll.register(self._core.event_fd, select.EPOLLIN)

        # Reader/writer callbacks: fd -> (callback, args)
        self._readers: dict[int, tuple[Callable[..., Any], tuple[Any, ...]]] = {}
        self._writers: dict[int, tuple[Callable[..., Any], tuple[Any, ...]]] = {}

        # Signal handlers: signum -> (callback, args)
        # Signal handlers: signum -> (callback, args)
        self._signal_handlers: dict[int, tuple[Callable[..., Any], tuple[Any, ...]]] = (
            {}
        )

        # Native I/O futures: (fd, op_type) -> Future
        self._io_futures: dict[tuple[int, str], asyncio.Future[Any]] = {}

    # =========================================================================
    # Task Factory support (Abstract Methods)
    # =========================================================================

    def get_task_factory(
        self,
    ) -> Optional[Callable[[asyncio.AbstractEventLoop, Any], asyncio.Future[Any]]]:
        """Return the task factory, or None if the default one is in use."""
        return None

    def set_task_factory(
        self,
        factory: Optional[
            Callable[[asyncio.AbstractEventLoop, Any], asyncio.Future[Any]]
        ],
    ) -> None:
        """Set a task factory."""
        pass

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _check_closed(self) -> None:
        """Check if the loop is closed and raise if so."""
        if self._closed:
            raise RuntimeError("Event loop is closed")

    def _write_to_self(self) -> None:
        """Wake up the event loop from another thread.

        This is called by call_soon_threadsafe to wake up the selector.
        It writes to the eventfd that the epoll is monitoring.
        """
        import os

        try:
            # Write 1 to eventfd to signal wakeup (8 bytes, little-endian)
            os.write(self._core.event_fd, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            # Ignore errors if fd is closed or write fails
            pass

    def _check_running(self) -> None:
        """Check if the loop is already running."""
        if self._running:
            raise RuntimeError("This event loop is already running")

    def _get_default_executor(self) -> Any:
        # This is a bit of a hack since _get_default_executor is not public API
        # but run_in_executor uses it.
        # In a real implementation we might want to carry our own default executor.
        # For now, we rely on the base class behavior if possible, or create a default.
        return None  # run_in_executor handles None by creating a ThreadPoolExecutor

    # =========================================================================
    # Running and stopping the event loop
    # =========================================================================

    def run_forever(self) -> None:
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
        if hasattr(self, "_default_executor") and self._default_executor is not None:
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
        if hasattr(self, "_default_executor") and self._default_executor is not None:
            self._default_executor.shutdown(wait=wait)
            self._default_executor = None

    # =========================================================================
    # Internal: Running one iteration
    # =========================================================================

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
                # io_uring completion signal / wakeup
                self._core.drain_eventfd()
                self._process_completions()
            else:
                # Reader/writer callback
                if event_mask & select.EPOLLIN and fd in self._readers:
                    callback, args = self._readers[fd]
                    handle = asyncio.Handle(callback, args, self)
                    self._core.push_task(handle)
                if event_mask & select.EPOLLOUT and fd in self._writers:
                    callback, args = self._writers[fd]
                    handle = asyncio.Handle(callback, args, self)
                    self._core.push_task(handle)

        # Run one tick of Rust scheduler (timers + ready queue)
        # Timeout handled by epoll above, so we pass 0.0 (non-blocking)
        self._core.run_tick(0.0)

    def _calculate_timeout(self) -> float:
        """Calculate the timeout for the next poll."""
        if self._stopping:
            return 0.0

        if self._core.ready_len() > 0:
            return 0.0

        next_time = self._core.next_expiration()
        if next_time is not None:
            now = time.monotonic()
            timeout = max(0.0, next_time - now)
            return min(timeout, 0.01)  # Cap at 10ms for responsiveness

        return 0.01

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
        # Check for direct I/O future
        fut = self._io_futures.pop((fd, "recv"), None)
        transport = self._transports.get(fd)

        if result > 0 and data:
            if fut is not None and not fut.done():
                fut.set_result(data)
            elif transport:
                # Data received - deliver to protocol
                transport._data_received(data)
                # Rearm receive
                self._core.submit_recv(fd)
        elif result == 0:
            if fut is not None and not fut.done():
                fut.set_result(b"")
            elif transport:
                # EOF
                transport._eof_received()
        else:
            if fut is not None and not fut.done():
                # Convert result (negative errno) to exception

                fut.set_exception(OSError(-result, os.strerror(-result)))
            elif transport:
                # Error
                transport._error_received(result)

    def _handle_send_completion(self, fd: int, result: int):
        """Handle a send completion."""
        # Check for direct I/O future
        fut = self._io_futures.pop((fd, "send"), None)
        if fut is not None and not fut.done():
            if result >= 0:
                fut.set_result(None)
            else:

                fut.set_exception(OSError(-result, os.strerror(-result)))
            # Don't return, allow transport to be notified if exists (shared FD logic?)
            # Usually one or the other.

        transport = self._transports.get(fd)
        if transport is None:
            return

        transport._send_completed(result)

    def _handle_accept_completion(self, fd: int, result: int):
        """Handle an accept completion."""
        server_info = self._servers.get(fd)
        if server_info is None:
            return

        # Check for direct I/O future
        fut = self._io_futures.pop((fd, "accept"), None)

        if result >= 0:
            if fut is not None and not fut.done():
                # For sock_accept, we need to return (conn, addr)
                # We can't get addr easily from here without getpeername or modifying core to return it
                # Typically accept returns the new FD.
                # Let's create the socket object.
                try:
                    client_sock = socket.socket(fileno=result)
                    client_sock.setblocking(False)
                    # Get address
                    try:
                        addr = client_sock.getpeername()
                    except OSError:
                        addr = ("", 0)  # Fallback
                    fut.set_result((client_sock, addr))
                except Exception as e:
                    fut.set_exception(e)

            # New connection accepted (for server helper)
            if self._servers.get(fd):
                client_fd = result
                server, protocol_factory = self._servers[fd]  # Already retrieved
                self._create_transport_for_accepted(client_fd, protocol_factory)
                # Rearm accept for server
                self._core.submit_accept(fd)
        else:
            if fut is not None and not fut.done():

                fut.set_exception(OSError(-result, os.strerror(-result)))

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
        # transport.resume_reading() logic includes rearm_recv
        transport.resume_reading()
        # Initial submission is done via resume_reading -> _rearm_recv

    # Removed _process_scheduled as it is handled by Rust run_tick

    def _process_ready(self):
        """Process ready callbacks."""
        # Now handled by run_tick
        pass

    # =========================================================================
    # Callback scheduling
    # =========================================================================

    def call_soon(self, callback, *args, context=None):
        """Schedule a callback to be called soon."""
        self._check_closed()
        if self._debug:
            self._check_callback(callback, "call_soon")

        # Use Rust-native UringHandle for optimization
        handle = UringHandle(callback, args, self, context)
        self._core.push_task(handle)
        return handle

    def call_soon_threadsafe(self, callback, *args, context=None):
        """Schedule a callback to be called from another thread."""
        self._check_closed()
        if self._debug:
            self._check_callback(callback, "call_soon_threadsafe")

        handle = UringHandle(callback, args, self, context)
        self._core.push_task(handle)
        self._write_to_self()
        return handle

    def call_later(self, delay, callback, *args, context=None):
        """Schedule a callback to be called after delay seconds."""
        self._check_closed()
        when = time.monotonic() + delay
        return self.call_at(when, callback, *args, context=context)

    def call_at(
        self, when: float, callback: Callable[..., Any], *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        """Schedule a callback to be called at a specific time."""
        self._check_closed()
        handle = asyncio.TimerHandle(when, callback, args, self, context)
        self._core.push_timer(when, handle)
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

    def add_reader(
        self, fd: int | Any, callback: Callable[..., Any], *args: Any
    ) -> None:
        """Start watching a file descriptor for read availability."""
        self._check_closed()
        if hasattr(fd, "fileno"):
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

    def remove_reader(self, fd: int | Any) -> bool:
        """Stop watching a file descriptor for read availability."""
        if hasattr(fd, "fileno"):
            fd = fd.fileno()
        return self._remove_reader_no_check(fd)

    def _remove_reader_no_check(self, fd: int) -> bool:
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

    def add_writer(
        self, fd: int | Any, callback: Callable[..., Any], *args: Any
    ) -> None:
        """Start watching a file descriptor for write availability."""
        self._check_closed()
        if hasattr(fd, "fileno"):
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
        if hasattr(fd, "fileno"):
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

    def create_future(self) -> asyncio.Future[Any]:
        """Create a Future object attached to the loop."""
        return UringFuture(self)

    def create_task(self, coro, *, name=None, context=None):
        """Create a Task from a coroutine."""
        self._check_closed()
        if self._task_factory is not None:
            return self._task_factory(self, coro)

        # Use Rust-native UringTask for max performance
        # Use Rust-native UringTask for max performance
        task = UringTask(coro, self, name, context)
        # Optimization: Push task directly to scheduler (avoiding UringHandle allocation)
        # The task checks for _run() method which delegates to _step()
        self._core.push_task(task)
        return task





    # =========================================================================
    # Missing Abstract Methods (Stubs to satisfy mypy)
    # =========================================================================

    async def getaddrinfo(
        self,
        host: str | bytes | None,
        port: str | int | None,
        *,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
        # Use synchronous getaddrinfo directly - it's fast for local addresses
        # and avoids executor/threadsafe scheduling complexity
        return socket.getaddrinfo(host, port, family, type, proto, flags)

    async def getnameinfo(
        self, sockaddr: tuple[str, int] | tuple[str, int, int, int], flags: int = 0
    ) -> tuple[str, str]:
        return await self.run_in_executor(None, socket.getnameinfo, sockaddr, flags)

    async def sock_sendto(self, sock: socket.socket, data: Any, address: Any) -> int:
        # TODO: Implement using io_uring
        return cast(int, await self.run_in_executor(None, sock.sendto, data, address))

    async def sock_recvfrom(
        self, sock: socket.socket, bufsize: int
    ) -> tuple[bytes, Any]:
        # TODO: Implement using io_uring
        data, addr = await self.run_in_executor(None, sock.recvfrom, bufsize)  # type: ignore
        return cast(bytes, data), addr

    async def sock_accept(self, sock: socket.socket) -> tuple[socket.socket, Any]:
        """Accept a connection.

        The socket must be bound to an address and listening for connections.
        The return value is a pair (conn, address) where conn is a new socket
        object usable to send and receive data on the connection, and address
        is the address bound to the socket on the other end of the connection.
        """
        fd = sock.fileno()

        # Register if not already
        self._core.register_fd(fd, "tcp_listener")  # Assuming TCP for now

        fut = self.create_future()
        self._io_futures[(fd, "accept")] = fut

        self._core.submit_accept(fd)
        return cast(tuple[socket.socket, Any], await fut)

    async def sock_connect(self, sock: socket.socket, address: Any) -> None:
        # TODO: Implement using io_uring (need submit_connect)
        await self.run_in_executor(None, sock.connect, address)

    async def sock_recv(self, sock: socket.socket, nbytes: int) -> bytes:
        """Receive data from the socket.

        The return value is a bytes object representing the data received.
        The maximum amount of data to be received at once is specified by nbytes.
        """
        fd = sock.fileno()

        # Register if not already (assuming TCP/Unix stream)
        self._core.register_fd(fd, "tcp")

        fut = self.create_future()
        self._io_futures[(fd, "recv")] = fut

        self._core.submit_recv(fd)
        return cast(bytes, await fut)

    async def sock_sendall(self, sock: socket.socket, data: Any) -> None:
        """Send data to the socket.

        The socket must be connected to a remote socket.
        """
        fd = sock.fileno()
        if not data:
            return

        # Register if not already
        self._core.register_fd(fd, "tcp")

        # Simplified: Assuming one send handles it all (io_uring usually sends full buffer if possible)
        # Proper impl would loop until all sent.

        fut = self.create_future()
        self._io_futures[(fd, "send")] = fut

        # Data might need to be bytes
        if isinstance(data, (bytes, bytearray, memoryview)):
            bdata = bytes(data)
        else:
            raise TypeError("data argument must be byte-ish")

        self._core.submit_send(fd, bdata)
        await fut

    async def sendfile(
        self,
        transport: asyncio.BaseTransport,
        file: Any,
        offset: int = 0,
        count: int | None = None,
        *,
        fallback: bool = True,
    ) -> int:
        return await super().sendfile(transport, file, offset, count, fallback=fallback)

    async def sock_recv_into(self, sock: socket.socket, buf: Any) -> int:
        return cast(int, await self.run_in_executor(None, sock.recv_into, buf))

    async def sock_recvfrom_into(
        self, sock: socket.socket, buf: Any, nbytes: int = 0
    ) -> tuple[int, Any]:
        return cast(
            tuple[int, Any],
            await self.run_in_executor(None, sock.recvfrom_into, buf, nbytes),
        )

    async def sock_sendfile(
        self,
        sock: socket.socket,
        file: Any,
        offset: int = 0,
        count: int | None = None,
        *,
        fallback: bool | None = True,
    ) -> int:
        return await super().sock_sendfile(sock, file, offset, count, fallback=fallback)

    async def connect_read_pipe(
        self,
        protocol_factory: Callable[[], _ProtocolT],
        pipe: Any,
    ) -> tuple[asyncio.ReadTransport, _ProtocolT]:
        raise NotImplementedError("connect_read_pipe not implemented")

    async def connect_write_pipe(
        self,
        protocol_factory: Callable[[], _ProtocolT],
        pipe: Any,
    ) -> tuple[asyncio.WriteTransport, _ProtocolT]:
        raise NotImplementedError("connect_write_pipe not implemented")

    async def start_tls(
        self,
        transport: asyncio.BaseTransport,
        protocol: asyncio.BaseProtocol,
        sslcontext: Any,
        *,
        server_side: bool = False,
        server_hostname: str | None = None,
        ssl_handshake_timeout: float | None = None,
        ssl_shutdown_timeout: float | None = None,
    ) -> asyncio.Transport | None:
        raise NotImplementedError("start_tls not implemented")

    # =========================================================================
    # Executor support
    # =========================================================================

    def run_in_executor(self, executor: Any, func: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:  # type: ignore[override]
        self._check_closed()
        if executor is None:
            executor = self._get_default_executor()
            if executor is None:
                # Default to ThreadPoolExecutor if not set
                import concurrent.futures

                executor = concurrent.futures.ThreadPoolExecutor()
                self._default_executor = executor

        # Submit to executor
        concurrent_future = executor.submit(func, *args)

        # Create an asyncio Future to wrap the result
        # Use asyncio.Future directly to avoid isfuture() issues with UringFuture
        loop_future = asyncio.Future(loop=self)

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

        concurrent_future.add_done_callback(on_done)
        return loop_future

    def _get_default_executor(self):
        """Get or create the default executor."""
        if not hasattr(self, "_default_executor") or self._default_executor is None:
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
        protocol_factory: Callable[[], asyncio.BaseProtocol],
        host: Any = None,
        port: int | None = None,
        *,
        family: int = socket.AF_UNSPEC,
        flags: int = socket.AI_PASSIVE,
        sock: socket.socket | None = None,
        backlog: int = 100,
        ssl: Any = None,
        reuse_address: bool | None = None,
        reuse_port: bool | None = None,
        ssl_handshake_timeout: float | None = None,
        ssl_shutdown_timeout: float | None = None,
        start_serving: bool = True,
    ) -> asyncio.AbstractServer:
        """Create a TCP server using io_uring accept."""
        if ssl is not None:
            raise NotImplementedError("SSL not yet supported")

        if sock is not None:
            sockets = [sock]
        else:
            sockets = []
            infos = await self.getaddrinfo(
                host,
                port,
                family=family,  # type: ignore
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                flags=flags,
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
        protocol_factory: Callable[[], _ProtocolT],
        local_addr: tuple[str, int] | str | None = None,
        remote_addr: tuple[str, int] | str | None = None,
        *,
        family: int = 0,
        proto: int = 0,
        flags: int = 0,
        reuse_address: bool | None = None,
        reuse_port: bool | None = None,
        allow_broadcast: bool | None = None,
        sock: socket.socket | None = None,
    ) -> tuple[asyncio.DatagramTransport, _ProtocolT]:
        """Create a datagram connection."""
        self._check_closed()

        if sock is not None:
            if local_addr or remote_addr:
                raise ValueError("socket and host/port cannot both be specified")
        else:
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
        protocol_factory: Callable[[], _ProtocolT],
        path: str | None = None,
        *,
        ssl: Any = None,
        sock: socket.socket | None = None,
        server_hostname: str | None = None,
        ssl_handshake_timeout: float | None = None,
        ssl_shutdown_timeout: float | None = None,
    ) -> tuple[asyncio.Transport, _ProtocolT]:
        """Create a UNIX connection."""
        self._check_closed()
        # TODO: Implement full UNIX support
        # The original implementation is commented out or replaced by the super() call
        # if ssl is not None:
        #     raise NotImplementedError("SSL not yet supported for Unix sockets")

        # if sock is None:
        #     sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        #     sock.setblocking(False)
        #     try:
        #         sock.connect(path)
        #     except BlockingIOError:
        #         pass  # Connection in progress - will complete async

        # # Wait for connection using add_writer
        # connected = self.create_future()

        # def on_connected():
        #     self.remove_writer(sock.fileno())
        #     # Check for connection error
        #     err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        #     if err:
        #         connected.set_exception(OSError(err, "Connect failed"))
        #     else:
        #         connected.set_result(None)

        # self.add_writer(sock.fileno(), on_connected)
        # await connected

        # # Create transport and protocol
        # protocol = protocol_factory()

        # from uringcore.transport import UringSocketTransport
        # transport = UringSocketTransport(self, sock.fileno(), protocol, sock)
        # self._transports[sock.fileno()] = transport

        # protocol.connection_made(transport)

        # self._core.register_fd(sock.fileno(), "tcp")
        # self._core.submit_recv(sock.fileno())

        # return transport, protocol
        return await super().create_unix_connection(
            protocol_factory,
            path,
            ssl=ssl,
            sock=sock,
            server_hostname=server_hostname,
            ssl_handshake_timeout=ssl_handshake_timeout,
            ssl_shutdown_timeout=ssl_shutdown_timeout,
        )

    async def create_unix_server(
        self,
        protocol_factory: Callable[[], asyncio.BaseProtocol],
        path: str | PathLike[str] | None = None,
        *,
        sock: socket.socket | None = None,
        backlog: int = 100,
        ssl: Any = None,
        ssl_handshake_timeout: float | None = None,
        ssl_shutdown_timeout: float | None = None,
        start_serving: bool = True,
    ) -> asyncio.Server:
        """Create a UNIX server."""
        self._check_closed()
        # TODO: Implement full UNIX server support
        # The original implementation is commented out or replaced by the super() call
        # if ssl is not None:
        #     raise NotImplementedError("SSL not yet supported for Unix sockets")

        # import os

        # if sock is not None:
        #     sockets = [sock]
        # else:
        #     sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        #     sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        #     sock.setblocking(False)

        #     # Remove existing socket file if it exists
        #     try:
        #         os.unlink(path)
        #     except FileNotFoundError:
        #         pass

        #     sock.bind(path)
        #     sock.listen(backlog)
        #     sockets = [sock]

        # # Create server object
        # from uringcore.server import UringServer
        # server = UringServer(self, sockets, protocol_factory)

        # # Register with io_uring
        # for s in sockets:
        #     fd = s.fileno()
        #     self._core.register_fd(fd, "unix_listener")
        #     self._servers[fd] = (server, protocol_factory)
        #     if start_serving:
        #         self._core.submit_accept(fd)

        # return server
        return await super().create_unix_server(
            protocol_factory,
            path,
            sock=sock,
            backlog=backlog,
            ssl=ssl,
            ssl_handshake_timeout=ssl_handshake_timeout,
            ssl_shutdown_timeout=ssl_shutdown_timeout,
            start_serving=start_serving,
        )

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
        transport.resume_reading()

        return transport, protocol

    # =========================================================================
    # Subprocess
    # =========================================================================

    async def subprocess_exec(
        self,
        protocol_factory: Callable[[], _ProtocolT],
        program: Any,
        *args: Any,
        stdin: int | IO[Any] | None = subprocess.PIPE,
        stdout: int | IO[Any] | None = subprocess.PIPE,
        stderr: int | IO[Any] | None = subprocess.PIPE,
        universal_newlines: bool = False,
        shell: bool = False,
        bufsize: int = 0,
        encoding: str | None = None,
        errors: str | None = None,
        **kwargs: Any,
    ) -> tuple[asyncio.SubprocessTransport, _ProtocolT]:
        """Execute a subprocess.

        Returns (transport, protocol) tuple.
        """
        self._check_closed()

        if universal_newlines:
            raise ValueError("universal_newlines must be False")
        if shell:
            raise ValueError("shell must be False")
        if encoding:
            raise ValueError("encoding must be None")
        if errors:
            raise ValueError("errors must be None")

        popen_args = [program, *args]
        proc = subprocess.Popen(
            popen_args,
            shell=False,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            bufsize=bufsize,
            **kwargs,
        )

        protocol = protocol_factory()

        # The protocol produced by the factory might not match SubprocessProtocol strictly in mypy's view
        # if _ProtocolT is just BaseProtocol. But runtime it likely is.
        # We cast to satisfy the constructor.
        transport = SubprocessTransport(
            self, cast(asyncio.SubprocessProtocol, protocol), proc
        )

        # Notify protocol
        protocol.connection_made(transport)

        return transport, protocol

    async def subprocess_shell(
        self,
        protocol_factory: Callable[[], _ProtocolT],
        cmd: str | bytes,
        *,
        stdin: int | IO[Any] | None = subprocess.PIPE,
        stdout: int | IO[Any] | None = subprocess.PIPE,
        stderr: int | IO[Any] | None = subprocess.PIPE,
        universal_newlines: bool = False,
        shell: bool = True,
        bufsize: int = 0,
        encoding: str | None = None,
        errors: str | None = None,
        **kwargs: Any,
    ) -> tuple[asyncio.SubprocessTransport, _ProtocolT]:
        """Execute a shell command.

        Returns (transport, protocol) tuple.
        """
        self._check_closed()

        if universal_newlines:
            raise ValueError("universal_newlines must be False")
        if not shell:
            raise ValueError("shell must be True")
        if encoding:
            raise ValueError("encoding must be None")
        if errors:
            raise ValueError("errors must be None")

        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            bufsize=bufsize,
            **kwargs,
        )

        protocol = protocol_factory()

        transport = SubprocessTransport(
            self, cast(asyncio.SubprocessProtocol, protocol), proc
        )

        # Notify protocol
        protocol.connection_made(transport)

        return transport, protocol

    # =========================================================================
    # Debug and exception handling
    # =========================================================================

    def get_debug(self):
        """Return the debug mode setting."""
        return self._debug

    def set_debug(self, enabled):
        """Set the debug mode."""
        self._debug = enabled

    def set_exception_handler(
        self,
        handler: Optional[Callable[[asyncio.AbstractEventLoop, dict[str, Any]], Any]],
    ) -> None:
        """Set the exception handler."""
        self._exception_handler = handler

    def get_exception_handler(
        self,
    ) -> Optional[Callable[[asyncio.AbstractEventLoop, dict[str, Any]], None]]:
        """Return the current exception handler."""
        return self._exception_handler

    def default_exception_handler(self, context: dict[str, Any]) -> None:
        """Default exception handler."""
        message = context.get("message")
        if not message:
            message = "Unhandled exception in event loop"


        # Log it (print for now, strict logging later)
        # print(f"Error: {message} {exc_info}")
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

    def add_signal_handler(
        self, sig: int, callback: Callable[..., object], *args: Any
    ) -> None:
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
