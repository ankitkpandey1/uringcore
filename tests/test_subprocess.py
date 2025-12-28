"""Tests for subprocess transport.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import os
import sys
import pytest
import uringcore


@pytest.fixture(scope="module")
def event_loop():
    """Create uringcore event loop."""
    policy = uringcore.EventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


class TestSubprocess:
    """Test subprocess functionality."""

    @pytest.mark.asyncio
    async def test_subprocess_exec_simple(self, event_loop):
        """Test simple subprocess execution."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('hello')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout, stderr = await proc.communicate()
        
        assert proc.returncode == 0
        assert b"hello" in stdout

    @pytest.mark.asyncio
    async def test_subprocess_shell(self, event_loop):
        """Test subprocess shell execution."""
        proc = await asyncio.create_subprocess_shell(
            "echo test",
            stdout=asyncio.subprocess.PIPE,
        )
        
        stdout, _ = await proc.communicate()
        
        assert proc.returncode == 0
        assert b"test" in stdout

    @pytest.mark.asyncio
    async def test_subprocess_stdin_stdout(self, event_loop):
        """Test subprocess with stdin/stdout."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; print(sys.stdin.read().upper())",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        
        stdout, _ = await proc.communicate(input=b"hello world")
        
        assert proc.returncode == 0
        assert b"HELLO WORLD" in stdout

    @pytest.mark.asyncio
    async def test_subprocess_exit_code(self, event_loop):
        """Test subprocess exit codes."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.exit(42)",
        )
        
        await proc.wait()
        
        assert proc.returncode == 42

    @pytest.mark.asyncio
    async def test_subprocess_timeout(self, event_loop):
        """Test subprocess with timeout."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(10)",
            stdout=asyncio.subprocess.PIPE,
        )
        
        try:
            await asyncio.wait_for(proc.communicate(), timeout=0.5)
            assert False, "Should have timed out"
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_subprocess_env(self, event_loop):
        """Test subprocess with custom environment."""
        env = os.environ.copy()
        env["TEST_VAR"] = "test_value"
        
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", 
            "import os; print(os.environ.get('TEST_VAR', ''))",
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )
        
        stdout, _ = await proc.communicate()
        
        assert b"test_value" in stdout

    @pytest.mark.asyncio
    async def test_multiple_subprocesses(self, event_loop):
        """Test running multiple subprocesses concurrently."""
        async def run_echo(n):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", f"print({n})",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return int(stdout.strip())
        
        results = await asyncio.gather(*[run_echo(i) for i in range(5)])
        
        assert set(results) == {0, 1, 2, 3, 4}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
