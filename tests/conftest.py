import pytest
import uringcore
import asyncio
import os

# Set limits for test environment (overridable)
os.environ.setdefault("URINGCORE_BUFFER_COUNT", "128")
os.environ.setdefault("URINGCORE_BUFFER_SIZE", "4096")

@pytest.fixture(scope="session", autouse=True)
def configure_event_loop_policy():
    """Ensure uringcore policy is used for all tests."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)

@pytest.fixture(scope="function")
def event_loop():
    """Create an instance of the default event loop for each test function."""
    loop = uringcore.new_event_loop()
    yield loop
    loop.close()
