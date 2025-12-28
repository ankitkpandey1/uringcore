"""Test backpressure and credit-based flow control.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore
from uringcore import get_metrics


# Set up uringcore as the event loop policy before tests
@pytest.fixture(scope="module", autouse=True)
def setup_policy():
    """Set uringcore as event loop policy."""
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    yield
    asyncio.set_event_loop_policy(None)


class TestBackpressure:
    """Verify credit-based backpressure mechanisms."""

    def test_buffer_stats_available(self):
        """Verify buffer statistics are accessible."""
        async def run():
            loop = asyncio.get_event_loop()
            core = loop._core
            stats = core.buffer_stats()
            
            assert len(stats) == 4
            assert all(isinstance(x, int) for x in stats)
            
            total, free, quarantined, in_use = stats
            assert total > 0
            assert free >= 0
        
        asyncio.run(run())

    def test_fd_stats_available(self):
        """Verify FD statistics are accessible."""
        async def run():
            loop = asyncio.get_event_loop()
            core = loop._core
            stats = core.fd_stats()
            
            assert len(stats) == 4
            assert all(isinstance(x, int) for x in stats)
        
        asyncio.run(run())

    def test_server_data_flow(self):
        """Test basic data flow through server."""
        async def run():
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

            for i in range(5):
                writer.write(f"msg{i}".encode())
            
            await writer.drain()
            
            for i in range(5):
                await reader.read(100)

            writer.close()
            await writer.wait_closed()

            server.close()
            await server.wait_closed()

            assert received_count >= 1
        
        asyncio.run(run())

    def test_metrics_integration(self):
        """Test metrics module integration."""
        async def run():
            loop = asyncio.get_event_loop()
            metrics = get_metrics(loop)
            
            assert metrics is not None
            assert metrics.buffers_total > 0
            assert metrics.timestamp > 0
            
            prom_output = metrics.to_prometheus()
            assert "uringcore_buffers_total" in prom_output
            assert "uringcore_fd_count" in prom_output
        
        asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
