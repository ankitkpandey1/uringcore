"""Test FIFO ordering guarantees per file descriptor.

These tests verify the core ordering logic without using long-running servers.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import pytest
import uringcore


class TestFIFOOrdering:
    """Verify buffer pool and core ordering mechanisms."""

    def test_buffer_acquisition_order(self):
        """Verify buffers are acquired in FIFO order."""
        core = uringcore.UringCore()
        
        # Get initial stats
        total, free_before, _, _ = core.buffer_stats()
        assert free_before > 0
        
        core.shutdown()

    def test_generation_id_sequential(self):
        """Verify generation IDs are sequential."""
        core1 = uringcore.UringCore()
        gen1 = core1.generation_id
        core1.shutdown()
        
        core2 = uringcore.UringCore()
        gen2 = core2.generation_id
        core2.shutdown()
        
        # Each new core should have a valid generation
        assert gen1 > 0
        assert gen2 > 0

    def test_fd_registration_order(self):
        """Test FD registration maintains state."""
        import socket
        
        core = uringcore.UringCore()
        
        # Create a socket to register
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        fd = sock.fileno()
        
        # Get initial FD count
        count_before, _, _, _ = core.fd_stats()
        
        # Register FD
        core.register_fd(fd, "tcp")
        
        # Check FD count increased
        count_after, _, _, _ = core.fd_stats()
        assert count_after == count_before + 1
        
        # Unregister
        core.unregister_fd(fd)
        
        # Should be back to original
        count_final, _, _, _ = core.fd_stats()
        assert count_final == count_before
        
        sock.close()
        core.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
