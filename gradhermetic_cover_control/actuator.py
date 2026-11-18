"""
The real cover as Home Assistant exposes it: commands, feedback, and the timer that stands in for
feedback that never arrives.

Knows nothing about the mechanism behind the cover. It drives one :class:`Move` at a time and turns
raw reports into a verdict on it, :class:`Arrived` or :class:`Stalled`. Every report also updates
:attr:`Actuator.position`, :attr:`Actuator.is_moving` and :attr:`Actuator.direction`.

A move arrives only on a settled report that satisfies it. The caller starts a move only when the
cover does not already satisfy it, so a stale report of the starting position cannot complete it.

The timer is an inactivity timeout, not a travel cap: it re-arms while the cover reports motion, and
stalls the move only when the cover settled short or its position became unreadable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from gradhermetic_cover_control.actions import Action, ActionKind

# How long a move may go without progress before the timer looks at it.
SETTLE_TIMEOUT_SECONDS = 45

# Shorter wait for a cover seen moving and then settled short: it has stopped, so only a late
# report could still change the verdict.
SETTLED_RECHECK_SECONDS = 10


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


@dataclass(frozen=True)
class Move:
    """
    One commanded movement: what to send, and when it counts as done.

    ``position`` is where the command sends the cover. It arrives at ``target`` exactly, or at or
    above it with ``at_least``. Once it has stopped, being within ``tolerance_pct`` of ``target``
    also counts.
    """

    command: CommandKind
    position: int
    target: int
    at_least: bool = False
    tolerance_pct: int = 0

    def arrived(self, position: int) -> bool:
        """
        Whether a reported position satisfies the move. The cover reports the whole percent it
        reached, so an exact target uses equality.
        """
        if self.at_least:
            return position >= self.target
        return position == self.target


@dataclass(frozen=True)
class Arrived:
    position: int


@dataclass(frozen=True)
class Stalled:
    reason: str


Verdict = Arrived | Stalled


@dataclass
class Feedback:
    """
    What one report or timer firing produced: actions to perform, and the verdict on the move.
    """

    actions: list[Action] = field(default_factory=list)
    verdict: Verdict | None = None


class Actuator:
    """
    One real cover and the move it is currently executing.
    """

    def __init__(self, log: Callable[..., None] = lambda *_args, **_kwargs: None) -> None:
        """
        Create an actuator with nothing known about the cover.
        """
        self._log = log
        self._position: int | None = None
        self._is_moving = False
        # From the controller if it says, else from the position trend.
        self._direction: Direction | None = None
        # Whether this controller has ever reported motion; see may_be_travelling.
        self._reports_motion = False
        self._move: Move | None = None
        # Whether the cover reported motion since the move was commanded, so settling short
        # afterwards gets the quick recheck.
        self._saw_motion = False
        # Whether the command is still unanswered by a settled report.
        self._awaiting_report = False

    # -- State -------------------------------------------------------------------------------------

    @property
    def position(self) -> int | None:
        """
        The last reported position, or None while it is unreadable.
        """
        return self._position

    @property
    def is_moving(self) -> bool:
        """
        Whether the controller last reported the cover travelling.
        """
        return self._is_moving

    @property
    def direction(self) -> Direction | None:
        """
        The reported direction of travel, if moving.
        """
        return self._direction

    @property
    def move(self) -> Move | None:
        """
        The move in progress, if any.
        """
        return self._move

    @property
    def may_be_travelling(self) -> bool:
        """
        Whether the cover might be moving, so a stop has something to stop.

        True while it reports motion, or a command is unanswered by a settled report. A controller
        that has never reported motion proves nothing by a settled-looking report, so for it any
        move in progress counts.
        """
        if self._is_moving:
            return True
        if self._move is None:
            return False
        return self._awaiting_report or not self._reports_motion

    @property
    def heading(self) -> Direction | None:
        """
        Which way the cover is travelling: as reported, else toward the move's commanded position
        while it may be travelling.
        """
        if self._is_moving and self._direction is not None:
            return self._direction
        if self._move is None or self._position is None or not self.may_be_travelling:
            return None
        if self._move.position == self._position:
            return None
        return Direction.UP if self._move.position > self._position else Direction.DOWN

    # -- Commands ----------------------------------------------------------------------------------

    def seed(self, position: int | None, is_moving: bool = False,
             direction: Direction | None = None) -> None:
        """
        Take the first reading, with no move in progress.
        """
        self._position = position
        self._is_moving = bool(is_moving) and position is not None
        self._direction = direction if self._is_moving else None

    def start(self, move: Move) -> list[Action]:
        """
        Command a move, replacing any in progress, and arm the timer.
        """
        self._move = move
        self._saw_motion = False
        self._awaiting_report = True
        return [_command(move), _arm()]

    def release(self) -> list[Action]:
        """
        Forget the move in progress without stopping the cover, and cancel the timer.
        """
        self._move = None
        self._saw_motion = False
        self._awaiting_report = False
        return [Action(ActionKind.CANCEL_SETTLE_TIMER)]

    def stop(self) -> list[Action]:
        """
        Abandon the move and stop the cover, but only if it may be travelling: on a KNX actuator
        without a dedicated stop object, HA sends the stop as a step telegram that nudges an idle
        cover.
        """
        actions = [Action(ActionKind.STOP)] if self.may_be_travelling else []
        return actions + self.release()

    # -- Feedback ----------------------------------------------------------------------------------

    def on_report(self, position: int | None, is_moving: bool,
                  direction: Direction | None = None) -> Feedback:
        """
        Consume a controller report. The move arrives once a settled report satisfies it.
        """
        self._observe(position, is_moving, direction)
        move = self._move
        if move is None or position is None:
            return Feedback()
        if is_moving:
            self._saw_motion = True
            return Feedback()
        self._awaiting_report = False
        if move.arrived(position):
            return Feedback(verdict=Arrived(position))
        if self._saw_motion:
            # It moved and has now stopped somewhere else: judge it soon, not after the timeout.
            self._saw_motion = False
            self._log(
                f"settled at {position} short of {move.target}; rechecking in "
                f"{SETTLED_RECHECK_SECONDS}s", level="DEBUG")
            return Feedback([_arm(SETTLED_RECHECK_SECONDS)])
        return Feedback()

    def on_timer(self, position: int | None, is_moving: bool,
                 direction: Direction | None = None) -> Feedback:
        """
        Consume a timer firing, with the controller state read when it fired: keep waiting, or
        deliver a verdict.
        """
        self._observe(position, is_moving, direction)
        move = self._move
        if move is None:
            return Feedback()
        if position is None:
            # It may still be travelling, so this stall sends a stop.
            return Feedback([Action(ActionKind.STOP)], Stalled("its position is unreadable"))
        if is_moving:
            self._log("settle timer fired while still moving; waiting longer", level="DEBUG")
            return Feedback([_arm()])
        if move.arrived(position):
            # The controller reported no intermediate states, only its final one -- or none at all.
            return Feedback(verdict=Arrived(position))
        if abs(position - move.target) <= move.tolerance_pct:
            self._log(
                f"accepting {position} for target {move.target}: settled within "
                f"{move.tolerance_pct}% of the setpoint", level="WARNING")
            return Feedback(verdict=Arrived(position))
        # At rest: no stop (see stop).
        return Feedback(verdict=Stalled(f"it settled at {position}%"))

    def _observe(self, position: int | None, is_moving: bool, direction: Direction | None) -> None:
        """
        Record a controller reading.
        """
        if is_moving:
            self._reports_motion = True
        if position is None:
            self._position = None
            self._is_moving = False
            self._direction = None
            return
        previous = self._position
        self._position = position
        self._is_moving = is_moving
        if not is_moving:
            self._direction = None
        elif direction is not None:
            self._direction = direction
        elif previous is not None and position != previous:
            self._direction = Direction.UP if position > previous else Direction.DOWN


def _command(move: Move) -> Action:
    """
    Translate a move into the real-cover command that starts it.
    """
    match move.command:
        case CommandKind.OPEN:
            return Action(ActionKind.OPEN_FULL)
        case CommandKind.CLOSE:
            return Action(ActionKind.CLOSE_FULL)
        case _:
            return Action(ActionKind.MOVE_TO, position=move.position)


def _arm(seconds: float = SETTLE_TIMEOUT_SECONDS) -> Action:
    """
    Arm the timer for the move just commanded, or a shorter recheck.
    """
    return Action(ActionKind.ARM_SETTLE_TIMER, seconds=seconds)
