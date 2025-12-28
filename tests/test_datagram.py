"""Tests for UDP datagram transport.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore


# Set up uringcore as the event loop policy before tests
@pytest.fixture(scope="module", autouse=True)
def setup_policy():
    """Set uringcore as event loop policy."""
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    yield
    asyncio.set_event_loop_policy(None)


class TestDatagramTransport:
    """Test UDP datagram transport functionality."""

    def test_udp_transport_creation(self):
        """Test UDP transport can be created."""
        async def run():
            loop = asyncio.get_event_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=('127.0.0.1', 0),
            )
            
            assert transport is not None
            sockname = transport.get_extra_info('sockname')
            assert sockname is not None
            
            transport.close()
        
        asyncio.run(run())

    def test_udp_sendto(self):
        """Test UDP sendto doesn't raise."""
        async def run():
            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=('127.0.0.1', 0),
            )
            
            server_addr = transport.get_extra_info('sockname')
            
            sender, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=server_addr,
            )
            
            sender.sendto(b"test")
            
            sender.close()
            transport.close()
        
        asyncio.run(run())

    def test_udp_get_extra_info(self):
        """Test UDP transport extra info."""
        async def run():
            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=('127.0.0.1', 0),
            )
            
            sockname = transport.get_extra_info('sockname')
            assert sockname is not None
            
            unknown = transport.get_extra_info('unknown', 'default')
            assert unknown == 'default'
            
            transport.close()
        
        asyncio.run(run())

    def test_udp_close(self):
        """Test UDP transport close."""
        async def run():
            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=('127.0.0.1', 0),
            )
            
            assert not transport.is_closing()
            transport.close()
            assert transport.is_closing()
        
        asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
