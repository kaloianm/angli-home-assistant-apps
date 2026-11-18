"""
The declarative side effects the core hands to the AppDaemon adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionKind(StrEnum):
    MOVE_TO = "move_to"
    OPEN_FULL = "open_full"
    CLOSE_FULL = "close_full"
    STOP = "stop"
    # Position, tilt mode and motion are published together so a slat angle is never read as a
    # height.
    PUBLISH_STATE = "publish_state"
    ARM_SETTLE_TIMER = "arm_settle_timer"
    CANCEL_SETTLE_TIMER = "cancel_settle_timer"
    NOTIFY = "notify"


class NotifyKind(StrEnum):
    STALL = "stall"
    INVARIANT = "invariant"


class Motion(StrEnum):
    """
    Virtual-scale motion: ``OPENING`` means toward more light, in either mode.
    """

    OPENING = "opening"
    CLOSING = "closing"
    IDLE = "idle"


@dataclass(frozen=True)
class Action:
    """
    Declarative side effect for the adapter to perform.
    """

    kind: ActionKind
    position: int | None = None
    in_tilt: bool | None = None
    motion: Motion | None = None
    seconds: float | None = None
    notify_kind: NotifyKind | None = None
    message: str | None = None
