"""Test backpressure and credit-based flow control.

Per ARCHITECTURE.md: Each FD maintains a credit budget limiting concurrent
in-flight operations. This prevents buffer exhaustion under high load.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore


@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop for tests."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


class TestBackpressure:
    """Verify credit-based backpressure mechanisms."""

    @pytest.mark.asyncio
    async def test_buffer_stats_available(self, event_loop):
        """Verify buffer statistics are accessible."""
        core = event_loop._core
        stats = core.buffer_stats()
        
        # Stats should be tuple of 4 ints: (total, free, quarantined, in_use)
        assert len(stats) == 4
        assert all(isinstance(x, int) for x in stats)
        
        total, free, quarantined, in_use = stats
        assert total > 0
        assert free >= 0
        assert quarantined >= 0
        assert in_use >= 0

    @pytest.mark.asyncio
    async def test_fd_stats_available(self, event_loop):
        """Verify FD statistics are accessible."""
        core = event_loop._core
        stats = core.fd_stats()
        
        # Stats format: (count, inflight, queued, paused)
        assert len(stats) == 4
        assert all(isinstance(x, int) for x in stats)

    @pytest.mark.asyncio
    async def test_server_data_flow(self, event_loop):
        """Test basic data flow through server."""
        received_count = 0
        
        async def handler(reader, writer):
            nonlocal received_count
            try:
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    received_count += 1
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection('127.0.0.1', port)

        # Send some messages
        for i in range(10):
            writer.write(f"msg{i}".encode())
        
        await writer.drain()
        
        # Read responses
        for i in range(10):
            await reader.read(100)

        writer.close()
        await writer.wait_closed()

        server.close()
        await server.wait_closed()

        assert received_count >= 1, "Server received data"

    @pytest.mark.asyncio
    async def test_metrics_during_operation(self, event_loop):
        """Test metrics module integration."""
        from uringcore import get_metrics
        
        metrics = get_metrics(event_loop)
        
        assert metrics is not None
        assert metrics.buffers_total > 0
        assert metrics.timestamp > 0
        
        # Test prometheus format
        prom_output = metrics.to_prometheus()
        assert "uringcore_buffers_total" in prom_output
        assert "uringcore_fd_count" in prom_output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
