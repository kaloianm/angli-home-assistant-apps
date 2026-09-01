"""
Movement planning for one Gradhermetic blind.

Every movement sequence and every latch-safety guard lives here and only here. Planning is a pure
function of the geometry, the current belief, and the user's intent: ``plan()`` returns an ordered
list of steps for :mod:`executor` to drive, or ``None`` when the intent is a no-op.

Two facts about the mechanism drive the whole design:

- The latch engages on a genuine down-then-up motion across the lower zone edge, and releases only
  by rising above the upper edge. Driving downward while latched is what the mechanism must never
  be asked to do.
- The actuator's reported percentage can only be trusted when the move is referenced from the top
  limit. So every sequence that must land on an exact percentage relative to the zone -- the enter
  sequence, and any latch release from an uncertain state -- begins by driving fully open with the
  open *command*, which re-references the actuator against its own limit switch.

The one exception is the tilt exit from a confidently ``LATCHED`` belief: that belief can only have
been established by an enter sequence that just re-referenced the actuator, with nothing but small
in-zone slat moves since, so the cheap rise to the zone's release target is trustworthy there.

:func:`check_plan` restates the safety argument as executable invariants and runs on every plan
before it is executed. It should be unreachable; the exhaustive tests exist to prove it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from gradhermetic_cover_control.geometry import Zone, clamp_pct, to_command

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

NEAR_EDGE_OPEN = "open"
NEAR_EDGE_CLOSED = "closed"

# -- Steps -----------------------------------------------------------------------------------------

# Reach exactly this reported position.
STEP_MOVE_TO = "move_to"
# Reach at least this reported position. Used for the tilt exit, where anything above the release
# target is equally good and the actuator may report a whole percent past it.
STEP_RISE_TO_AT_LEAST = "rise_to_at_least"

# Which real-cover service carries a step. ``open``/``close`` send the commands rather than a
# position so the actuator drives against its own limit switch.
COMMAND_POSITION = "position"
COMMAND_OPEN = "open"
COMMAND_CLOSE = "close"

# How far above its acceptance threshold the tilt exit is commanded, in real travel percent.
#
# A position command is only satisfied to within the actuator's own settling accuracy, so commanding
# exactly the release target lets a blind that stops a percent low report success while the
# mechanism is still physically latched. Commanding higher than the threshold makes the shortfall
# the actuator is allowed the app's margin rather than its risk.
#
# This is deliberately equal to ``executor.DEVIATION_TOLERANCE_PCT`` -- the amount the executor is
# willing to forgive a ``MoveTo`` for -- so an actuator that settles anywhere within tolerance of
# the commanded position still satisfies the ``>=`` predicate on its own, without the settle timer
# having to forgive anything (it does not forgive rise steps at all). The constant lives here rather
# than being imported, because the planner must not depend on the executor; if the executor's
# tolerance is ever widened, widen this with it.
EXIT_OVERSHOOT_PCT = 2.0

# -- Plans -----------------------------------------------------------------------------------------

PLAN_NORMAL = "normal"
PLAN_SLAT = "slat"
PLAN_ENTER = "enter"
PLAN_LEAVE = "leave"
PLAN_RECOVER = "recover"

# -- Intents ---------------------------------------------------------------------------------------

INTENT_OPEN = "open"
INTENT_CLOSE = "close"
INTENT_SET_POSITION = "set_position"
INTENT_ENTER_TILT = "enter_tilt"
INTENT_LEAVE_TILT = "leave_tilt"
INTENT_SLAT_STEP = "slat_step"
INTENT_ENTER_TOWARD_ZONE = "enter_toward_zone"
INTENT_LONG_PRESS = "long_press"
INTENT_RECOVER = "recover"

# Guard for floating-point edge comparisons on the virtual scale.
_VIRTUAL_EPSILON = 1e-6


@dataclass(frozen=True)
class Belief:
    """
    What the app believes about the blind right now.
    """

    position: Optional[float]
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

    ``target`` is the *satisfaction* threshold; ``command_pct`` optionally names a different
    position to actually command. They differ only where commanding exactly the threshold would let
    the actuator's settling accuracy leave the physical move short of what the mechanism needs --
    the tilt exit. When it is ``None`` the target is commanded, which is the ordinary case.
    """

    kind: str
    target: float
    command: str = COMMAND_POSITION
    command_pct: Optional[float] = None

    @property
    def command_position(self) -> float:
        """
        The real position to command for this step, which defaults to its satisfaction target.
        """
        if self.command_pct is None:
            return self.target
        return self.command_pct

    def satisfied_by(self, position: float) -> bool:
        """
        Whether a reported real position satisfies this step.

        The comparison happens in the integer domain the actuator speaks: commands are rounded to
        whole percent and a KNX actuator reports the setpoint value it reached, so exact integer
        equality is the honest test -- and it cannot be accidentally satisfied by a stale report of
        the position the step started from.
        """
        reported = to_command(position)
        target = to_command(self.target)
        if self.kind == STEP_RISE_TO_AT_LEAST:
            return reported >= target
        return reported == target


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
    What the user (or the app itself, when recovering) asked for.

    An enter intent says where in the zone the sequence should finish, in one of two ways.
    ``near_edge`` is the wall-button rule: entry lands on whichever end of the zone the press came
    toward, which is a property of the press and not of the installation. ``landing_virtual`` is an
    explicit virtual slat position and, when given, wins -- that is how the deliberate "enter tilt"
    control applies the configured ``tilt_enter_landing_pct``. That setting is a real travel
    position; ``Zone.enter_landing_virtual`` converts it to the virtual scale used here, which is
    the only scale the planner ever speaks.
    """

    kind: str
    virtual_pct: Optional[float] = None
    direction: Optional[str] = None
    near_edge: str = NEAR_EDGE_CLOSED
    cross_open_edge: bool = False
    landing_virtual: Optional[float] = None


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
        return _plan_enter_tilt(zone, intent.near_edge, intent.landing_virtual)
    if intent.kind == INTENT_LEAVE_TILT:
        return _plan_leave_tilt(zone, belief)
    if intent.kind == INTENT_SLAT_STEP:
        return _plan_slat_step(zone, belief, intent.direction, intent.cross_open_edge)
    if intent.kind == INTENT_ENTER_TOWARD_ZONE:
        return _plan_enter_toward_zone(zone, belief, intent.direction)
    if intent.kind == INTENT_LONG_PRESS:
        return _plan_long_press(belief, intent.direction)
    if intent.kind == INTENT_RECOVER:
        return _plan_recover()
    raise ValueError(f"unknown intent {intent.kind!r}")


def can_change_latch(zone: Zone, movement: Plan) -> bool:
    """
    Whether executing -- or interrupting -- this plan could engage or release the latch.

    A plan that never leaves the zone is pure slat rotation: it starts inside the zone (that is what
    being latched means), moves monotonically to another in-zone position, and so never crosses
    either edge. Neither running it nor abandoning it half-way can change the latch, which is why an
    interrupted slat move does not cost the app its confident latch belief. Both the satisfaction
    target and the commanded position have to stay inside, since the blind travels to the latter.
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
    return Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),), LATCH_UNLATCHED)


