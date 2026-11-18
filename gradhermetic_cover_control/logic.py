"""
The state machine for one Gradhermetic cover.

Holds the app's *belief* about the blind and routes events to :mod:`planner` (intent -> plan) and
:mod:`executor` (drives the plan). Returns declarative actions; no AppDaemon dependencies.

The belief has three parts:

- ``position`` -- the last reported real travel position, or ``None`` while unavailable.
- ``latch`` -- ``LATCHED`` / ``UNLATCHED`` / ``UNKNOWN``. Tracked from events, not derived from the
  position: a position inside the band neither proves nor rules out a latch.
- ``is_moving`` -- whether the blind is travelling, and which way.

Latch transitions, in full:

- to ``LATCHED``: a completed enter plan, and nothing else.
- to ``UNLATCHED``: a completed plan that ends released, or feedback placing the blind clearly
  outside the ambiguity band (a latched mechanism cannot rest there).
- to ``UNKNOWN``: startup with the position unknown or inside the band; an interrupted plan that
  could have crossed a zone edge; externally-caused motion ending inside the band; the cover
  becoming unavailable; a position-commanded rise that ends exactly on the release height.

After every event it publishes position, mode and motion if they changed. The mode shown is the
belief the app would hold if the plan in flight were interrupted now: an entry shows tilt mode only
once it latches, an exit drops it at once.
"""

from __future__ import annotations

from collections.abc import Callable

from gradhermetic_cover_control import planner
from gradhermetic_cover_control.executor import (
    Action,
    ActionKind,
    Executor,
    Motion,
    NotifyKind,
    Outcome,
    Status,
    virtual_position,
)
from gradhermetic_cover_control.geometry import Zone, clamp_pct
from gradhermetic_cover_control.planner import (
    Belief,
    Direction,
    Intent,
    IntentKind,
    Latch,
    Plan,
    PlanKind,
)

# What one publish carries, for deduplication: (virtual position, in tilt, motion).
_Published = tuple[int | None, bool, str]


