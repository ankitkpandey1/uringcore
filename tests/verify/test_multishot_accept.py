
import asyncio
import socket
import logging
import uringcore
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

async def handler(reader, writer):
    try:
        data = await reader.read(100)
        writer.write(data)
        await writer.drain()
    except Exception as e:
        logger.error(f"Handler error: {e}")
    finally:
        writer.close()

async def client(port):
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(b"Ping")
        await writer.drain()
        data = await reader.read(100)
        writer.close()
        await writer.wait_closed()
        return data == b"Ping"
    except Exception as e:
        logger.error(f"Client error: {e}")
        return False

async def main():
    loop = asyncio.get_event_loop()
    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    logger.info(f"Server started on port {port} using Multishot Accept")

    # Launch multiple clients concurrently
    n_clients = 20
    tasks = [client(port) for _ in range(n_clients)]
    
    start = time.time()
    results = await asyncio.gather(*tasks)
    end = time.time()
    
    success_count = sum(results)
    logger.info(f"Accepted {success_count}/{n_clients} connections in {end-start:.4f}s")
    
    server.close()
    await server.wait_closed()
    
    assert success_count == n_clients, "Not all clients connected successfully"
    print("SUCCESS")

if __name__ == "__main__":
    import uringcore
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
