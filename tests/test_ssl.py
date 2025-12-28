"""Tests for SSL/TLS transport.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import os
import ssl
import tempfile
import pytest
import uringcore


# Generate self-signed certificate for testing
def generate_test_cert():
    """Generate a self-signed certificate for testing."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import datetime
        
        # Generate key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        
        # Generate certificate
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ])
        
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        
        # Write to temp files
        cert_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pem')
        key_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pem')
        
        cert_file.write(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
        
        cert_file.close()
        key_file.close()
        
        return cert_file.name, key_file.name
    except ImportError:
        return None, None


@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


@pytest.fixture(scope="module")
def ssl_certs():
    """Generate SSL certificates for testing."""
    cert_file, key_file = generate_test_cert()
    if cert_file is None:
        pytest.skip("cryptography library not available")
    yield cert_file, key_file
    # Cleanup
    try:
        os.unlink(cert_file)
        os.unlink(key_file)
    except:
        pass


class TestSSLTransport:
    """Test SSL/TLS transport functionality."""

    @pytest.mark.asyncio
    async def test_ssl_context_creation(self, event_loop):
        """Test SSL context can be created."""
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_ssl_server_client_handshake(self, event_loop, ssl_certs):
        """Test SSL handshake between server and client."""
        cert_file, key_file = ssl_certs
        
        # Server SSL context
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_file, key_file)
        
        # Client SSL context
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        
        received_data = []
        
        async def handle_client(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=5.0)
                received_data.append(data)
                writer.write(b"PONG")
                await writer.drain()
            finally:
                writer.close()
        
        # Start SSL server
        server = await asyncio.start_server(
            handle_client,
            '127.0.0.1',
            0,
            ssl=server_ctx,
        )
        port = server.sockets[0].getsockname()[1]
        
        try:
            # Connect with SSL client
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', port, ssl=client_ctx),
                timeout=5.0
            )
            
            writer.write(b"PING")
            await writer.drain()
            
            response = await asyncio.wait_for(reader.read(1024), timeout=5.0)
            
            writer.close()
            await writer.wait_closed()
            
            assert response == b"PONG"
            assert received_data == [b"PING"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_ssl_data_integrity(self, event_loop, ssl_certs):
        """Test data integrity over SSL connection."""
        cert_file, key_file = ssl_certs
        
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_file, key_file)
        
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        
        # Large test data
        test_data = b"X" * 10000
        received_chunks = []
        
        async def echo_handler(reader, writer):
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    received_chunks.append(data)
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        
        server = await asyncio.start_server(
            echo_handler, '127.0.0.1', 0, ssl=server_ctx
        )
        port = server.sockets[0].getsockname()[1]
        
        try:
            reader, writer = await asyncio.open_connection(
                '127.0.0.1', port, ssl=client_ctx
            )
            
            writer.write(test_data)
            await writer.drain()
            writer.write_eof()
            
            response = await reader.read()
            writer.close()
            
            assert response == test_data
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
