"""SSL/TLS transport wrapper for uringcore."""

import asyncio
import ssl
from typing import Any, Optional


class SSLTransport(asyncio.Transport):
    """SSL wrapper transport that layers TLS on top of a base transport."""

    def __init__(self, loop, base_transport, protocol, ssl_context, 
                 server_hostname=None, server_side=False):
        """Initialize SSL transport.
        
        Args:
            loop: The event loop
            base_transport: The underlying transport (e.g., UringSocketTransport)
            protocol: The application protocol
            ssl_context: SSL context
            server_hostname: Server hostname for SNI
            server_side: True if this is the server side
        """
        self._loop = loop
        self._base_transport = base_transport
        self._app_protocol = protocol
        self._ssl_context = ssl_context
        self._server_hostname = server_hostname
        self._server_side = server_side
        self._closing = False
        self._closed = False
        
        # Create in-memory BIO for SSL
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        
        # Create SSL object
        self._ssl_object = ssl_context.wrap_bio(
            self._incoming, self._outgoing,
            server_side=server_side,
            server_hostname=server_hostname
        )
        
        # Handshake state
        self._handshake_started = False
        self._handshake_complete = False
        self._handshake_future = None
        
        # Internal protocol for base transport
        self._ssl_protocol = _SSLProtocol(self)

    async def do_handshake(self):
        """Perform SSL handshake asynchronously."""
        if self._handshake_complete:
            return
        
        self._handshake_started = True
        self._handshake_future = self._loop.create_future()
        
        # Start handshake
        self._do_handshake_step()
        
        await self._handshake_future

    def _do_handshake_step(self):
        """Execute one step of the SSL handshake."""
        try:
            self._ssl_object.do_handshake()
            self._handshake_complete = True
            if self._handshake_future and not self._handshake_future.done():
                self._handshake_future.set_result(None)
            # Notify app protocol
            self._app_protocol.connection_made(self)
        except ssl.SSLWantReadError:
            # Need more data from peer
            self._flush_outgoing()
        except ssl.SSLWantWriteError:
            # Need to write data
            self._flush_outgoing()
        except Exception as exc:
            if self._handshake_future and not self._handshake_future.done():
                self._handshake_future.set_exception(exc)

    def _data_received(self, data):
        """Called when data is received from the base transport."""
        self._incoming.write(data)
        
        if not self._handshake_complete:
            self._do_handshake_step()
        else:
            self._read_decrypted_data()

    def _read_decrypted_data(self):
        """Read and deliver decrypted data to the application."""
        while True:
            try:
                data = self._ssl_object.read(65536)
                if data:
                    self._app_protocol.data_received(data)
                else:
                    break
            except ssl.SSLWantReadError:
                break
            except Exception as exc:
                self._force_close(exc)
                return

    def _flush_outgoing(self):
        """Send any pending outgoing SSL data."""
        data = self._outgoing.read()
        if data:
            self._base_transport.write(data)

    def write(self, data):
        """Write encrypted data."""
        if self._closing:
            return
        
        try:
            self._ssl_object.write(data)
            self._flush_outgoing()
        except Exception as exc:
            self._force_close(exc)

    def close(self):
        """Close the transport."""
        if self._closing:
            return
        self._closing = True
        
        try:
            self._ssl_object.unwrap()
            self._flush_outgoing()
        except Exception:
            pass
        
        self._base_transport.close()
        self._closed = True

    def _force_close(self, exc):
        """Force close the transport."""
        if self._closed:
            return
        self._closed = True
        self._closing = True
        self._base_transport.close()
        self._loop.call_soon(self._app_protocol.connection_lost, exc)

    def is_closing(self):
        return self._closing or self._closed

    def get_extra_info(self, name, default=None):
        if name == 'ssl_object':
            return self._ssl_object
        if name == 'peercert':
            return self._ssl_object.getpeercert()
        if name == 'cipher':
            return self._ssl_object.cipher()
        return self._base_transport.get_extra_info(name, default)

    def get_write_buffer_size(self):
        return self._base_transport.get_write_buffer_size()

    def get_write_buffer_limits(self):
        return self._base_transport.get_write_buffer_limits()

    def set_write_buffer_limits(self, high=None, low=None):
        self._base_transport.set_write_buffer_limits(high, low)

    def abort(self):
        self._force_close(None)


class _SSLProtocol(asyncio.Protocol):
    """Internal protocol that handles data from base transport for SSL."""
    
    def __init__(self, ssl_transport):
        self._ssl_transport = ssl_transport

    def connection_made(self, transport):
        pass

    def data_received(self, data):
        self._ssl_transport._data_received(data)

    def connection_lost(self, exc):
        if self._ssl_transport._app_protocol:
            self._ssl_transport._app_protocol.connection_lost(exc)

    def eof_received(self):
        return False
