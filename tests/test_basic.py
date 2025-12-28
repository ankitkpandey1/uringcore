"""Unit tests for uringcore submit_* methods."""

import asyncio
import pytest
import uringcore


# Use a single event loop for all tests to avoid multiple io_uring instances
_loop = None


@pytest.fixture(scope="module")
def event_loop():
    """Create a single uringcore event loop for all tests in module."""
    global _loop
    if _loop is None or _loop.is_closed():
        policy = uringcore.EventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        _loop = asyncio.new_event_loop()
    yield _loop
    # Don't close - reuse for other tests


@pytest.fixture
def loop(event_loop):
    """Provide the shared event loop."""
    return event_loop


class TestUringCore:
    """Test the UringCore Rust backend."""

    def test_core_creation(self, loop):
        """Test UringCore can be accessed via loop."""
        assert loop._core is not None

    def test_event_fd(self, loop):
        """Test event_fd is a valid file descriptor."""
        assert loop._core.event_fd > 0

    def test_fd_registration(self, loop):
        """Test FD can be registered and unregistered."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fd = sock.fileno()
        
        loop._core.register_fd(fd, "tcp")
        loop._core.unregister_fd(fd)
        sock.close()

    def test_fd_stats(self, loop):
        """Test fd_stats returns tuple of ints."""
        stats = loop._core.fd_stats()
        assert isinstance(stats, tuple)
        assert len(stats) == 4

    def test_drain_completions_empty(self, loop):
        """Test drain_completions returns list."""
        completions = loop._core.drain_completions()
        assert isinstance(completions, list)


class TestEventLoop:
    """Test UringEventLoop functionality."""

    def test_loop_creation(self, loop):
        """Test event loop creation."""
        assert loop is not None
        assert not loop.is_closed()

    def test_call_soon(self, loop):
        """Test call_soon schedules callback."""
        result = []
        
        def callback():
            result.append(1)
        
        loop.call_soon(callback)
        loop._run_once()
        
        assert result == [1]

    def test_call_later(self, loop):
        """Test call_later schedules delayed callback."""
        result = []
        
        def callback():
            result.append(1)
        
        loop.call_later(0.01, callback)
        
        # Run loop briefly
        async def runner():
            await asyncio.sleep(0.05)
        
        loop.run_until_complete(runner())
        assert result == [1]

    def test_create_future(self, loop):
        """Test create_future returns a Future."""
        fut = loop.create_future()
        assert isinstance(fut, asyncio.Future)

    def test_create_task(self, loop):
        """Test create_task creates a Task from coroutine."""
        async def coro():
            return 42
        
        task = loop.create_task(coro())
        result = loop.run_until_complete(task)
        assert result == 42


class TestAddReaderWriter:
    """Test add_reader/add_writer functionality."""

    def test_add_reader(self, loop):
        """Test add_reader registers fd for reading."""
        import os
        r_fd, w_fd = os.pipe()
        os.set_blocking(r_fd, False)
        
        result = []
        
        def on_read():
            result.append(os.read(r_fd, 100))
            loop.remove_reader(r_fd)
        
        loop.add_reader(r_fd, on_read)
        os.write(w_fd, b'test')
        
        async def runner():
            await asyncio.sleep(0.05)
        
        loop.run_until_complete(runner())
        
        os.close(r_fd)
        os.close(w_fd)
        
        assert result == [b'test']

    def test_add_writer(self, loop):
        """Test add_writer registers fd for writing."""
        import os
        r_fd, w_fd = os.pipe()
        os.set_blocking(w_fd, False)
        
        written = []
        
        def on_write():
            written.append(True)
            os.write(w_fd, b'test')
            loop.remove_writer(w_fd)
        
        loop.add_writer(w_fd, on_write)
        
        async def runner():
            await asyncio.sleep(0.05)
        
        loop.run_until_complete(runner())
        
        data = os.read(r_fd, 100)
        os.close(r_fd)
        os.close(w_fd)
        
        assert written == [True]
        assert data == b'test'


class TestNetworking:
    """Test networking functionality."""

    def test_tcp_echo(self, loop):
        """Test TCP echo server works."""
        async def test():
            async def handle(reader, writer):
                data = await reader.read(100)
                writer.write(data)
                await writer.drain()
                writer.close()
            
            server = await asyncio.start_server(handle, '127.0.0.1', 19876)
            await asyncio.sleep(0.05)
            
            reader, writer = await asyncio.open_connection('127.0.0.1', 19876)
            writer.write(b'hello')
            await writer.drain()
            
            response = await reader.read(100)
            writer.close()
            
            server.close()
            await server.wait_closed()
            
            assert response == b'hello'
        
        loop.run_until_complete(test())
