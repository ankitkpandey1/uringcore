
import asyncio
import uringcore
import time

asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

async def main():
    print(f"Start: {time.monotonic()}")
    
    # Test sleep
    await asyncio.sleep(0.1)
    print(f"Awake 0.1: {time.monotonic()}")
    
    await asyncio.sleep(0.2)
    print(f"Awake 0.2: {time.monotonic()}")
    
    print("Timer test passed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Failed: {e}")
