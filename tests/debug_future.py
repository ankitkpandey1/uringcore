
import asyncio
import asyncio.futures
import uringcore

async def main():
    loop = asyncio.get_running_loop()
    
    # Test with asyncio.Future instead of loop.create_future (UringFuture)
    print("Testing with asyncio.Future instead of UringFuture...")
    future = asyncio.Future(loop=loop)
    print(f"Future type: {type(future)}")
    
    callback = asyncio.futures._set_result_unless_cancelled
    print(f"Callback: {callback}")
    
    h = loop.call_later(0.01, callback, future, "success_value")
    print(f"Handle: {h}")
    
    try:
        result = await future
        print(f"Success! Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())
