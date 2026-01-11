import socket
import pytest
import uringcore
import asyncio

@pytest.fixture
def event_loop():
    loop = uringcore.new_event_loop()
    yield loop
    loop.close()

def test_sock_sendto(event_loop):
    """Test sock_sendto updates using loop.sock_sendto."""
    
    # Create two UDP sockets
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    server.setblocking(False)
    server_addr = server.getsockname()
    
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.setblocking(False)
    
    async def main():
        # Send data from client to server
        data = b"Hello, uring!"
        
        # Test io_uring path
        sent = await event_loop.sock_sendto(client, data, server_addr)
        assert sent == len(data)
        
        # Verify receipt
        received, addr = await event_loop.sock_recvfrom(server, 1024)
        # Client is bound to 0.0.0.0 but server sees 127.0.0.1
        assert received == data
        assert addr[1] == client.getsockname()[1] # Port must match

    event_loop.run_until_complete(main())
    
    server.close()
    client.close()
