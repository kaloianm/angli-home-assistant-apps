"""
Pure business logic for Gradhermetic cover control.

This module intentionally has no AppDaemon or Home Assistant dependencies. The runtime integration
layer feeds events into ``GradhermeticCoverLogic`` and executes the returned declarative actions.

The Gradhermetic mechanism has two regimes:

- Outside the tilt zone the virtual cover maps one-to-one to the blind's absolute travel position
  (up = open = more light = 100).
- Inside the narrow tilt zone ``[lower, upper]`` the blind is latched and further travel changes
  slat orientation instead of height. There the virtual position is inverted: virtual 100 maps to
  the lower edge (slats open / perpendicular / most light) and virtual 0 maps to the upper edge
  (slats closed / parallel / least light).

Latching into tilt mode requires a full down-then-up motion across the lower edge, so movements are
modelled as an ordered list of waypoints (a "movement plan"). The adapter drives one waypoint at a
time and feeds position feedback back in; the plan advances only once a waypoint is reached and the
blind has settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

ACTION_MOVE_TO = "move_to"
ACTION_OPEN_FULL = "open_full"
ACTION_CLOSE_FULL = "close_full"
ACTION_STOP = "stop"
ACTION_PUBLISH_POSITION = "publish_position"
ACTION_PERSIST_STATE = "persist_state"

STEP_MOVE_TO = "move_to"
STEP_OPEN_FULL = "open_full"
STEP_CLOSE_FULL = "close_full"

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"

NEAR_EDGE_OPEN = "open"
NEAR_EDGE_CLOSED = "closed"

# A reported position within this many percent of a waypoint target counts as "reached".
POSITION_TOLERANCE_PCT = 1.5

# Guard for floating-point edge comparisons on the virtual scale.
_VIRTUAL_EPSILON = 1e-6


@dataclass(frozen=True)
class LogicConfig:
    """
    Tilt-zone geometry for one blind.
    """

    tilt_zone_upper_pct: float
    tilt_zone_lower_pct: float
    tilt_zone_epsilon_pct: float
    tilt_step_pct: float

    def validate(self) -> None:
        """
        Validate geometry invariants.
        """
        if not 0.0 <= self.tilt_zone_lower_pct < self.tilt_zone_upper_pct <= 100.0:
            raise ValueError("require 0 <= tilt_zone_lower_pct < tilt_zone_upper_pct <= 100")
        if self.tilt_zone_epsilon_pct <= 0.0:
            raise ValueError("tilt_zone_epsilon_pct must be > 0")
        if self.tilt_zone_epsilon_pct <= POSITION_TOLERANCE_PCT:
            raise ValueError(
                f"tilt_zone_epsilon_pct must be > POSITION_TOLERANCE_PCT ({POSITION_TOLERANCE_PCT}) "
                "so the dip and leave margins clear the zone edges beyond position-feedback tolerance"
            )
        if self.tilt_zone_lower_pct - self.tilt_zone_epsilon_pct < 0.0:
            raise ValueError("tilt_zone_lower_pct - tilt_zone_epsilon_pct must be >= 0")
        if self.tilt_zone_upper_pct + self.tilt_zone_epsilon_pct > 100.0:
            raise ValueError("tilt_zone_upper_pct + tilt_zone_epsilon_pct must be <= 100")
        if self.tilt_step_pct <= 0.0:
            raise ValueError("tilt_step_pct must be > 0")
        # A step must map to at least one whole reported percent of real travel, otherwise the
        # actuator (which reports integer positions) never moves and the slats never change.
        min_step = 100.0 / (self.tilt_zone_upper_pct - self.tilt_zone_lower_pct)
        if self.tilt_step_pct < min_step:
            raise ValueError(
                f"tilt_step_pct must be >= {min_step:.2f} so one step moves the actuator at least "
                "one reported percent within the tilt zone")


@dataclass(frozen=True)
class Action:
    """
    Declarative action produced by the logic engine.
    """

    kind: str
    position: Optional[float] = None
    in_tilt: Optional[bool] = None


@dataclass(frozen=True)
class _Step:
    """
    One waypoint in a movement plan.
    """

    kind: str
    target: float


@dataclass
class _MovementPlan:
    """
    An ordered sequence of waypoints plus the state to commit once they all complete.
    """

    steps: List[_Step]
    final_in_tilt: bool
    final_virtual: float


class GradhermeticCoverLogic:
    """
    State machine translating user/KNX intent into blind movements for one Gradhermetic cover.
    """

    def __init__(
        self,
        config: LogicConfig,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        """
        Create logic state for one blind.

        ``config`` holds the tilt-zone geometry. All runtime state is kept internally and every
        public event method returns declarative actions.
        """
        config.validate()
        self._cfg = config
        self._log = log
        self._disabled = False

        # Authority for "where is the blind now" (real absolute travel position, 0-100), taken from
        # controller feedback. None until the first feedback / seed.
        self._last_position: Optional[float] = None
        # Whether the mechanism is believed latched in tilt mode.
        self._in_tilt = False
        # Whether the blind is currently travelling, from controller feedback.
        self._is_moving = False
        # Pending movement plan, if any.
        self._plan: Optional[_MovementPlan] = None

    # -- Accessors ---------------------------------------------------------------------------------

    @property
    def last_position(self) -> Optional[float]:
        """
        Most recent real travel position known to the logic.
        """
        return self._last_position

    @property
    def in_tilt(self) -> bool:
        """
        Whether the blind is believed latched in tilt mode.
        """
        return self._in_tilt

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
        return self._plan is not None

    def current_virtual_position(self) -> Optional[float]:
        """
        Virtual cover position for the current real position and mode, or None if unknown.
        """
        if self._last_position is None:
            return None
        if self._in_tilt:
            return self._real_to_virtual(self._last_position)
        return _clamp_pct(self._last_position)

    # -- Tilt-zone math ----------------------------------------------------------------------------

    def _virtual_to_real(self, virtual: float) -> float:
        """
        Map an in-tilt virtual position (0-100) to a real travel position within the zone.
        """
        virtual = _clamp_pct(virtual)
        span = self._cfg.tilt_zone_upper_pct - self._cfg.tilt_zone_lower_pct
        return self._cfg.tilt_zone_upper_pct - (virtual / 100.0) * span

    def _real_to_virtual(self, real: float) -> float:
        """
        Map an in-tilt real travel position within the zone to a virtual position (0-100).
        """
        span = self._cfg.tilt_zone_upper_pct - self._cfg.tilt_zone_lower_pct
        return _clamp_pct((self._cfg.tilt_zone_upper_pct - real) / span * 100.0)

    def _predip_target(self) -> float:
        """
        Real position just below the lower edge used to cleanly engage the latch.
        """
        return self._cfg.tilt_zone_lower_pct - self._cfg.tilt_zone_epsilon_pct

    def _leave_target(self) -> float:
        """
        Real position just above the upper edge used to cleanly disengage the latch.
        """
        return self._cfg.tilt_zone_upper_pct + self._cfg.tilt_zone_epsilon_pct

    def _position_in_band(self, position: float) -> bool:
        """
        Whether a real position falls inside the latch-ambiguity band ``[lower-eps, upper+eps]``.
        """
        return self._predip_target() <= position <= self._leave_target()

    def _might_be_latched(self) -> bool:
        """
        Whether the mechanism could currently be latched.

        The blind can only be latched while it sits inside the tilt band, so an unknown position or a
        position inside the band is treated as possibly latched. Any downward move made while this is
        true must first release the latch upward, mirroring the restart-recovery rule. This makes the
        logic robust to a stale ``in_tilt`` flag (interrupted latch sequence, external move of the
        real cover, or the brief startup window before recovery seeds state).
        """
        return self._last_position is None or self._position_in_band(self._last_position)

    # -- Lifecycle ---------------------------------------------------------------------------------

    def seed_state(self, last_position: Optional[float], in_tilt: bool) -> None:
        """
        Seed persisted state after a restart, before any events are processed.
        """
        self._last_position = last_position
        self._in_tilt = in_tilt

    def disable(self) -> List[Action]:
        """
        Permanently stop automation decisions for this blind until restart.
        """
        self._disabled = True
        self._plan = None
        self._log("disabled")
        return []

    # -- User / command events ---------------------------------------------------------------------

    def on_open(self) -> List[Action]:
        """
        Handle ``cover.open_cover``: most light.

        Outside tilt this opens the blind fully; inside tilt it orients the slats perpendicular
        (virtual 100, the lower edge).
        """
        if self._disabled:
            return []
        if self._in_tilt:
            return self._start_plan([_Step(STEP_MOVE_TO, self._cfg.tilt_zone_lower_pct)],
                                    final_in_tilt=True, final_virtual=100.0)
        return self._start_plan([_Step(STEP_OPEN_FULL, 100.0)], final_in_tilt=False,
                                final_virtual=100.0)

    def on_close(self) -> List[Action]:
        """
        Handle ``cover.close_cover``: least light.

        Outside tilt this closes the blind fully; inside tilt it orients the slats parallel
        (virtual 0, the upper edge).
        """
        if self._disabled:
            return []
        if self._in_tilt:
            return self._start_plan([_Step(STEP_MOVE_TO, self._cfg.tilt_zone_upper_pct)],
                                    final_in_tilt=True, final_virtual=0.0)
        return self._start_plan(self._guard_descent([_Step(STEP_CLOSE_FULL, 0.0)]),
                                final_in_tilt=False, final_virtual=0.0)

    def on_stop(self) -> List[Action]:
        """
        Handle ``cover.stop_cover``: abandon the current plan and stop travel.
        """
        if self._disabled:
            return []
        self._plan = None
        return [Action(ACTION_STOP)]

    def on_set_position(self, virtual_pct: float) -> List[Action]:
        """
        Handle ``cover.set_cover_position`` to an absolute virtual position.

        Outside tilt the virtual position is the real position; inside tilt it interpolates the slat
        angle between the zone edges.
        """
        if self._disabled:
            return []
        virtual_pct = _clamp_pct(virtual_pct)
        if self._in_tilt:
            return self._start_plan([_Step(STEP_MOVE_TO, self._virtual_to_real(virtual_pct))],
                                    final_in_tilt=True, final_virtual=virtual_pct)
        # Outside tilt the virtual position is the real target. Only a downward move (or an unknown
        # start) risks driving down while latched; an upward move self-releases across the upper edge.
        steps = [_Step(STEP_MOVE_TO, virtual_pct)]
        descending = self._last_position is None or virtual_pct < self._last_position
        if descending:
            steps = self._guard_descent(steps)
        return self._start_plan(steps, final_in_tilt=False, final_virtual=virtual_pct)

    def on_set_tilt_mode(self, enabled: bool) -> List[Action]:
        """
        Handle the custom ``set_tilt_mode`` service.

        Entering latches the mechanism (landing slats closed); leaving disengages it upward.
        """
        if self._disabled:
            return []
        if enabled and not self._in_tilt:
            return self._enter_tilt(NEAR_EDGE_CLOSED)
        if not enabled and self._in_tilt:
            return self._leave_tilt()
        return []

    # -- KNX wall-button events --------------------------------------------------------------------

    def on_knx_long(self, direction: str) -> List[Action]:
        """
        Handle a long wall-button press: jump to an extreme.

        Up drives fully open (leaving tilt naturally); down drives fully closed (rising out of the
        zone first if latched, since the latch only releases upward).
        """
        if self._disabled:
            return []
        if direction == DIRECTION_UP:
            return self._start_plan([_Step(STEP_OPEN_FULL, 100.0)], final_in_tilt=False,
                                    final_virtual=100.0)
        return self._start_plan(self._guard_descent([_Step(STEP_CLOSE_FULL, 0.0)]),
                                final_in_tilt=False, final_virtual=0.0)

    def on_knx_short(self, direction: str) -> List[Action]:
        """
        Handle a short wall-button press, in priority order: stop, else step slats, else enter the
        tilt zone when the press points toward it.
        """
        if self._disabled:
            return []

        # 1. If the blind is moving, stop it (matches native KNX stop/step behavior).
        if self._is_moving:
            self._plan = None
            return [Action(ACTION_STOP)]

        # 2. If latched in tilt, step the slats.
        if self._in_tilt:
            return self._step_slats(direction)

        # 3. Idle and outside the zone: enter tilt when the press points toward the zone.
        return self._enter_toward_zone(direction)

    # -- Restart recovery --------------------------------------------------------------------------

    def on_recover(self) -> List[Action]:
        """
        Recover from an unknown/mismatched position by driving fully open (upward-only).
        """
        if self._disabled:
            return []
        self._in_tilt = False
        return self._start_plan([_Step(STEP_OPEN_FULL, 100.0)], final_in_tilt=False,
                                final_virtual=100.0)

    # -- Position feedback -------------------------------------------------------------------------

    def on_real_position(self, position: float, is_moving: bool) -> List[Action]:
        """
        Consume controller position feedback, advancing the movement plan when a waypoint is reached
        and the blind has settled.
        """
        if self._disabled:
            return []

        was_moving = self._is_moving
        self._last_position = position
        self._is_moving = is_moving

        if self._plan is not None:
            return self._advance_plan(position, is_moving)

        # No plan of our own: the real cover may have been driven externally. If it now sits clearly
        # outside the tilt band it cannot be latched, so drop a stale in_tilt belief. We never set
        # in_tilt from feedback -- entering the latch is always an explicit, app-driven sequence.
        if self._in_tilt and not self._position_in_band(position):
            self._in_tilt = False
            self._log("cleared in_tilt: real position moved outside the tilt band")

        # If the blind just came to rest (e.g. after a manual stop), publish and persist.
        if was_moving and not is_moving:
            return self._publish_and_persist()
        return []

    # -- Internal plan handling --------------------------------------------------------------------

    def _advance_plan(self, position: float, is_moving: bool) -> List[Action]:
        """
        Advance the pending plan by one waypoint if the current waypoint is reached and settled.
        """
        step = self._plan.steps[0]
        if is_moving:
            return []
        if abs(position - step.target) > POSITION_TOLERANCE_PCT:
            return []

        self._plan.steps.pop(0)
        if self._plan.steps:
            return [self._step_action(self._plan.steps[0])]

        # Plan complete: commit the terminal state.
        self._in_tilt = self._plan.final_in_tilt
        final_virtual = self._plan.final_virtual
        self._plan = None
        self._log(f"plan complete: in_tilt={self._in_tilt} virtual={final_virtual}")
        return [
            Action(ACTION_PUBLISH_POSITION, position=final_virtual),
            Action(ACTION_PERSIST_STATE, position=self._last_position, in_tilt=self._in_tilt),
        ]

    def _publish_and_persist(self) -> List[Action]:
        """
        Emit publish + persist actions for the current resting position and mode.
        """
        actions: List[Action] = []
        virtual = self.current_virtual_position()
        if virtual is not None:
            actions.append(Action(ACTION_PUBLISH_POSITION, position=virtual))
        actions.append(
            Action(ACTION_PERSIST_STATE, position=self._last_position, in_tilt=self._in_tilt))
        return actions

    def _guard_descent(self, steps: List[_Step]) -> List[_Step]:
        """
        Prefix a downward movement with an upward latch release when the mechanism might be latched.

        The latch only ever releases upward, so before driving down we first rise clear of the upper
        edge whenever ``_might_be_latched`` holds. Rising from above the zone cannot re-latch (that
        needs a down-then-up across the lower edge), so the subsequent descent is safe.
        """
        if self._might_be_latched():
            return [_Step(STEP_MOVE_TO, self._leave_target())] + steps
        return steps

    def _start_plan(self, steps: List[_Step], *, final_in_tilt: bool,
                    final_virtual: float) -> List[Action]:
        """
        Begin a movement plan and emit the first waypoint's action.
        """
        self._plan = _MovementPlan(steps=steps, final_in_tilt=final_in_tilt,
                                   final_virtual=final_virtual)
        return [self._step_action(steps[0])]

    def _step_action(self, step: _Step) -> Action:
        """
        Translate a plan step into its declarative action.
        """
        if step.kind == STEP_MOVE_TO:
            return Action(ACTION_MOVE_TO, position=step.target)
        if step.kind == STEP_OPEN_FULL:
            return Action(ACTION_OPEN_FULL)
        return Action(ACTION_CLOSE_FULL)

    def _enter_tilt(self, near_edge: str) -> List[Action]:
        """
        Build and start the latch sequence, landing at the requested near edge.

        Latching requires a genuine down-then-up motion across the lower edge, so the sequence must
        approach that edge from above before dipping below it and rising back through it. Callers may
        start anywhere, so we first ensure the blind is safely above the lower edge:

        - unknown position: recover fully open;
        - inside the ambiguity band: the latch may already be engaged (e.g. an earlier entry was
          interrupted mid-rise, which physically latches the moment the rise crosses the lower
          edge), so rise above the zone to release it first -- dipping down while latched is exactly
          what the latch invariant forbids;
        - clearly below the band: hop just above the lower edge;
        - already above the zone: dip straight down.

        Then dip below the edge and rise to the upper edge to latch.
        """
        steps: List[_Step] = []
        pos = self._last_position
        lower = self._cfg.tilt_zone_lower_pct
        if pos is None:
            steps.append(_Step(STEP_OPEN_FULL, 100.0))
        elif self._might_be_latched():
            # Inside the band the mechanism might be latched; release it upward before dipping, since
            # the latch only ever releases by rising above the upper edge.
            steps.append(_Step(STEP_MOVE_TO, self._leave_target()))
        elif pos <= lower:
            # Below the band: hop just above the lower edge so the following dip crosses it downward.
            steps.append(_Step(STEP_MOVE_TO, lower + self._cfg.tilt_zone_epsilon_pct))
        steps.append(_Step(STEP_MOVE_TO, self._predip_target()))
        steps.append(_Step(STEP_MOVE_TO, self._cfg.tilt_zone_upper_pct))

        if near_edge == NEAR_EDGE_OPEN:
            steps.append(_Step(STEP_MOVE_TO, lower))
            final_virtual = 100.0
        else:
            final_virtual = 0.0
        return self._start_plan(steps, final_in_tilt=True, final_virtual=final_virtual)

    def _leave_tilt(self) -> List[Action]:
        """
        Disengage the latch with a single upward move above the zone.
        """
        return self._start_plan([_Step(STEP_MOVE_TO, self._leave_target())], final_in_tilt=False,
                                final_virtual=self._leave_target())

    def _step_slats(self, direction: str) -> List[Action]:
        """
        Step the slats by one ``tilt_step_pct`` within the zone, crossing the open edge upward when
        already fully open.
        """
        position = self._last_position
        if position is None:
            position = self._cfg.tilt_zone_upper_pct
        current_virtual = self._real_to_virtual(position)
        if direction == DIRECTION_UP:
            if current_virtual >= 100.0 - _VIRTUAL_EPSILON:
                # At the open edge already: an up step leaves tilt upward (boundary crossing).
                return self._leave_tilt()
            target_virtual = min(100.0, current_virtual + self._cfg.tilt_step_pct)
        else:
            if current_virtual <= _VIRTUAL_EPSILON:
                # At the closed edge already: nothing more to close.
                return []
            target_virtual = max(0.0, current_virtual - self._cfg.tilt_step_pct)
        return self._start_plan([_Step(STEP_MOVE_TO, self._virtual_to_real(target_virtual))],
                                final_in_tilt=True, final_virtual=target_virtual)

    def _enter_toward_zone(self, direction: str) -> List[Action]:
        """
        From outside the zone, enter tilt only when the press points toward the zone.
        """
        pos = self._last_position
        if pos is None:
            # Position unknown: a short press cannot safely determine direction; do nothing.
            return []
        if pos > self._cfg.tilt_zone_upper_pct and direction == DIRECTION_DOWN:
            # From above the zone, a down press enters at the most-closed near edge.
            return self._enter_tilt(NEAR_EDGE_CLOSED)
        if pos < self._cfg.tilt_zone_lower_pct and direction == DIRECTION_UP:
            # From below the zone, an up press enters at the most-open near edge.
            return self._enter_tilt(NEAR_EDGE_OPEN)
        # Press points away from the zone (or already past it): long press covers the extremes.
        return []


def _clamp_pct(value: float) -> float:
    """
    Clamp a percentage into [0, 100].
    """
    return max(0.0, min(100.0, value))
