
import asyncio
import os
import uringcore
import unittest

class TestAddReaderWriter(unittest.TestCase):
    def test_add_reader(self):
        """Test add_reader registers fd for reading."""
        policy = uringcore.EventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
        
            result = []
        
            def on_read():
                print("DEBUG: on_read called!")
                result.append(os.read(r_fd, 100))
                loop.remove_reader(r_fd)
        
            print(f"DEBUG: Adding reader for fd {r_fd}")
            loop.add_reader(r_fd, on_read)
            print("DEBUG: Writing to pipe")
            os.write(w_fd, b'test')
        
            async def runner():
                print("DEBUG: Runner started")
                await asyncio.sleep(0.05)
                print("DEBUG: Runner finished")
        
            print("DEBUG: Starting loop")
            loop.run_until_complete(runner())
            print("DEBUG: Loop finished")
        
            os.close(r_fd)
            os.close(w_fd)
            
            assert result == [b'test']
        finally:
            loop.close()

if __name__ == "__main__":
    unittest.main()
