"""Test FIFO ordering guarantees per file descriptor.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore


# Set up uringcore as the event loop policy before tests
@pytest.fixture(scope="module", autouse=True)
def setup_policy():
    """Set uringcore as event loop policy."""
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    yield
    asyncio.set_event_loop_policy(None)


class TestFIFOOrdering:
    """Verify strict FIFO ordering per file descriptor."""

    def test_sequential_messages_arrive_in_order(self):
        """Send numbered messages and verify received in exact order."""
        async def run():
            messages_received = []
            num_messages = 50

            async def handler(reader, writer):
                try:
                    while True:
                        data = await reader.read(1024)
                        if not data:
                            break
                        messages_received.append(data)
                finally:
                    writer.close()

            server = await asyncio.start_server(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection('127.0.0.1', port)

            for i in range(num_messages):
                msg = f"MSG:{i:05d}\n".encode()
                writer.write(msg)

            await writer.drain()
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)
            server.close()
            await server.wait_closed()

            all_data = b''.join(messages_received)
            received_msgs = [m for m in all_data.decode().strip().split('\n') if m]

            for i, msg in enumerate(received_msgs):
                expected = f"MSG:{i:05d}"
                assert msg == expected, f"Out of order: expected {expected}, got {msg}"
        
        asyncio.run(run())

    def test_partial_reads_maintain_order(self):
        """Verify partial reads preserve FIFO ordering."""
        async def run():
            received_chunks = []

            async def handler(reader, writer):
                try:
                    while True:
                        data = await reader.read(8)
                        if not data:
                            break
                        received_chunks.append(data)
                finally:
                    writer.close()

            server = await asyncio.start_server(handler, '127.0.0.1', 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection('127.0.0.1', port)

            test_data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 5
            writer.write(test_data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)
            server.close()
            await server.wait_closed()

            reassembled = b''.join(received_chunks)
            assert reassembled == test_data
        
        asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
