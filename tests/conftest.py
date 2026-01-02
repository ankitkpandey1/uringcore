import pytest
import uringcore
import asyncio
import os

# Set limits for test environment (overridable)
os.environ.setdefault("URINGCORE_BUFFER_COUNT", "512")
os.environ.setdefault("URINGCORE_BUFFER_SIZE", "32768")

@pytest.fixture(scope="session", autouse=True)
def configure_event_loop_policy():
    """Ensure uringcore policy is used for all tests."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
