"""
Pure business logic for the Gradhermetic blind controller.

This module has ZERO dependencies on AppDaemon, Home Assistant, or MQTT.
Every public method returns a list of Action dataclasses describing the side
effects the caller should perform.  This makes the entire state machine
unit-testable without any framework mocking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

# ── Actions (side effects) ──────────────────────────────────────────────
#
# The controller never performs I/O itself.  Instead it returns one or more
# of the following action objects.  The hosting environment (AppDaemon, a
# test harness, etc.) is responsible for executing them.


@dataclass(frozen=True)
class SetCoverPosition:
    """Move the real cover to *position* (0-100 %)."""
    position: int


@dataclass(frozen=True)
class OpenCover:
    """Fully open the real cover."""


@dataclass(frozen=True)
class CloseCover:
    """Fully close the real cover."""


@dataclass(frozen=True)
class StopCover:
    """Stop the real cover motor."""


@dataclass(frozen=True)
class ScheduleTimer:
    """Ask the host to call *on_timer(timer_id)* after *seconds*."""
    timer_id: str
    seconds: float


@dataclass(frozen=True)
class CancelTimer:
    """Cancel a previously scheduled timer."""
    timer_id: str


@dataclass(frozen=True)
class PublishState:
    """Publish the virtual cover's current state."""
    cover_state: str  # "open" | "closed" | "opening" | "closing"
    position: int  # 0-100
    tilt: int  # 0-100  (HA convention: 0=closed, 100=open)


@dataclass(frozen=True)
class Log:
    """Emit a log message."""
    message: str


from typing import Union

Action = Union[
    SetCoverPosition,
    OpenCover,
    CloseCover,
    StopCover,
    ScheduleTimer,
    CancelTimer,
    PublishState,
    Log,
]

ENGAGE_TIMER = "engage"

# ── Engagement phases ───────────────────────────────────────────────────


class EngagePhase(Enum):
    PHASE1 = auto()  # lowering to tilt_lower − ε
    PHASE2 = auto()  # raising  to tilt_upper + ε


# ── Controller ──────────────────────────────────────────────────────────


