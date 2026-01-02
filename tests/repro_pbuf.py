
import asyncio
import uringcore
import socket
import os

HOST = '127.0.0.1'
PORT = 9999

# Current buffer_count default is 1024 or 256 depending on init
# We send enough data to exhaust the ring

# Shared state
PORT = 0
ready_event = asyncio.Event()

async def server():
    global PORT
    # Standard asyncio server is fine, or raw socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, 0))
    PORT = srv.getsockname()[1]
    print(f"Server listening on {PORT}")
    srv.listen(1)
    srv.setblocking(False)
    
    ready_event.set()
    
    loop = asyncio.get_running_loop()
    
    while True:
        try:
            conn, _ = await loop.sock_accept(srv)
            # Send 2000 packets of data. If ring is 64, it should fail without replenishment
            # Each packet is small but count exceeds ring size
            for i in range(2000):
                await loop.sock_sendall(conn, b"x" * 100)
            conn.close()
            break
        except Exception:
            pass
    srv.close()

async def client():
    # Wait for server port
    await ready_event.wait()
    
    # Use uringcore loop
    loop = asyncio.get_running_loop()
    
    if PORT == 0:
        raise RuntimeError("Port not set")

    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.connect((HOST, PORT))
    cli.setblocking(False)
    fd = cli.fileno()
    
    loop._core.register_fd(fd, "tcp")
    
    count = 0
    try:
        # PBufRing usage test via submit_recv_multishot? 
        # Actually standard recv now uses submit_recv (one-shot). 
        # But wait, submit_recv uses PBufRing if available?
        # NO. lib.rs submit_recv uses buffer_pool.acquire() manually.
        # ONLY submit_recv_multishot uses PBufRing.
        
        # We need to call submit_recv_multishot manually to test this path and the fix.
        # The fix involved drain_completions replenishing PBufRing.
        
        # We invoke it once, and then wait for data?
        # A single submit_recv_multishot should yield multiple completions?
        # Or does it need one submission per batch?
        # Usually multishot means "keep receiving".
        
        fut = loop.create_future()
        # We pass a future, but loop.py handles it by calling a handler.
        # Ideally we'd have a callback.
        
        # Just calling it to start the flow
        loop._core.submit_recv_multishot(fd, fut)
        
        # We just sleep and let the loop process completions.
        # The completions will trigger _handle_recv_completion.
        # Since we passed a future, the FIRST completion might resolve it.
        # Subsequent ones might be dropped if we don't re-arm or if valid?
        # loop.py _handle_recv_completion: "if fut is not None... if not fut.done(): fut.set_result(data)"
        # So only the first packet resolves the future.
        # The rest are dropped ("pass") or handled by protocol?
        # We don't have a protocol here.
        
        # BUT, the goal is to trigger ENOBUFS on the RING side.
        # Even if Python drops the data, the kernel consumes a buffer.
        # If we don't replenish, the kernel will stop sending.
        # So successful "dropping" of 2000 packets means the ring IS working.
        # If it wasn't working, the server would stall or we'd see errors.
        
        await asyncio.sleep(2) 
            
    except Exception as e:
        print(f"Client Error: {e}")
    finally:
        cli.close()

async def main():
    await asyncio.gather(server(), client())

if __name__ == "__main__":
    event_loop = uringcore.new_event_loop(buffer_count=64)
    try:
        event_loop.run_until_complete(main())
    finally:
        event_loop.close()
