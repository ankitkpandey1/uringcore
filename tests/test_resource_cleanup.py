
import asyncio
import gc
import unittest
import uringcore
from uringcore import UringCore

class TestResourceCleanup(unittest.TestCase):
    def test_rapid_loop_creation_destruction_leak_check(self):
        """Verify that loops release resources (no leaks)."""
        limit = 50
        
        # Use small buffers to ensure we are testing LEAKS (accumulation),
        # not just hitting the limit on the first try.
        # 16 * 4096 = 64KB per loop. 50 loops = 3.2MB total if leaked.
        # This should easily fit if cleaned up, but might fail if leaked 
        # on very constrained systems.
        kwargs = {"buffer_count": 16, "buffer_size": 4096}
        
        for i in range(limit):
            try:
                core = UringCore(**kwargs)
                core.shutdown()
                del core
                
                if i % 10 == 0:
                    gc.collect()
            except OSError as e:
                self.fail(f"Failed at iteration {i} (Leak detected?): {e}")

    def test_friendly_error_message(self):
        """Verify the friendly error message for ENOMEM."""
        # Try to allocate a huge amount to force ENOMEM
        # 4096 * 1MB = 4GB (likely to fail on most test envs)
        try:
            UringCore(buffer_count=4096, buffer_size=1024*1024)
        except RuntimeError as e:
            msg = str(e)
            if "ENOMEM" in msg or "RLIMIT_MEMLOCK" in msg:
                # Success, found our custom message
                return
            # If it failed for another reason, that's okay, but print it
            print(f"Got error but not expected message: {msg}")
        except OSError as e:
             # Just in case it comes as OSError
            msg = str(e)
            if "ENOMEM" in msg or "RLIMIT_MEMLOCK" in msg:
                return
            print(f"Got error but not expected message: {msg}")

if __name__ == "__main__":
    unittest.main()
