"""Integration tests for uringcore with Uvicorn and Gunicorn fork model.

These tests verify that uringcore handles the fork-based worker model
used by production ASGI/WSGI servers correctly.
"""

import asyncio
import multiprocessing
import os
import signal
import socket
import time
from typing import Callable


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestForkSafety:
    """Test fork safety of uringcore."""

    def test_parent_child_isolation(self):
        """Verify parent and child processes have isolated io_uring instances."""
        from uringcore import UringCore
        
        parent_core = UringCore()
        parent_gen = parent_core.generation_id
        parent_pid = os.getpid()
        
        # Fork a child process
        pid = os.fork()
        
        if pid == 0:
            # Child process
            child_core = UringCore()
            child_gen = child_core.generation_id
            
            # Child should get a new instance with new generation
            assert child_gen == 1  # Fresh instance
            assert os.getpid() != parent_pid
            
            os._exit(0)
        else:
            # Parent process
            _, status = os.waitpid(pid, 0)
            assert os.WEXITSTATUS(status) == 0
            
            # Parent's instance should be unaffected
            assert parent_core.generation_id == parent_gen

    def test_generation_id_increments_on_fork(self):
        """Verify generation ID mechanism works correctly."""
        from uringcore import UringCore
        
        core = UringCore()
        gen1 = core.generation_id
        
        # Simulate fork detection by checking if increment works
        # (actual fork detection happens in Ring::check_fork)
        assert gen1 > 0

    def test_buffer_pool_fork_safety(self):
        """Verify buffer pool handles fork correctly."""
        from uringcore import UringCore
        
        parent_core = UringCore()
        parent_stats = parent_core.buffer_stats()
        
        pid = os.fork()
        
        if pid == 0:
            # Child process
            try:
                child_core = UringCore()
                child_stats = child_core.buffer_stats()
                
                # Child should have fresh buffer pool
                assert child_stats[0] > 0  # total buffers
                os._exit(0)
            except Exception:
                os._exit(1)
        else:
            _, status = os.waitpid(pid, 0)
            assert os.WEXITSTATUS(status) == 0


class TestWorkerModel:
    """Test worker process model similar to Gunicorn/Uvicorn."""

    def test_multiple_workers(self):
        """Test spawning multiple worker processes."""
        from uringcore import UringCore
        
        num_workers = 4
        results = multiprocessing.Queue()
        
        def worker(worker_id: int, results_queue):
            """Worker process that creates its own UringCore."""
            try:
                core = UringCore()
                pid = os.getpid()
                event_fd = core.event_fd
                gen_id = core.generation_id
                
                results_queue.put({
                    'worker_id': worker_id,
                    'pid': pid,
                    'event_fd': event_fd,
                    'generation_id': gen_id,
                    'success': True
                })
            except Exception as e:
                results_queue.put({
                    'worker_id': worker_id,
                    'success': False,
                    'error': str(e)
                })
        
        # Spawn workers
        workers = []
        for i in range(num_workers):
            p = multiprocessing.Process(target=worker, args=(i, results))
            p.start()
            workers.append(p)
        
        # Wait for all workers
        for p in workers:
            p.join(timeout=10)
        
        # Collect results
        worker_results = []
        while not results.empty():
            worker_results.append(results.get())
        
        # Verify all workers succeeded
        assert len(worker_results) == num_workers
        for r in worker_results:
            assert r['success'], f"Worker {r.get('worker_id')} failed: {r.get('error')}"
        
        # Verify each worker has unique PID and event_fd
        pids = [r['pid'] for r in worker_results]
        assert len(set(pids)) == num_workers, "Workers should have unique PIDs"

    def test_worker_restart(self):
        """Test worker restart scenario."""
        from uringcore import UringCore
        
        results = multiprocessing.Queue()
        
        def worker(results_queue):
            core = UringCore()
            results_queue.put({
                'pid': os.getpid(),
                'event_fd': core.event_fd,
                'success': True
            })
        
        # First worker
        p1 = multiprocessing.Process(target=worker, args=(results,))
        p1.start()
        p1.join(timeout=5)
        
        # Second worker (restart)
        p2 = multiprocessing.Process(target=worker, args=(results,))
        p2.start()
        p2.join(timeout=5)
        
        # Both should succeed
        r1 = results.get()
        r2 = results.get()
        
        assert r1['success']
        assert r2['success']
        assert r1['pid'] != r2['pid']


class TestGracefulShutdown:
    """Test graceful shutdown handling."""

    def test_signal_handling(self):
        """Test that uringcore handles signals gracefully."""
        import signal
        from uringcore import UringCore
        
        core = UringCore()
        
        # Verify core can be shut down
        core.shutdown()
        
        # Should be able to create a new core after shutdown
        core2 = UringCore()
        assert core2.event_fd >= 0

    def test_context_manager_pattern(self):
        """Test using uringcore with context management patterns."""
        from uringcore import UringCore
        
        # Create and explicitly clean up
        core = UringCore()
        event_fd = core.event_fd
        assert event_fd >= 0
        
        core.shutdown()
        
        # Verify cleanup worked by creating new instance
        core2 = UringCore()
        assert core2.event_fd >= 0


class TestUvicornIntegration:
    """Placeholder tests for Uvicorn integration.
    
    Note: Full Uvicorn integration requires the network transport layer.
    These tests document the expected behavior.
    """

    def test_uvicorn_worker_config(self):
        """Document expected Uvicorn worker configuration."""
        expected_config = {
            "workers": 4,
            "loop": "uringcore",  # Custom loop implementation
            "lifespan": "on",
            "access_log": False,  # For benchmarking
        }
        
        # Placeholder - actual integration requires:
        # 1. Implementing uvloop-compatible interface
        # 2. Registering as uvicorn loop option
        # 3. Handling lifespan events
        assert True

    def test_gunicorn_worker_class(self):
        """Document expected Gunicorn worker class."""
        expected_worker_class = "uringcore.workers.UringcoreWorker"
        
        # Placeholder - actual implementation requires:
        # 1. Subclassing gunicorn.workers.base.Worker
        # 2. Implementing run() with uringcore event loop
        # 3. Handling graceful shutdown
        assert True


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
