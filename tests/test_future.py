import asyncio
import pytest
import uringcore

@pytest.fixture
def event_loop():
    """Create UringEventLoop using the modern asyncio.Runner pattern."""
    # Use the new loop_factory pattern (Python 3.11+, works in 3.14)
    loop = uringcore.new_event_loop(buffer_count=16, buffer_size=1024)
    yield loop
    loop.close()

class TestPublicFutureBehavior:
    def test_future_state_transitions(self, event_loop):
        f = event_loop.create_future()
        assert not f.done()
        assert not f.cancelled()
        
        f.set_result(42)
        assert f.done()
        assert not f.cancelled()
        assert f.result() == 42
        assert f.exception() is None

    def test_future_exception(self, event_loop):
        f = event_loop.create_future()
        exc = ValueError("Test Error")
        f.set_exception(exc)
        
        assert f.done()
        assert f.exception() is exc
        with pytest.raises(ValueError, match="Test Error"):
            f.result()

    def test_future_cancel(self, event_loop):
        f = event_loop.create_future()
        assert f.cancel()
        assert f.cancelled()
        assert f.done()
        
        with pytest.raises(asyncio.CancelledError):
            f.result()
            
        # Cancelling again should return False
        assert not f.cancel()

    def test_await_future(self, event_loop):
        f = event_loop.create_future()
        event_loop.call_soon(f.set_result, "hello")
        
        async def main():
            return await f
            
        res = event_loop.run_until_complete(main())
        assert res == "hello"

    def test_callbacks_public_api(self, event_loop):
        f = event_loop.create_future()
        results = []
        
        def cb(future):
            results.append(future.result())
            
        f.add_done_callback(cb)
        f.set_result("callback_val")
        
        # User expects callback to run 'soon'
        event_loop.run_until_complete(asyncio.sleep(0))
        assert results == ["callback_val"]

class TestTaskInteractionPublic:
    def test_task_awaiting_future(self, event_loop):
        f = event_loop.create_future()
        
        async def waiter():
            return await f
            
        t = event_loop.create_task(waiter())
        event_loop.call_soon(f.set_result, 100)
        
        res = event_loop.run_until_complete(t)
        assert res == 100

    def test_task_recursive_chain(self, event_loop):
        # Stress test deep chain of futures (Fast Path verification via behavior)
        async def recursive(depth):
            if depth == 0:
                return 0
            f = event_loop.create_future()
            event_loop.call_soon(f.set_result, 1)
            return (await f) + (await recursive(depth - 1))
            
        res = event_loop.run_until_complete(recursive(50))
        assert res == 50
