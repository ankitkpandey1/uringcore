
import asyncio
import socket
import time
import uringcore
import os

async def benchmark_udp(loop_factory, name):
    print(f"Benchmarking {name}...")
    loop = loop_factory()
    asyncio.set_event_loop(loop)

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    server.setblocking(False)
    server_addr = server.getsockname()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.setblocking(False)

    data = b"x" * 1024
    N = 100000

    async def run():
        start = time.perf_counter()
        for _ in range(N):
            await loop.sock_sendto(client, data, server_addr)
            await loop.sock_recvfrom(server, 4096)
        end = time.perf_counter()
        
        duration = end - start
        ops = N / duration
        print(f"{name}: {ops:.2f} ops/sec, {duration:.2f}s total")
        return ops

    try:
        ops = loop.run_until_complete(run())
    finally:
        server.close()
        client.close()
        loop.close()
    return ops

def main():
    import sys
    
    # Benchmark asyncio
    if sys.version_info >= (3, 11):
        print("Benchmarking asyncio...")
        try:
             asyncio.run(benchmark_udp(asyncio.new_event_loop, "asyncio"))
        except Exception as e:
             print(f"Asyncio bench failed: {e}")
    else:
        print("Asyncio sock_recvfrom/sendto requires Python 3.11+")

    # Benchmark uvloop if available
    try:
        import uvloop
        print("Benchmarking uvloop...")
        # uvloop doesn't like being run inside asyncio.run if it replaces policy globally?
        # Actually standard usage is fine.
        asyncio.run(benchmark_udp(uvloop.new_event_loop, "uvloop"))
    except ImportError:
        print("uvloop not installed")
    except Exception as e:
        print(f"uvloop bench failed: {e}")

    # Benchmark uringcore
    print("Benchmarking uringcore...")
    # uringcore loop needs to be created and used. 
    # asyncio.run creates a loop using the policy. 
    # We want to manually drive it for fair comparison logic in our func.
    
    loop = uringcore.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Re-using the logic inside benchmark_udp but adapted since we have an active loop
        # We can't reuse benchmark_udp as is because it creates a NEW loop.
        # Let's adapt benchmark_udp to NOT create loop if passed. 
        pass
    except Exception:
        pass
    loop.close()
    
    # Actually, simpler: define a runner wrapper
    def run_benchmark_simple(name, loop_factory):
         loop = loop_factory()
         asyncio.set_event_loop(loop)
         try:
            # Copy body of benchmark logic or call separate async func
            server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            server.bind(('127.0.0.1', 0))
            server.setblocking(False)
            server_addr = server.getsockname()
        
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.setblocking(False)
        
            data = b"x" * 1024
            N = 100000
        
            async def run():
                start = time.perf_counter()
                for _ in range(N):
                    await loop.sock_sendto(client, data, server_addr)
                    await loop.sock_recvfrom(server, 4096)
                end = time.perf_counter()
                
                duration = end - start
                ops = N / duration
                print(f"{name}: {ops:.2f} ops/sec, {duration:.2f}s total")
            
            loop.run_until_complete(run())
         finally:
            try:
                server.close()
                client.close()
            except: pass
            loop.close()

    if sys.version_info >= (3, 11):
        run_benchmark_simple("asyncio", asyncio.new_event_loop)
    
    
    try:
        import uvloop
        try:
            run_benchmark_simple("uvloop", uvloop.new_event_loop)
        except Exception as e:
            print(f"uvloop failed (expected if sock_sendto ignored/unsupported): {e}")
    except ImportError:
        pass

    run_benchmark_simple("uringcore", uringcore.new_event_loop)

if __name__ == "__main__":
    main()