class BlindController:
    """
    Stateful controller for one Gradhermetic blind.

    Tracks tilt engagement, converts commands into sequences of real-cover
    movements, and maps positions to/from HA tilt values.
    """

    def __init__(
        self,
        tilt_lower_pct: float = 3.0,
        tilt_upper_pct: float = 10.0,
        epsilon_pct: float = 2.0,
        full_travel_time_secs: float = 60.0,
        step_pct: float = 5.0,
        tilt_step_pct: float = 10.0,
    ):
        self.tilt_lower = tilt_lower_pct
        self.tilt_upper = tilt_upper_pct
        self.epsilon = epsilon_pct
        self.full_travel_time = full_travel_time_secs
        self.step_pct = step_pct
        self.tilt_step_pct = tilt_step_pct

        self.tilt_engaged: bool = False
        self.tilt_value: float = 100.0  # 0 = closed, 100 = open
        self.engaging: bool = False
        self.engage_phase: Optional[EngagePhase] = None
        self.pending_tilt: Optional[float] = None
        self.real_position: float = 0.0
        self.real_state: str = "closed"

    # ── Position / tilt conversion ──────────────────────────────────────
    #  HA convention: tilt 0 = closed (vertical), tilt 100 = open (horizontal)
    #  Physical:  position @ tilt_upper → closed,  position @ tilt_lower → open.

    def tilt_to_position(self, tilt: float) -> float:
        r = self.tilt_upper - self.tilt_lower
        return max(
            self.tilt_lower,
            min(self.tilt_upper, self.tilt_upper - (tilt / 100.0) * r),
        )

    def position_to_tilt(self, position: float) -> float:
        r = self.tilt_upper - self.tilt_lower
        if r <= 0:
            return 0.0
        return max(0.0, min(100.0, (self.tilt_upper - position) / r * 100.0))

    # ── State snapshot ──────────────────────────────────────────────────

    def make_publish(self) -> PublishState:
        if self.real_state in ("opening", "closing"):
            state = self.real_state
        elif self.real_position >= 99:
            state = "open"
        elif self.real_position <= 1:
            state = "closed"
        else:
            state = "open"
        return PublishState(
            cover_state=state,
            position=int(self.real_position),
            tilt=int(self.tilt_value),
        )

    # ── Command handlers (return actions) ───────────────────────────────

    def handle_open(self) -> list[Action]:
        actions = self._exit_tilt()
        actions.append(OpenCover())
        return actions

    def handle_close(self) -> list[Action]:
        actions = self._exit_tilt()
        actions.append(CloseCover())
        return actions

    def handle_stop(self) -> list[Action]:
        actions = self._cancel_engagement()
        actions.append(StopCover())
        return actions

    def handle_set_position(self, pos: float) -> list[Action]:
        actions = self._exit_tilt()
        actions.append(SetCoverPosition(position=int(max(0, min(100, pos)))))
        return actions

    def handle_set_tilt(self, tilt: float) -> list[Action]:
        tilt = max(0.0, min(100.0, tilt))
        actions: list[Action] = []

        if self.tilt_engaged:
            target_pos = self.tilt_to_position(tilt)
            actions.append(SetCoverPosition(position=int(target_pos)))
            self.tilt_value = tilt
            actions.append(self.make_publish())
        else:
            self.pending_tilt = tilt
            actions.extend(self._start_engagement())

        return actions

    def handle_enter_slat(self) -> list[Action]:
        if not self.tilt_engaged and not self.engaging:
            self.pending_tilt = 50.0
            return self._start_engagement()
        return []

    def handle_slat_step_up(self) -> list[Action]:
        """Step slats toward closed (tilt value decreases)."""
        if self.tilt_engaged:
            return self.handle_set_tilt(max(0.0, self.tilt_value - self.tilt_step_pct))
        self.pending_tilt = max(0.0, 50.0 - self.tilt_step_pct)
        return self._start_engagement()

    def handle_slat_step_down(self) -> list[Action]:
        """Step slats toward open (tilt value increases)."""
        if self.tilt_engaged:
            return self.handle_set_tilt(min(100.0, self.tilt_value + self.tilt_step_pct))
        self.pending_tilt = min(100.0, 50.0 + self.tilt_step_pct)
        return self._start_engagement()

    # ── Event callbacks (return actions) ────────────────────────────────

    def on_real_cover_changed(self, state: str, position: float) -> list[Action]:
        """Called when the real cover's HA state or position changes."""
        self.real_state = state
        self.real_position = position

        if self.tilt_engaged and not self.engaging:
            self.tilt_value = self.position_to_tilt(position)

        return [self.make_publish()]

    def on_timer(self, timer_id: str) -> list[Action]:
        """Called when a previously-scheduled timer fires."""
        if timer_id != ENGAGE_TIMER or not self.engaging:
            return []

        if self.engage_phase == EngagePhase.PHASE1:
            self.engage_phase = EngagePhase.PHASE2
            return self._do_engage_phase2()

        if self.engage_phase == EngagePhase.PHASE2:
            self.engaging = False
            self.tilt_engaged = True
            self.tilt_value = 0.0  # at tilt_upper → slats closed

            actions: list[Action] = [Log("Tilt engaged")]

            if self.pending_tilt is not None:
                tilt = self.pending_tilt
                self.pending_tilt = None
                target_pos = self.tilt_to_position(tilt)
                actions.append(SetCoverPosition(position=int(target_pos)))
                self.tilt_value = tilt

            actions.append(self.make_publish())
            return actions

        return []

    # ── Internal helpers ────────────────────────────────────────────────

    def _exit_tilt(self) -> list[Action]:
        actions = self._cancel_engagement()
        if self.tilt_engaged:
            self.tilt_engaged = False
            actions.append(Log("Tilt disengaged"))
            actions.append(self.make_publish())
        return actions

    def _cancel_engagement(self) -> list[Action]:
        actions: list[Action] = []
        if self.engaging:
            actions.append(CancelTimer(timer_id=ENGAGE_TIMER))
        self.engaging = False
        self.engage_phase = None
        self.pending_tilt = None
        return actions

    def _start_engagement(self) -> list[Action]:
        self.engaging = True
        self.engage_phase = EngagePhase.PHASE1

        lower_target = max(0.0, self.tilt_lower - self.epsilon)

        if self.real_position <= lower_target + 1.0:
            self.engage_phase = EngagePhase.PHASE2
            return self._do_engage_phase2()

        travel_pct = abs(self.real_position - lower_target)
        travel_secs = self.full_travel_time * travel_pct / 100.0

        return [
            SetCoverPosition(position=int(lower_target)),
            ScheduleTimer(timer_id=ENGAGE_TIMER, seconds=travel_secs + 3),
            Log(f"Engage phase 1: moving to {lower_target:.1f}% (~{travel_secs:.1f}s)"),
        ]

    def _do_engage_phase2(self) -> list[Action]:
        upper_target = min(100.0, self.tilt_upper + self.epsilon)
        travel_pct = abs(upper_target - self.real_position)
        travel_secs = self.full_travel_time * travel_pct / 100.0

        return [
            SetCoverPosition(position=int(upper_target)),
            ScheduleTimer(timer_id=ENGAGE_TIMER, seconds=travel_secs + 3),
            Log(f"Engage phase 2: moving to {upper_target:.1f}% (~{travel_secs:.1f}s)"),
        ]
