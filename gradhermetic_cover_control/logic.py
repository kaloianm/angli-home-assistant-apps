"""
The state machine for one Gradhermetic cover.

This module holds the app's *belief* about the blind and routes events to the two pure modules that
do the thinking: :mod:`planner` compiles an intent into a movement plan, :mod:`executor` drives that
plan and decides every timing question. It has no AppDaemon or Home Assistant dependencies -- the
adapter feeds events in and performs the returned actions.

The belief has three parts:

- ``position`` -- the last real travel position reported by the controller, or ``None`` once the
  cover becomes unavailable. An unknown position makes every guard conservative automatically.
- ``latch`` -- ``LATCHED`` / ``UNLATCHED`` / ``UNKNOWN``, event-sourced rather than derived from the
  position, because a position inside the band is neither necessary nor sufficient for being
  latched. Only a completed enter sequence establishes ``LATCHED``; everything else can only
  degrade it.
- ``is_moving`` -- whether the blind is travelling, from feedback, and which way.

Latch transitions, in full:

- to ``LATCHED``: a completed enter plan, and nothing else.
- to ``UNLATCHED``: a completed plan that ends released, or feedback placing the blind clearly
  outside the ambiguity band (a latched mechanism cannot rest there).
- to ``UNKNOWN``: startup with the position unknown or inside the band; an interrupted plan that
  could have crossed a zone edge; externally-caused motion ending inside the band; the cover
  becoming unavailable; a position-commanded rise that ends exactly on the release height.

Publishing is this module's job too: after every event it emits what the virtual cover should show
-- position, mode and motion -- whenever that differs from what it last emitted. The mode shown is
the belief the app would hold if the plan in flight were interrupted right now, so an entry shows
the real height climbing to the top and back until the moment it actually latches, and an exit
shows height mode from the moment it starts.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from gradhermetic_cover_control import planner
from gradhermetic_cover_control.executor import (
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_NOTIFY,
    ACTION_PUBLISH_STATE,
    ACTION_STOP,
    MOTION_CLOSING,
    MOTION_IDLE,
    MOTION_OPENING,
    NOTIFY_INVARIANT,
    STATUS_ABANDONED,
    STATUS_COMPLETED,
    STATUS_STALLED,
    Action,
    Executor,
    Outcome,
    virtual_position,
)
from gradhermetic_cover_control.geometry import Zone, clamp_pct, to_command
from gradhermetic_cover_control.planner import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    INTENT_CLOSE,
    INTENT_ENTER_TILT,
    INTENT_HEIGHT_STEP,
    INTENT_LEAVE_TILT,
    INTENT_LONG_PRESS,
    INTENT_OPEN,
    INTENT_SET_POSITION,
    INTENT_SLAT_STEP,
    LATCH_LATCHED,
    LATCH_UNKNOWN,
    LATCH_UNLATCHED,
    NEAR_EDGE_CLOSED,
    PLAN_ENTER,
    PLAN_LEAVE,
    Belief,
    Intent,
    Plan,
)

# What one publish carries, for deduplication: (virtual position, in tilt, motion).
_Published = Tuple[Optional[float], bool, str]


class GradhermeticCoverLogic:
    """
    State machine translating user/KNX intent into blind movements for one Gradhermetic cover.
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

        ``zone`` holds the tilt-zone geometry. All runtime state is kept internally and every public
        event method returns declarative actions.

        ``log`` is called as ``log(message, level=...)``. Only decisions worth seeing in the normal
        log are logged at ``INFO``; the per-event/per-step trace explaining how a decision was
        reached goes to ``DEBUG``.
        """
        self._zone = zone
        self._log = log
        self._disabled = False

        self._position: Optional[float] = None
        self._latch = LATCH_UNKNOWN
        self._is_moving = False
        # Which way the blind is travelling in real terms, while it is; from the controller when
        # it says, else from the trend of its reported positions.
        self._direction: Optional[str] = None
        self._executor = Executor(zone, log)
        self._last_published: Optional[_Published] = None

    # -- Accessors ---------------------------------------------------------------------------------

    @property
    def last_position(self) -> Optional[float]:
        """
        Most recent real travel position known to the logic.
        """
        return self._position

    @property
    def latch(self) -> str:
        """
        The latch belief: ``LATCHED``, ``UNLATCHED`` or ``UNKNOWN``.
        """
        return self._latch

    @property
    def in_tilt(self) -> bool:
        """
        Whether slat control applies. Only a confident latch belief offers it.
        """
        return self._latch == LATCH_LATCHED

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

    def current_virtual_position(self) -> Optional[float]:
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

    def seed_state(self, last_position: Optional[float], is_moving: bool = False,
                   direction: Optional[str] = None) -> None:
        """
        Establish the belief from a first position reading, before any events are processed.

        State is never persisted across restarts, so the latch belief starts from the position
        alone: clearly outside the band the mechanism cannot be latched, and anywhere else it is
        genuinely unknown.
        """
        self._position = last_position
        self._is_moving = bool(is_moving) and last_position is not None
        self._direction = direction if self._is_moving else None
        if last_position is not None and not self._zone.in_band(last_position):
            self._latch = LATCH_UNLATCHED
        else:
            self._latch = LATCH_UNKNOWN

    def on_startup(self, position: Optional[float], is_moving: bool = False,
                   direction: Optional[str] = None) -> List[Action]:
        """
        Seed the belief from the first position reading. Startup never moves the blind.

        An unreadable or in-band position leaves the latch belief ``UNKNOWN``, and that is where it
        stays: re-referencing the actuator is deferred to the first action that actually needs a
        trusted position. Every such action already opens fully on its own -- every descent is
        guarded by one, every tilt entry begins with one, and opening is one -- so the reference is
        bought lazily, by the move that needs it, instead of raising the blind unprompted in the
        middle of the night after a power cut.
        """
        if self._disabled:
            return []
        self.seed_state(position, is_moving, direction)
        if self._latch == LATCH_UNKNOWN:
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

    def disable(self) -> List[Action]:
        """
        Permanently stop automation decisions for this blind until restart.
        """
        self._disabled = True
        self._executor.abandon()
        self._log("disabled")
        return []

    # -- User / command events ---------------------------------------------------------------------

    def on_open(self) -> List[Action]:
        """
        Handle ``cover.open_cover``: most light.

        Outside tilt this opens the blind fully; inside tilt it orients the slats perpendicular
        (virtual 100, the lower edge).
        """
        return self._run(Intent(INTENT_OPEN))

    def on_close(self) -> List[Action]:
        """
        Handle ``cover.close_cover``: least light.

        Outside tilt this closes the blind fully; inside tilt it orients the slats parallel
        (virtual 0, the upper edge).
        """
        return self._run(Intent(INTENT_CLOSE))

    def on_stop(self) -> List[Action]:
        """
        Handle ``cover.stop_cover``: abandon the current plan and stop travel.

        The stop command itself goes out only when there is something to stop -- a plan in flight
        or a blind reported moving. Every command reaches the actuator through this app, and on a
        KNX actuator without a dedicated stop object Home Assistant carries a stop on the step
        object, which nudges an idle blind instead of stopping it.
        """
        if self._disabled:
            return []
        stopping = self.has_pending_plan or self._is_moving
        self._abandon_plan()
        actions = [Action(ACTION_STOP)] if stopping else []
        actions.append(Action(ACTION_CANCEL_SETTLE_TIMER))
        return actions + self._publish_current()

    def on_set_position(self, virtual_pct: float) -> List[Action]:
        """
        Handle ``cover.set_cover_position`` to an absolute virtual position.

        Outside tilt the virtual position is the real position, snapped clear of the ambiguity band;
        inside tilt it interpolates the slat angle between the zone edges.
        """
        return self._run(Intent(INTENT_SET_POSITION, virtual_pct=clamp_pct(virtual_pct)))

    def on_set_tilt_mode(self, enabled: bool) -> List[Action]:
        """
        Handle the custom ``set_tilt_mode`` service.

        Entering latches the mechanism and finishes at the configured ``tilt_enter_landing_pct``
        slat angle -- a real travel position inside the zone, which :class:`Zone` converts to the
        virtual scale the enter intent speaks; leaving disengages it upward. Both are no-ops when
        the blind is already in the requested mode.

        The landing exists because the latching rise necessarily ends at the closed edge, where some
        blinds show no visible slat opening at all -- a deliberate "enter tilt" is worth nothing if
        it lands somewhere the user cannot see it worked.

        A request for the mode already being entered or left is a no-op rather than a restart:
        repeating it would replan the sequence from scratch on every press, each one a real-cover
        command counted against the rate limit. A request to leave while an entry is in flight
        stops the entry, which is the nearest thing to what was asked.
        """
        if self._disabled:
            return []
        pending = self._executor.plan
        if enabled:
            if self.in_tilt or (pending is not None and pending.kind == PLAN_ENTER):
                return []
            return self._run(
                Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_CLOSED,
                       landing_virtual=self._zone.enter_landing_virtual))
        if pending is not None and pending.kind == PLAN_LEAVE:
            return []
        if pending is not None and pending.kind == PLAN_ENTER:
            return self.on_stop()
        return self._run(Intent(INTENT_LEAVE_TILT))

    def on_toggle_tilt_mode(self) -> List[Action]:
        """
        Toggle tilt mode from a control that has no state of its own: the dashboard tilt helper,
        or the KNX tilt address.

        While an entry or an exit is in flight the toggle cancels it, whatever the belief happens
        to read mid-sequence. A toggle read off the belief alone would instead restart the very
        sequence the user is tapping at, once per tap.
        """
        if self._disabled:
            return []
        pending = self._executor.plan
        if pending is not None and pending.kind in (PLAN_ENTER, PLAN_LEAVE):
            return self.on_stop()
        return self.on_set_tilt_mode(not self.in_tilt)

    # -- Step events -------------------------------------------------------------------------------

    def on_knx_long(self, direction: str) -> List[Action]:
        """
        Handle a long wall-button press: jump to an extreme.

        Up drives fully open (leaving tilt naturally); down drives fully closed, releasing the latch
        upward first whenever it might be engaged.
        """
        return self._run(Intent(INTENT_LONG_PRESS, direction=direction))

    def on_knx_short(self, direction: str) -> List[Action]:
        """
        Handle a short wall-button press: the step rule of :meth:`on_step`, plus one thing only a
        two-button wall switch needs -- an up step at the open slat edge leaves tilt upward.
        """
        return self._step(direction, cross_open_edge=True)

    def on_step(self, direction: str) -> List[Action]:
        """
        Handle a press of a dashboard step helper (the ``..._step_up`` / ``..._step_down``
        input_buttons): stop, else step slats, else step height.
        """
        return self._step(direction, cross_open_edge=False)

    def _step(self, direction: str, cross_open_edge: bool) -> List[Action]:
        """
        The step rule every step control shares, in priority order:

        1. If anything is moving -- a plan in flight or the blind reported travelling -- stop it.
           This is what a KNX stop/step object does, and what a step press means mid-move.
        2. If latched, step the slats by one ``tilt_step_pct`` of real travel, clamping at the
           closed edge always and at the open edge unless ``cross_open_edge``.
        3. Otherwise step the height by one ``height_step_pct``, skipping the ambiguity band in the
           direction of travel. A step down while the latch might be engaged pays the full-open
           release first, like every other descent.

        A press that plans nothing -- at a travel limit, at a slat edge, with no position -- says
        so in the log instead of vanishing.
        """
        if self._disabled:
            return []
        if self.has_pending_plan or self._is_moving:
            return self.on_stop()
        if self.in_tilt:
            intent = Intent(INTENT_SLAT_STEP, direction=direction, cross_open_edge=cross_open_edge)
        else:
            intent = Intent(INTENT_HEIGHT_STEP, direction=direction)
        if planner.plan(self._zone, self.belief(), intent) is None:
            self._log(f"step {direction} does nothing: {self._describe_step_limit(direction)}")
            return []
        return self._run(intent)

    def _describe_step_limit(self, direction: str) -> str:
        """
        Why a step press has nowhere to go, for the log.
        """
        if self._position is None:
            return "the blind's position is unknown"
        if self.in_tilt:
            edge = "open" if direction == DIRECTION_UP else "closed"
            return f"the slats are already at the {edge} edge"
        limit = "top" if direction == DIRECTION_UP else "bottom"
        return f"the blind is already at the {limit} limit"

    # -- Position feedback -------------------------------------------------------------------------

    def on_real_position(self, position: Optional[float], is_moving: bool,
                         direction: Optional[str] = None) -> List[Action]:
        """
        Consume controller position/motion feedback, advancing any plan in progress.

        ``direction`` is the real direction of travel when the controller reports one; without it
        the direction is read off the trend of the reported positions.

        A ``None`` position means the cover became unavailable: the motion belief is cleared and the
        latch belief degrades to unknown, so no later decision reasons from a stale position.

        Every reading ends in a publish of whatever changed -- position, mode or motion -- which is
        what makes the virtual cover show travel as it happens rather than only where it ended.
        """
        if self._disabled:
            return []

        was_moving = self._is_moving
        had_plan = self.has_pending_plan
        self._observe(position, is_moving, direction)

        actions: List[Action] = []
        if had_plan:
            actions = self._consume(self._executor.on_feedback(position, is_moving))
        elif (was_moving and not is_moving and position is not None
              and self._zone.in_band(position) and self._latch != LATCH_UNKNOWN):
            # The real cover moved under external control and we did not see how it got here; a
            # rise across the lower edge latches.
            self._latch = LATCH_UNKNOWN
            self._log("latch belief cleared: external motion ended inside the tilt band")
        return actions + self._publish_current()

    def on_settle_timer(self, position: Optional[float], is_moving: bool) -> List[Action]:
        """
        Consume a settle-timer firing, with the controller state read at the moment it fired.

        The executor decides what it means: keep waiting through a long travel, accept an actuator
        that reported only its final state (or stopped a hair short), or declare a stall.
        """
        if self._disabled or not self.has_pending_plan:
            return []
        self._observe(position, is_moving)
        actions = self._consume(self._executor.on_timer(position, is_moving))
        return actions + self._publish_current()

    # -- Internals ---------------------------------------------------------------------------------

    def _run(self, intent: Intent) -> List[Action]:
        """
        Plan an intent from the current belief, check it, and start executing it.

        A plan already in flight is replaced rather than queued: the replacement is derived from the
        belief as it will be *after* the interruption, so it re-derives every safety guard. An
        intent that plans to nothing leaves any in-flight plan alone.
        """
        if self._disabled:
            return []
        belief = self._belief_after_interrupt()
        movement = planner.plan(self._zone, belief, intent)
        if movement is None:
            return []
        violation = planner.check_plan(self._zone, belief, movement)
        if violation is not None:
            return self._fail_invariant(violation)
        self._abandon_plan()
        actions = self._consume(self._executor.start(movement, self._position, self._is_moving))
        return actions + self._publish_current()

    def _consume(self, outcome: Outcome) -> List[Action]:
        """
        Apply an executor outcome to the latch belief and return its actions.
        """
        if outcome.status == STATUS_COMPLETED:
            self._latch = outcome.plan.final_latch
        elif outcome.status == STATUS_STALLED:
            self._degrade_latch(outcome.plan)
        return outcome.actions

    def _observe(self, position: Optional[float], is_moving: bool,
                 direction: Optional[str] = None) -> None:
        """
        Fold a controller reading into the belief.

        Feedback can only ever degrade the latch belief: a blind resting clear of the band cannot be
        latched, and an unreadable position means we no longer know anything about it.
        """
        if position is None:
            if self._position is not None:
                self._log("cover position unreadable; motion and latch beliefs cleared")
            self._position = None
            self._is_moving = False
            self._direction = None
            self._latch = LATCH_UNKNOWN
            return
        previous = self._position
        self._position = position
        self._is_moving = is_moving
        if not is_moving:
            self._direction = None
        elif direction is not None:
            self._direction = direction
        elif previous is not None and to_command(position) != to_command(previous):
            self._direction = DIRECTION_UP if position > previous else DIRECTION_DOWN
        if not self._zone.in_band(position) and self._latch != LATCH_UNLATCHED:
            self._latch = LATCH_UNLATCHED
            self._log(f"latch belief cleared: {position} rests outside the tilt band")

    def _abandon_plan(self) -> None:
        """
        Drop any plan in flight, degrading the latch belief for the interruption.

        The caller emits its own timer action -- a stop cancels the timer, a replacement re-arms it.
        """
        outcome = self._executor.abandon()
        if outcome.status == STATUS_ABANDONED:
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

    def _degraded_latch(self, movement: Plan) -> str:
        """
        What the latch belief becomes if ``movement`` is abandoned where the blind is now.

        A plan whose every target lies inside the zone is pure slat rotation and cannot have changed
        anything, so a confident belief survives its interruption -- otherwise stopping a slat move
        would drop the blind out of tilt mode. Any other plan may have been interrupted
        mid-crossing, which is exactly the case the latch invariant exists for.
        """
        if self._latch == LATCH_LATCHED and not planner.can_change_latch(self._zone, movement):
            return LATCH_LATCHED
        if self._position is None or self._zone.in_band(self._position):
            return LATCH_UNKNOWN
        return LATCH_UNLATCHED

    def _publish_current(self) -> List[Action]:
        """
        Emit what the virtual cover shows now, if it differs from what was last emitted.

        Three things travel together: the position, the scale it is on, and the motion. The scale
        is decided by the belief the app would hold if the plan in flight were interrupted this
        instant (:meth:`_belief_after_interrupt`), never by the plan's hoped-for outcome -- so the
        mode flag claims slat control only while the app would stand behind that claim. A
        pure slat move keeps it; an entry earns it on completion; an exit drops it at once.

        An unknown position is published as such (``position=None``) rather than left stale.
        """
        display_latch = self._belief_after_interrupt().latch
        in_tilt = display_latch == LATCH_LATCHED
        if self._position is None:
            state: _Published = (None, False, MOTION_IDLE)
        else:
            state = (virtual_position(self._zone, display_latch, self._position), in_tilt,
                     self._motion(in_tilt))
        if state == self._last_published:
            return []
        self._last_published = state
        return [Action(ACTION_PUBLISH_STATE, position=state[0], in_tilt=state[1],
                       motion=state[2])]

    def _motion(self, in_tilt: bool) -> str:
        """
        What the blind is doing, on the scale the published position is on.

        The real direction comes from the controller while it reports motion, else from the step
        being driven toward (a command has gone out, so the blind is about to move that way). On
        the slat scale the sense inverts: a real descent opens the slats.
        """
        real = self._direction if self._is_moving else None
        step = self._executor.current_step
        if real is None and step is not None and self._position is not None:
            commanded = to_command(step.command_position)
            here = to_command(self._position)
            if commanded != here:
                real = DIRECTION_UP if commanded > here else DIRECTION_DOWN
        if real is None:
            return MOTION_IDLE
        rising = real == DIRECTION_UP
        if in_tilt:
            rising = not rising
        return MOTION_OPENING if rising else MOTION_CLOSING

    def _fail_invariant(self, violation: str) -> List[Action]:
        """
        Refuse a plan that failed a safety check, and disable the blind.

        Unreachable by construction -- the planner is written to satisfy every invariant and the
        model tests prove it does -- so reaching here means a planner bug, and the safe response is
        to stop deciding anything for this blind until a human looks at it.
        """
        self._log(f"refusing a plan that violates {violation}", level="ERROR")
        self._disabled = True
        self._executor.abandon()
        return [
            Action(ACTION_CANCEL_SETTLE_TIMER),
            Action(ACTION_NOTIFY, notify_kind=NOTIFY_INVARIANT,
                   message=(f"was disabled by a failed safety check: {violation}. This is a bug in "
                            "the movement planner.")),
        ]
