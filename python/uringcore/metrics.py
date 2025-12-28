"""Observability module for uringcore.

Provides runtime metrics and tracing integration for production monitoring.

Copyright (c) 2025 Ankit Kumar Pandey
SPDX-License-Identifier: Apache-2.0
"""

from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any
import time


@dataclass
class Metrics:
    """Runtime metrics snapshot.
    
    Attributes:
        buffers_total: Total number of buffers in pool
        buffers_free: Available buffers
        buffers_quarantined: Buffers in quarantine period
        buffers_in_use: Buffers currently in use
        fd_count: Number of registered file descriptors
        fd_inflight: Operations currently in flight
        fd_queued: Operations waiting in queue
        fd_paused: Number of paused file descriptors
        timestamp: When metrics were captured
    """
    buffers_total: int
    buffers_free: int
    buffers_quarantined: int
    buffers_in_use: int
    fd_count: int
    fd_inflight: int
    fd_queued: int
    fd_paused: int
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "buffers": {
                "total": self.buffers_total,
                "free": self.buffers_free,
                "quarantined": self.buffers_quarantined,
                "in_use": self.buffers_in_use,
            },
            "file_descriptors": {
                "count": self.fd_count,
                "inflight": self.fd_inflight,
                "queued": self.fd_queued,
                "paused": self.fd_paused,
            },
            "timestamp": self.timestamp,
        }

    def to_prometheus(self) -> str:
        """Format as Prometheus exposition format."""
        lines = [
            "# HELP uringcore_buffers_total Total buffers in pool",
            "# TYPE uringcore_buffers_total gauge",
            f"uringcore_buffers_total {self.buffers_total}",
            "",
            "# HELP uringcore_buffers_free Available buffers",
            "# TYPE uringcore_buffers_free gauge",
            f"uringcore_buffers_free {self.buffers_free}",
            "",
            "# HELP uringcore_buffers_quarantined Buffers in quarantine",
            "# TYPE uringcore_buffers_quarantined gauge",
            f"uringcore_buffers_quarantined {self.buffers_quarantined}",
            "",
            "# HELP uringcore_buffers_in_use Buffers currently in use",
            "# TYPE uringcore_buffers_in_use gauge",
            f"uringcore_buffers_in_use {self.buffers_in_use}",
            "",
            "# HELP uringcore_fd_count Registered file descriptors",
            "# TYPE uringcore_fd_count gauge",
            f"uringcore_fd_count {self.fd_count}",
            "",
            "# HELP uringcore_fd_inflight Operations in flight",
            "# TYPE uringcore_fd_inflight gauge",
            f"uringcore_fd_inflight {self.fd_inflight}",
            "",
            "# HELP uringcore_fd_queued Queued operations",
            "# TYPE uringcore_fd_queued gauge",
            f"uringcore_fd_queued {self.fd_queued}",
            "",
            "# HELP uringcore_fd_paused Paused file descriptors",
            "# TYPE uringcore_fd_paused gauge",
            f"uringcore_fd_paused {self.fd_paused}",
        ]
        return "\n".join(lines)


class MetricsCollector:
    """Collects metrics from UringCore instance."""

    def __init__(self, core):
        """Initialize collector with UringCore instance.
        
        Args:
            core: UringCore instance to collect metrics from
        """
        self._core = core
        self._callbacks: list[Callable[[Metrics], None]] = []

    def collect(self) -> Metrics:
        """Collect current metrics snapshot."""
        buffer_stats = self._core.buffer_stats()
        fd_stats = self._core.fd_stats()

        metrics = Metrics(
            buffers_total=buffer_stats[0],
            buffers_free=buffer_stats[1],
            buffers_quarantined=buffer_stats[2],
            buffers_in_use=buffer_stats[3],
            fd_count=fd_stats[0],
            fd_inflight=fd_stats[1],
            fd_queued=fd_stats[2],
            fd_paused=fd_stats[3],
            timestamp=time.time(),
        )

        # Notify callbacks
        for callback in self._callbacks:
            try:
                callback(metrics)
            except Exception:
                pass  # Don't let callback errors affect collection

        return metrics

    def add_callback(self, callback: Callable[[Metrics], None]) -> None:
        """Add callback to be invoked on each metrics collection.
        
        Args:
            callback: Function receiving Metrics instance
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[Metrics], None]) -> None:
        """Remove a previously added callback."""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass


def get_metrics(loop) -> Optional[Metrics]:
    """Get current metrics from event loop.
    
    Args:
        loop: UringEventLoop instance
        
    Returns:
        Metrics snapshot or None if not a uringcore loop
    """
    core = getattr(loop, '_core', None)
    if core is None:
        return None
    
    collector = MetricsCollector(core)
    return collector.collect()
