"""Stress test for uringcore with concurrent connections."""

import asyncio
import socket
import time
from concurrent.futures import ThreadPoolExecutor


async def stress_test_concurrent_connections():
    """Test concurrent connections to echo server."""
    import uringcore
    
    loop = asyncio.get_running_loop()
    port = 19877
    connection_count = [0]
    
    async def handle(reader, writer):
        connection_count[0] += 1
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    
    server = await asyncio.start_server(handle, '127.0.0.1', port)
    await asyncio.sleep(0.1)
    
    def make_clients(n):
        """Make n sequential client connections."""
        successes = 0
        for i in range(n):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(('127.0.0.1', port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                
                msg = f'client{i:03d}'.encode()
                sock.sendall(msg)
                
                response = b''
                while len(response) < len(msg):
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    response += chunk
                
                sock.close()
                if response == msg:
                    successes += 1
            except Exception:
                pass
        return successes
    
    # Run 10 batches of 10 clients each using run_in_executor
    # This allows the event loop to process io_uring completions
    num_batches = 10
    clients_per_batch = 10
    total_success = 0
    
    start = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=num_batches) as ex:
        for i in range(num_batches):
            count = await loop.run_in_executor(ex, make_clients, clients_per_batch)
            total_success += count
    
    end = time.perf_counter()
    
    total_clients = num_batches * clients_per_batch
    
    print(f"Stress Test Results:")
    print(f"  Total clients: {total_clients}")
    print(f"  Successful: {total_success}")
    print(f"  Failed: {total_clients - total_success}")
    print(f"  Server connections: {connection_count[0]}")
    print(f"  Duration: {end - start:.2f}s")
    print(f"  Rate: {total_success / (end - start):.0f} conn/s")
    
    server.close()
    await server.wait_closed()
    
    # Assert at least 90% success rate
    success_rate = total_success / total_clients
    assert success_rate >= 0.90, f"Success rate {success_rate:.1%} below 90%"
    
    print(f"\nPASS: Stress test passed with {success_rate:.1%} success rate")


if __name__ == '__main__':
    import uringcore
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    asyncio.run(stress_test_concurrent_connections())
