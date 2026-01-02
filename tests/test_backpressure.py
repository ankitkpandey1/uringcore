"""Test backpressure and credit-based flow control.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import pytest
import uringcore


class TestBackpressure:
    """Verify credit-based backpressure mechanisms."""

    def test_buffer_stats_available(self):
        """Verify buffer statistics are accessible."""
        core = uringcore.UringCore(buffer_count=16, buffer_size=4096)
        stats = core.buffer_stats()
        
        assert len(stats) == 4
        assert all(isinstance(x, int) for x in stats)
        
        total, free, quarantined, in_use = stats
        assert total > 0
        assert free >= 0
        
        core.shutdown()

    def test_fd_stats_available(self):
        """Verify FD statistics are accessible."""
        core = uringcore.UringCore(buffer_count=16, buffer_size=4096)
        stats = core.fd_stats()
        
        assert len(stats) == 4
        assert all(isinstance(x, int) for x in stats)
        
        core.shutdown()

    def test_metrics_integration(self):
        """Test metrics module integration."""
        core = uringcore.UringCore(buffer_count=16, buffer_size=4096)
        
        # Get buffer stats directly
        stats = core.buffer_stats()
        total, free, quarantined, in_use = stats
        
        assert total > 0
        assert total == free + quarantined + in_use
        
        core.shutdown()

    def test_generation_id_positive(self):
        """Test generation ID is available and positive."""
        core = uringcore.UringCore(buffer_count=16, buffer_size=4096)
        gen_id = core.generation_id
        
        assert isinstance(gen_id, int)
        assert gen_id > 0
        
        core.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
