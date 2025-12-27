"""Basic tests for uringcore."""

import asyncio
import pytest


class TestBasicFunctionality:
    """Test basic event loop functionality."""

    def test_import(self):
        """Test that uringcore can be imported."""
        import uringcore
        assert hasattr(uringcore, "UringCore")
        assert hasattr(uringcore, "UringEventLoop")
        assert hasattr(uringcore, "EventLoopPolicy")

    def test_core_creation(self):
        """Test that UringCore can be created."""
        from uringcore import UringCore
        
        core = UringCore()
        assert core.event_fd >= 0
        assert core.generation_id > 0

    def test_event_loop_creation(self):
        """Test that UringEventLoop can be created."""
        from uringcore import UringEventLoop
        
        loop = UringEventLoop()
        assert not loop.is_running()
        assert not loop.is_closed()
        loop.close()
        assert loop.is_closed()

    def test_buffer_stats(self):
        """Test buffer statistics."""
        from uringcore import UringCore
        
        core = UringCore()
        total, free, quarantined, in_use = core.buffer_stats()
        assert total > 0
        assert free > 0
        assert quarantined >= 0
        assert in_use >= 0
        assert total == free + quarantined + in_use

    def test_fd_stats(self):
        """Test FD state statistics."""
        from uringcore import UringCore
        
        core = UringCore()
        fd_count, inflight, queued, paused = core.fd_stats()
        assert fd_count >= 0
        assert inflight >= 0
        assert queued >= 0
        assert paused >= 0


class TestEventLoopPolicy:
    """Test the event loop policy."""

    def test_policy_creation(self):
        """Test that EventLoopPolicy can be created."""
        from uringcore import EventLoopPolicy
        
        policy = EventLoopPolicy()
        assert policy is not None

    def test_new_event_loop(self):
        """Test creating a new event loop via policy."""
        from uringcore import EventLoopPolicy, UringEventLoop
        
        policy = EventLoopPolicy()
        try:
            loop = policy.new_event_loop()
            assert isinstance(loop, UringEventLoop)
            loop.close()
        except RuntimeError as e:
            if "Cannot allocate memory" in str(e):
                pytest.skip("Insufficient memlock limit for buffer pool")
            raise


class TestAsyncExecution:
    """Test async execution."""

    @pytest.mark.asyncio
    async def test_simple_coroutine(self):
        """Test running a simple coroutine."""
        result = await asyncio.sleep(0, result=42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_call_soon(self):
        """Test call_soon scheduling."""
        results = []
        
        def callback(value):
            results.append(value)
        
        loop = asyncio.get_event_loop()
        loop.call_soon(callback, 1)
        loop.call_soon(callback, 2)
        
        await asyncio.sleep(0)
        
        assert results == [1, 2]

    @pytest.mark.asyncio
    async def test_create_task(self):
        """Test task creation."""
        async def coro():
            return 42
        
        task = asyncio.create_task(coro())
        result = await task
        assert result == 42
