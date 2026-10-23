"""
Movement planning for one Gradhermetic blind.

All movement sequences and latch-safety guards. ``plan()`` is a pure function of geometry, belief
and intent, returning the steps for :mod:`executor` to drive, or ``None`` for a no-op.

Two facts about the mechanism drive the design:

- The latch engages on a down-then-up motion across the lower zone edge and releases only by rising
  above the upper edge. Driving down while latched must never happen.
- Reported percentages are only trustworthy after referencing against the top limit, so every
  sequence that needs an exact zone-relative position starts with the open *command*.

:func:`check_plan` restates the safety rules as invariants checked on every plan before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from gradhermetic_cover_control.geometry import Zone

# -- Latch belief ----------------------------------------------------------------------------------

# The mechanism is known to be latched: established only by a completed enter sequence.
LATCH_LATCHED = "latched"
# The mechanism is known not to be latched: the blind rests clear of the band, or a plan that ends
# unlatched has just completed.
LATCH_UNLATCHED = "unlatched"
# The latch state is genuinely uncertain: startup, an interrupted sequence, or motion we did not
# command. Treated as "possibly latched" by every guard.
LATCH_UNKNOWN = "unknown"

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"

# -- Steps -----------------------------------------------------------------------------------------

# Reach exactly this reported position.
STEP_MOVE_TO = "move_to"
# Reach at least this reported position (the tilt exit).
STEP_RISE_TO_AT_LEAST = "rise_to_at_least"

# Which real-cover service carries a step. ``open``/``close`` send the commands rather than a
# position so the actuator drives against its own limit switch.
COMMAND_POSITION = "position"
COMMAND_OPEN = "open"
COMMAND_CLOSE = "close"

# -- Plans -----------------------------------------------------------------------------------------

PLAN_NORMAL = "normal"
PLAN_SLAT = "slat"
PLAN_ENTER = "enter"
PLAN_LEAVE = "leave"

# -- Intents ---------------------------------------------------------------------------------------

INTENT_OPEN = "open"
INTENT_CLOSE = "close"
INTENT_SET_POSITION = "set_position"
INTENT_ENTER_TILT = "enter_tilt"
INTENT_LEAVE_TILT = "leave_tilt"
INTENT_SLAT_STEP = "slat_step"
INTENT_HEIGHT_STEP = "height_step"
INTENT_LONG_PRESS = "long_press"


@dataclass(frozen=True)
class Belief:
    """
    What the app believes about the blind right now.
    """

    position: Optional[int]
    latch: str
    is_moving: bool = False

    @property
    def in_tilt(self) -> bool:
        """
        Whether slat control applies: only a confidently latched mechanism offers it.
        """
        return self.latch == LATCH_LATCHED

    @property
    def may_be_latched(self) -> bool:
        """
        Whether a descent has to release the latch first. Anything but a known release qualifies.
        """
        return self.latch != LATCH_UNLATCHED


@dataclass(frozen=True)
class Step:
    """
    One waypoint of a movement plan, with the predicate that decides when it is done.

    ``target`` is the satisfaction threshold. ``command_pct``, if set, is a different position to
    command; only the tilt exit uses it, to travel past its target to the top limit.
    """

    kind: str
    target: int
    command: str = COMMAND_POSITION
    command_pct: Optional[int] = None

    @property
    def command_position(self) -> int:
        """
        The real position to command for this step, which defaults to its satisfaction target.
        """
        if self.command_pct is None:
            return self.target
        return self.command_pct

    def satisfied_by(self, position: int) -> bool:
        """
        Whether a reported real position satisfies this step. The actuator reports the whole
        percent it reached, so ``move_to`` uses exact equality.
        """
        if self.kind == STEP_RISE_TO_AT_LEAST:
            return position >= self.target
        return position == self.target


@dataclass(frozen=True)
class Plan:
    """
    An ordered sequence of steps plus the latch belief to commit once they all complete.
    """

    kind: str
    steps: Tuple[Step, ...]
    final_latch: str


@dataclass(frozen=True)
class Intent:
    """
    What the user asked for. ``landing_virtual`` is where an enter sequence finishes (default: the
    closed edge).
    """

    kind: str
    virtual_pct: Optional[int] = None
    direction: Optional[str] = None
    cross_open_edge: bool = False
    landing_virtual: Optional[int] = None


def plan(zone: Zone, belief: Belief, intent: Intent) -> Optional[Plan]:
    """
    Compile an intent into a movement plan, or None when there is nothing to do.
    """
    if intent.kind == INTENT_OPEN:
        return _plan_open(zone, belief)
    if intent.kind == INTENT_CLOSE:
        return _plan_close(zone, belief)
    if intent.kind == INTENT_SET_POSITION:
        return _plan_set_position(zone, belief, intent.virtual_pct)
    if intent.kind == INTENT_ENTER_TILT:
        return _plan_enter_tilt(zone, intent.landing_virtual)
    if intent.kind == INTENT_LEAVE_TILT:
        return _plan_leave_tilt(zone, belief)
    if intent.kind == INTENT_SLAT_STEP:
        return _plan_slat_step(zone, belief, intent.direction, intent.cross_open_edge)
    if intent.kind == INTENT_HEIGHT_STEP:
        return _plan_height_step(zone, belief, intent.direction)
    if intent.kind == INTENT_LONG_PRESS:
        return _plan_long_press(belief, intent.direction)
    raise ValueError(f"unknown intent {intent.kind!r}")


def can_change_latch(zone: Zone, movement: Plan) -> bool:
    """
    Whether executing or interrupting this plan could engage or release the latch: true unless
    every target and commanded position stays inside the zone (pure slat rotation).
    """
    return any(not zone.in_zone(step.target) or not zone.in_zone(step.command_position)
               for step in movement.steps)


# -- Sequences -------------------------------------------------------------------------------------


def _plan_open(zone: Zone, belief: Belief) -> Plan:
    """
    Most light: slats perpendicular when latched, otherwise the blind fully open.
    """
    if belief.in_tilt:
        return _slat_plan(zone.lower)
    return Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 100, COMMAND_OPEN), ), LATCH_UNLATCHED)


def _plan_close(zone: Zone, belief: Belief) -> Plan:
    """
    Least light: slats parallel when latched, otherwise the blind fully closed.
    """
    if belief.in_tilt:
        return _slat_plan(zone.upper)
    return Plan(PLAN_NORMAL, _guard_descent(belief, (Step(STEP_MOVE_TO, 0, COMMAND_CLOSE), )),
                LATCH_UNLATCHED)


def _plan_set_position(zone: Zone, belief: Belief, virtual_pct: Optional[int]) -> Optional[Plan]:
    """
    Move to an absolute virtual position: a slat angle when latched, a height otherwise.
    """
    if virtual_pct is None:
        return None
    if belief.in_tilt:
        return _slat_plan(zone.virtual_to_real(virtual_pct))
    # Snapping keeps normal mode out of the band interior, where a rise would silently latch.
    return _height_move(zone, belief, zone.snap_normal_target(virtual_pct, belief.position))


def _plan_height_step(zone: Zone, belief: Belief, direction: Optional[str]) -> Optional[Plan]:
    """
    Nudge the blind's height by one ``height_step_pct`` while it is not latched. A step into the
    ambiguity band continues to the band edge ahead of it.
    """
    position = belief.position
    if belief.in_tilt or position is None:
        return None
    rising = direction == DIRECTION_UP
    target = position + zone.height_step_pct if rising else position - zone.height_step_pct
    target = zone.snap_step_target(target, rising)
    if target == position:
        return None
    return _height_move(zone, belief, target)


def _height_move(zone: Zone, belief: Belief, target: int) -> Plan:
    """
    One whole-height move to a target already snapped clear of the band interior.

    Commits ``UNLATCHED``, except for a position-commanded rise ending exactly on the release height
    that may have crossed the lower edge: with zero margin, a percent of calibration error could
    leave it latched, so it commits ``UNKNOWN``.
    """
    position = belief.position
    steps: Tuple[Step, ...] = (Step(STEP_MOVE_TO, target), )
    if position is None or target < position:
        steps = _guard_descent(belief, steps)
    if steps[0].command == COMMAND_OPEN:
        # Referenced against the top limit first: released by construction, wherever it ends.
        return Plan(PLAN_NORMAL, steps, LATCH_UNLATCHED)
    if position is not None and target < position:
        # A descent from a known release cannot latch.
        return Plan(PLAN_NORMAL, steps, LATCH_UNLATCHED)
    lands_on_release = target == zone.band_high
    provably_clear = (belief.latch == LATCH_UNLATCHED and position is not None
                      and position >= zone.lower)
    if lands_on_release and not provably_clear:
        return Plan(PLAN_NORMAL, steps, LATCH_UNKNOWN)
    return Plan(PLAN_NORMAL, steps, LATCH_UNLATCHED)


def _plan_enter_tilt(zone: Zone, landing_virtual: Optional[int] = None) -> Plan:
    """
    The latch sequence: open fully (reference), dip below the lower edge, rise to the upper edge
    (latches), then optionally move to the landing slat angle.
    """
    if landing_virtual is None:
        landing_virtual = 0
    steps = [
        Step(STEP_MOVE_TO, 100, COMMAND_OPEN),
        Step(STEP_MOVE_TO, zone.dip_target),
        Step(STEP_MOVE_TO, zone.upper),
    ]
    landing = zone.virtual_to_real(landing_virtual)
    if landing != zone.upper:
        steps.append(Step(STEP_MOVE_TO, landing))
    return Plan(PLAN_ENTER, tuple(steps), LATCH_LATCHED)


def _plan_leave_tilt(zone: Zone, belief: Belief) -> Optional[Plan]:
    """
    Release the latch by driving fully open; satisfied once the blind clears ``release_target``.

    Only planned from a LATCHED belief; an uncertain one is released by :func:`_guard_descent`.
    """
    if not belief.in_tilt:
        return None
    return Plan(PLAN_LEAVE,
                (Step(STEP_RISE_TO_AT_LEAST, zone.release_target, COMMAND_OPEN, command_pct=100), ),
                LATCH_UNLATCHED)


def _plan_slat_step(zone: Zone, belief: Belief, direction: Optional[str],
                    cross_open_edge: bool) -> Optional[Plan]:
    """
    Step the slats by ``tilt_step_pct`` of real travel, clamped to the zone. Up (more light) is
    real-downward.

    At the open edge an up step leaves tilt if ``cross_open_edge`` (KNX), else does nothing.
    """
    if not belief.in_tilt or belief.position is None:
        return None
    current = min(max(belief.position, zone.lower), zone.upper)
    if direction == DIRECTION_UP:
        if current == zone.lower:
            return _plan_leave_tilt(zone, belief) if cross_open_edge else None
        return _slat_plan(max(zone.lower, current - zone.tilt_step_pct))
    if current == zone.upper:
        return None
    return _slat_plan(min(zone.upper, current + zone.tilt_step_pct))


def _plan_long_press(belief: Belief, direction: Optional[str]) -> Plan:
    """
    Long wall-button press: jump to an extreme, releasing the latch first when descending.
    """
    if direction == DIRECTION_UP:
        return Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 100, COMMAND_OPEN), ), LATCH_UNLATCHED)
    return Plan(PLAN_NORMAL, _guard_descent(belief, (Step(STEP_MOVE_TO, 0, COMMAND_CLOSE), )),
                LATCH_UNLATCHED)


def _slat_plan(real_target: int) -> Plan:
    """
    A single in-zone move that changes slat angle without changing height.
    """
    return Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, real_target), ), LATCH_LATCHED)


def _guard_descent(belief: Belief, steps: Tuple[Step, ...]) -> Tuple[Step, ...]:
    """
    Prefix a descent with a full open unless the latch is known to be released.
    """
    if belief.may_be_latched:
        return (Step(STEP_MOVE_TO, 100, COMMAND_OPEN), ) + steps
    return steps


# -- Invariants ------------------------------------------------------------------------------------


def check_plan(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    Check a plan against the safety invariants, returning the first violation or None. A violation
    is a planner bug.
    """
    return (_check_normal_targets(zone, movement) or _check_slat_targets(zone, belief, movement)
            or _check_descents(zone, belief, movement) or _check_latching(zone, belief, movement)
            or _check_releases(zone, belief, movement)
            or _check_release_claims(zone, belief, movement))


