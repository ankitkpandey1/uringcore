import pytest
import uringcore
import socket
import os
import asyncio

def test_submit_send_zc_basic(event_loop):
    """Test basic Zero-Copy Send functionality."""
    if not isinstance(event_loop, uringcore.UringEventLoop):
        pytest.skip("Test requires uringcore loop")
        
    async def main():
        try:
            # Check kernel version (approximate)
            # SEND_ZC requires 6.0+
            uname = os.uname()
            release = uname.release
            major = int(release.split('.')[0])
            if major < 6:
                print(f"Kernel {release} too old for SEND_ZC")
                return # Skip logic but pass test
            
            # Create a TCP pair manually (socketpair is AF_UNIX usually)
            server_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_listener.bind(('127.0.0.1', 0))
            server_listener.listen(1)
            port = server_listener.getsockname()[1]
            
            wsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            wsock.setblocking(False)
            
            # Connect
            try:
                wsock.connect(('127.0.0.1', port))
            except BlockingIOError:
                pass
                
            rsock, _ = server_listener.accept()
            server_listener.close()
            
            rsock.setblocking(False)
            
            server_fd = rsock.fileno()
            client_fd = wsock.fileno()
            
            # Register FDs explicitly
            event_loop._core.register_fd(server_fd, "tcp")
            event_loop._core.register_fd(client_fd, "tcp")
            
            # Send using Zero-Copy
            data = b"Hello Zero-Copy World"
            fut = event_loop.create_future()
            
            try:
                # Attempt submission
                event_loop._core.submit_send_zc(client_fd, data, fut)
                
                result = await fut
                assert result == len(data)
                
                # Verify receipt
                received = rsock.recv(1024)
                assert received == data
                
            except (RuntimeError, OSError) as e:
                # If kernel supports it but detected inability at runtime
                if "EINVAL" in str(e) or "EOPNOTSUPP" in str(e) or "Operation not supported" in str(e):
                    pytest.skip(f"SEND_ZC not supported: {e}")
                raise
                
            finally:
                rsock.close()
                wsock.close()
        except Exception as e:
            raise e

    event_loop.run_until_complete(main())
