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

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from gradhermetic_cover_control.geometry import Zone, clamp_pct
from gradhermetic_cover_control.planner import CommandKind, Latch, Plan, PlanKind, Step, StepKind

# -- Actions ---------------------------------------------------------------------------------------


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


# How long a step may go without progress before the fallback timer looks at it.
SETTLE_TIMEOUT_SECONDS = 45

# Shorter wait for a blind seen moving and then settled short: it has stopped, so only a late
# report could still change the verdict.
SETTLED_RECHECK_SECONDS = 10

# How far off target an ordinary height move may settle and still count as arrived; see
# Executor._tolerates_deviation.
DEVIATION_TOLERANCE_PCT = 2

# -- Outcomes --------------------------------------------------------------------------------------


class Status(StrEnum):
    # Nothing happened: no plan, or the plan is still waiting for the blind to reach the current
    # step.
    IDLE = "idle"
    # A command went out; the plan is waiting on feedback.
    RUNNING = "running"
    # Every step is done. The caller commits the plan's terminal latch belief.
    COMPLETED = "completed"
    # The blind settled short of its target, or its position became unreadable. The plan was
    # dropped.
    STALLED = "stalled"
    # The plan was dropped for a stop or a replacement.
    ABANDONED = "abandoned"


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


@dataclass
class Outcome:
    """
    What one executor call produced: actions to perform, and what became of the plan.
    """

    actions: list[Action] = field(default_factory=list)
    status: Status = Status.IDLE
    plan: Plan | None = None


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
        self._plan: Plan | None = None
        self._index = 0
        # Whether the blind reported motion since the current step was commanded, so settling short
        # afterwards gets the quick recheck.
        self._saw_motion = False
        # Whether a command for the current step is still unanswered by a settled report.
        self._awaiting_arrival = False

    @property
    def plan(self) -> Plan | None:
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
    def current_step(self) -> Step | None:
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

    def start(self, movement: Plan, position: int | None, is_moving: bool) -> Outcome:
        """
        Begin a plan, activating its first unsatisfied step. The caller abandons any previous plan.
        """
        self._plan = movement
        self._index = 0
        return self._advance(position, is_moving)

    def on_feedback(self, position: int | None, is_moving: bool) -> Outcome:
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
            return Outcome([_arm(SETTLED_RECHECK_SECONDS)], Status.RUNNING, self._plan)
        return Outcome()

    def on_timer(self, position: int | None, is_moving: bool) -> Outcome:
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
            return Outcome([_arm()], Status.RUNNING, self._plan)

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
        if step.kind != StepKind.MOVE_TO or self._plan.kind == PlanKind.SLAT:
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
        return Outcome([Action(ActionKind.CANCEL_SETTLE_TIMER)], Status.ABANDONED, movement)

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

    def _advance(self, position: int | None, is_moving: bool) -> Outcome:
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
            return Outcome([_command(step), _arm()], Status.RUNNING, self._plan)
        return self._complete(position)

    def _complete(self, position: int | None) -> Outcome:
        """
        Finish the plan and cancel the fallback timer. The caller commits the latch and publishes.
        """
        movement = self._plan
        self._clear()
        self._log(f"plan complete: latch={movement.final_latch} position={position}")
        return Outcome([Action(ActionKind.CANCEL_SETTLE_TIMER)], Status.COMPLETED, movement)

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
        actions = [Action(ActionKind.STOP)] if stop else []
        actions += [
            Action(ActionKind.CANCEL_SETTLE_TIMER),
            Action(ActionKind.NOTIFY, notify_kind=NotifyKind.STALL, message=message),
        ]
        return Outcome(actions, Status.STALLED, movement)


def virtual_position(zone: Zone, latch: Latch, position: int) -> int:
    """
    Map a real position to the virtual position the user-facing cover shows.
    """
    if latch == Latch.LATCHED:
        return zone.real_to_virtual(position)
    return clamp_pct(position)


def _command(step: Step) -> Action:
    """
    Translate a step into the real-cover command that starts it.
    """
    match step.command:
        case CommandKind.OPEN:
            return Action(ActionKind.OPEN_FULL)
        case CommandKind.CLOSE:
            return Action(ActionKind.CLOSE_FULL)
        case _:
            return Action(ActionKind.MOVE_TO, position=step.command_position)


def _arm(seconds: float = SETTLE_TIMEOUT_SECONDS) -> Action:
    """
    Arm the fallback settle timer for the step just commanded, or a shorter recheck.
    """
    return Action(ActionKind.ARM_SETTLE_TIMER, seconds=seconds)
