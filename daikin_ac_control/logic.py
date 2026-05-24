"""
Pure business logic for Daikin AC hysteresis control.

This module intentionally has no AppDaemon or Home Assistant dependencies.
The runtime integration layer feeds observed entity state into ``DaikinACLogic`` and executes
the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

ACTION_SET_COOL = "set_cool"
ACTION_SET_FAN_ONLY = "set_fan_only"
ACTION_TURN_OFF = "turn_off"

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


class EntityState(Enum):
    """
    Per-entity management state tracked by the app.

    ``IDLE``: entity is not being managed (not in cooling mode, or user took manual control).
    ``COOLING``: entity is in cool mode; Daikin is working; app monitors temperature only.
    ``VENTILATION``: app switched the entity to fan-only to prevent overshoot.
    ``OFF``: app turned the entity off; waiting for room to warm before resuming cooling.
    """

    IDLE = "idle"
    COOLING = "cooling"
    VENTILATION = "ventilation"
    OFF = "off"


class ACEntityLogic:
    """
    State machine for one climate entity.

    Reacts to Home Assistant state change events (hvac_mode, current_temperature,
    target_temperature) and returns declarative actions. All business decisions live here;
    no I/O is performed.

    Manual override detection relies on the invariant that each non-IDLE state expects a specific
    hvac_mode from the entity:
    - ``COOLING``     expects ``cool``
    - ``VENTILATION`` expects ``fan_only``
    - ``OFF``         expects ``off``

    Any deviation from the expected mode is treated as a manual change by the user and causes a
    transition to IDLE (or back to COOLING if the user manually restored cool mode).
    """

    def __init__(self, ventilation_hysteresis: float, on_off_hysteresis: float) -> None:
        """
        Create state machine for one entity.

        ``ventilation_hysteresis``: degrees below setpoint at which the app switches to fan-only.
        ``on_off_hysteresis``: degrees below setpoint at which the app turns the entity off, and
            degrees above setpoint at which the app turns it back on.
        """
        self._ventilation_hysteresis = ventilation_hysteresis
        self._on_off_hysteresis = on_off_hysteresis
        self._state = EntityState.IDLE

    @property
    def state(self) -> EntityState:
        """
        Current management state of this entity.
        """
        return self._state

    def reset(self) -> None:
        """
        Reset to IDLE, discarding any accumulated state.

        Called when the global AC mode leaves "cold" so the app stops managing the entity.
        No actions are emitted; the entity is left in whatever state it is in.
        """
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
        if self._state == EntityState.IDLE:
            return self._from_idle(observed_hvac_mode)
        if self._state == EntityState.COOLING:
            return self._from_cooling(observed_hvac_mode, current_temp, target_temp)
        if self._state == EntityState.VENTILATION:
            return self._from_ventilation(observed_hvac_mode, current_temp, target_temp)
        if self._state == EntityState.OFF:
            return self._from_off(observed_hvac_mode, current_temp, target_temp)
        return []  # pragma: no cover

    def _from_idle(self, hvac_mode: str) -> List[Action]:
        if hvac_mode == HVAC_COOL:
            self._state = EntityState.COOLING
        return []

    def _from_cooling(
        self,
        hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        if hvac_mode != HVAC_COOL:
            self._state = EntityState.IDLE
            return []

        if current_temp is None or target_temp is None:
            return []

        delta = current_temp - target_temp
        if delta < -self._on_off_hysteresis:
            # Room is already far below target; skip ventilation and turn off directly.
            self._state = EntityState.OFF
            return [Action(ACTION_TURN_OFF)]
        if delta < -self._ventilation_hysteresis:
            self._state = EntityState.VENTILATION
            return [Action(ACTION_SET_FAN_ONLY)]
        return []

    def _from_ventilation(
        self,
        hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        if hvac_mode != HVAC_FAN_ONLY:
            if hvac_mode == HVAC_COOL:
                # User manually restored cooling; resume management from COOLING.
                self._state = EntityState.COOLING
            else:
                self._state = EntityState.IDLE
            return []

        if current_temp is None or target_temp is None:
            return []

        delta = current_temp - target_temp
        if delta < -self._on_off_hysteresis:
            self._state = EntityState.OFF
            return [Action(ACTION_TURN_OFF)]
        if delta > self._on_off_hysteresis:
            self._state = EntityState.COOLING
            return [Action(ACTION_SET_COOL)]
        return []

    def _from_off(
        self,
        hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Action]:
        if hvac_mode != HVAC_OFF:
            if hvac_mode == HVAC_COOL:
                # User manually turned it back on in cooling; resume management.
                self._state = EntityState.COOLING
            else:
                self._state = EntityState.IDLE
            return []

        if current_temp is None or target_temp is None:
            return []

        delta = current_temp - target_temp
        if delta > self._on_off_hysteresis:
            self._state = EntityState.COOLING
            return [Action(ACTION_SET_COOL)]
        return []


class DaikinACLogic:
    """
    App-level coordinator: manages global AC mode state and all per-entity logic instances.

    The only public methods are ``on_mode_change`` and ``on_entity_update``. Both return a list
    of ``(entity_id, Action)`` pairs for the adapter to execute.
    """

    def __init__(
        self,
        ac_entities: List[str],
        ventilation_hysteresis: float,
        on_off_hysteresis: float,
    ) -> None:
        """
        Create the coordinator.

        ``ac_entities``: list of climate entity IDs to manage.
        ``ventilation_hysteresis``: degrees below setpoint at which to switch to fan-only.
        ``on_off_hysteresis``: degrees below setpoint to turn off; degrees above to turn back on.
        """
        self._ac_mode: str = ""
        self._entities: Dict[str, ACEntityLogic] = {
            entity_id: ACEntityLogic(ventilation_hysteresis, on_off_hysteresis)
            for entity_id in ac_entities
        }

    @property
    def mode_is_cold(self) -> bool:
        """
        Whether the global AC mode is currently cold (ac_mode entity reads ``AC_MODE_COLD``).
        """
        return self._ac_mode == AC_MODE_COLD

    def entity_state(self, entity_id: str) -> Optional[EntityState]:
        """
        Return the current management state for ``entity_id``, or ``None`` if not tracked.
        """
        entity = self._entities.get(entity_id)
        return entity.state if entity is not None else None

    def on_mode_change(self, new_mode: str) -> List[Tuple[str, Action]]:
        """
        Handle a change in the global AC mode select entity.

        Transitions to ``AC_MODE_COLD`` enable management. Any other value resets all per-entity
        states to IDLE without emitting actions (entities are left in whatever state they are in).
        """
        self._ac_mode = new_mode
        if self._ac_mode != AC_MODE_COLD:
            for entity_logic in self._entities.values():
                entity_logic.reset()
        return []

    def on_entity_update(
        self,
        entity_id: str,
        hvac_mode: str,
        current_temp: Optional[float],
        target_temp: Optional[float],
    ) -> List[Tuple[str, Action]]:
        """
        Process an observed entity state snapshot.

        Returns ``(entity_id, action)`` pairs for the adapter to execute. Returns an empty list
        when the global mode is not "cold" or the entity is not tracked.
        """
        if self._ac_mode != AC_MODE_COLD:
            return []
        entity_logic = self._entities.get(entity_id)
        if entity_logic is None:
            return []
        actions = entity_logic.update(hvac_mode, current_temp, target_temp)
        return [(entity_id, action) for action in actions]
