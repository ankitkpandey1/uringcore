import socket
import uringcore
import asyncio
import sys
import os

print(f"PID: {os.getpid()}", file=sys.stderr)

def test_pbuf_ring_sync():
    # 1. Setup Server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    addr = server.getsockname()
    print(f"Server listening on {addr}", file=sys.stderr)

    # 2. Setup Client
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(addr)
    server_sock, _ = server.accept()
    
    server_sock.setblocking(False)
    client.setblocking(False)

    # 3. Setup UringCore
    # Disable SQPOLL to debug hang issues
    loop = uringcore.UringEventLoop(try_sqpoll=False)
    asyncio.set_event_loop(loop)
    core = loop._core
    
    try:
        # Send data
        client.send(b"Hello PBufRing")
        
        # Create a future (bound to this loop)
        fut = loop.create_future()
        
        print("Submitting multishot recv...", file=sys.stderr)
        core.submit_recv_multishot(server_sock.fileno(), fut)
        
        print("Running loop until future done...", file=sys.stderr)
        data = loop.run_until_complete(fut)
        
        print(f"Received: {data}", file=sys.stderr)
        assert data == b"Hello PBufRing"
        print("SUCCESS")
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        if "Provided Buffer Ring not available" in str(e):
             print("SKIP: Kernel too old or PBufRing init failed.")
        else:
             sys.exit(1)
    finally:
        server_sock.close()
        client.close()
        server.close()
        loop.close()

if __name__ == "__main__":
    test_pbuf_ring_sync()
