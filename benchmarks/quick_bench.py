
import sys
import os
from unittest.mock import patch

# Add current directory to sys.path
sys.path.append(os.getcwd())

# Set low limits for restricted environment
os.environ.setdefault("URINGCORE_BUFFER_COUNT", "128")
os.environ.setdefault("URINGCORE_BUFFER_SIZE", "4096")

import benchmarks.benchmark_suite as suite

# Reduce iterations for speed
suite.BENCHMARKS = [
    (suite.bench_sleep_zero, "sleep(0)", 100),
    (suite.bench_create_task, "create_task", 100),
    (suite.bench_gather_10, "gather(10)", 50),
    (suite.bench_gather_100, "gather(100)", 20),
    (suite.bench_queue_put_get, "queue_put", 100),
    (suite.bench_event_set_wait, "event_wait", 100),
    (suite.bench_future_result, "future_res", 100),
    (suite.bench_sock_pair, "sock_pair", 100) if hasattr(suite, 'bench_sock_pair') else (suite.bench_socketpair_overhead, "sock_pair", 100),
]

if __name__ == "__main__":
    suite.main()
