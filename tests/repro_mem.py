import uringcore
import gc
import os
import time

def test_standard_config():
    print("\nTesting standard configuration (512 buffers)...")
    try:
        # Standard configuration
        loop = uringcore.UringEventLoop(buffer_count=512, buffer_size=32768)
        print("Success: Standard configuration initialized")
        loop.close()
    except Exception as e:
        print(f"FAILURE: Standard configuration failed: {e}")
        raise

def test_leak(iterations=50):
    print(f"\nTesting for leaks ({iterations} iterations)...")
    loops = []
    
    for i in range(iterations):
        try:
            # 512 * 32768 = 16MB per loop
            # If we leak, we will hit OOM very fast even with increased limits
            loop = uringcore.UringEventLoop(buffer_count=512, buffer_size=32768)
            # print(f"Created loop {i+1}")
            loop.close()
            
            # Explicitly clear reference and GC
            loop = None
            if i % 10 == 0:
                gc.collect()
                print(f"Iteration {i+1} passed")
            
        except Exception as e:
            print(f"Failed at iteration {i+1}: {e}")
            raise

if __name__ == "__main__":
    try:
        test_standard_config()
        test_leak()
        print("\nPASS: Memory limits fixed and no leaks detected.")
    except Exception:
        print("\nFAIL: Memory issues persist.")
        exit(1)