def _plan_close(zone: Zone, belief: Belief) -> Plan:
    """
    Least light: slats parallel when latched, otherwise the blind fully closed.
    """
    if belief.in_tilt:
        return _slat_plan(zone.upper)
    return Plan(PLAN_NORMAL, _guard_descent(belief, (Step(STEP_MOVE_TO, 0.0, COMMAND_CLOSE),)),
                LATCH_UNLATCHED)


def _plan_set_position(zone: Zone, belief: Belief, virtual_pct: Optional[float]) -> Optional[Plan]:
    """
    Move to an absolute virtual position: a slat angle when latched, a height otherwise.
    """
    if virtual_pct is None:
        return None
    if belief.in_tilt:
        return _slat_plan(zone.virtual_to_real(clamp_pct(virtual_pct)))
    # Snapping keeps normal mode out of the band interior, where a rise would silently latch.
    target = zone.snap_normal_target(virtual_pct)
    steps: Tuple[Step, ...] = (Step(STEP_MOVE_TO, target),)
    descending = belief.position is None or to_command(target) < to_command(belief.position)
    if descending:
        steps = _guard_descent(belief, steps)
    return Plan(PLAN_NORMAL, steps, LATCH_UNLATCHED)


def _plan_enter_tilt(zone: Zone, near_edge: str, landing_virtual: Optional[float] = None) -> Plan:
    """
    The canonical latch sequence, correct from any starting position.

    Drive fully open with the open command first: the latch percentages are only reliable when the
    sequence is referenced from the top limit, and it also guarantees the dip is a pure descent from
    above, which cannot latch. Then dip below the lower edge and rise back across it, which latches
    with the slats parallel.

    The latching rise necessarily ends at the closed edge, so landing anywhere else costs one more
    in-zone slat move. ``landing_virtual`` names that landing explicitly (the configured
    ``tilt_enter_landing_pct``, converted to the virtual scale by the zone); without it the
    wall-button ``near_edge`` rule decides. The extra
    step is omitted when it would command the position the rise already reached -- compared in the
    integer domain the actuator speaks, since a command that rounds to the current setpoint moves
    nothing and would only be skipped again by the executor.
    """
    if landing_virtual is None:
        landing_virtual = 100.0 if near_edge == NEAR_EDGE_OPEN else 0.0
    steps = [
        Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
        Step(STEP_MOVE_TO, zone.dip_target),
        Step(STEP_MOVE_TO, zone.upper),
    ]
    landing = zone.virtual_to_real(clamp_pct(landing_virtual))
    if to_command(landing) != to_command(zone.upper):
        steps.append(Step(STEP_MOVE_TO, landing))
    return Plan(PLAN_ENTER, tuple(steps), LATCH_LATCHED)


