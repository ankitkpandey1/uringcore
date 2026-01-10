
import asyncio
import socket
import os
import pytest
import uringcore



def test_unix_datagram_sendto_recvfrom(event_loop):
    server_path = "/tmp/uring_unix_dgram_server.sock"
    client_path = "/tmp/uring_unix_dgram_client.sock"

    if os.path.exists(server_path):
        os.remove(server_path)
    if os.path.exists(client_path):
        os.remove(client_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(server_path)
    server.setblocking(False)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.bind(client_path)
    client.setblocking(False)

    async def main():
        try:
            data = b"Hello UNIX Datagram"
            
            # Test io_uring sendto with path
            sent = await event_loop.sock_sendto(client, data, server_path)
            assert sent == len(data)

            # Test io_uring recvfrom
            received, addr = await event_loop.sock_recvfrom(server, 1024)
            assert received == data
            # Address in UNIX datagram might be the client path
            assert addr == client_path
            
        finally:
            server.close()
            client.close()
            if os.path.exists(server_path):
                os.remove(server_path)
            if os.path.exists(client_path):
                os.remove(client_path)

    event_loop.run_until_complete(main())
