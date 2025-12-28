"""Test FIFO ordering guarantees per file descriptor.

Per ARCHITECTURE.md: Data is delivered to Python in strict per-FD FIFO order.
Partial-consumption uses pending_offset to preserve ordering across buffer boundaries.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import pytest
import uringcore


@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop for tests."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestFIFOOrdering:
    """Verify strict FIFO ordering per file descriptor."""

    @pytest.mark.asyncio
    async def test_sequential_messages_arrive_in_order(self, event_loop):
        """Send numbered messages and verify received in exact order."""
        messages_received = []
        num_messages = 100

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

        # Send numbered messages rapidly
        for i in range(num_messages):
            msg = f"MSG:{i:05d}\n".encode()
            writer.write(msg)

        await writer.drain()
        writer.close()
        await writer.wait_closed()

        # Wait for server to process
        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()

        # Reconstruct and verify order
        all_data = b''.join(messages_received)
        received_msgs = [m for m in all_data.decode().strip().split('\n') if m]

        for i, msg in enumerate(received_msgs):
            expected = f"MSG:{i:05d}"
            assert msg == expected, f"Out of order: expected {expected}, got {msg}"

    @pytest.mark.asyncio
    async def test_partial_reads_maintain_order(self, event_loop):
        """Verify partial reads preserve FIFO ordering."""
        received_chunks = []

        async def handler(reader, writer):
            try:
                # Read in small chunks to force partial reads
                while True:
                    data = await reader.read(8)  # Small buffer
                    if not data:
                        break
                    received_chunks.append(data)
            finally:
                writer.close()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection('127.0.0.1', port)

        # Send a large message that will be split
        test_data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 10
        writer.write(test_data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()

        # Verify reassembled data matches original
        reassembled = b''.join(received_chunks)
        assert reassembled == test_data, "Partial reads corrupted data order"

    @pytest.mark.asyncio
    async def test_concurrent_connections_isolated(self, event_loop):
        """Verify FIFO ordering is per-FD, not global."""
        results = {}

        async def handler(reader, writer):
            conn_id = None
            messages = []
            try:
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    decoded = data.decode()
                    if conn_id is None:
                        # First message contains connection ID
                        conn_id = decoded.split(':')[0]
                    messages.append(decoded)
            finally:
                if conn_id:
                    results[conn_id] = messages
                writer.close()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        async def client(conn_id: str):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            for i in range(20):
                writer.write(f"{conn_id}:{i}\n".encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        # Run multiple clients concurrently
        await asyncio.gather(
            client("A"),
            client("B"),
            client("C"),
        )

        await asyncio.sleep(0.2)
        server.close()
        await server.wait_closed()

        # Verify each connection maintained its own order
        for conn_id, messages in results.items():
            all_data = ''.join(messages)
            nums = [int(x.split(':')[1]) for x in all_data.strip().split('\n') if ':' in x]
            assert nums == sorted(nums), f"Connection {conn_id} received out of order"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
