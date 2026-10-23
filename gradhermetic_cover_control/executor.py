"""
Execution of one movement plan, step by step.

Consumes position feedback and settle-timer firings and emits declarative :class:`Action` values.
Two lifecycle rules rule out stale-feedback races:

1. **Activation.** A step already satisfied by a settled blind is skipped without a command, so no
   step waits for feedback the actuator will never send.
2. **Arrival.** A step completes only on settled feedback that satisfies it. Since it was not
   satisfied when commanded, a stale report of the starting position cannot complete it.

The settle timer is an inactivity timeout, not a travel cap: it re-arms while the blind reports
motion, and declares a stall only when the blind settled short or its position became unreadable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from gradhermetic_cover_control.geometry import Zone, clamp_pct
from gradhermetic_cover_control.planner import (
    COMMAND_CLOSE,
    COMMAND_OPEN,
    LATCH_LATCHED,
    PLAN_SLAT,
    STEP_MOVE_TO,
    Plan,
    Step,
)

# -- Actions ---------------------------------------------------------------------------------------

ACTION_MOVE_TO = "move_to"
ACTION_OPEN_FULL = "open_full"
ACTION_CLOSE_FULL = "close_full"
ACTION_STOP = "stop"
# Position, tilt mode and motion are published together so a slat angle is never read as a height.
ACTION_PUBLISH_STATE = "publish_state"
ACTION_ARM_SETTLE_TIMER = "arm_settle_timer"
ACTION_CANCEL_SETTLE_TIMER = "cancel_settle_timer"
ACTION_NOTIFY = "notify"

NOTIFY_STALL = "stall"
NOTIFY_INVARIANT = "invariant"

# Virtual-scale motion: "opening" means toward more light, in either mode.
MOTION_OPENING = "opening"
MOTION_CLOSING = "closing"
MOTION_IDLE = "idle"

# How long a step may go without progress before the fallback timer looks at it.
SETTLE_TIMEOUT_SECONDS = 45

# Shorter wait for a blind seen moving and then settled short: it has stopped, so only a late
# report could still change the verdict.
SETTLED_RECHECK_SECONDS = 10

# How far off target an ordinary height move may settle and still count as arrived; see
# Executor._tolerates_deviation.
DEVIATION_TOLERANCE_PCT = 2

# -- Outcomes --------------------------------------------------------------------------------------

# Nothing happened: no plan, or the plan is still waiting for the blind to reach the current step.
STATUS_IDLE = "idle"
# A command went out; the plan is waiting on feedback.
STATUS_RUNNING = "running"
# Every step is done. The caller commits the plan's terminal latch belief.
STATUS_COMPLETED = "completed"
# The blind settled short of its target, or its position became unreadable. The plan was dropped.
STATUS_STALLED = "stalled"
# The plan was dropped for a stop or a replacement.
STATUS_ABANDONED = "abandoned"


@dataclass(frozen=True)
class Action:
    """
    Declarative side effect for the adapter to perform.
    """

    kind: str
    position: Optional[int] = None
    in_tilt: Optional[bool] = None
    motion: Optional[str] = None
    seconds: Optional[float] = None
    notify_kind: Optional[str] = None
    message: Optional[str] = None


@dataclass
class Outcome:
    """
    What one executor call produced: actions to perform, and what became of the plan.
    """

    actions: List[Action] = field(default_factory=list)
    status: str = STATUS_IDLE
    plan: Optional[Plan] = None


class Executor:
    """
    Drives one movement plan at a time.
    """

    def __init__(
        self,
        zone: Zone,
        log: Callable[..., None] = lambda *_args, **_kwargs: None,
    ) -> None:
        """
        Create an idle executor for one blind.
        """
        self._zone = zone
        self._log = log
        self._plan: Optional[Plan] = None
        self._index = 0
        # Whether the blind reported motion since the current step was commanded, so settling short
        # afterwards gets the quick recheck.
        self._saw_motion = False
        # Whether a command for the current step is still unanswered by a settled report.
        self._awaiting_arrival = False

    @property
    def plan(self) -> Optional[Plan]:
        """
        The plan currently being executed, if any.
        """
        return self._plan

    @property
    def has_plan(self) -> bool:
        """
        Whether a movement plan is in progress.
        """
        return self._plan is not None

    @property
    def current_step(self) -> Optional[Step]:
        """
        The step the blind is currently being driven toward, if a plan is in progress.
        """
        if self._plan is None:
            return None
        return self._step()

    @property
    def may_be_travelling(self) -> bool:
        """
        Whether the blind might be moving: a command is out and no settled report has answered it.

        A pending plan alone is not enough, since the settled-short recheck keeps a plan open after
        the blind has stopped. A stop sent to an idle blind matters: on a KNX actuator without a
        dedicated stop object it is a step telegram that nudges the blind.
        """
        return self.has_plan and self._awaiting_arrival

    # -- Events ------------------------------------------------------------------------------------

    def start(self, movement: Plan, position: Optional[int], is_moving: bool) -> Outcome:
        """
        Begin a plan, activating its first unsatisfied step. The caller abandons any previous plan.
        """
        self._plan = movement
        self._index = 0
        return self._advance(position, is_moving)

    def on_feedback(self, position: Optional[int], is_moving: bool) -> Outcome:
        """
        Consume controller feedback, completing the current step once it has genuinely arrived.
        """
        if self._plan is None or position is None:
            return Outcome()
        if is_moving:
            self._saw_motion = True
            return Outcome()
        self._awaiting_arrival = False
        if self._step().satisfied_by(position):
            self._index += 1
            return self._advance(position, is_moving)
        if self._saw_motion:
            # It moved and has now stopped somewhere else: judge it soon, not after the timeout.
            self._saw_motion = False
            self._log(
                f"settled at {position} short of {self._step().target}; rechecking in "
                f"{SETTLED_RECHECK_SECONDS}s", level="DEBUG")
            return Outcome([_arm(SETTLED_RECHECK_SECONDS)], STATUS_RUNNING, self._plan)
        return Outcome()

    def on_timer(self, position: Optional[int], is_moving: bool) -> Outcome:
        """
        Consume a settle-timer firing: keep waiting, accept, or declare a stall.
        """
        if self._plan is None:
            return Outcome()
        if position is None:
            # It may still be travelling, so this stall sends a stop.
            return self._stall("its position is unreadable", stop=True)
        if is_moving:
            # An inactivity timeout, not a travel cap: the move is simply long.
            self._log("settle timer fired while still moving; waiting longer", level="DEBUG")
            return Outcome([_arm()], STATUS_RUNNING, self._plan)

        step = self._step()
        if step.satisfied_by(position):
            # The actuator reported no intermediate states, only its final one -- or none at all.
            self._index += 1
            return self._advance(position, is_moving)
        deviation = abs(position - step.target)
        if self._tolerates_deviation(step) and deviation <= DEVIATION_TOLERANCE_PCT:
            self._log(
                f"accepting {position} for target {step.target}: settled within "
                f"{DEVIATION_TOLERANCE_PCT}% of the setpoint", level="WARNING")
            self._index += 1
            return self._advance(position, is_moving)
        # At rest: no stop (see may_be_travelling).
        return self._stall(f"it settled at {position}%", stop=False)

    def _tolerates_deviation(self, step: Step) -> bool:
        """
        Whether settling a percent or so off this step's target may be accepted as arrival.

        Only an ordinary height move clear of the band qualifies. A slat step is about as small as
        the tolerance, and in the band a percent decides whether the mechanism latched.
        """
        if step.kind != STEP_MOVE_TO or self._plan.kind == PLAN_SLAT:
            return False
        return not self._zone.in_band(step.target)

    def abandon(self) -> Outcome:
        """
        Drop the current plan because it is being stopped or replaced.
        """
        movement = self._plan
        if movement is None:
            return Outcome()
        self._log("plan abandoned", level="DEBUG")
        self._clear()
        return Outcome([Action(ACTION_CANCEL_SETTLE_TIMER)], STATUS_ABANDONED, movement)

    # -- Internals ---------------------------------------------------------------------------------

    def _step(self) -> Step:
        """
        The step currently being executed.
        """
        return self._plan.steps[self._index]

    def _clear(self) -> None:
        """
        Forget the current plan.
        """
        self._plan = None
        self._index = 0
        self._saw_motion = False
        self._awaiting_arrival = False

    def _advance(self, position: Optional[int], is_moving: bool) -> Outcome:
        """
        Skip every already-satisfied step and command the first one that remains.
        """
        while self._index < len(self._plan.steps):
            step = self._step()
            if not is_moving and position is not None and step.satisfied_by(position):
                self._log(f"skipping {step.kind} {step.target}: already at {position}",
                          level="DEBUG")
                self._index += 1
                continue
            self._log(
                f"commanding {step.kind} {step.command_position} (satisfied at "
                f"{step.target}) from {position}", level="DEBUG")
            self._saw_motion = False
            self._awaiting_arrival = True
            return Outcome([_command(step), _arm()], STATUS_RUNNING, self._plan)
        return self._complete(position)

    def _complete(self, position: Optional[int]) -> Outcome:
        """
        Finish the plan and cancel the fallback timer. The caller commits the latch and publishes.
        """
        movement = self._plan
        self._clear()
        self._log(f"plan complete: latch={movement.final_latch} position={position}")
        return Outcome([Action(ACTION_CANCEL_SETTLE_TIMER)], STATUS_COMPLETED, movement)

    def _stall(self, reason: str, stop: bool) -> Outcome:
        """
        Abandon a plan the blind is not going to finish, and notify, naming the satisfaction target.
        """
        movement = self._plan
        target = self._step().target
        self._clear()
        message = (f"did not reach {target}% ({reason}) and the move was abandoned. Check the "
                   "blind for a mechanical obstruction or a misconfigured tilt zone.")
        self._log(f"plan stalled short of {target}%: {reason}", level="ERROR")
        actions = [Action(ACTION_STOP)] if stop else []
        actions += [
            Action(ACTION_CANCEL_SETTLE_TIMER),
            Action(ACTION_NOTIFY, notify_kind=NOTIFY_STALL, message=message),
        ]
        return Outcome(actions, STATUS_STALLED, movement)


def virtual_position(zone: Zone, latch: str, position: int) -> int:
    """
    Map a real position to the virtual position the user-facing cover shows.
    """
    if latch == LATCH_LATCHED:
        return zone.real_to_virtual(position)
    return clamp_pct(position)


def _command(step: Step) -> Action:
    """
    Translate a step into the real-cover command that starts it.
    """
    if step.command == COMMAND_OPEN:
        return Action(ACTION_OPEN_FULL)
    if step.command == COMMAND_CLOSE:
        return Action(ACTION_CLOSE_FULL)
    return Action(ACTION_MOVE_TO, position=step.command_position)


def _arm(seconds: float = SETTLE_TIMEOUT_SECONDS) -> Action:
    """
    Arm the fallback settle timer for the step just commanded, or a shorter recheck.
    """
    return Action(ACTION_ARM_SETTLE_TIMER, seconds=seconds)
