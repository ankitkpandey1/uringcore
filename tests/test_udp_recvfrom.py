import asyncio
import socket
import pytest
import uringcore

def test_sock_recvfrom(event_loop):
    loop = event_loop
    assert isinstance(loop, uringcore.UringEventLoop)

    async def run_test():
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.bind(("127.0.0.1", 0))
        server_addr = server_sock.getsockname()
        server_sock.setblocking(False)

        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        msg = b"Hello io_uring"
        client_sock.sendto(msg, server_addr)

        print(f"Waiting for data on {server_addr}...")
        data, addr = await loop.sock_recvfrom(server_sock, 1024)
        print(f"Received {data} from {addr}")
        
        assert data == msg
        # addr is (ip, port)
        assert addr[0] == "127.0.0.1"
        
        server_sock.close()
        client_sock.close()

    loop.run_until_complete(run_test())
