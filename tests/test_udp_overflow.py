
import asyncio
import socket
import pytest
import os
import errno
import time

def test_udp_send_overflow(event_loop):
    # Create a UDP socket pair
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    server.setblocking(False)
    server_addr = server.getsockname()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.setblocking(False)
    
    # Set a small send buffer to trigger EAGAIN/ENOBUFS easily
    try:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    except OSError:
        pass # System might enforce minimum

    print(f"Send buffer size: {client.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)}")

    async def main():
        data = b"x" * 1024 # 1KB chunks
        
        count = 0
        try:
            # Try to blast data faster than it can be drained (we are not reading)
            # 10MB should be enough to overflow a small buffer
            for _ in range(10000): 
                await event_loop.sock_sendto(client, data, server_addr)
                count += 1
                if count % 1000 == 0:
                    print(f"Sent {count} packets...")
        except OSError as e:
            print(f"Caught expected error after {count} packets: {e}")
            # Map io_uring -EAGAIN result (-11) to python errno.EAGAIN
            # Depending on how it's raised, standard OSError might be errno 11.
            assert e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS)
            return

        print(f"Finished sending {count} packets without error.")
    
    event_loop.run_until_complete(main())