def _plan_leave_tilt(zone: Zone, belief: Belief) -> Optional[Plan]:
    """
    Release the latch with a single rise clear of the upper edge.

    Only available from a confident LATCHED belief, which implies the actuator was re-referenced by
    the entry sequence and has only made small in-zone moves since. From an uncertain belief the
    release is a full open instead (see :func:`_guard_descent`).

    The rise is *commanded* past the height it has to reach (see :data:`EXIT_OVERSHOOT_PCT`), so an
    actuator that settles a little low still ends up at or above the release target rather than
    reporting a rise that never physically disengaged the mechanism.
    """
    if not belief.in_tilt:
        return None
    release = zone.release_target
    step = Step(STEP_RISE_TO_AT_LEAST, release, COMMAND_POSITION,
                command_pct=min(100.0, release + EXIT_OVERSHOOT_PCT))
    return Plan(PLAN_LEAVE, (step,), LATCH_UNLATCHED)


def _plan_slat_step(zone: Zone, belief: Belief, direction: Optional[str],
                    cross_open_edge: bool) -> Optional[Plan]:
    """
    Step the slats by one step within the zone.

    The step is taken on the virtual scale, as ``zone.step`` -- the configured ``tilt_step_pct``
    real travel percent expressed as a fraction of the zone's span.

    ``cross_open_edge`` decides what an up step does when the slats are already fully open: a KNX
    wall button leaves tilt upward (its only way back out), while the dedicated slat-step helpers
    clamp and stay open. The closed edge always clamps -- there is nowhere lower to go without
    driving the latch downward, which the mechanism forbids.
    """
    if not belief.in_tilt or belief.position is None:
        return None
    current_virtual = zone.real_to_virtual(belief.position)
    if direction == DIRECTION_UP:
        if current_virtual >= 100.0 - _VIRTUAL_EPSILON:
            return _plan_leave_tilt(zone, belief) if cross_open_edge else None
        target_virtual = min(100.0, current_virtual + zone.step)
    else:
        if current_virtual <= _VIRTUAL_EPSILON:
            return None
        target_virtual = max(0.0, current_virtual - zone.step)
    return _slat_plan(zone.virtual_to_real(target_virtual))


def _plan_enter_toward_zone(zone: Zone, belief: Belief, direction: Optional[str]) -> Optional[Plan]:
    """
    From outside the zone, enter tilt when a short press points toward it.

    A press pointing away, or one made while resting inside the zone without a latch belief, does
    nothing: the long press covers the extremes and the tilt helper covers deliberate entry.
    """
    position = belief.position
    if position is None:
        return None
    if position > zone.upper and direction == DIRECTION_DOWN:
        return _plan_enter_tilt(zone, NEAR_EDGE_CLOSED)
    if position < zone.lower and direction == DIRECTION_UP:
        return _plan_enter_tilt(zone, NEAR_EDGE_OPEN)
    return None


def _plan_long_press(belief: Belief, direction: Optional[str]) -> Plan:
    """
    Long wall-button press: jump to an extreme, releasing the latch first when descending.
    """
    if direction == DIRECTION_UP:
        return Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),), LATCH_UNLATCHED)
    return Plan(PLAN_NORMAL, _guard_descent(belief, (Step(STEP_MOVE_TO, 0.0, COMMAND_CLOSE),)),
                LATCH_UNLATCHED)


def _plan_recover() -> Plan:
    """
    Startup recovery: a single upward-only full open, which re-references the actuator too.
    """
    return Plan(PLAN_RECOVER, (Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),), LATCH_UNLATCHED)


def _slat_plan(real_target: float) -> Plan:
    """
    A single in-zone move that changes slat angle without changing height.
    """
    return Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, real_target),), LATCH_LATCHED)


