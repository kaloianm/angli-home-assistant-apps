"""
Pure business logic for Daikin AC hysteresis control.

This module intentionally has no AppDaemon or Home Assistant dependencies.
The runtime integration layer feeds observed entity state into ``DaikinACLogic`` and executes
the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

ACTION_SET_COOL = "set_cool"
ACTION_TURN_OFF = "turn_off"

CONTROL_MIN_INTERVAL_SECONDS = 60.0

AC_MODE_COLD = "0"

HVAC_COOL = "cool"
HVAC_FAN_ONLY = "fan_only"
HVAC_OFF = "off"


@dataclass(frozen=True)
class Action:
    """
    Declarative action produced by the logic engine.
    """

    kind: str


class ControlRateLimitError(Exception):
    """
    Raised when turn_off or set_cool is requested sooner than the minimum control interval.
    """


class ControlRateLimiter:
    """
    Enforces a minimum interval between turn_off and set_cool commands per entity.
    """

    def __init__(self, min_interval_seconds: float = CONTROL_MIN_INTERVAL_SECONDS) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._last_at: Dict[str, datetime] = {}

    def check(self, entity_id: str, now: datetime) -> None:
        """
        Raise ``ControlRateLimitError`` if ``entity_id`` was controlled too recently.
        """
        last_at = self._last_at.get(entity_id)
        if last_at is None:
            return
        elapsed = (now - last_at).total_seconds()
        if elapsed < self._min_interval_seconds:
            raise ControlRateLimitError(
                f"{entity_id}: control action {elapsed:.1f}s after previous "
                f"(minimum {self._min_interval_seconds}s)")

    def record(self, entity_id: str, now: datetime) -> None:
        """
        Record that a control action was issued for ``entity_id`` at ``now``.
        """
        self._last_at[entity_id] = now


class EntityState(Enum):
    """
    Per-entity management state tracked by the app.

    ``COOLING``: entity is in cool mode; app monitors temperature.
    ``OFF``: app turned the entity off; waiting for room to warm before resuming cooling.
    ``DISABLED``: unrecoverable error; no transitions out until AppDaemon restart.
    """

    IDLE = "idle"
    COOLING = "cooling"
    OFF = "off"
    DISABLED = "disabled"


class ACEntityLogic:
    """
    State machine for one climate entity.

    Reacts to Home Assistant state change events (hvac_mode, current_temperature,
    target_temperature) and returns declarative actions. All business decisions live here;
    no I/O is performed.

    Whenever the entity is in ``cool`` mode, the off hysteresis is enforced on every update.
    While app-managed in ``OFF``, the on hysteresis is evaluated on each update. Manual
    ``fan_only`` or ``off`` disables management until the entity re-enters ``cool`` mode.
    """

    def __init__(self, off_hysteresis: float, on_hysteresis: float) -> None:
        """
        Create state machine for one entity.

        ``off_hysteresis``: degrees below setpoint at which the app turns the entity off.
        ``on_hysteresis``: degrees above setpoint at which the app turns the entity back on.
        """
        self._off_hysteresis = off_hysteresis
        self._on_hysteresis = on_hysteresis
        self._state = EntityState.IDLE

    @property
    def state(self) -> EntityState:
        """
        Current management state of this entity.
        """
        return self._state

    def disable(self) -> None:
        """
        Permanently stop management for this entity until the app is restarted.
        """
        self._state = EntityState.DISABLED

    def reset(self) -> None:
        """
        Reset to IDLE, discarding any accumulated state.

        Called when the global AC mode leaves "cold" so the app stops managing the entity.
        No actions are emitted; the entity is left in whatever state it is in.
        """
        if self._state == EntityState.DISABLED:
            return
        self._state = EntityState.IDLE

    def update(
        self,
        observed_hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        """
        React to a Home Assistant state change event for this entity.

        Called whenever Home Assistant reports a change in hvac_mode or current_temperature.
        ``observed_hvac_mode`` is the entity's current hvac_mode (e.g. "cool", "fan_only", "off").
        ``current_temp`` is the current room temperature measured by the entity, or ``None`` if
        the sensor is unavailable. ``target_temp`` is the user's desired setpoint, or ``None``.

        Temperature-based transitions are skipped when either value is ``None``.
        Returns declarative actions for the adapter to execute; returns an empty list if no
        state change is needed.
        """
        if self._state == EntityState.DISABLED:
            return []

        if observed_hvac_mode == HVAC_COOL:
            self._state = EntityState.COOLING
            return self._from_cooling(current_temp, target_temp)

        if observed_hvac_mode == HVAC_OFF and self._state == EntityState.OFF:
            return self._from_off(current_temp, target_temp)

        if observed_hvac_mode in (HVAC_FAN_ONLY, HVAC_OFF):
            self._state = EntityState.IDLE
            return []

        if self._state != EntityState.IDLE:
            self._state = EntityState.IDLE

        return []

    def _from_cooling(
        self,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        if current_temp is None or target_temp is None:
            return []

        if current_temp - target_temp < -self._off_hysteresis:
            self._state = EntityState.OFF
            return [Action(ACTION_TURN_OFF)]
        return []

    def _from_off(
        self,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        if current_temp is None or target_temp is None:
            return []

        if current_temp - target_temp > self._on_hysteresis:
            self._state = EntityState.COOLING
            return [Action(ACTION_SET_COOL)]
        return []


class DaikinACLogic:
    """
    App-level coordinator: manages global AC mode state and all per-entity logic instances.

    The only public methods are ``on_mode_change`` and ``on_entity_changed``. Both return a list
    of ``(entity_id, Action)`` pairs for the adapter to execute.
    """

    def __init__(
        self,
        ac_entities: List[str],
        off_hysteresis: float,
        on_hysteresis: float,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        """
        Create the coordinator.

        ``ac_entities``: list of climate entity IDs to manage.
        ``off_hysteresis``: degrees below setpoint at which to turn off.
        ``on_hysteresis``: degrees above setpoint at which to turn back on.
        ``log``: optional callable for diagnostic output; receives a single message string.
        """
        self._log = log
        self._ac_mode: str = ""
        self._entities: Dict[str, ACEntityLogic] = {
            entity_id: ACEntityLogic(off_hysteresis, on_hysteresis)
            for entity_id in ac_entities
        }

    @property
    def mode_is_cold(self) -> bool:
        """
        Whether the global AC mode is currently cold (ac_mode entity reads ``AC_MODE_COLD``).
        """
        return self._ac_mode == AC_MODE_COLD

    def entity_state(self, entity_id: str) -> EntityState:
        """
        Return the current management state for ``entity_id``.
        """
        return self._entities[entity_id].state

    def disable(self, entity_id: str) -> None:
        """
        Permanently stop management for ``entity_id`` until the app is restarted.
        """
        self._entities[entity_id].disable()
        self._log(f"[{entity_id}] disabled due to error")

    def on_mode_change(self, new_mode: str) -> List[Tuple[str, Action]]:
        """
        Handle a change in the global AC mode select entity.

        Transitions to ``AC_MODE_COLD`` enable management. Any other value resets all per-entity
        states to IDLE without emitting actions (entities are left in whatever state they are in).
        """
        self._ac_mode = new_mode
        self._log(f"AC mode: {new_mode!r}")

        if self._ac_mode != AC_MODE_COLD:
            for entity_logic in self._entities.values():
                entity_logic.reset()

        return []

    def on_entity_changed(
        self,
        entity_id: str,
        hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Tuple[str, Action]]:
        """
        Process an observed entity state snapshot.

        Returns ``(entity_id, action)`` pairs for the adapter to execute. Returns an empty list
        when the global mode is not cold.
        """
        if self._ac_mode != AC_MODE_COLD:
            return []

        entity_logic = self._entities[entity_id]
        self._log(f"[{entity_id}] hvac={hvac_mode!r} temp={current_temp} setpoint={target_temp}"
                  f" state={entity_logic.state.value}")

        actions = entity_logic.update(hvac_mode, current_temp, target_temp)
        for action in actions:
            self._log(f"[{entity_id}] → {action.kind} (new state: {entity_logic.state.value})")

        return [(entity_id, action) for action in actions]
