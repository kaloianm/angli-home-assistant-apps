"""
Runtime state container for ExtractorFanControl pair handling.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Optional

from extractor_fan_control.config import PairConfig
from extractor_fan_control.logic import ExtractorFanPairLogic

# If a single pair sends more than this many fan switch commands (including keepalive retriggers)
# within the sliding window, the pair is permanently disabled until AppDaemon is restarted.
FAN_CMD_RATE_LIMIT = 5
FAN_CMD_RATE_WINDOW_SECONDS = 30


@dataclass
class PairRuntime:
    """
    Mutable runtime state for one pair.
    """

    config: PairConfig

    logic: ExtractorFanPairLogic
    light_listener_handle: Optional[Any] = None
    activation_timer_handle: Optional[Any] = None
    deadline_timer_handle: Optional[Any] = None
    keepalive_timer_handle: Optional[Any] = None
    daily_schedule_handle: Optional[Any] = None

    disabled: bool = False

    # Record of fan switch command timestamps for rate limiting.
    _fan_command_timestamps: Deque[datetime] = field(default_factory=deque)

    def record_fan_command(self, now: datetime) -> bool:
        """
        Record a fan switch command and return True if the rate limit is exceeded.

        Maintains a sliding window of timestamps. When more than ``FAN_CMD_RATE_LIMIT`` commands
        land within ``FAN_CMD_RATE_WINDOW_SECONDS``, marks this pair as disabled and returns True
        so the caller can alert and stop processing.
        """
        window_start = now - timedelta(seconds=FAN_CMD_RATE_WINDOW_SECONDS)
        while self._fan_command_timestamps and self._fan_command_timestamps[0] <= window_start:
            self._fan_command_timestamps.popleft()
        self._fan_command_timestamps.append(now)
        if len(self._fan_command_timestamps) > FAN_CMD_RATE_LIMIT:
            self.disabled = True
            return True
        return False
