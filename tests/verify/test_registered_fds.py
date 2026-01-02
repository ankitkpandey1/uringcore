import socket
import uringcore
import asyncio
import sys
import os

print(f"PID: {os.getpid()}", file=sys.stderr)

def test_registered_fds():
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
    # Disable SQPOLL to avoid potential flakes, focus on logic
    loop = uringcore.UringEventLoop(try_sqpoll=False)
    asyncio.set_event_loop(loop)
    core = loop._core
    
    try:
        # Register server socket FD
        fd = server_sock.fileno()
        print(f"Registering FD {fd}...", file=sys.stderr)
        idx = core.register_file(fd)
        print(f"Registered FD {fd} as index {idx}", file=sys.stderr)
        
        # Test 1: Receive using registered FD
        # Send data
        client.send(b"Hello Fixed FD")
        
        # Recv
        print("Receiving data...", file=sys.stderr)
        # Using standard sock_recv, but UringCore should detect registered FD and use it internally
        # We can't easily query kernel to prove it used fixed file, but if it works, logic held up.
        # Ideally we'd benchmark, but verification checks correctness.
        
        async def do_recv():
            return await loop.sock_recv(server_sock, 1024)
            
        data = loop.run_until_complete(do_recv())
        print(f"Received: {data}", file=sys.stderr)
        assert data == b"Hello Fixed FD"
        
        # Test 2: Unregister and Recv again
        print(f"Unregistering FD {fd}...", file=sys.stderr)
        core.unregister_file(fd)
        
        client.send(b"Hello Regular FD")
        print("Receiving data again...", file=sys.stderr)
        
        data = loop.run_until_complete(do_recv())
        print(f"Received: {data}", file=sys.stderr)
        assert data == b"Hello Regular FD"
        
        print("SUCCESS")
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        server_sock.close()
        client.close()
        server.close()
        loop.close()

if __name__ == "__main__":
    test_registered_fds()
