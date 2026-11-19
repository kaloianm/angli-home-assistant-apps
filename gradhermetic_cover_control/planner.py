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
from enum import StrEnum

from gradhermetic_cover_control.cover_types import CommandKind, Direction
from gradhermetic_cover_control.geometry import Zone

# -- Latch belief ----------------------------------------------------------------------------------


class Latch(StrEnum):
    """
    What is believed about the blind's latch mechanism.
    """

    # The mechanism is known to be latched: established only by a completed enter sequence.
    LATCHED = "latched"
    # The mechanism is known not to be latched: the blind rests clear of the band, or a plan that
    # ends unlatched has just completed.
    UNLATCHED = "unlatched"
    # The latch state is genuinely uncertain: startup, an interrupted sequence, or motion we did
    # not command. Treated as "possibly latched" by every guard.
    UNKNOWN = "unknown"


# -- Steps -----------------------------------------------------------------------------------------


class StepKind(StrEnum):
    # Reach exactly this reported position.
    MOVE_TO = "move_to"
    # Reach at least this reported position (the tilt exit).
    RISE_TO_AT_LEAST = "rise_to_at_least"


# -- Plans -----------------------------------------------------------------------------------------


class PlanKind(StrEnum):
    NORMAL = "normal"
    SLAT = "slat"
    ENTER = "enter"
    LEAVE = "leave"


# -- Intents ---------------------------------------------------------------------------------------


class IntentKind(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    SET_POSITION = "set_position"
    ENTER_TILT = "enter_tilt"
    LEAVE_TILT = "leave_tilt"
    SLAT_STEP = "slat_step"
    HEIGHT_STEP = "height_step"


@dataclass(frozen=True)
class Belief:
    """
    What the app believes about the blind right now.
    """

    position: int | None
    latch: Latch

    @property
    def in_tilt(self) -> bool:
        """
        Whether slat control applies: only a confidently latched mechanism offers it.
        """
        return self.latch == Latch.LATCHED

    @property
    def may_be_latched(self) -> bool:
        """
        Whether a descent has to release the latch first. Anything but a known release qualifies.
        """
        return self.latch != Latch.UNLATCHED


@dataclass(frozen=True)
class Step:
    """
    One waypoint of a movement plan, with the predicate that decides when it is done.

    ``target`` is the satisfaction threshold. ``command_pct``, if set, is a different position to
    command; only the tilt exit uses it, to travel past its target to the top limit.
    """

    kind: StepKind
    target: int
    command: CommandKind = CommandKind.POSITION
    command_pct: int | None = None

    @property
    def command_position(self) -> int:
        """
        The real position to command for this step, which defaults to its satisfaction target.
        """
        if self.command_pct is None:
            return self.target
        return self.command_pct


@dataclass(frozen=True)
class Plan:
    """
    An ordered sequence of steps plus the latch belief to commit once they all complete.
    """

    kind: PlanKind
    steps: tuple[Step, ...]
    final_latch: Latch


@dataclass(frozen=True)
class Intent:
    """
    What the user asked for. ``landing_virtual`` is where an enter sequence finishes (default: slat
    0%, the entry landing).
    """

    kind: IntentKind
    virtual_pct: int | None = None
    direction: Direction | None = None
    landing_virtual: int | None = None


def plan(zone: Zone, belief: Belief, intent: Intent) -> Plan | None:
    """
    Compile an intent into a movement plan, or None when there is nothing to do.
    """
    match intent.kind:
        case IntentKind.OPEN:
            return _plan_open(zone, belief)
        case IntentKind.CLOSE:
            return _plan_close(zone, belief)
        case IntentKind.SET_POSITION:
            return _plan_set_position(zone, belief, intent.virtual_pct)
        case IntentKind.ENTER_TILT:
            return _plan_enter_tilt(zone, intent.landing_virtual)
        case IntentKind.LEAVE_TILT:
            return _plan_leave_tilt(zone, belief)
        case IntentKind.SLAT_STEP:
            return _plan_slat_step(zone, belief, intent.direction)
        case IntentKind.HEIGHT_STEP:
            return _plan_height_step(zone, belief, intent.direction)
        case _:
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
    return Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 100, CommandKind.OPEN), ), Latch.UNLATCHED)


def _plan_close(zone: Zone, belief: Belief) -> Plan:
    """
    Least light: slats at the entry landing when latched, otherwise the blind fully closed.
    """
    if belief.in_tilt:
        return _slat_plan(zone.enter_landing_real)
    return Plan(PlanKind.NORMAL,
                _guard_descent(belief, (Step(StepKind.MOVE_TO, 0, CommandKind.CLOSE), )),
                Latch.UNLATCHED)


