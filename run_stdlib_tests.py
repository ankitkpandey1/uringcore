
import sys
import os
import unittest
from test import support

# Ensure uringcore is importable
sys.path.insert(0, os.path.abspath("python"))

import uringcore
import asyncio

def run_tests():
    print("Replacing asyncio event loop policy with UringEventLoopPolicy...")
    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    
    # We want to run `test.test_asyncio`
    # We can load it using unittest
    print("Loading test.test_asyncio...")
    
    # Python 3.13 location might differ slightly or require strict module naming
    try:
        from test import test_asyncio
    except ImportError:
        print("Could not import test.test_asyncio. Are you on a standard Python install?")
        return

    # Create a test suite
    suite = unittest.TestLoader().loadTestsFromModule(test_asyncio)
    
    # Run it
    print("Running asyncio stdlib tests with uringcore...")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    
    if not result.wasSuccessful():
        sys.exit(1)

if __name__ == "__main__":
    # Reduce buffer usage for test suite as well
    os.environ["URINGCORE_BUFFER_COUNT"] = "64" 
    run_tests()
