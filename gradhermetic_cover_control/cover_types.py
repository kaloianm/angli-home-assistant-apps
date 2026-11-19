"""
Real-cover vocabulary shared by the planner, the executor, the actuator and the adapter.
"""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class CommandKind(StrEnum):
    """
    Which real-cover service carries a move. ``OPEN``/``CLOSE`` send the commands rather than a
    position so the actuator drives against its own limit switch.
    """

    POSITION = "position"
    OPEN = "open"
    CLOSE = "close"
