#!/bin/bash
# Run tests with increased locked memory limit for io_uring buffer registration.
# Copyright (c) 2025 Ankit Kumar Pandey
# SPDX-License-Identifier: Apache-2.0

set -e

# Try to increase memlock limit using sudo prlimit
if sudo -n prlimit --pid $$ --memlock=unlimited:unlimited 2>/dev/null; then
    echo "✓ Raised memlock limit to unlimited"
elif sudo prlimit --pid $$ --memlock=unlimited:unlimited 2>/dev/null; then
    echo "✓ Raised memlock limit to unlimited (with password)"
else
    echo "⚠ Could not raise memlock limit - tests may fail"
    echo "  Run: sudo tee /etc/security/limits.d/99-memlock.conf <<< '* soft memlock unlimited'"
    echo "  Then log out and back in"
fi

# Activate virtual environment
source .venv/bin/activate

# Run pytest with provided arguments
exec pytest "$@"
