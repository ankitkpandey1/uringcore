
import asyncio
import socket
import os
import pytest
import uringcore



def test_create_unix_server(event_loop):
    path = "/tmp/uring_unix_server.sock"
    if os.path.exists(path):
        os.remove(path)

    async def handler(reader, writer):
        data = await reader.read(100)
        writer.write(data)
        await writer.drain()
        writer.write_eof()
        await asyncio.sleep(0.1)
        writer.close()
        await writer.wait_closed()

    async def main():
        server = await event_loop.create_unix_server(handler, path=path)
        
        try:
            reader, writer = await asyncio.open_unix_connection(path, limit=65536) # Removed loop=event_loop as it's implicit or uses get_event_loop
            # Actually, open_unix_connection uses get_running_loop internally.
            # Since we are in main(), running loop is event_loop.
            
            msg = b"Hello UNIX Stream"
            writer.write(msg)
            await writer.drain()
            
            response = await reader.read(100)
            assert response == msg
            
            writer.close()
            await writer.wait_closed()
            
        finally:
            server.close()
            await server.wait_closed()
            if os.path.exists(path):
                os.remove(path)

    event_loop.run_until_complete(main())
