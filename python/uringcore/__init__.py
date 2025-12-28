"""uringcore: Completion-driven asyncio event loop using io_uring.

This module provides a drop-in replacement for uvloop using io_uring
with Completion-Driven Virtual Readiness (CDVR).

Usage:
    import asyncio
    import uringcore

    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())

    async def main():
        # Your async code here
        pass

    asyncio.run(main())

Copyright (c) 2025 Ankit Kumar Pandey <itsankitkp@gmail.com>
Licensed under the Apache-2.0 License.
"""

from uringcore._core import UringCore, __version__, __author__
from uringcore.loop import UringEventLoop
from uringcore.policy import EventLoopPolicy
from uringcore.transport import UringSocketTransport
from uringcore.server import UringServer
from uringcore.metrics import Metrics, MetricsCollector, get_metrics

__all__ = [
    "UringCore",
    "UringEventLoop",
    "EventLoopPolicy",
    "UringSocketTransport",
    "UringServer",
    "Metrics",
    "MetricsCollector",
    "get_metrics",
    "__version__",
    "__author__",
]

