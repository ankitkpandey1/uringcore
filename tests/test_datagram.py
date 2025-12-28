"""Tests for UDP datagram transport.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore


@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


class EchoUDPProtocol(asyncio.DatagramProtocol):
    """Simple UDP echo protocol for testing."""
    
    def __init__(self):
        self.received = []
        self.transport = None
    
    def connection_made(self, transport):
        self.transport = transport
    
    def datagram_received(self, data, addr):
        self.received.append((data, addr))
        self.transport.sendto(data, addr)
    
    def error_received(self, exc):
        pass


class TestDatagramTransport:
    """Test UDP datagram transport functionality."""

    @pytest.mark.asyncio
    async def test_udp_echo(self, event_loop):
        """Test basic UDP echo."""
        # Create server
        transport, protocol = await event_loop.create_datagram_endpoint(
            EchoUDPProtocol,
            local_addr=('127.0.0.1', 0),
        )
        
        server_addr = transport.get_extra_info('sockname')
        
        # Create client
        class ClientProtocol(asyncio.DatagramProtocol):
            def __init__(self):
                self.received = asyncio.Event()
                self.data = None
                self.transport = None
            
            def connection_made(self, transport):
                self.transport = transport
            
            def datagram_received(self, data, addr):
                self.data = data
                self.received.set()
        
        client_transport, client_protocol = await event_loop.create_datagram_endpoint(
            ClientProtocol,
            remote_addr=server_addr,
        )
        
        try:
            # Send and receive
            client_transport.sendto(b"hello udp")
            
            await asyncio.wait_for(client_protocol.received.wait(), timeout=2.0)
            
            assert client_protocol.data == b"hello udp"
            assert len(protocol.received) == 1
        finally:
            transport.close()
            client_transport.close()

    @pytest.mark.asyncio
    async def test_udp_multiple_messages(self, event_loop):
        """Test sending multiple UDP messages."""
        received = []
        
        class CollectorProtocol(asyncio.DatagramProtocol):
            def __init__(self):
                self.transport = None
                self.done = asyncio.Event()
            
            def connection_made(self, transport):
                self.transport = transport
            
            def datagram_received(self, data, addr):
                received.append(data)
                if len(received) >= 5:
                    self.done.set()
        
        transport, protocol = await event_loop.create_datagram_endpoint(
            CollectorProtocol,
            local_addr=('127.0.0.1', 0),
        )
        
        server_addr = transport.get_extra_info('sockname')
        
        # Send from another endpoint
        sender, _ = await event_loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=server_addr,
        )
        
        try:
            for i in range(5):
                sender.sendto(f"msg{i}".encode())
            
            await asyncio.wait_for(protocol.done.wait(), timeout=2.0)
            
            assert len(received) == 5
        finally:
            transport.close()
            sender.close()

    @pytest.mark.asyncio
    async def test_udp_large_datagram(self, event_loop):
        """Test large UDP datagram (within MTU limits)."""
        received_data = None
        done = asyncio.Event()
        
        class ReceiverProtocol(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport
            
            def datagram_received(self, data, addr):
                nonlocal received_data
                received_data = data
                done.set()
        
        transport, _ = await event_loop.create_datagram_endpoint(
            ReceiverProtocol,
            local_addr=('127.0.0.1', 0),
        )
        
        server_addr = transport.get_extra_info('sockname')
        
        sender, _ = await event_loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=server_addr,
        )
        
        try:
            # Send 1KB datagram (safe for localhost)
            large_data = b"X" * 1024
            sender.sendto(large_data)
            
            await asyncio.wait_for(done.wait(), timeout=2.0)
            
            assert received_data == large_data
        finally:
            transport.close()
            sender.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
