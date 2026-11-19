"""
Sliding-window rate limiter for outgoing commands.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class RateLimiter:
    """
    Counts commands over a sliding time window.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        """
        Allow at most ``limit`` commands in any ``window_seconds`` window.
        """
        self._limit = limit
        self._window = timedelta(seconds=window_seconds)
        self._timestamps: deque[datetime] = deque()

    def record(self, now: datetime) -> bool:
        """
        Record a command sent at ``now``, returning True if it exceeds the limit.
        """
        window_start = now - self._window
        while self._timestamps and self._timestamps[0] <= window_start:
            self._timestamps.popleft()
        self._timestamps.append(now)
        return len(self._timestamps) > self._limit
