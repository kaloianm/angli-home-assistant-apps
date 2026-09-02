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
- ``is_moving`` -- whether the blind is travelling, from feedback.

Latch transitions, in full:

- to ``LATCHED``: a completed enter plan, and nothing else.
- to ``UNLATCHED``: a completed plan that ends released, or feedback placing the blind clearly
  outside the ambiguity band (a latched mechanism cannot rest there).
- to ``UNKNOWN``: startup with the position unknown or inside the band; an interrupted plan that
  could have crossed a zone edge; externally-caused motion ending inside the band; the cover
  becoming unavailable.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from gradhermetic_cover_control import planner
from gradhermetic_cover_control.executor import (
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_NOTIFY,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    NOTIFY_INVARIANT,
    STATUS_ABANDONED,
    STATUS_COMPLETED,
    STATUS_STALLED,
    Action,
    Executor,
    Outcome,
    virtual_position,
)
from gradhermetic_cover_control.geometry import Zone, clamp_pct
from gradhermetic_cover_control.planner import (
    INTENT_CLOSE,
    INTENT_ENTER_TILT,
    INTENT_ENTER_TOWARD_ZONE,
    INTENT_LEAVE_TILT,
    INTENT_LONG_PRESS,
    INTENT_OPEN,
    INTENT_RECOVER,
    INTENT_SET_POSITION,
    INTENT_SLAT_STEP,
    LATCH_LATCHED,
    LATCH_UNKNOWN,
    LATCH_UNLATCHED,
    NEAR_EDGE_CLOSED,
    Belief,
    Intent,
    Plan,
)


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
        self._executor = Executor(zone, log)

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

    def seed_state(self, last_position: Optional[float], is_moving: bool = False) -> None:
        """
        Establish the belief from a first position reading, before any events are processed.

        State is never persisted across restarts, so the latch belief starts from the position
        alone: clearly outside the band the mechanism cannot be latched, and anywhere else it is
        genuinely unknown.
        """
        self._position = last_position
        self._is_moving = bool(is_moving) and last_position is not None
        if last_position is not None and not self._zone.in_band(last_position):
            self._latch = LATCH_UNLATCHED
        else:
            self._latch = LATCH_UNKNOWN

    def on_startup(self, position: Optional[float], is_moving: bool = False) -> List[Action]:
        """
        Establish a known-safe state at startup, recovering upward when the latch is ambiguous.
        """
        if self._disabled:
            return []
        self.seed_state(position, is_moving)
        if self._latch == LATCH_UNKNOWN:
            if position is None:
                self._log("startup position unreadable; recovering fully open")
            else:
                self._log(f"startup position {position}% is inside the tilt band; recovering "
                          "fully open")
            return self.on_recover()
        self._log(f"startup position {position}% is outside the tilt band; resuming whole-height "
                  "control")
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
        """
        if self._disabled:
            return []
        self._abandon_plan()
        return [Action(ACTION_STOP), Action(ACTION_CANCEL_SETTLE_TIMER)]

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
        it lands somewhere the user cannot see it worked. A wall button keeps its own near-edge rule
        instead (see :meth:`on_knx_short`), since there the direction of the press says where to
        land.
        """
        if self._disabled:
            return []
        if enabled:
            if self.in_tilt:
                return []
            return self._run(
                Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_CLOSED,
                       landing_virtual=self._zone.enter_landing_virtual))
        return self._run(Intent(INTENT_LEAVE_TILT))

    # -- KNX wall-button events --------------------------------------------------------------------

    def on_knx_long(self, direction: str) -> List[Action]:
        """
        Handle a long wall-button press: jump to an extreme.

        Up drives fully open (leaving tilt naturally); down drives fully closed, releasing the latch
        upward first whenever it might be engaged.
        """
        return self._run(Intent(INTENT_LONG_PRESS, direction=direction))

    def on_knx_short(self, direction: str) -> List[Action]:
        """
        Handle a short wall-button press, in priority order: stop, else step slats, else enter the
        tilt zone when the press points toward it.

        This is the two-button KNX wall-switch control, which has no dedicated tilt button, so a
        short press must also get in and out of tilt: it crosses the zone boundaries (entering from
        outside when the press points toward the zone, leaving upward at the open edge). The
        dedicated slat-step helpers use :meth:`on_slat_step` instead, which never crosses.
        """
        if self._disabled:
            return []
        # 1. If the blind is moving, stop it (matches native KNX stop/step behavior).
        if self._is_moving:
            return self.on_stop()
        # 2. If latched in tilt, step the slats (leaving upward at the open edge).
        if self.in_tilt:
            return self._run(Intent(INTENT_SLAT_STEP, direction=direction, cross_open_edge=True))
        # 3. Idle and outside the zone: enter tilt when the press points toward the zone.
        return self._run(Intent(INTENT_ENTER_TOWARD_ZONE, direction=direction))

    def on_slat_step(self, direction: str) -> List[Action]:
        """
        Handle a press of a dedicated slat-step helper (the ``..._step_up`` / ``..._step_down``
        input_buttons).

        Two cases, decided by the latch belief:

        - **Latched** -- step the slats by one ``tilt_step_pct`` of real travel, clamping at both
          zone edges. This never leaves tilt, not even at the open edge where a wall button would
          (:meth:`on_knx_short`): the dashboard has a dedicated tilt control for that.
        - **Not latched** -- adopt the wall button's rule 3 and enter the tilt zone when the press
          points toward it (from above, a down press lands at the closed edge; from below, an up
          press lands at the open edge). Without this a dashboard-only blind -- one with no KNX wall
          switch -- would have no directional way into tilt at all, and the buttons would look
          broken. A press pointing away from the zone, or made while resting inside the band with no
          latch belief, still does nothing.

        A press is ignored outright while a plan is in flight or the blind is travelling, so it can
        never abort an enter/leave/step sequence.
        """
        if self._disabled:
            return []
        if self.has_pending_plan or self._is_moving:
            return []
        if self.in_tilt:
            return self._run(Intent(INTENT_SLAT_STEP, direction=direction))
        return self._run(Intent(INTENT_ENTER_TOWARD_ZONE, direction=direction))

    # -- Restart recovery --------------------------------------------------------------------------

    def on_recover(self) -> List[Action]:
        """
        Recover from an unknown/ambiguous position by driving fully open (upward-only).
        """
        return self._run(Intent(INTENT_RECOVER))

    # -- Position feedback -------------------------------------------------------------------------

    def on_real_position(self, position: Optional[float], is_moving: bool) -> List[Action]:
        """
        Consume controller position/motion feedback, advancing any plan in progress.

        A ``None`` position means the cover became unavailable: the motion belief is cleared and the
        latch belief degrades to unknown, so no later decision reasons from a stale position.
        """
        if self._disabled:
            return []

        was_moving = self._is_moving
        had_plan = self.has_pending_plan
        self._observe(position, is_moving)

        if had_plan:
            return self._consume(self._executor.on_feedback(position, is_moving))

        # No plan of our own: the real cover moved under external control.
        if was_moving and not is_moving and position is not None:
            if self._zone.in_band(position) and self._latch != LATCH_UNKNOWN:
                # We did not see how it got here, and a rise across the lower edge latches.
                self._latch = LATCH_UNKNOWN
                self._log("latch belief cleared: external motion ended inside the tilt band")
            return self._publish_current()
        return []

    def on_settle_timer(self, position: Optional[float], is_moving: bool) -> List[Action]:
        """
        Consume a settle-timer firing, with the controller state read at the moment it fired.

        The executor decides what it means: keep waiting through a long travel, accept an actuator
        that reported only its final state (or stopped a hair short), or declare a stall.
        """
        if self._disabled or not self.has_pending_plan:
            return []
        self._observe(position, is_moving)
        return self._consume(self._executor.on_timer(position, is_moving))

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
        return self._consume(self._executor.start(movement, self._position, self._is_moving))

    def _consume(self, outcome: Outcome) -> List[Action]:
        """
        Apply an executor outcome to the latch belief and return its actions.
        """
        if outcome.status == STATUS_COMPLETED:
            self._latch = outcome.plan.final_latch
        elif outcome.status == STATUS_STALLED:
            self._degrade_latch(outcome.plan)
        return outcome.actions

    def _observe(self, position: Optional[float], is_moving: bool) -> None:
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
            self._latch = LATCH_UNKNOWN
            return
        self._position = position
        self._is_moving = is_moving
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
        Emit the virtual position for where the blind rests now, if that is known.
        """
        virtual = self.current_virtual_position()
        if virtual is None:
            return []
        return [Action(ACTION_PUBLISH_POSITION, position=virtual)]

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
