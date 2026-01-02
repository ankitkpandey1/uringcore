
import asyncio
import uringcore
import concurrent.futures
import pickle

def worker_func(loop):
    return "worker"

async def main():
    loop = asyncio.get_running_loop()
    print(f"Loop type: {type(loop)}")
    
    # Try direct pickle
    try:
        pickle.dumps(loop)
        print("Pickle successful (unexpected)")
    except Exception as e:
        print(f"Pickle failed as expected: {e}")

    # Try ProcessPoolExecutor
    with concurrent.futures.ProcessPoolExecutor() as executor:
        try:
            # We pass 'loop' to worker, which triggers pickle
            future = loop.run_in_executor(executor, worker_func, loop)
            await future
        except Exception as e:
            print(f"Executor failed: {e}")

if __name__ == "__main__":
    event_loop = uringcore.new_event_loop(buffer_count=256)
    try:
        event_loop.run_until_complete(main())
    finally:
        event_loop.close()
