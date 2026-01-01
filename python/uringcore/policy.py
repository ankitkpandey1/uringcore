"""Event loop policy for uringcore.

This module provides the EventLoopPolicy class that enables
uringcore to be used as a drop-in replacement for uvloop.

Note: asyncio.AbstractEventLoopPolicy is deprecated in Python 3.16.
We use a simple class that implements the required interface without
inheriting from the deprecated ABC.

Recommended Modern Usage (Python 3.11+):
    import asyncio
    import uringcore

    with asyncio.Runner(loop_factory=uringcore.new_event_loop) as runner:
        runner.run(main())

Legacy Usage (deprecated in Python 3.16):
    import asyncio
    import uringcore

    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
"""

import asyncio
import sys
import threading
from typing import Optional, Any

from uringcore.loop import UringEventLoop


class EventLoopPolicy:
    """Event loop policy for uringcore.

    This policy creates UringEventLoop instances for asyncio operations.

    Note: This class maintains backward compatibility with
    asyncio.set_event_loop_policy() but users are encouraged to migrate
    to the asyncio.Runner pattern for Python 3.11+.
    """

    def __init__(self, **kwargs) -> None:
        """Initialize the event loop policy."""
        self._local = threading.local()
        self._loop_kwargs = kwargs

    def get_event_loop(self) -> UringEventLoop:
        """Get the event loop for the current context.

        Creates a new event loop if one doesn't exist.
        """
        loop = getattr(self._local, "loop", None)

        if loop is None or loop.is_closed():
            loop = self.new_event_loop()
            self.set_event_loop(loop)

        return loop

    def set_event_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Set the event loop for the current context."""
        self._local.loop = loop

    def new_event_loop(self) -> UringEventLoop:
        """Create a new UringEventLoop instance."""
        return UringEventLoop(**self._loop_kwargs)

    # =========================================================================
    # Child watcher (for subprocess support)
    # =========================================================================

    if sys.platform != "win32":

        def get_child_watcher(self) -> Any:
            """Get child watcher (deprecated in Python 3.12+)."""
            return None

        def set_child_watcher(self, watcher: Any) -> None:
            """Set child watcher (deprecated in Python 3.12+)."""
            pass
