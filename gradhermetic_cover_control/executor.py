"""
Execution of one movement plan, step by step, on top of :mod:`actuator`.

Each step becomes one actuator :class:`Move`. The executor only decides what happens next: a step
already satisfied by a settled blind is skipped without a command, an :class:`Arrived` verdict
advances the plan, and a :class:`Stalled` one abandons it and notifies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from gradhermetic_cover_control.actions import Action, ActionKind, NotifyKind
from gradhermetic_cover_control.actuator import Actuator, Stalled, Verdict
from gradhermetic_cover_control.geometry import Zone, clamp_pct
from gradhermetic_cover_control.planner import Latch, Plan, PlanKind, Step, StepKind

# How far off target an ordinary height move may settle and still count as arrived; see
# Executor._tolerance.
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
        actuator: Actuator,
        log: Callable[..., None] = lambda *_args, **_kwargs: None,
    ) -> None:
        """
        Create an idle executor for one blind.
        """
        self._zone = zone
        self._actuator = actuator
        self._log = log
        self._plan: Plan | None = None
        self._index = 0

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

    # -- Events ------------------------------------------------------------------------------------

    def start(self, movement: Plan) -> Outcome:
        """
        Begin a plan, activating its first unsatisfied step. The caller abandons any previous plan.
        """
        self._plan = movement
        self._index = 0
        return self._advance()

    def on_verdict(self, verdict: Verdict | None) -> Outcome:
        """
        Consume the actuator's verdict on the current step, if it gave one.
        """
        if self._plan is None or verdict is None:
            return Outcome()
        if isinstance(verdict, Stalled):
            return self._stall(verdict.reason)
        self._index += 1
        return self._advance()

    def abandon(self) -> Outcome:
        """
        Drop the current plan because it is being stopped or replaced.
        """
        movement = self._plan
        if movement is None:
            return Outcome()
        self._log("plan abandoned", level="DEBUG")
        self._clear()
        return Outcome(self._actuator.release(), Status.ABANDONED, movement)

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

    def _advance(self) -> Outcome:
        """
        Skip every already-satisfied step and command the first one that remains.

        A blind still travelling is passing through, not resting, so nothing is skipped.
        """
        position = self._actuator.position
        resting = not self._actuator.is_moving and position is not None
        while self._index < len(self._plan.steps):
            step = self._step()
            if resting and step.satisfied_by(position):
                self._log(f"skipping {step.kind} {step.target}: already at {position}",
                          level="DEBUG")
                self._index += 1
                continue
            self._log(
                f"commanding {step.kind} {step.command_position} (satisfied at "
                f"{step.target}) from {position}", level="DEBUG")
            actions = self._actuator.start(step.move(self._tolerance(step)))
            return Outcome(actions, Status.RUNNING, self._plan)
        return self._complete()

    def _tolerance(self, step: Step) -> int:
        """
        How far off its target this step may settle and still count as arrived.

        Only an ordinary height move clear of the band gets any. A slat step is about as small as
        the tolerance, and in the band a percent decides whether the mechanism latched.
        """
        if step.kind != StepKind.MOVE_TO or self._plan.kind == PlanKind.SLAT:
            return 0
        if self._zone.in_band(step.target):
            return 0
        return DEVIATION_TOLERANCE_PCT

    def _complete(self) -> Outcome:
        """
        Finish the plan and cancel the timer. The caller commits the latch and publishes.
        """
        movement = self._plan
        self._clear()
        self._log(f"plan complete: latch={movement.final_latch} "
                  f"position={self._actuator.position}")
        return Outcome(self._actuator.release(), Status.COMPLETED, movement)

    def _stall(self, reason: str) -> Outcome:
        """
        Abandon a plan the blind is not going to finish, and notify, naming the satisfaction target.
        """
        movement = self._plan
        target = self._step().target
        self._clear()
        message = (f"did not reach {target}% ({reason}) and the move was abandoned. Check the "
                   "blind for a mechanical obstruction or a misconfigured tilt zone.")
        self._log(f"plan stalled short of {target}%: {reason}", level="ERROR")
        actions = self._actuator.release()
        actions.append(Action(ActionKind.NOTIFY, notify_kind=NotifyKind.STALL, message=message))
        return Outcome(actions, Status.STALLED, movement)


def virtual_position(zone: Zone, latch: Latch, position: int) -> int:
    """
    Map a real position to the virtual position the user-facing cover shows.
    """
    if latch == Latch.LATCHED:
        return zone.real_to_virtual(position)
    return clamp_pct(position)
