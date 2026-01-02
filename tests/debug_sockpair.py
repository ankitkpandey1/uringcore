
import asyncio
import socket
import uringcore

async def main():
    print("Testing socketpair with uringcore...")
    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)
    
    loop = asyncio.get_event_loop()
    print(f"Loop type: {type(loop)}")
    
    async def sender():
        print("Sender: sending...")
        await loop.sock_sendall(wsock, b"x")
        print("Sender: done")
        
    async def receiver():
        print("Receiver: receiving...")
        data = await loop.sock_recv(rsock, 1)
        print(f"Receiver: got {data}")
        
    try:
        await asyncio.gather(sender(), receiver())
        print("SUCCESS")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        rsock.close()
        wsock.close()

if __name__ == "__main__":
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
