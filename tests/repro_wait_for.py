
import asyncio
import uringcore

async def main():
    async def noop():
        pass
    
    print(f"Current task: {asyncio.current_task()}")
    try:
        await asyncio.wait_for(noop(), timeout=1.0)
        print("Success")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
