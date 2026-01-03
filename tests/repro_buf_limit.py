import asyncio
import os
import socket
import uringcore
import time

# Force small buffer count in case env works
os.environ["URINGCORE_BUFFER_COUNT"] = "128"

async def stress_test():
    loop = asyncio.get_running_loop()
    
    print(f"Starting exhaustion test. Open files limit: {os.sysconf('SC_OPEN_MAX')}")
    
    iters = 0
    total_closed = 0
    
    try:
        while True:
            iters += 1
            # Batch of 50 pairs
            pairs = []
            futs = []
            
            # Create and recv
            for _ in range(50):
                try:
                    rsock, wsock = socket.socketpair()
                    rsock.setblocking(False)
                    wsock.setblocking(False)
                    pairs.append((rsock, wsock))
                    futs.append(loop.create_task(loop.sock_recv(rsock, 1)))
                except OSError as e:
                    if e.errno == 24: # EMFILE
                        print("Hit file descriptor limit, stopping loop")
                        break
                    raise

            if not pairs:
                break
                
            # Yield to let submissions happen
            await asyncio.sleep(0)
            
            # Close all violently
            for rsock, wsock in pairs:
                rsock.close()
                wsock.close()
            
            total_closed += len(pairs)
            
            # Clean up futures
            for f in futs:
                if not f.done():
                    f.cancel()
            
            if total_closed % 1000 == 0:
                print(f"Closed {total_closed} pairs...")
                # Try a verification to see if we're dead
                try:
                    verify_sock, verify_w = socket.socketpair()
                    verify_sock.setblocking(False)
                    verify_w.setblocking(False)
                    verify_task = loop.sock_recv(verify_sock, 1)
                    await loop.sock_sendall(verify_w, b"x")
                    await verify_task
                    verify_sock.close()
                    verify_w.close()
                except Exception as e:
                    print(f"FAILED at {total_closed} closed: {e}")
                    raise

    except Exception as e:
        print(f"Stopped with error: {e}")
        raise

if __name__ == "__main__":
    # Also pass arguments if possible, but env var is read by loop.py
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(stress_test())