def _guard_descent(belief: Belief, steps: Tuple[Step, ...]) -> Tuple[Step, ...]:
    """
    Prefix a descent with a full-open latch release whenever the latch might be engaged.

    The release is a full open rather than a short rise to a merely reported release height because an
    uncertain belief also means an uncertain calibration: only the top limit is a position the
    actuator cannot be wrong about. Rising from above the zone cannot re-latch, so the descent that
    follows is safe. A known-released mechanism descends directly, which is the common case (closing
    right after leaving tilt costs no detour).
    """
    if belief.may_be_latched:
        return (Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),) + steps
    return steps


# -- Invariants ------------------------------------------------------------------------------------


def check_plan(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    Check a plan against the safety invariants, returning the first violation or None.

    This is defence in depth: every sequence above is written to satisfy these, and the exhaustive
    model tests prove it. A violation means the planner has a bug, so the caller disables the blind
    rather than executing anything.
    """
    return (_check_normal_targets(zone, movement) or _check_slat_targets(zone, belief, movement)
            or _check_descents(zone, belief, movement) or _check_latching(zone, belief, movement)
            or _check_releases(zone, belief, movement))


def _check_normal_targets(zone: Zone, movement: Plan) -> Optional[str]:
    """
    N1: in normal mode no target lies strictly inside the ambiguity band.

    Both the satisfaction target and the position actually commanded are checked: the hazard is
    where the blind physically comes to rest, and those two are allowed to differ.
    """
    if movement.final_latch != LATCH_UNLATCHED:
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
        # As with N1, the commanded position counts as well as the threshold that satisfies it.
        for position in (step.target, step.command_position):
            if not zone.in_zone(position):
                return f"T1: slat target {position} lies outside the tilt zone"
    return None


def _check_descents(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    L1: a descent below the lower edge is preceded by a full open unless the latch is known clear.

    The hazard is *travel* below the lower edge while latched, so the check walks the plan tracking
    where the blind will be -- the position each step *commands*, not the threshold that satisfies
    it, because the actuator travels to the former. A step whose command already equals the current
    position moves nothing and is no descent. Once a full open has run, the mechanism is released
    and re-referenced, so everything after it is safe by construction.
    """
    if belief.latch == LATCH_UNLATCHED:
        return None
    position = belief.position
    for index, step in enumerate(movement.steps):
        if any(earlier.command == COMMAND_OPEN for earlier in movement.steps[:index]):
            break
        commanded = step.command_position
        descends = position is None or to_command(commanded) < to_command(position)
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
    if steps[0].command != COMMAND_OPEN or to_command(steps[0].target) != 100:
        return "E1: the enter sequence must begin by driving fully open"
    if steps[1].target >= zone.lower:
        return f"E1: the enter dip to {steps[1].target} does not clear the lower edge"
    if to_command(steps[2].target) != to_command(zone.upper):
        return f"E1: the latching rise must end at the upper edge, not {steps[2].target}"
    # The optional fourth step is the configured landing: any slat angle inside the zone. It starts
    # from the upper edge, so it can only ever descend to another in-zone position -- never across
    # the lower edge, and never back out of the zone.
    if len(steps) == 4 and not zone.in_zone(steps[3].target):
        return (f"E1: the enter sequence may only continue to a target inside the tilt zone, not "
                f"{steps[3].target}")
    return None


def _check_releases(zone: Zone, belief: Belief, movement: Plan) -> Optional[str]:
    """
    X1/R1: leaving tilt and recovering are upward-only, and every release from an uncertain belief
    is a full open rather than a rise to an unreferenced percentage.
    """
    if movement.kind == PLAN_LEAVE:
        if belief.latch != LATCH_LATCHED:
            return f"X1: the short tilt exit requires a LATCHED belief, not {belief.latch}"
        if len(movement.steps) != 1 or movement.steps[0].kind != STEP_RISE_TO_AT_LEAST:
            return "X1: leaving tilt must be a single upward step"
        step = movement.steps[0]
        if step.target < zone.release_target:
            return f"X1: the tilt exit to {step.target} does not reach the release height"
        # Commanding exactly the acceptance threshold would let an actuator that settles low report
        # a release the mechanism never performed, so the command must aim at least that high.
        if step.command_position < step.target:
            return (f"X1: the tilt exit commands {step.command_position}, below its own acceptance "
                    f"target {step.target}")
    if movement.kind == PLAN_RECOVER:
        if len(movement.steps) != 1 or movement.steps[0].command != COMMAND_OPEN:
            return "R1: recovery must be a single full open"
    return None
