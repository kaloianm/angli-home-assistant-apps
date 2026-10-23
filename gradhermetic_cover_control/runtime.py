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

# More real-cover commands than this within the window disables the blind until AppDaemon restarts.
# It catches a runaway replan loop (which hits it in seconds) while staying well above a burst of
# human taps (~30 a minute).
COMMAND_RATE_LIMIT = 60
COMMAND_RATE_WINDOW_SECONDS = 60


@dataclass
class CoverRuntime:
    """
    Mutable runtime state for one Gradhermetic cover.
    """

    config: GradhermeticConfig
    logic: GradhermeticCoverLogic

    settle_timer_handle: Optional[Any] = None

    disabled: bool = False

    _command_timestamps: Deque[datetime] = field(default_factory=deque)

    def record_command(self, now: datetime) -> bool:
        """
        Record a real-cover command; if the rate limit is exceeded, mark disabled and return True.
        """
        window_start = now - timedelta(seconds=COMMAND_RATE_WINDOW_SECONDS)
        while self._command_timestamps and self._command_timestamps[0] <= window_start:
            self._command_timestamps.popleft()
        self._command_timestamps.append(now)
        if len(self._command_timestamps) > COMMAND_RATE_LIMIT:
            self.disabled = True
            return True
        return False
