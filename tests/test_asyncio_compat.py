"""Test uringcore compatibility with asyncio APIs.

This test suite verifies that uringcore implements the asyncio.AbstractEventLoop
interface correctly and can be used as a drop-in replacement for asyncio.
"""

import asyncio
import pytest
import signal
import socket
import tempfile
import os
import uringcore


# Use a single event loop for all tests
_loop = None


@pytest.fixture(scope="module")
def event_loop():
    """Create a single uringcore event loop for all tests."""
    global _loop
    if _loop is None or _loop.is_closed():
        policy = uringcore.EventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    yield _loop


@pytest.fixture
def loop(event_loop):
    return event_loop


class TestAsyncioCompatibility:
    """Test asyncio API compatibility."""

    # =========================================================================
    # Event Loop Core
    # =========================================================================

    def test_is_running(self, loop):
        """Test is_running() method."""
        assert not loop.is_running()
        
        async def check():
            assert loop.is_running()
        
        loop.run_until_complete(check())
        assert not loop.is_running()

    def test_is_closed(self, loop):
        """Test is_closed() method."""
        assert not loop.is_closed()

    def test_time(self, loop):
        """Test time() method."""
        import time
        t1 = loop.time()
        time.sleep(0.001)
        t2 = loop.time()
        assert t2 > t1

    # =========================================================================
    # Callbacks
    # =========================================================================

    def test_call_soon(self, loop):
        """Test call_soon()."""
        result = []
        loop.call_soon(result.append, 1)
        loop.run_until_complete(asyncio.sleep(0.01))
        assert result == [1]

    def test_call_later(self, loop):
        """Test call_later()."""
        result = []
        loop.call_later(0.01, result.append, 1)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert result == [1]

    def test_call_at(self, loop):
        """Test call_at()."""
        result = []
        when = loop.time() + 0.01
        loop.call_at(when, result.append, 1)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert result == [1]

    def test_call_soon_threadsafe(self, loop):
        """Test call_soon_threadsafe()."""
        import threading
        result = []
        
        def thread_func():
            loop.call_soon_threadsafe(result.append, 1)
        
        async def test():
            t = threading.Thread(target=thread_func)
            t.start()
            await asyncio.sleep(0.1)
            t.join()
        
        loop.run_until_complete(test())
        assert result == [1]

    # =========================================================================
    # Futures and Tasks
    # =========================================================================

    def test_create_future(self, loop):
        """Test create_future()."""
        fut = loop.create_future()
        assert isinstance(fut, asyncio.Future)
        assert not fut.done()

    def test_create_task(self, loop):
        """Test create_task()."""
        async def coro():
            return 42
        
        task = loop.create_task(coro())
        result = loop.run_until_complete(task)
        assert result == 42

    # =========================================================================
    # Reader/Writer Callbacks
    # =========================================================================

    def test_add_reader(self, loop):
        """Test add_reader/remove_reader."""
        r_fd, w_fd = os.pipe()
        os.set_blocking(r_fd, False)
        result = []
        
        def on_read():
            result.append(os.read(r_fd, 100))
            loop.remove_reader(r_fd)
        
        loop.add_reader(r_fd, on_read)
        os.write(w_fd, b'test')
        
        loop.run_until_complete(asyncio.sleep(0.05))
        
        os.close(r_fd)
        os.close(w_fd)
        
        assert result == [b'test']

    def test_add_writer(self, loop):
        """Test add_writer/remove_writer."""
        r_fd, w_fd = os.pipe()
        os.set_blocking(w_fd, False)
        written = []
        
        def on_write():
            written.append(True)
            os.write(w_fd, b'test')
            loop.remove_writer(w_fd)
        
        loop.add_writer(w_fd, on_write)
        loop.run_until_complete(asyncio.sleep(0.05))
        
        data = os.read(r_fd, 100)
        os.close(r_fd)
        os.close(w_fd)
        
        assert written == [True]
        assert data == b'test'

    # =========================================================================
    # Signal Handlers
    # =========================================================================

    def test_add_signal_handler(self, loop):
        """Test add_signal_handler/remove_signal_handler."""
        result = []
        
        def on_signal():
            result.append(True)
        
        loop.add_signal_handler(signal.SIGUSR1, on_signal)
        
        async def test():
            os.kill(os.getpid(), signal.SIGUSR1)
            await asyncio.sleep(0.1)
        
        loop.run_until_complete(test())
        
        removed = loop.remove_signal_handler(signal.SIGUSR1)
        assert removed
        assert result == [True]

    # =========================================================================
    # TCP Networking
    # =========================================================================

    def test_tcp_echo(self, loop):
        """Test TCP echo server using start_server."""
        async def test():
            async def handle(reader, writer):
                data = await reader.read(100)
                writer.write(data)
                await writer.drain()
                writer.close()
            
            server = await asyncio.start_server(handle, '127.0.0.1', 19880)
            await asyncio.sleep(0.05)
            
            reader, writer = await asyncio.open_connection('127.0.0.1', 19880)
            writer.write(b'hello')
            await writer.drain()
            
            response = await reader.read(100)
            writer.close()
            
            server.close()
            await server.wait_closed()
            
            assert response == b'hello'
        
        loop.run_until_complete(test())

    # =========================================================================
    # UDP Networking
    # =========================================================================

    def test_udp_echo(self, loop):
        """Test UDP using create_datagram_endpoint."""
        async def test():
            class ServerProtocol(asyncio.DatagramProtocol):
                def __init__(self):
                    self.transport = None
                
                def connection_made(self, transport):
                    self.transport = transport
                
                def datagram_received(self, data, addr):
                    self.transport.sendto(data, addr)
            
            class ClientProtocol(asyncio.DatagramProtocol):
                def __init__(self, future):
                    self.future = future
                
                def connection_made(self, transport):
                    self.transport = transport
                    transport.sendto(b'hello udp')
                
                def datagram_received(self, data, addr):
                    self.future.set_result(data)
            
            server, _ = await loop.create_datagram_endpoint(
                ServerProtocol, local_addr=('127.0.0.1', 19881)
            )
            
            future = loop.create_future()
            client, _ = await loop.create_datagram_endpoint(
                lambda: ClientProtocol(future),
                remote_addr=('127.0.0.1', 19881)
            )
            
            result = await asyncio.wait_for(future, timeout=2.0)
            
            client.close()
            server.close()
            
            assert result == b'hello udp'
        
        loop.run_until_complete(test())

    # =========================================================================
    # Unix Sockets
    # =========================================================================

    def test_unix_socket(self, loop):
        """Test Unix socket server/client."""
        async def test():
            with tempfile.TemporaryDirectory() as tmpdir:
                path = os.path.join(tmpdir, 'test.sock')
                
                async def handle(reader, writer):
                    data = await reader.read(100)
                    writer.write(data.upper())
                    await writer.drain()
                    writer.close()
                
                server = await asyncio.start_unix_server(handle, path)
                await asyncio.sleep(0.05)
                
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(b'hello')
                await writer.drain()
                
                response = await reader.read(100)
                writer.close()
                
                server.close()
                await server.wait_closed()
                
                assert response == b'HELLO'
        
        loop.run_until_complete(test())

    # =========================================================================
    # Subprocess
    # =========================================================================

    def test_subprocess_exec(self, loop):
        """Test subprocess_exec."""
        async def test():
            class Protocol(asyncio.SubprocessProtocol):
                def __init__(self, future):
                    self.future = future
                    self.output = []
                
                def connection_made(self, transport):
                    pass
                
                def pipe_data_received(self, fd, data):
                    self.output.append(data)
                
                def pipe_connection_lost(self, fd, exc):
                    pass
                
                def process_exited(self):
                    self.future.set_result(b''.join(self.output))
            
            future = loop.create_future()
            transport, protocol = await loop.subprocess_exec(
                lambda: Protocol(future),
                'echo', 'hello subprocess'
            )
            
            result = await asyncio.wait_for(future, timeout=5.0)
            
            assert b'hello subprocess' in result
        
        loop.run_until_complete(test())

    # =========================================================================
    # Executor
    # =========================================================================

    def test_run_in_executor(self, loop):
        """Test run_in_executor."""
        async def test():
            def blocking():
                return 42
            
            result = await loop.run_in_executor(None, blocking)
            assert result == 42
        
        loop.run_until_complete(test())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