def _check_normal_targets(zone: Zone, movement: Plan) -> Optional[str]:
    """
    N1: in normal mode no target or commanded position lies strictly inside the ambiguity band.
    """
    if movement.kind != PLAN_NORMAL:
        return None
    for step in movement.steps:
        if step.kind != STEP_MOVE_TO:
            continue
        for position in (step.target, step.command_position):
            if zone.band_low < position < zone.band_high:
                return f"N1: normal-mode target {position} lies inside the ambiguity band"
    return None


def _check_slat_targets(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    T1: slat moves stay within the zone and are only planned from a LATCHED belief.
    """
    if movement.kind != PLAN_SLAT:
        return None
    if belief.latch != LATCH_LATCHED:
        return f"T1: slat move planned while the latch belief is {belief.latch}"
    for step in movement.steps:
        for position in (step.target, step.command_position):
            if not zone.in_zone(position):
                return f"T1: slat target {position} lies outside the tilt zone"
    return None


def _check_descents(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    L1: a descent below the lower edge is preceded by a full open unless the latch is known clear.
    Tracks commanded positions, since that is where the blind travels.
    """
    if belief.latch == LATCH_UNLATCHED:
        return None
    position = belief.position
    for index, step in enumerate(movement.steps):
        if any(earlier.command == COMMAND_OPEN for earlier in movement.steps[:index]):
            break
        commanded = step.command_position
        descends = position is None or commanded < position
        if descends and commanded < zone.lower:
            return (f"L1: descent to {commanded} below the lower edge is not preceded by a "
                    "full open")
        position = commanded
    return None


def _check_latching(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    E1: the latch belief is only established by the canonical enter sequence.
    """
    if movement.final_latch != LATCH_LATCHED:
        return None
    if belief.latch != LATCH_LATCHED and movement.kind != PLAN_ENTER:
        return f"E1: {movement.kind} plan claims to latch from a {belief.latch} belief"
    if movement.kind != PLAN_ENTER:
        return None
    steps = movement.steps
    if len(steps) not in (3, 4) or any(step.kind != STEP_MOVE_TO for step in steps):
        return "E1: the enter sequence must be three or four move steps"
    if steps[0].command != COMMAND_OPEN or steps[0].target != 100:
        return "E1: the enter sequence must begin by driving fully open"
    if steps[1].target >= zone.lower:
        return f"E1: the enter dip to {steps[1].target} does not clear the lower edge"
    if steps[2].target != zone.upper:
        return f"E1: the latching rise must end at the upper edge, not {steps[2].target}"
    # The optional fourth step is the configured landing, which must stay inside the zone.
    if len(steps) == 4 and not zone.in_zone(steps[3].target):
        return (f"E1: the enter sequence may only continue to a target inside the tilt zone, not "
                f"{steps[3].target}")
    return None


def _check_releases(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    X1: leaving tilt is a single upward step that drives fully open, planned only from LATCHED.
    """
    if movement.kind == PLAN_LEAVE:
        if belief.latch != LATCH_LATCHED:
            return f"X1: the tilt exit requires a LATCHED belief, not {belief.latch}"
        if len(movement.steps) != 1 or movement.steps[0].kind != STEP_RISE_TO_AT_LEAST:
            return "X1: leaving tilt must be a single upward step"
        step = movement.steps[0]
        if step.target < zone.release_target:
            return f"X1: the tilt exit accepts {step.target}, below the release height"
        if step.command != COMMAND_OPEN or step.command_position != 100:
            return (f"X1: the tilt exit must drive fully open, not {step.command} "
                    f"{step.command_position}")
    return None


def _check_release_claims(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    R1: a normal plan commits ``UNLATCHED`` only if no position-commanded rise in it ends on the
    release height having possibly crossed the lower edge (see :func:`_height_move`).
    """
    if movement.kind != PLAN_NORMAL or movement.final_latch != LATCH_UNLATCHED:
        return None
    position = belief.position
    latch = belief.latch
    for step in movement.steps:
        if step.command == COMMAND_OPEN:
            return None
        commanded = step.command_position
        rises = position is None or commanded > position
        ends_on_release = commanded == zone.band_high
        provably_clear = (latch == LATCH_UNLATCHED and position is not None
                          and position >= zone.lower)
        if rises and ends_on_release and not provably_clear:
            return (f"R1: a rise to the release height {commanded} by position command cannot "
                    f"establish a release from {position} with the latch {latch}")
        position = commanded
    return None