def _plan_set_position(zone: Zone, belief: Belief, virtual_pct: int | None) -> Plan | None:
    """
    Move to an absolute virtual position: a slat angle when latched, a height otherwise.
    """
    if virtual_pct is None:
        return None
    if belief.in_tilt:
        return _slat_plan(zone.virtual_to_real(virtual_pct))
    # Snapping keeps normal mode out of the band interior, where a rise would silently latch.
    return _height_move(zone, belief, zone.snap_normal_target(virtual_pct, belief.position))


def _plan_height_step(zone: Zone, belief: Belief, direction: Direction | None) -> Plan | None:
    """
    Move the blind's height to the next multiple of ``height_step_pct`` in the step's direction
    while it is not latched. A step into the ambiguity band continues to the band edge ahead of it.
    """
    position = belief.position
    if belief.in_tilt or position is None:
        return None
    rising = direction == Direction.UP
    grid = zone.height_step_pct
    target = (position // grid + 1) * grid if rising else (position - 1) // grid * grid
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
    steps: tuple[Step, ...] = (Step(StepKind.MOVE_TO, target), )
    if position is None or target < position:
        steps = _guard_descent(belief, steps)
    if steps[0].command == CommandKind.OPEN:
        # Referenced against the top limit first: released by construction, wherever it ends.
        return Plan(PlanKind.NORMAL, steps, Latch.UNLATCHED)
    if position is not None and target < position:
        # A descent from a known release cannot latch.
        return Plan(PlanKind.NORMAL, steps, Latch.UNLATCHED)
    lands_on_release = target == zone.band_high
    provably_clear = (belief.latch == Latch.UNLATCHED and position is not None
                      and position >= zone.lower)
    if lands_on_release and not provably_clear:
        return Plan(PlanKind.NORMAL, steps, Latch.UNKNOWN)
    return Plan(PlanKind.NORMAL, steps, Latch.UNLATCHED)


def _plan_enter_tilt(zone: Zone, landing_virtual: int | None = None) -> Plan:
    """
    The latch sequence: open fully (reference), dip below the lower edge, rise to the upper edge
    (latches), then optionally move to the landing slat angle.
    """
    if landing_virtual is None:
        landing_virtual = 0
    steps = [
        Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
        Step(StepKind.MOVE_TO, zone.dip_target),
        Step(StepKind.MOVE_TO, zone.upper),
    ]
    landing = zone.virtual_to_real(landing_virtual)
    if landing != zone.upper:
        steps.append(Step(StepKind.MOVE_TO, landing))
    return Plan(PlanKind.ENTER, tuple(steps), Latch.LATCHED)


def _plan_leave_tilt(zone: Zone, belief: Belief) -> Plan | None:
    """
    Release the latch by driving fully open; satisfied once the blind clears ``release_target``.

    Only planned from a LATCHED belief; an uncertain one is released by :func:`_guard_descent`.
    """
    if not belief.in_tilt:
        return None
    return Plan(
        PlanKind.LEAVE,
        (Step(StepKind.RISE_TO_AT_LEAST, zone.release_target, CommandKind.OPEN, command_pct=100), ),
        Latch.UNLATCHED)


def _plan_slat_step(zone: Zone, belief: Belief, direction: Direction | None) -> Plan | None:
    """
    Move the slats to the next slat position in the step's direction. Up (more light) is
    real-downward. A step past either end of the slat window does nothing.
    """
    position = belief.position
    if not belief.in_tilt or position is None:
        return None
    if direction == Direction.UP:
        ahead = [real for real in zone.slat_positions if real < position]
        return _slat_plan(max(ahead)) if ahead else None
    ahead = [real for real in zone.slat_positions if real > position]
    return _slat_plan(min(ahead)) if ahead else None


def _slat_plan(real_target: int) -> Plan:
    """
    A single in-zone move that changes slat angle without changing height.
    """
    return Plan(PlanKind.SLAT, (Step(StepKind.MOVE_TO, real_target), ), Latch.LATCHED)


def _guard_descent(belief: Belief, steps: tuple[Step, ...]) -> tuple[Step, ...]:
    """
    Prefix a descent with a full open unless the latch is known to be released.
    """
    if belief.may_be_latched:
        return (Step(StepKind.MOVE_TO, 100, CommandKind.OPEN), ) + steps
    return steps


# -- Invariants ------------------------------------------------------------------------------------


def check_plan(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    Check a plan against the safety invariants, returning the first violation or None. A violation
    is a planner bug.
    """
    return (_check_normal_targets(zone, movement) or _check_slat_targets(zone, belief, movement)
            or _check_descents(zone, belief, movement) or _check_latching(zone, belief, movement)
            or _check_releases(zone, belief, movement)
            or _check_release_claims(zone, belief, movement))


def _check_normal_targets(zone: Zone, movement: Plan) -> str | None:
    """
    N1: in normal mode no target or commanded position lies strictly inside the ambiguity band.
    """
    if movement.kind != PlanKind.NORMAL:
        return None
    for step in movement.steps:
        if step.kind != StepKind.MOVE_TO:
            continue
        for position in (step.target, step.command_position):
            if zone.band_low < position < zone.band_high:
                return f"N1: normal-mode target {position} lies inside the ambiguity band"
    return None


def _check_slat_targets(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    T1: slat moves stay within the zone and are only planned from a LATCHED belief.
    """
    if movement.kind != PlanKind.SLAT:
        return None
    if belief.latch != Latch.LATCHED:
        return f"T1: slat move planned while the latch belief is {belief.latch}"
    for step in movement.steps:
        for position in (step.target, step.command_position):
            if not zone.in_zone(position):
                return f"T1: slat target {position} lies outside the tilt zone"
    return None


def _check_descents(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    L1: a descent below the lower edge is preceded by a full open unless the latch is known clear.
    Tracks commanded positions, since that is where the blind travels.
    """
    if belief.latch == Latch.UNLATCHED:
        return None
    position = belief.position
    for index, step in enumerate(movement.steps):
        if any(earlier.command == CommandKind.OPEN for earlier in movement.steps[:index]):
            break
        commanded = step.command_position
        descends = position is None or commanded < position
        if descends and commanded < zone.lower:
            return (f"L1: descent to {commanded} below the lower edge is not preceded by a "
                    "full open")
        position = commanded
    return None


def _check_latching(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    E1: the latch belief is only established by the canonical enter sequence.
    """
    if movement.final_latch != Latch.LATCHED:
        return None
    if belief.latch != Latch.LATCHED and movement.kind != PlanKind.ENTER:
        return f"E1: {movement.kind} plan claims to latch from a {belief.latch} belief"
    if movement.kind != PlanKind.ENTER:
        return None
    steps = movement.steps
    if len(steps) not in (3, 4) or any(step.kind != StepKind.MOVE_TO for step in steps):
        return "E1: the enter sequence must be three or four move steps"
    if steps[0].command != CommandKind.OPEN or steps[0].target != 100:
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


def _check_releases(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    X1: leaving tilt is a single upward step that drives fully open, planned only from LATCHED.
    """
    if movement.kind == PlanKind.LEAVE:
        if belief.latch != Latch.LATCHED:
            return f"X1: the tilt exit requires a LATCHED belief, not {belief.latch}"
        if len(movement.steps) != 1 or movement.steps[0].kind != StepKind.RISE_TO_AT_LEAST:
            return "X1: leaving tilt must be a single upward step"
        step = movement.steps[0]
        if step.target < zone.release_target:
            return f"X1: the tilt exit accepts {step.target}, below the release height"
        if step.command != CommandKind.OPEN or step.command_position != 100:
            return (f"X1: the tilt exit must drive fully open, not {step.command} "
                    f"{step.command_position}")
    return None


def _check_release_claims(zone: Zone, belief: Belief, movement: Plan) -> str | None:
    """
    R1: a normal plan commits ``UNLATCHED`` only if no position-commanded rise in it ends on the
    release height having possibly crossed the lower edge (see :func:`_height_move`).
    """
    if movement.kind != PlanKind.NORMAL or movement.final_latch != Latch.UNLATCHED:
        return None
    position = belief.position
    latch = belief.latch
    for step in movement.steps:
        if step.command == CommandKind.OPEN:
            return None
        commanded = step.command_position
        rises = position is None or commanded > position
        ends_on_release = commanded == zone.band_high
        provably_clear = (latch == Latch.UNLATCHED and position is not None
                          and position >= zone.lower)
        if rises and ends_on_release and not provably_clear:
            return (f"R1: a rise to the release height {commanded} by position command cannot "
                    f"establish a release from {position} with the latch {latch}")
        position = commanded
    return None
