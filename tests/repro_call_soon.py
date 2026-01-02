
import asyncio
import uringcore

async def main():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    # Test call_soon with trivial callback
    def cb(val):
        print(f"Callback called with {val}")
    
    loop.call_soon(cb, 42)
    # loop.call_soon(fut.set_result, 42)
    await asyncio.sleep(0.1) # Yield to allow callback to run
    
    # Enable future test later
    # res = await fut
    # print(f"Result: {res}")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
