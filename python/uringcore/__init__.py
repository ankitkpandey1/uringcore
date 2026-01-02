"""uringcore: Completion-driven asyncio event loop using io_uring.

This module provides a drop-in replacement for uvloop using io_uring
with Completion-Driven Virtual Readiness (CDVR).

Usage (Python 3.11+ recommended pattern with asyncio.Runner):
    import asyncio
    import uringcore

    async def main():
        # Your async code here
        pass

    with asyncio.Runner(loop_factory=uringcore.new_event_loop) as runner:
        runner.run(main())

Legacy Usage (deprecated in Python 3.16):
    import asyncio
    import uringcore

    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    asyncio.run(main())

Environment Variables:
    URINGCORE_BUFFER_COUNT: Number of io_uring buffers (default: 512)
    URINGCORE_BUFFER_SIZE: Size of each buffer in bytes (default: 32768)

Copyright (c) 2025 Ankit Kumar Pandey <ankitkpandey1@gmail.com>
Licensed under the Apache-2.0 License.
"""

from uringcore._core import (
    UringCore,
    UringFuture,
    UringHandle,
    UringTask,
    __version__,
    __author__,
)
from uringcore.loop import UringEventLoop
from uringcore.policy import EventLoopPolicy
from uringcore.transport import UringSocketTransport
from uringcore.server import UringServer
from uringcore.metrics import Metrics, MetricsCollector, get_metrics


def new_event_loop(**kwargs) -> UringEventLoop:
    """Factory function for creating UringEventLoop instances.

    Recommended for use with asyncio.Runner (Python 3.11+):

        with asyncio.Runner(loop_factory=uringcore.new_event_loop) as runner:
            runner.run(main())

    Args:
        **kwargs: Passed to UringEventLoop (buffer_count, buffer_size, etc.)

    Returns:
        A new UringEventLoop instance.
    """
    return UringEventLoop(**kwargs)


__all__ = [
    "UringCore",
    "UringEventLoop",
    "EventLoopPolicy",
    "UringSocketTransport",
    "UringServer",
    "Metrics",
    "MetricsCollector",
    "get_metrics",
    "new_event_loop",
    "UringFuture",
    "UringHandle",
    "UringTask",
    "__version__",
    "__author__",
]
