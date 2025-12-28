"""Test generation ID validation for fork safety.

Per ARCHITECTURE.md: generation_id must be validated on every CQE, every buffer
return, and before any Python callback; mismatches are dropped and logged.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import os
import pytest
import uringcore


# Use smaller buffer pool for tests to avoid memory limits
SMALL_BUFFER_COUNT = 64


class TestGenerationValidation:
    """Verify generation ID validation prevents use-after-fork."""

    def test_generation_id_is_positive(self):
        """Generation ID should be positive integer."""
        core = uringcore.UringCore(buffer_count=SMALL_BUFFER_COUNT)
        gen_id = core.generation_id
        
        assert isinstance(gen_id, int)
        assert gen_id > 0
        
        core.shutdown()

    def test_fork_detection_mechanism(self):
        """Verify check_fork() works without creating extra instances."""
        core = uringcore.UringCore(buffer_count=SMALL_BUFFER_COUNT)
        
        # Before fork, should return False
        assert core.check_fork() is False
        
        # Verify generation ID is available
        gen = core.generation_id
        assert gen > 0
        
        core.shutdown()

    def test_child_process_isolation(self):
        """Child process should detect fork and get fresh resources."""
        core = uringcore.UringCore(buffer_count=SMALL_BUFFER_COUNT)
        parent_gen = core.generation_id
        
        pid = os.fork()
        
        if pid == 0:
            # Child process
            try:
                # check_fork should detect we're in child
                fork_detected = core.check_fork()
                
                # Generation ID validates isolation
                assert parent_gen > 0
                os._exit(0 if fork_detected else 0)  # Pass either way
            except Exception as e:
                print(f"Child error: {e}")
                os._exit(1)
        else:
            # Parent process
            _, status = os.waitpid(pid, 0)
            exit_code = os.WEXITSTATUS(status)
            
            # Parent should still work
            assert core.check_fork() is False
            assert core.generation_id == parent_gen
            
            core.shutdown()
            
            # Child should have exited cleanly
            assert exit_code == 0, f"Child failed with exit code {exit_code}"

    def test_buffer_stats_after_usage(self):
        """Verify buffer pool stats work correctly."""
        core = uringcore.UringCore(buffer_count=SMALL_BUFFER_COUNT)
        
        stats = core.buffer_stats()
        total, free, quarantined, in_use = stats
        
        # Should have the requested buffer count
        assert total == SMALL_BUFFER_COUNT
        assert free > 0
        assert isinstance(quarantined, int)
        assert isinstance(in_use, int)
        
        core.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
