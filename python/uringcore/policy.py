"""Event loop policy for uringcore.

This module provides the EventLoopPolicy class that enables
uringcore to be used as a drop-in replacement for uvloop.

Usage:
    import asyncio
    import uringcore

    asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
"""

import asyncio
import sys
import threading
from typing import Optional

from uringcore.loop import UringEventLoop


class EventLoopPolicy(asyncio.AbstractEventLoopPolicy):
    """Event loop policy for uringcore.
    
    This policy creates UringEventLoop instances for asyncio operations.
    Use asyncio.set_event_loop_policy(uringcore.EventLoopPolicy())
    to enable uringcore as the default event loop.
    """

    def __init__(self):
        """Initialize the event loop policy."""
        self._local = threading.local()

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
        return UringEventLoop()

    # =========================================================================
    # Child watcher (for subprocess support)
    # =========================================================================

    if sys.platform != "win32":
        def get_child_watcher(self):
            """Get the child watcher.
            
            Note: UringEventLoop currently uses the default child watcher.
            """
            return asyncio.get_child_watcher()

        def set_child_watcher(self, watcher):
            """Set the child watcher."""
            asyncio.set_child_watcher(watcher)
