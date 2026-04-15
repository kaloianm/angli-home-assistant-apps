"""AppDaemon entry point for ExtractorFanControl."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from extractor_fan_control.config import PairConfig, parse_app_config
from extractor_fan_control.logic import (
    ACTION_CANCEL_TIMER,
    ACTION_FAN_OFF,
    ACTION_FAN_ON,
    ACTION_SET_TIMER,
    ACTION_START_KEEPALIVE,
    ACTION_STOP_KEEPALIVE,
    TIMER_ACTIVATION,
    TIMER_DEADLINE,
    Action,
    ExtractorFanPairLogic,
    LogicConfig,
)

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class _HassBase:
        pass

    class hass:  # type: ignore[no-redef]
        Hass = _HassBase


@dataclass
class PairRuntime:
    """Mutable runtime state for one pair."""

    config: PairConfig
    logic: Optional[ExtractorFanPairLogic] = None
    light_listener_handle: Optional[Any] = None
    fan_listener_handle: Optional[Any] = None
    activation_timer_handle: Optional[Any] = None
    deadline_timer_handle: Optional[Any] = None
    keepalive_timer_handle: Optional[Any] = None
    daily_schedule_handle: Optional[Any] = None
    expected_fan_state: Optional[str] = None


class ExtractorFanControl(hass.Hass):
    """AppDaemon app wiring for extractor fan control logic."""

    def initialize(self) -> None:
        """AppDaemon startup hook."""
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        self._config = parse_app_config(self.args or {})
        self._runtime_by_name: Dict[str, PairRuntime] = {}
        self._runtime_by_light_entity: Dict[str, PairRuntime] = {}
        self._runtime_by_fan_entity: Dict[str, PairRuntime] = {}

        for pair_config in self._config.pairs:
            self.log("Processing pair "
                     f"name={pair_config.name}, "
                     f"light_entity={pair_config.light_entity}, "
                     f"fan_switch_entity={pair_config.fan_switch_entity}, "
                     "min_light_on_for_fan_seconds="
                     f"{pair_config.min_light_on_for_fan_seconds}, "
                     "short_visit_threshold_seconds="
                     f"{pair_config.short_visit_threshold_seconds}, "
                     f"daily_run_time={pair_config.daily_run_time}, "
                     "daily_run_duration_seconds="
                     f"{pair_config.daily_run_duration_seconds}")
            runtime = PairRuntime(
                config=pair_config,
                logic=ExtractorFanPairLogic(
                    LogicConfig(
                        min_light_on_for_fan_seconds=pair_config.
                        min_light_on_for_fan_seconds,
                        short_visit_threshold_seconds=pair_config.
                        short_visit_threshold_seconds,
                    )),
            )
            self._runtime_by_name[pair_config.name] = runtime
            self._runtime_by_light_entity[pair_config.light_entity] = runtime
            self._runtime_by_fan_entity[
                pair_config.fan_switch_entity] = runtime

            runtime.light_listener_handle = self.listen_state(
                self._on_light_state,
                pair_config.light_entity,
                pair_name=pair_config.name)
            runtime.fan_listener_handle = self.listen_state(
                self._on_fan_state,
                pair_config.fan_switch_entity,
                pair_name=pair_config.name)
            if pair_config.daily_run_time and pair_config.daily_run_duration_seconds:
                runtime.daily_schedule_handle = self.run_daily(
                    self._on_daily_schedule_start,
                    self.parse_time(pair_config.daily_run_time),
                    pair_name=pair_config.name,
                )

        self.log("ExtractorFanControl initialized with "
                 f"{len(self._runtime_by_name)} pair(s). "
                 "keepalive_pulse_interval_seconds="
                 f"{self._config.keepalive_pulse_interval_seconds}")

    def _on_light_state(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """Process light ON/OFF state transitions for one pair."""
        if new == old:
            return
        if new not in ("on", "off"):
            return

        pair_name = kwargs["pair_name"]
        runtime = self._runtime_by_name[pair_name]
        now = self.datetime()
        if runtime.logic is None:
            return
        if new == "on":
            actions = runtime.logic.on_light_on(now)
        else:
            actions = runtime.logic.on_light_off(now)
        self._apply_actions(runtime, actions)

    def _on_fan_state(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """Process manual fan toggles and forward them to pair logic."""
        if new == old:
            return
        if new not in ("on", "off"):
            return

        runtime = self._runtime_by_name[kwargs["pair_name"]]

        # Ignore the next state update if it matches what we just requested.
        if runtime.expected_fan_state is not None and new == runtime.expected_fan_state:
            runtime.expected_fan_state = None
            return
        runtime.expected_fan_state = None

        if runtime.logic is None:
            return
        actions = runtime.logic.on_manual_fan_toggle(self.datetime(),
                                                     fan_on=(new == "on"))
        self._apply_actions(runtime, actions)

    def _on_daily_schedule_start(self, kwargs: Dict[str, Any]) -> None:
        """Trigger scheduled run for one pair."""
        runtime = self._runtime_by_name[kwargs["pair_name"]]
        if runtime.logic is None or runtime.config.daily_run_duration_seconds is None:
            return
        actions = runtime.logic.on_schedule_started(
            self.datetime(),
            duration_seconds=runtime.config.daily_run_duration_seconds,
        )
        self._apply_actions(runtime, actions)

    def _on_pair_timer(self, kwargs: Dict[str, Any]) -> None:
        """Drive logic timer progression for activation/deadline events."""
        runtime = self._runtime_by_name[kwargs["pair_name"]]
        timer_name = kwargs["timer_name"]
        if timer_name == TIMER_ACTIVATION:
            runtime.activation_timer_handle = None
        elif timer_name == TIMER_DEADLINE:
            runtime.deadline_timer_handle = None
        if runtime.logic is None:
            return
        actions = runtime.logic.on_time_tick(self.datetime())
        self._apply_actions(runtime, actions)

    def _on_keepalive_tick(self, kwargs: Dict[str, Any]) -> None:
        """Send periodic ON pulse to keep KNX staircase output alive."""
        runtime = self._runtime_by_name[kwargs["pair_name"]]
        self._turn_fan(runtime, on=True)

    def _apply_actions(self, runtime: PairRuntime,
                       actions: list[Action]) -> None:
        """Translate pure logic actions into AppDaemon side effects."""
        for action in actions:
            if action.kind == ACTION_FAN_ON:
                self._turn_fan(runtime, on=True)
            elif action.kind == ACTION_FAN_OFF:
                self._turn_fan(runtime, on=False)
            elif action.kind == ACTION_START_KEEPALIVE:
                self._start_keepalive(runtime)
            elif action.kind == ACTION_STOP_KEEPALIVE:
                self._stop_keepalive(runtime)
            elif action.kind == ACTION_SET_TIMER:
                self._set_timer(runtime, action)
            elif action.kind == ACTION_CANCEL_TIMER:
                self._cancel_timer(runtime, action.timer_name)

    def _turn_fan(self, runtime: PairRuntime, *, on: bool) -> None:
        """Issue fan switch command and mark expected resulting state."""
        runtime.expected_fan_state = "on" if on else "off"
        service = "switch/turn_on" if on else "switch/turn_off"
        self.call_service(service, entity_id=runtime.config.fan_switch_entity)

    def _start_keepalive(self, runtime: PairRuntime) -> None:
        """Start periodic staircase keepalive pulses."""
        if runtime.keepalive_timer_handle is not None:
            return
        interval = self._config.keepalive_pulse_interval_seconds
        runtime.keepalive_timer_handle = self.run_every(
            self._on_keepalive_tick,
            self.datetime() + timedelta(seconds=interval),
            interval,
            pair_name=runtime.config.name,
        )

    def _stop_keepalive(self, runtime: PairRuntime) -> None:
        """Stop periodic keepalive pulses."""
        if runtime.keepalive_timer_handle is None:
            return
        self.cancel_timer(runtime.keepalive_timer_handle)
        runtime.keepalive_timer_handle = None

    def _set_timer(self, runtime: PairRuntime, action: Action) -> None:
        """Set activation/deadline one-shot timer at the requested timestamp."""
        if action.timer_name not in (TIMER_ACTIVATION,
                                     TIMER_DEADLINE) or action.at is None:
            return
        self._cancel_timer(runtime, action.timer_name)
        handle = self.run_at(
            self._on_pair_timer,
            action.at,
            pair_name=runtime.config.name,
            timer_name=action.timer_name,
        )
        if action.timer_name == TIMER_ACTIVATION:
            runtime.activation_timer_handle = handle
        else:
            runtime.deadline_timer_handle = handle

    def _cancel_timer(self, runtime: PairRuntime,
                      timer_name: Optional[str]) -> None:
        """Cancel activation/deadline timer if currently scheduled."""
        if timer_name == TIMER_ACTIVATION:
            handle = runtime.activation_timer_handle
            runtime.activation_timer_handle = None
        elif timer_name == TIMER_DEADLINE:
            handle = runtime.deadline_timer_handle
            runtime.deadline_timer_handle = None
        else:
            return

        if handle is not None:
            self.cancel_timer(handle)
