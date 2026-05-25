"""
AppDaemon entry point for DaikinACControl.

This module contains only wiring: listener registration, state extraction from Home Assistant, and
action dispatch. All business decisions and logging live in ``daikin_ac_control.logic``.
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Tuple

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
            log=self.log,
        )

        self.listen_state(self._on_mode_change, config.ac_mode_entity)

        for entity_id in config.ac_entities:
            self._validate_entity(entity_id, config)
            self.listen_state(self._on_entity_changed, entity_id)
            self.listen_state(self._on_entity_changed, entity_id, attribute="current_temperature")
            self.listen_state(self._on_entity_changed, entity_id, attribute="temperature")

        current_mode = self.get_state(config.ac_mode_entity) or ""
        self._on_mode_change(config.ac_mode_entity, "state", None, current_mode, {})

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
        try:
            self._execute_actions(self._logic.on_mode_change(new or ""))
            if self._logic.mode_is_cold:
                for entity_id in self._ac_entities:
                    self._on_entity_changed(entity_id, None, None, None, {})
        except Exception as exc:
            self._report_error(f"_on_mode_change(new={new!r})", exc)

    def _on_entity_changed(
        self,
        entity_id: str,
        attribute: Any,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Handle any state or attribute change on a managed climate entity (hvac_mode,
        current_temperature, or target temperature setpoint). Also called directly on startup
        and after a mode change to cold to seed the logic with current HA state.
        """
        try:
            raw = self.get_state(entity_id, attribute="all")
            if raw is None:
                return
            hvac_mode: str = raw.get("state") or ""
            attrs: Dict[str, Any] = raw.get("attributes") or {}
            self._execute_actions(
                self._logic.on_entity_changed(entity_id, hvac_mode,
                                              attrs.get("current_temperature"),
                                              attrs.get("temperature")))
        except Exception as exc:
            self._report_error(f"_on_entity_changed(entity={entity_id!r})", exc)

    def _execute_actions(self, actions: List[Tuple[str, Action]]) -> None:
        """
        Execute a list of (entity_id, action) pairs returned by the logic layer.
        """
        for entity_id, action in actions:
            if action.kind == ACTION_SET_COOL:
                self.call_service("climate/set_hvac_mode", entity_id=entity_id, hvac_mode="cool")
            elif action.kind == ACTION_SET_FAN_ONLY:
                self.call_service("climate/set_hvac_mode", entity_id=entity_id,
                                  hvac_mode="fan_only")
            elif action.kind == ACTION_TURN_OFF:
                self.call_service("climate/turn_off", entity_id=entity_id)

    def _report_error(self, context: str, exc: Exception) -> None:
        """
        Log an unhandled exception with full traceback and send a persistent HA notification.
        """
        tb = traceback.format_exc()
        self.log(f"Unhandled exception in {context}: {type(exc).__name__}: {exc}\n{tb}",
                 level="ERROR")
        self.call_service(
            "persistent_notification/create",
            title="DaikinACControl error",
            message=f"{context}\n{type(exc).__name__}: {exc}",
        )
