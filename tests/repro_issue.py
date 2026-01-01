
import asyncio
import unittest
import uringcore

class TestIssues(unittest.TestCase):
    def test_cancellation_suppression(self):
        """Test that a task catching CancelledError is not marked as cancelled."""
        policy = uringcore.EventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def catch_cancel():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "caught"
        
        async def main():
            t = asyncio.create_task(catch_cancel())
            await asyncio.sleep(0) # Let it start
            t.cancel()
            
            try:
                res = await t
                return res
            except asyncio.CancelledError:
                # raise Exception("Task raised CancelledError but should have returned 'caught'")
                return "failed_suppression"
            except TypeError as e:
                return f"type_error_{e}"
        
        try:
            res = loop.run_until_complete(main())
            self.assertEqual(res, "caught")
        finally:
            loop.close()

if __name__ == "__main__":
    unittest.main()
