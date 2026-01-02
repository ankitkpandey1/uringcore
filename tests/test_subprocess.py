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
    """Create uringcore event loop using modern factory pattern."""
    loop = uringcore.new_event_loop(buffer_count=16, buffer_size=4096)
    yield loop
    if not loop.is_closed():
        loop.close()


@pytest.mark.usefixtures("event_loop")
class TestSubprocess:
    """Test subprocess functionality."""

    def test_subprocess_exec_simple(self, loop):
        """Test simple subprocess execution."""
        async def check():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "print('hello')",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await proc.communicate()
            
            assert proc.returncode == 0
            assert b"hello" in stdout

        loop.run_until_complete(check())

    def test_subprocess_shell(self, loop):
        """Test subprocess shell execution."""
        async def check():
            proc = await asyncio.create_subprocess_shell(
                "echo test",
                stdout=asyncio.subprocess.PIPE,
            )
            
            stdout, _ = await proc.communicate()
            
            assert proc.returncode == 0
            assert b"test" in stdout
        
        loop.run_until_complete(check())

    def test_subprocess_stdin_stdout(self, loop):
        """Test subprocess with stdin/stdout."""
        async def check():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import sys; print(sys.stdin.read().upper())",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            
            stdout, _ = await proc.communicate(input=b"hello world")
            
            assert proc.returncode == 0
            assert b"HELLO WORLD" in stdout
        
        loop.run_until_complete(check())

    def test_subprocess_exit_code(self, loop):
        """Test subprocess exit codes."""
        async def check():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import sys; sys.exit(42)",
            )
            
            await proc.wait()
            
            assert proc.returncode == 42
        
        loop.run_until_complete(check())

    def test_subprocess_timeout(self, loop):
        """Test subprocess with timeout."""
        async def check():
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

        loop.run_until_complete(check())

    def test_subprocess_env(self, loop):
        """Test subprocess with custom environment."""
        async def check():
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

        loop.run_until_complete(check())

    def test_multiple_subprocesses(self, loop):
        """Test running multiple subprocesses concurrently."""
        async def run_echo(n):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", f"print({n})",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return int(stdout.strip())
        
        async def check():
            results = await asyncio.gather(*[run_echo(i) for i in range(5)])
            assert set(results) == {0, 1, 2, 3, 4}

        loop.run_until_complete(check())

@pytest.fixture
def loop(event_loop):
    return event_loop

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
