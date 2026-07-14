"""
Runtime state container for GradhermeticCoverControl.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Optional

from gradhermetic_cover_control.config import GradhermeticConfig
from gradhermetic_cover_control.logic import GradhermeticCoverLogic

# If the app sends more than this many real-cover commands within the sliding window, the blind is
# permanently disabled until AppDaemon is restarted. A runaway plan (for example a synchronous replan
# loop) fires far faster than this, so the limit still trips within a second; the limit is sized to
# leave headroom for legitimate rapid interaction such as a burst of wall-button slat steps.
COMMAND_RATE_LIMIT = 30
COMMAND_RATE_WINDOW_SECONDS = 60


@dataclass
class CoverRuntime:
    """
    Mutable runtime state for one Gradhermetic cover.
    """

    config: GradhermeticConfig
    logic: GradhermeticCoverLogic

    state_listener_handle: Optional[Any] = None
    command_listener_handle: Optional[Any] = None
    knx_listener_handle: Optional[Any] = None
    step_up_listener_handle: Optional[Any] = None
    step_down_listener_handle: Optional[Any] = None
    tilt_listener_handle: Optional[Any] = None
    settle_timer_handle: Optional[Any] = None

    disabled: bool = False

    # Record of real-cover command timestamps for rate limiting.
    _command_timestamps: Deque[datetime] = field(default_factory=deque)

    def record_command(self, now: datetime) -> bool:
        """
        Record a real-cover command and return True if the rate limit is exceeded.

        Maintains a sliding window of timestamps. When more than ``COMMAND_RATE_LIMIT`` commands
        land within ``COMMAND_RATE_WINDOW_SECONDS``, marks this blind disabled and returns True so
        the caller can alert and stop processing.
        """
        window_start = now - timedelta(seconds=COMMAND_RATE_WINDOW_SECONDS)
        while self._command_timestamps and self._command_timestamps[0] <= window_start:
            self._command_timestamps.popleft()
        self._command_timestamps.append(now)
        if len(self._command_timestamps) > COMMAND_RATE_LIMIT:
            self.disabled = True
            return True
        return False
