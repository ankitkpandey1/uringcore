"""UringServer: Server implementation for io_uring event loop."""

import asyncio
from typing import List, Callable, Any


class UringServer(asyncio.AbstractServer):
    """Server using io_uring for accepting connections."""

    def __init__(self, loop, sockets: List[Any], protocol_factory: Callable):
        """Initialize the server.
        
        Args:
            loop: The UringEventLoop instance
            sockets: List of listening sockets
            protocol_factory: Factory function for creating protocols
        """
        self._loop = loop
        self._sockets = list(sockets)
        self._protocol_factory = protocol_factory
        self._serving = False
        self._serving_forever_fut = None

    def get_loop(self):
        """Return the event loop associated with the server."""
        return self._loop

    def is_serving(self):
        """Return True if the server is accepting connections."""
        return self._serving

    @property
    def sockets(self):
        """Return a list of server sockets."""
        return tuple(self._sockets)

    def close(self):
        """Stop serving and close the server."""
        if not self._sockets:
            return
        
        self._serving = False
        
        # Unregister and close sockets
        for sock in self._sockets:
            fd = sock.fileno()
            self._loop._servers.pop(fd, None)
            self._loop._core.unregister_fd(fd)
            sock.close()
        
        self._sockets.clear()

    async def start_serving(self):
        """Start accepting connections."""
        if self._serving:
            return
        
        self._serving = True
        for sock in self._sockets:
            fd = sock.fileno()
            self._loop._core.submit_accept(fd)

    async def serve_forever(self):
        """Start accepting connections and run until close() is called."""
        if self._serving_forever_fut is not None:
            raise RuntimeError("server.serve_forever() called twice")
        
        await self.start_serving()
        self._serving_forever_fut = self._loop.create_future()
        
        try:
            await self._serving_forever_fut
        except asyncio.CancelledError:
            pass
        finally:
            self._serving_forever_fut = None
            self.close()

    async def wait_closed(self):
        """Wait until the server is closed."""
        # Simple implementation - just return immediately after close
        pass

    def __repr__(self):
        return f"<UringServer sockets={len(self._sockets)} serving={self._serving}>"
