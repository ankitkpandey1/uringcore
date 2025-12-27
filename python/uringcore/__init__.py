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

Copyright (c) 2024 Ankit Kumar Pandey <itsankitkp@gmail.com>
Licensed under the MIT License.
"""

from uringcore._core import UringCore, __version__, __author__
from uringcore.loop import UringEventLoop
from uringcore.policy import EventLoopPolicy

__all__ = [
    "UringCore",
    "UringEventLoop",
    "EventLoopPolicy",
    "__version__",
    "__author__",
]
