"""
Execution of one movement plan, step by step.

The executor owns the step lifecycle and every timing decision, so both are testable without an
AppDaemon runtime. It consumes position feedback and settle-timer firings and emits declarative
:class:`Action` values the adapter translates one-to-one.

The lifecycle is what makes two whole classes of race impossible:

1. **Activation.** If a step's predicate already holds and the blind is settled, the step is
   *skipped* -- no command is sent and the next step activates immediately. A plan can therefore
   never begin with a move the actuator will never acknowledge, so no waypoint can stall waiting for
   feedback that will never arrive. (A step is not skipped while the blind is still travelling: it
   may be passing through the target rather than resting on it.)
2. **Arrival.** A step completes only on settled feedback whose position satisfies the predicate.
   Because activation guaranteed the predicate did *not* hold when the command went out, a duplicate
   or delayed report carrying the pre-command position cannot satisfy it -- however small the step
   was.

The settle timer is an inactivity timeout rather than a travel-time cap: a blind still reporting
motion is merely slow, so the timer re-arms. It only declares a stall when the blind has settled
short of its target, or when its position has become unreadable while a plan is pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from gradhermetic_cover_control.geometry import Zone, clamp_pct, to_command
from gradhermetic_cover_control.planner import (
    COMMAND_CLOSE,
    COMMAND_OPEN,
    LATCH_LATCHED,
    STEP_MOVE_TO,
    Plan,
    Step,
)

# -- Actions ---------------------------------------------------------------------------------------

ACTION_MOVE_TO = "move_to"
ACTION_OPEN_FULL = "open_full"
ACTION_CLOSE_FULL = "close_full"
ACTION_STOP = "stop"
ACTION_PUBLISH_POSITION = "publish_position"
ACTION_ARM_SETTLE_TIMER = "arm_settle_timer"
ACTION_CANCEL_SETTLE_TIMER = "cancel_settle_timer"
ACTION_NOTIFY = "notify"

NOTIFY_STALL = "stall"
NOTIFY_INVARIANT = "invariant"

# How long a step may go without progress before the fallback timer looks at it.
SETTLE_TIMEOUT_SECONDS = 45

# Real actuators occasionally settle a percent or so off their setpoint. A move that stopped this
# close to its target is accepted (with a warning) rather than reported as an obstruction.
DEVIATION_TOLERANCE_PCT = 2.0

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
    position: Optional[float] = None
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

    def __init__(self, zone: Zone, log: Callable[[str], None] = lambda _: None) -> None:
        """
        Create an idle executor for one blind.
        """
        self._zone = zone
        self._log = log
        self._plan: Optional[Plan] = None
        self._index = 0

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

    # -- Events ------------------------------------------------------------------------------------

    def start(self, movement: Plan, position: Optional[float], is_moving: bool) -> Outcome:
        """
        Begin a plan, activating its first unsatisfied step.

        Any plan already in progress must have been abandoned by the caller first, which is what
        makes a replacement re-derive its safety guards from the current belief.
        """
        self._plan = movement
        self._index = 0
        return self._advance(position, is_moving)

    def on_feedback(self, position: Optional[float], is_moving: bool) -> Outcome:
        """
        Consume controller feedback, completing the current step once it has genuinely arrived.
        """
        if self._plan is None or position is None or is_moving:
            return Outcome()
        if not self._step().satisfied_by(position):
            return Outcome()
        self._index += 1
        return self._advance(position, is_moving)

    def on_timer(self, position: Optional[float], is_moving: bool) -> Outcome:
        """
        Consume a settle-timer firing: keep waiting, accept, or declare a stall.
        """
        if self._plan is None:
            # A stray firing after the plan already finished; there is nothing left to time.
            return Outcome()
        if position is None:
            return self._stall("its position is unreadable")
        if is_moving:
            # An inactivity timeout, not a travel cap: the move is simply long.
            self._log("settle timer fired while still moving; waiting longer")
            return Outcome([_arm()], STATUS_RUNNING, self._plan)

        step = self._step()
        if step.satisfied_by(position):
            # The actuator reported no intermediate states, only its final one -- or none at all.
            self._index += 1
            return self._advance(position, is_moving)
        if step.kind == STEP_MOVE_TO and abs(position - step.target) <= DEVIATION_TOLERANCE_PCT:
            self._log(f"WARNING: accepting {position} for target {step.target}: settled within "
                      f"{DEVIATION_TOLERANCE_PCT}% of the setpoint")
            self._index += 1
            return self._advance(position, is_moving)
        return self._stall(f"it settled at {position}%")

    def abandon(self) -> Outcome:
        """
        Drop the current plan because it is being stopped or replaced.
        """
        movement = self._plan
        if movement is None:
            return Outcome()
        self._log("plan abandoned")
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

    def _advance(self, position: Optional[float], is_moving: bool) -> Outcome:
        """
        Skip every already-satisfied step and command the first one that remains.
        """
        while self._index < len(self._plan.steps):
            step = self._step()
            if not is_moving and position is not None and step.satisfied_by(position):
                self._log(f"skipping {step.kind} {step.target}: already at {position}")
                self._index += 1
                continue
            self._log(f"commanding {step.kind} {step.target} from {position}")
            return Outcome([_command(step), _arm()], STATUS_RUNNING, self._plan)
        return self._complete(position)

    def _complete(self, position: Optional[float]) -> Outcome:
        """
        Finish the plan: cancel the fallback timer and publish where the blind ended up.
        """
        movement = self._plan
        self._clear()
        self._log(f"plan complete: latch={movement.final_latch} position={position}")
        actions = [Action(ACTION_CANCEL_SETTLE_TIMER)]
        if position is not None:
            actions.append(Action(ACTION_PUBLISH_POSITION,
                                  position=self.virtual_position(movement.final_latch, position)))
        return Outcome(actions, STATUS_COMPLETED, movement)

    def _stall(self, reason: str) -> Outcome:
        """
        Abandon a plan the blind is not going to finish, and say so.
        """
        movement = self._plan
        target = to_command(self._step().target)
        self._clear()
        message = (f"did not reach {target}% within {SETTLE_TIMEOUT_SECONDS} seconds ({reason}) and "
                   "was stopped. Check the blind for a mechanical obstruction or a misconfigured "
                   "tilt zone.")
        self._log(f"ERROR: plan stalled short of {target}%: {reason}")
        return Outcome([
            Action(ACTION_STOP),
            Action(ACTION_CANCEL_SETTLE_TIMER),
            Action(ACTION_NOTIFY, notify_kind=NOTIFY_STALL, message=message),
        ], STATUS_STALLED, movement)

    def virtual_position(self, latch: str, position: float) -> float:
        """
        Map a real position to the virtual position the user-facing cover shows.

        Publishing what the blind actually reports -- rather than what was commanded -- keeps the
        published value and the app's own belief identical, which is what makes them comparable in
        the model tests.
        """
        if latch == LATCH_LATCHED:
            return self._zone.real_to_virtual(position)
        return clamp_pct(position)


def _command(step: Step) -> Action:
    """
    Translate a step into the real-cover command that starts it.
    """
    if step.command == COMMAND_OPEN:
        return Action(ACTION_OPEN_FULL)
    if step.command == COMMAND_CLOSE:
        return Action(ACTION_CLOSE_FULL)
    return Action(ACTION_MOVE_TO, position=step.target)


def _arm() -> Action:
    """
    Arm the fallback settle timer for the step just commanded.
    """
    return Action(ACTION_ARM_SETTLE_TIMER, seconds=SETTLE_TIMEOUT_SECONDS)
