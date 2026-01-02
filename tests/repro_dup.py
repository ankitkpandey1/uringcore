import asyncio
import uringcore
import sys

async def main():
    loop = asyncio.get_running_loop()
    
    async def handle_echo(reader, writer):
        data = await reader.read(100)
        print(f"Server received: {data!r}")
        writer.write(data)
        await writer.drain()
        print(f"Server sent: {data!r}")
        writer.close()

    server = await asyncio.start_server(handle_echo, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    
    print("Client sending 'hello'...")
    writer.write(b'hello')
    await writer.drain()
    
    print("Client reading...")
    data = await reader.read(100)
    print(f"Client received: {data!r}")
    
    writer.close()
    server.close()
    await server.wait_closed()
    
    if data != b'hello':
        print(f"FAILURE: Expected b'hello', got {data!r}")
        sys.exit(1)
    else:
        print("SUCCESS")

uringcore.new_event_loop(buffer_count=256)
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
asyncio.run(main())
