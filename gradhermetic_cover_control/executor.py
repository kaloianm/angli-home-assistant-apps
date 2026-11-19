"""
Execution of one movement plan, step by step, on top of :mod:`actuator`.

The executor owns the :class:`Actuator`: every command and every controller reading goes through it.
Each step becomes one actuator :class:`Move`. The executor only decides what happens next: a step
already satisfied by a settled blind is skipped without a command, an :class:`Arrived` verdict
advances the plan, and a :class:`Stalled` one abandons it and reports why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from gradhermetic_cover_control.actions import Action
from gradhermetic_cover_control.actuator import Actuator, Feedback, Move, Stalled
from gradhermetic_cover_control.cover_types import Direction
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.planner import Plan, PlanKind, Step, StepKind

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
    # The plan was dropped for a stop or because the blind is being disabled.
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Stall:
    """
    Why a plan stalled: the satisfaction target of the step it gave up on, and what the blind did.
    """

    target: int
    reason: str


@dataclass
class Outcome:
    """
    What one executor call produced: actions to perform, and what became of the plan.
    """

    actions: list[Action] = field(default_factory=list)
    status: Status = Status.IDLE
    plan: Plan | None = None
    stall: Stall | None = None


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
        Create an idle executor for one blind, with nothing known about the cover.
        """
        self._zone = zone
        self._actuator = Actuator(log)
        self._log = log
        self._plan: Plan | None = None
        self._index = 0

    @property
    def position(self) -> int | None:
        """
        The last reported real position, or None while it is unreadable.
        """
        return self._actuator.position

    @property
    def is_moving(self) -> bool:
        """
        Whether the controller last reported the blind travelling.
        """
        return self._actuator.is_moving

    @property
    def heading(self) -> Direction | None:
        """
        Which way the blind is travelling (see :attr:`Actuator.heading`).
        """
        return self._actuator.heading

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

    def seed(self, position: int | None, is_moving: bool = False,
             direction: Direction | None = None) -> None:
        """
        Take the first controller reading, with no plan in progress.
        """
        self._actuator.seed(position, is_moving, direction)

    def start(self, movement: Plan) -> Outcome:
        """
        Begin a plan, activating its first unsatisfied step.

        A plan in flight is replaced without a stop: the new command supersedes its move, or, if
        nothing is left to command, completion releases it. The caller degrades its belief for the
        replaced plan first.
        """
        if self._plan is not None:
            self._log(f"replacing the {self._plan.kind} plan", level="DEBUG")
        self._plan = movement
        self._index = 0
        return self._advance()

    def on_report(self, position: int | None, is_moving: bool,
                  direction: Direction | None = None) -> Outcome:
        """
        Consume a controller report, advancing the plan on the actuator's verdict.
        """
        return self._judge(self._actuator.on_report(position, is_moving, direction))

    def on_timer(self, position: int | None, is_moving: bool,
                 direction: Direction | None = None) -> Outcome:
        """
        Consume a settle-timer firing, with the controller state read when it fired.
        """
        return self._judge(self._actuator.on_timer(position, is_moving, direction))

    def stop(self) -> Outcome:
        """
        Drop the current plan and stop the blind if it may be travelling (see
        :meth:`Actuator.stop`).
        """
        return self._drop(self._actuator.stop())

    def abandon(self) -> Outcome:
        """
        Drop the current plan without stopping the blind.
        """
        if self._plan is None:
            return Outcome()
        return self._drop(self._actuator.release())

    # -- Internals ---------------------------------------------------------------------------------

    def _judge(self, feedback: Feedback) -> Outcome:
        """
        Act on the actuator's verdict on the current step, if it gave one.
        """
        if self._plan is None or feedback.verdict is None:
            return Outcome(feedback.actions)
        if isinstance(feedback.verdict, Stalled):
            outcome = self._stall(feedback.verdict.reason)
        else:
            self._index += 1
            outcome = self._advance()
        return Outcome(feedback.actions + outcome.actions, outcome.status, outcome.plan,
                       outcome.stall)

    def _drop(self, actions: list[Action]) -> Outcome:
        """
        Forget the current plan, reporting it as abandoned alongside the actuator's ``actions``.
        """
        movement = self._plan
        if movement is None:
            return Outcome(actions)
        self._log("plan abandoned", level="DEBUG")
        self._clear()
        return Outcome(actions, Status.ABANDONED, movement)

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
            move = self._move(step)
            if resting and move.arrived(position):
                self._log(f"skipping {step.kind} {step.target}: already at {position}",
                          level="DEBUG")
                self._index += 1
                continue
            self._log(
                f"commanding {step.kind} {step.command_position} (satisfied at "
                f"{step.target}) from {position}", level="DEBUG")
            actions = self._actuator.start(move)
            return Outcome(actions, Status.RUNNING, self._plan)
        return self._complete()

    def _move(self, step: Step) -> Move:
        """
        The actuator move that carries out a step.
        """
        return Move(step.command, step.command_position, step.target,
                    at_least=step.kind == StepKind.RISE_TO_AT_LEAST,
                    tolerance_pct=self._tolerance(step))

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
        Abandon a plan the blind is not going to finish, naming the satisfaction target it missed.
        """
        movement = self._plan
        target = self._step().target
        self._clear()
        self._log(f"plan stalled short of {target}%: {reason}", level="ERROR")
        return Outcome(self._actuator.release(), Status.STALLED, movement, Stall(target, reason))
