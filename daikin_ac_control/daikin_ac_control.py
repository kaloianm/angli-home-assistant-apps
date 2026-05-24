"""
AppDaemon entry point for DaikinACControl.

This module contains only wiring: listener registration, state extraction from Home Assistant, and
action dispatch. All business decisions live in ``daikin_ac_control.logic``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from daikin_ac_control.config import AppConfig, parse_app_config
from daikin_ac_control.logic import (
    ACTION_SET_COOL,
    ACTION_SET_FAN_ONLY,
    ACTION_TURN_OFF,
    Action,
    DaikinACLogic,
)

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class _HassBase:
        pass

    class hass:  # type: ignore[no-redef]
        Hass = _HassBase


class DaikinACControl(hass.Hass):
    """
    AppDaemon app wiring for Daikin AC hysteresis control logic.
    """

    def initialize(self) -> None:
        """
        AppDaemon startup hook.
        """
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        config = parse_app_config(self.args or {})

        self._ac_mode_entity: str = config.ac_mode_entity
        self._ac_entities: List[str] = config.ac_entities

        self._logic = DaikinACLogic(
            ac_entities=config.ac_entities,
            ventilation_hysteresis=config.settings.switch_to_ventilation_hysteresis,
            on_off_hysteresis=config.settings.on_off_ac_hysteresis,
        )

        self.listen_state(self._on_mode_change, config.ac_mode_entity)

        for entity_id in config.ac_entities:
            self._validate_entity(entity_id, config)
            self.listen_state(self._on_entity_mode_change, entity_id, entity_id=entity_id)
            self.listen_state(
                self._on_entity_current_temp_change,
                entity_id,
                attribute="current_temperature",
                entity_id=entity_id,
            )
            self.listen_state(
                self._on_entity_setpoint_change,
                entity_id,
                attribute="temperature",
                entity_id=entity_id,
            )

        current_mode = self.get_state(config.ac_mode_entity) or ""
        self._execute_actions(self._logic.on_mode_change(current_mode))
        if self._logic.mode_is_cold:
            for entity_id in self._ac_entities:
                self._push_entity_update(entity_id)

        self.log(f"DaikinACControl initialized: mode_entity={config.ac_mode_entity}, "
                 f"entities={config.ac_entities}, "
                 f"ventilation_hysteresis={config.settings.switch_to_ventilation_hysteresis}, "
                 f"on_off_hysteresis={config.settings.on_off_ac_hysteresis}")

    def _validate_entity(self, entity_id: str, config: AppConfig) -> None:
        """
        Log a warning for any configured entity that does not exist in Home Assistant.
        """
        if self.get_state(entity_id, default=None) is None:
            self.log(
                f"Configured entity '{entity_id}' does not exist in Home Assistant.",
                level="WARNING",
            )

    def _on_mode_change(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Handle global AC mode select entity state changes.
        """
        if new == old:
            return
        self.log(f"AC mode changed: {old!r} -> {new!r}")
        self._execute_actions(self._logic.on_mode_change(new or ""))
        if self._logic.mode_is_cold:
            for entity_id in self._ac_entities:
                self._push_entity_update(entity_id)

    def _on_entity_mode_change(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Handle HVAC mode state changes on a managed climate entity.
        """
        if new == old:
            return
        entity_id: str = kwargs["entity_id"]
        self.log(f"[{entity_id}] HVAC mode: {old!r} -> {new!r}")
        self._push_entity_update(entity_id)

    def _on_entity_current_temp_change(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Handle current_temperature attribute changes on a managed climate entity.
        """
        entity_id: str = kwargs["entity_id"]
        self._push_entity_update(entity_id)

    def _on_entity_setpoint_change(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Handle target temperature (setpoint) changes on a managed climate entity.

        A setpoint change alters the delta used for all threshold comparisons, so it can trigger
        state transitions even when the room temperature has not changed.
        """
        entity_id: str = kwargs["entity_id"]
        self._push_entity_update(entity_id)

    def _push_entity_update(self, entity_id: str) -> None:
        """
        Read current entity state from Home Assistant and feed it to the logic layer.
        """
        raw = self.get_state(entity_id, attribute="all")
        if raw is None:
            return
        hvac_mode: str = raw.get("state") or ""
        attrs: Dict[str, Any] = raw.get("attributes") or {}
        current_temp: Optional[float] = _to_float(attrs.get("current_temperature"))
        target_temp: Optional[float] = _to_float(attrs.get("temperature"))
        actions = self._logic.on_entity_update(entity_id, hvac_mode, current_temp, target_temp)
        self._execute_actions(actions)

    def _execute_actions(self, actions: List[Tuple[str, Action]]) -> None:
        """
        Execute a list of (entity_id, action) pairs returned by the logic layer.
        """
        for entity_id, action in actions:
            self._execute(entity_id, action)

    def _execute(self, entity_id: str, action: Action) -> None:
        """
        Translate one declarative action into a Home Assistant service call.
        """
        if action.kind == ACTION_SET_COOL:
            self.log(f"[{entity_id}] Switching to cooling mode")
            self.call_service("climate/set_hvac_mode", entity_id=entity_id, hvac_mode="cool")
        elif action.kind == ACTION_SET_FAN_ONLY:
            self.log(f"[{entity_id}] Switching to ventilation (fan only)")
            self.call_service("climate/set_hvac_mode", entity_id=entity_id, hvac_mode="fan_only")
        elif action.kind == ACTION_TURN_OFF:
            self.log(f"[{entity_id}] Turning off")
            self.call_service("climate/turn_off", entity_id=entity_id)


def _to_float(value: Any) -> Optional[float]:
    """
    Safely convert a value to float, returning ``None`` on failure.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
