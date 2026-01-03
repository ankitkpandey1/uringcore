
import asyncio
import socket
import uringcore

async def main():
    print("Testing with sync between iterations...")
    loop = asyncio.get_event_loop()
    
    for i in range(5):
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)
        wsock.setblocking(False)
        
        print(f"Iteration {i+1}: fd={rsock.fileno()}")
        
        async def sender():
            await loop.sock_sendall(wsock, b"x")
            
        async def receiver():
            return await loop.sock_recv(rsock, 1)
            
        await asyncio.gather(sender(), receiver())
        
        # Close sockets
        rsock.close()
        wsock.close()
        
        # Give time for I/O completions to be processed
        await asyncio.sleep(0.001)
        
        print(f"  Completed iteration {i+1}")
    
    print("5 iterations complete with sync")
    
    # Now test rapid fire
    print("\nNow testing 2000 iterations without sync...")
    errors = 0
    for i in range(2000):
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)
        wsock.setblocking(False)
        
        async def sender():
            await loop.sock_sendall(wsock, b"x")
            
        async def receiver():
            return await loop.sock_recv(rsock, 1)
            
        try:
            await asyncio.gather(sender(), receiver())
        except Exception as e:
            if errors == 0:
                print(f"First error at iteration {i+1}: {e}")
            errors += 1
        finally:
            rsock.close()
            wsock.close()
    
    print(f"Complete: {2000 - errors} success, {errors} errors")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