class GradhermeticCoverLogic:
    """
    State machine translating user intent into blind movements for one Gradhermetic cover.
    """

    # The event surface is wide by design: one method per thing that can happen to a blind.
    # pylint: disable=too-many-public-methods

    def __init__(
        self,
        zone: Zone,
        log: Callable[..., None] = lambda *_args, **_kwargs: None,
    ) -> None:
        """
        Create logic state for one blind.
        """
        self._zone = zone
        self._log = log
        self._disabled = False

        self._position: int | None = None
        self._latch = Latch.UNKNOWN
        self._is_moving = False
        # Real direction of travel: from the controller if it says, else from the position trend.
        self._direction: Direction | None = None
        # Whether this controller has ever reported motion; see _may_be_travelling.
        self._controller_reports_motion = False
        self._executor = Executor(zone, log)
        self._last_published: _Published | None = None

    # -- Accessors ---------------------------------------------------------------------------------

    @property
    def last_position(self) -> int | None:
        """
        Most recent real travel position known to the logic.
        """
        return self._position

    @property
    def latch(self) -> Latch:
        """
        The latch belief: ``LATCHED``, ``UNLATCHED`` or ``UNKNOWN``.
        """
        return self._latch

    @property
    def in_tilt(self) -> bool:
        """
        Whether slat control applies. Only a confident latch belief offers it.
        """
        return self._latch == Latch.LATCHED

    @property
    def is_moving(self) -> bool:
        """
        Whether the blind is currently travelling.
        """
        return self._is_moving

    @property
    def has_pending_plan(self) -> bool:
        """
        Whether a movement plan is currently in progress.
        """
        return self._executor.has_plan

    def current_virtual_position(self) -> int | None:
        """
        Virtual cover position for the current real position and mode, or None if unknown.
        """
        if self._position is None:
            return None
        return virtual_position(self._zone, self._latch, self._position)

    def belief(self) -> Belief:
        """
        The current belief, as the planner sees it.
        """
        return Belief(position=self._position, latch=self._latch, is_moving=self._is_moving)

    # -- Lifecycle ---------------------------------------------------------------------------------

    def seed_state(self, last_position: int | None, is_moving: bool = False,
                   direction: Direction | None = None) -> None:
        """
        Establish the belief from a first position reading: unlatched if clear of the band,
        otherwise unknown.
        """
        self._position = last_position
        self._is_moving = bool(is_moving) and last_position is not None
        self._direction = direction if self._is_moving else None
        if last_position is not None and not self._zone.in_band(last_position):
            self._latch = Latch.UNLATCHED
        else:
            self._latch = Latch.UNKNOWN

    def on_startup(self, position: int | None, is_moving: bool = False,
                   direction: Direction | None = None) -> list[Action]:
        """
        Seed the belief from the first position reading. Startup never moves the blind.

        An unknown latch is not resolved here: every move that needs a trusted position already
        starts with a full open, so the blind is not raised unprompted after a power cut.
        """
        if self._disabled:
            return []
        self.seed_state(position, is_moving, direction)
        if self._latch == Latch.UNKNOWN:
            if position is None:
                self._log("startup position unreadable; the actuator will be re-referenced by the "
                          "first move that needs it")
            else:
                self._log(f"startup position {position}% is inside the tilt band; the latch belief "
                          "is unknown and the actuator will be re-referenced by the first move "
                          "that needs it")
        else:
            self._log(f"startup position {position}% is outside the tilt band; resuming "
                      "whole-height control")
        return self._publish_current()

    def disable(self) -> list[Action]:
        """
        Permanently stop automation decisions for this blind until restart.
        """
        self._disabled = True
        self._executor.abandon()
        self._log("disabled")
        return []

    # -- User / command events ---------------------------------------------------------------------

    def on_open(self) -> list[Action]:
        """
        Handle ``cover.open_cover``: most light.

        Outside tilt this opens the blind fully; inside tilt it orients the slats perpendicular
        (virtual 100, the lower edge).
        """
        return self._run(Intent(IntentKind.OPEN))

    def on_close(self) -> list[Action]:
        """
        Handle ``cover.close_cover``: least light.

        Outside tilt this closes the blind fully; inside tilt it orients the slats parallel
        (virtual 0, the upper edge).
        """
        return self._run(Intent(IntentKind.CLOSE))

    def on_stop(self) -> list[Action]:
        """
        Handle ``cover.stop_cover``: abandon the current plan, and send a stop only if the blind
        may be travelling (see :attr:`Executor.may_be_travelling` for why an idle blind must not
        get one).
        """
        if self._disabled:
            return []
        stopping = self._may_be_travelling()
        self._abandon_plan()
        actions = [Action(ActionKind.STOP)] if stopping else []
        actions.append(Action(ActionKind.CANCEL_SETTLE_TIMER))
        return actions + self._publish_current()

    def on_set_position(self, virtual_pct: int) -> list[Action]:
        """
        Handle ``cover.set_cover_position`` to an absolute virtual position.

        Outside tilt the virtual position is the real position, snapped clear of the ambiguity band;
        inside tilt it interpolates the slat angle between the zone edges.
        """
        return self._run(Intent(IntentKind.SET_POSITION, virtual_pct=clamp_pct(virtual_pct)))

    def on_set_tilt_mode(self, enabled: bool) -> list[Action]:
        """
        Handle the ``set_tilt_mode`` command: enter (landing on ``tilt_zone_enter_pct``) or leave.

        A no-op if already in, entering or leaving the requested mode, judged by what the
        dashboard shows (:meth:`_displayed_in_tilt`). Requesting the opposite of a sequence in
        flight stops it.
        """
        if self._disabled:
            return []
        pending = self._executor.plan
        if enabled:
            if pending is not None and pending.kind == PlanKind.LEAVE:
                return self.on_stop()
            if self._displayed_in_tilt():
                self._log("enter tilt does nothing: already in tilt mode")
                return []
            if pending is not None and pending.kind == PlanKind.ENTER:
                self._log("enter tilt does nothing: an entry is already in flight")
                return []
            return self._run(
                Intent(IntentKind.ENTER_TILT, landing_virtual=self._zone.enter_landing_virtual))
        if pending is not None and pending.kind == PlanKind.LEAVE:
            self._log("leave tilt does nothing: an exit is already in flight")
            return []
        if pending is not None and pending.kind == PlanKind.ENTER:
            return self.on_stop()
        if not self._displayed_in_tilt():
            self._log("leave tilt does nothing: no latch belief to leave")
            return []
        return self._run(Intent(IntentKind.LEAVE_TILT))

    def on_toggle_tilt_mode(self) -> list[Action]:
        """
        Toggle tilt mode. Cancels an entry or exit in flight; otherwise toggles what the dashboard
        shows (:meth:`_displayed_in_tilt`).
        """
        if self._disabled:
            return []
        pending = self._executor.plan
        if pending is not None and pending.kind in (PlanKind.ENTER, PlanKind.LEAVE):
            return self.on_stop()
        return self.on_set_tilt_mode(not self._displayed_in_tilt())

    def _may_be_travelling(self) -> bool:
        """
        Whether the blind might be moving right now.

        Some controllers report positions but never motion; for those a settled-looking report
        proves nothing, so any pending plan counts as possibly moving.
        """
        if self._is_moving:
            return True
        if self._executor.may_be_travelling:
            return True
        return self.has_pending_plan and not self._controller_reports_motion

    def _displayed_in_tilt(self) -> bool:
        """
        Whether the published mode is tilt. Differs from the raw latch while a plan that leaves
        tilt is still running.
        """
        return self._belief_after_interrupt().latch == Latch.LATCHED

    # -- Step events -------------------------------------------------------------------------------

    def on_step(self, direction: Direction) -> list[Action]:
        """
        Handle a step, like a KNX stop/step object:

        1. If anything is moving, stop it.
        2. If latched, step the slats by ``tilt_step_pct``.
        3. Otherwise step the height to the next multiple of ``height_step_pct``, skipping over
           the ambiguity band.
        """
        if self._disabled:
            return []
        if self.has_pending_plan or self._is_moving:
            return self.on_stop()
        if self.in_tilt:
            intent = Intent(IntentKind.SLAT_STEP, direction=direction)
        else:
            intent = Intent(IntentKind.HEIGHT_STEP, direction=direction)
        belief = self.belief()
        movement = planner.plan(self._zone, belief, intent)
        if movement is None:
            self._log(f"step {direction} does nothing: {self._describe_step_limit(direction)}")
            return []
        return self._execute(belief, movement)

    def _describe_step_limit(self, direction: Direction) -> str:
        """
        Why a step has nowhere to go, for the log.
        """
        if self._position is None:
            return "the blind's position is unknown"
        if self.in_tilt:
            edge = "open" if direction == Direction.UP else "closed"
            return f"the slats are already at the {edge} edge"
        limit = "top" if direction == Direction.UP else "bottom"
        return f"the blind is already at the {limit} limit"

    # -- Position feedback -------------------------------------------------------------------------

    def on_real_position(self, position: int | None, is_moving: bool,
                         direction: Direction | None = None) -> list[Action]:
        """
        Consume controller position/motion feedback, advancing any plan in progress. A ``None``
        position means the cover became unavailable.
        """
        if self._disabled:
            return []

        was_moving = self._is_moving
        had_plan = self.has_pending_plan
        self._observe(position, is_moving, direction)

        actions: list[Action] = []
        if had_plan:
            actions = self._consume(self._executor.on_feedback(position, is_moving))
        elif (was_moving and not is_moving and position is not None and self._zone.in_band(position)
              and self._latch != Latch.UNKNOWN):
            # External motion ended in the band; it may have latched on the way.
            self._latch = Latch.UNKNOWN
            self._log("latch belief cleared: external motion ended inside the tilt band")
        return actions + self._publish_current()

    def on_settle_timer(self, position: int | None, is_moving: bool,
                        direction: Direction | None = None) -> list[Action]:
        """
        Consume a settle-timer firing, with the controller state read at the moment it fired.
        """
        if self._disabled or not self.has_pending_plan:
            return []
        self._observe(position, is_moving, direction)
        actions = self._consume(self._executor.on_timer(position, is_moving))
        return actions + self._publish_current()

    # -- Internals ---------------------------------------------------------------------------------

    def _run(self, intent: Intent) -> list[Action]:
        """
        Plan an intent from the current belief, check it, and start executing it.

        A plan in flight is replaced, planned from the belief as it will be after the
        interruption. An intent that plans to nothing leaves it alone.
        """
        if self._disabled:
            return []
        belief = self._belief_after_interrupt()
        movement = planner.plan(self._zone, belief, intent)
        if movement is None:
            return []
        return self._execute(belief, movement)

    def _execute(self, belief: Belief, movement: Plan) -> list[Action]:
        """
        Safety-check a planned movement and start executing it, replacing any plan in flight.
        """
        violation = planner.check_plan(self._zone, belief, movement)
        if violation is not None:
            return self._fail_invariant(violation)
        self._abandon_plan()
        actions = self._consume(self._executor.start(movement, self._position, self._is_moving))
        return actions + self._publish_current()

    def _consume(self, outcome: Outcome) -> list[Action]:
        """
        Apply an executor outcome to the latch belief and return its actions.
        """
        match outcome.status:
            case Status.COMPLETED:
                self._latch = outcome.plan.final_latch
            case Status.STALLED:
                self._degrade_latch(outcome.plan)
        return outcome.actions

    def _observe(self, position: int | None, is_moving: bool,
                 direction: Direction | None = None) -> None:
        """
        Fold a controller reading into the belief. Feedback can only degrade the latch belief.
        """
        if is_moving:
            self._controller_reports_motion = True
        if position is None:
            if self._position is not None:
                self._log("cover position unreadable; motion and latch beliefs cleared")
            self._position = None
            self._is_moving = False
            self._direction = None
            self._latch = Latch.UNKNOWN
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
        if not self._zone.in_band(position) and self._latch != Latch.UNLATCHED:
            self._latch = Latch.UNLATCHED
            self._log(f"latch belief cleared: {position} rests outside the tilt band")

    def _abandon_plan(self) -> None:
        """
        Drop any plan in flight, degrading the latch belief for the interruption.

        The caller emits its own timer action -- a stop cancels the timer, a replacement re-arms it.
        """
        outcome = self._executor.abandon()
        if outcome.status == Status.ABANDONED:
            self._degrade_latch(outcome.plan)

    def _belief_after_interrupt(self) -> Belief:
        """
        The belief a replacement plan must be derived from: as if the plan in flight were abandoned.
        """
        pending = self._executor.plan
        latch = self._latch if pending is None else self._degraded_latch(pending)
        return Belief(position=self._position, latch=latch, is_moving=self._is_moving)

    def _degrade_latch(self, movement: Plan) -> None:
        """
        Degrade the latch belief because ``movement`` was interrupted part-way.
        """
        degraded = self._degraded_latch(movement)
        if degraded != self._latch:
            self._log(f"latch belief {self._latch} -> {degraded}: {movement.kind} plan interrupted")
            self._latch = degraded

    def _degraded_latch(self, movement: Plan) -> Latch:
        """
        What the latch belief becomes if ``movement`` is abandoned where the blind is now.

        Interrupting a pure slat move keeps a confident latch; anything else may have stopped
        mid-crossing.
        """
        if self._latch == Latch.LATCHED and not planner.can_change_latch(self._zone, movement):
            return Latch.LATCHED
        if self._position is None or self._zone.in_band(self._position):
            return Latch.UNKNOWN
        return Latch.UNLATCHED

    def _publish_current(self) -> list[Action]:
        """
        Emit position, mode and motion if they differ from what was last emitted. The mode comes
        from :meth:`_belief_after_interrupt`, not from the plan's intended outcome.
        """
        display_latch = self._belief_after_interrupt().latch
        in_tilt = display_latch == Latch.LATCHED
        if self._position is None:
            state: _Published = (None, False, Motion.IDLE)
        else:
            state = (virtual_position(self._zone, display_latch,
                                      self._position), in_tilt, self._motion(in_tilt))
        if state == self._last_published:
            return []
        self._last_published = state
        return [
            Action(ActionKind.PUBLISH_STATE, position=state[0], in_tilt=state[1], motion=state[2])
        ]

    def _motion(self, in_tilt: bool) -> str:
        """
        What the blind is doing, on the published scale.

        The direction comes from the controller, else from the step being driven toward while a
        command for it may be outstanding. On the slat scale it inverts: a real descent opens the
        slats.
        """
        real = self._direction if self._is_moving else None
        step = self._executor.current_step if self._may_be_travelling() else None
        if real is None and step is not None and self._position is not None:
            commanded = step.command_position
            here = self._position
            if commanded != here:
                real = Direction.UP if commanded > here else Direction.DOWN
        if real is None:
            return Motion.IDLE
        rising = real == Direction.UP
        if in_tilt:
            rising = not rising
        return Motion.OPENING if rising else Motion.CLOSING

    def _fail_invariant(self, violation: str) -> list[Action]:
        """
        Refuse a plan that failed a safety check (a planner bug) and disable the blind.
        """
        self._log(f"refusing a plan that violates {violation}", level="ERROR")
        self._disabled = True
        self._executor.abandon()
        return [
            Action(ActionKind.CANCEL_SETTLE_TIMER),
            Action(
                ActionKind.NOTIFY, notify_kind=NotifyKind.INVARIANT,
                message=(f"was disabled by a failed safety check: {violation}. This is a bug in "
                         "the movement planner.")),
        ]
