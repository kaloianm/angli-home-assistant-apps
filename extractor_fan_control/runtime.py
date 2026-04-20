"""
Runtime state container for ExtractorFanControl pair handling.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Optional, Tuple, TYPE_CHECKING

from extractor_fan_control.config import PairConfig

if TYPE_CHECKING:
    from extractor_fan_control.logic import ExtractorFanPairLogic

# If a single pair sends more than this many fan switch commands (including keepalive retriggers)
# within the sliding window, the pair is permanently disabled until AppDaemon is restarted.
FAN_CMD_RATE_LIMIT = 5
FAN_CMD_RATE_WINDOW_SECONDS = 30

EXPECTED_FAN_STATE_TTL_SECONDS = 2


@dataclass
class PairRuntime:
    """
    Mutable runtime state for one pair.
    """

    config: PairConfig

    logic: Optional["ExtractorFanPairLogic"] = None
    light_listener_handle: Optional[Any] = None
    fan_listener_handle: Optional[Any] = None
    activation_timer_handle: Optional[Any] = None
    deadline_timer_handle: Optional[Any] = None
    keepalive_timer_handle: Optional[Any] = None
    daily_schedule_handle: Optional[Any] = None
    # We command and also listen to the same switch. Home Assistant/AppDaemon
    # does not provide a reliable per-call custom tag in listen_state callbacks,
    # so we track short-lived expected states to avoid treating our own KNX
    # feedback updates as manual user toggles.
    expected_fan_states: Deque[Tuple[str, datetime]] = field(default_factory=deque)

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

    def record_expected_fan_state(self, state: str, now: datetime) -> None:
        """
        Remember an automation-driven fan state update for short-term filtering.
        """
        self.prune_expected_fan_states(now)
        expires_at = now + timedelta(seconds=EXPECTED_FAN_STATE_TTL_SECONDS)
        self.expected_fan_states.append((state, expires_at))

    def consume_expected_fan_state(self, observed_state: str, now: datetime) -> bool:
        """
        Consume and match one expected state callback, if present.
        """
        self.prune_expected_fan_states(now)
        for state, expires_at in list(self.expected_fan_states):
            if state == observed_state and now <= expires_at:
                self.expected_fan_states.remove((state, expires_at))
                return True
        return False

    def prune_expected_fan_states(self, now: datetime) -> None:
        """
        Drop stale expected state entries.
        """
        while self.expected_fan_states and self.expected_fan_states[0][1] < now:
            self.expected_fan_states.popleft()
