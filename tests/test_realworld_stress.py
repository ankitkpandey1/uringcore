#!/usr/bin/env python3
"""
Real-world stress test: HTTP server running for 60 seconds with concurrent requests.
This verifies uringcore works correctly under sustained load.
"""
import asyncio
import time
import sys

# Must set policy before any asyncio usage
import uringcore
asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single HTTP request."""
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not data:
            return
        
        # Simple HTTP response
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\nHello, World!"
        writer.write(response)
        await writer.drain()
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"Handler error: {e}", file=sys.stderr)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def client_worker(host: str, port: int, results: dict, stop_event: asyncio.Event):
    """Continuously send requests until stopped."""
    while not stop_event.is_set():
        reader = None
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            if b"200 OK" in response:
                results["success"] += 1
            else:
                results["error"] += 1
        except asyncio.CancelledError:
            break
        except Exception as e:
            results["error"] += 1
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
        await asyncio.sleep(0.001)  # Small delay between requests


async def run_stress_test(duration_seconds: int = 60, num_clients: int = 10):
    """Run the stress test."""
    print(f"=== uringcore Real-World Stress Test ===")
    print(f"Duration: {duration_seconds}s | Clients: {num_clients}")
    print()
    
    # Start server
    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    addr = server.sockets[0].getsockname()
    print(f"Server started on {addr[0]}:{addr[1]}")
    
    # Stats
    results = {"success": 0, "error": 0}
    stop_event = asyncio.Event()
    
    # Start clients
    client_tasks = [
        asyncio.create_task(client_worker(addr[0], addr[1], results, stop_event))
        for _ in range(num_clients)
    ]
    
    # Run for duration
    start_time = time.time()
    last_print = start_time
    
    while time.time() - start_time < duration_seconds:
        await asyncio.sleep(1.0)
        elapsed = time.time() - start_time
        rps = results["success"] / elapsed if elapsed > 0 else 0
        print(f"  [{int(elapsed):3d}s] Requests: {results['success']:,} | Errors: {results['error']} | RPS: {rps:,.0f}")
    
    # Stop clients
    stop_event.set()
    for t in client_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    
    # Stop server
    server.close()
    await server.wait_closed()
    
    # Final stats
    total_time = time.time() - start_time
    total_requests = results["success"] + results["error"]
    rps = results["success"] / total_time
    
    print()
    print("=== Results ===")
    print(f"Total Time:     {total_time:.1f}s")
    print(f"Total Requests: {total_requests:,}")
    print(f"Successful:     {results['success']:,}")
    print(f"Errors:         {results['error']}")
    print(f"RPS:            {rps:,.0f}")
    print()
    
    # Verify success
    if results["error"] > total_requests * 0.01:  # <1% error rate
        print("❌ FAILED: Error rate too high")
        return False
    if results["success"] < 100:
        print("❌ FAILED: Too few successful requests")
        return False
    
    print("✅ PASSED: Real-world stress test")
    return True


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    success = asyncio.run(run_stress_test(duration_seconds=duration))
    sys.exit(0 if success else 1)
