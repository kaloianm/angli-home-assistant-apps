"""
AppDaemon entry point for ExtractorFanControl.
"""

from __future__ import annotations

import traceback
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
from extractor_fan_control.runtime import (
    FAN_CMD_RATE_LIMIT,
    FAN_CMD_RATE_WINDOW_SECONDS,
    PairRuntime,
)

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class _HassBase:
        pass

    class hass:  # type: ignore[no-redef]
        Hass = _HassBase


class ExtractorFanControl(hass.Hass):
    """
    AppDaemon app wiring for extractor fan control logic.
    """

    def initialize(self) -> None:
        """
        AppDaemon startup hook.
        """
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        self._config = parse_app_config(self.args or {})
        self._runtime_by_name: Dict[str, PairRuntime] = {}

        for pair_config in self._config.pairs:
            self.log(f"Processing pair: {pair_config}")
            self._validate_pair_entities(pair_config)
            runtime = PairRuntime(
                config=pair_config,
                logic=ExtractorFanPairLogic(
                    LogicConfig(
                        min_light_on_for_fan_seconds=pair_config.min_light_on_for_fan_seconds,
                        short_visit_threshold_seconds=pair_config.short_visit_threshold_seconds,
                    ),
                    log=lambda message, pair_name=pair_config.name: self.log(
                        f"[{pair_name}] {message}"),
                ),
            )
            self._runtime_by_name[pair_config.name] = runtime

            runtime.light_listener_handle = self.listen_state(self._on_light_state,
                                                              pair_config.light_entity,
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

    def _validate_pair_entities(self, pair_config: PairConfig) -> None:
        """
        Log configuration errors for missing entities without failing startup.
        """
        entities = (
            ("light_entity", pair_config.light_entity),
            ("fan_switch_entity", pair_config.fan_switch_entity),
        )
        for key, entity_id in entities:
            if self.get_state(entity_id, default=None) is None:
                self.log(
                    f"[{pair_config.name}] Configured {key} '{entity_id}' does not exist in Home Assistant.",
                    level="ERROR",
                )

    def _on_light_state(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Dict[str, Any],
    ) -> None:
        """
        Process light ON/OFF state transitions for one pair.
        """
        pair_name = kwargs.get("pair_name")
        try:
            if new == old:
                return
            if new not in ("on", "off"):
                return

            runtime = self._runtime_by_name[pair_name]
            now = self.datetime()
            if new == "on":
                actions = runtime.logic.on_light_on(now)
            else:
                actions = runtime.logic.on_light_off(now)
            self._apply_actions(runtime, actions)
        except Exception as exc:
            self._report_error(f"_on_light_state(entity={entity!r}, new={new!r})", exc, pair_name)

    def _on_daily_schedule_start(self, kwargs: Dict[str, Any]) -> None:
        """
        Trigger scheduled run for one pair.
        """
        pair_name = kwargs.get("pair_name")
        try:
            runtime = self._runtime_by_name[pair_name]
            if runtime.config.daily_run_duration_seconds is None:
                return
            actions = runtime.logic.on_schedule_started(
                self.datetime(),
                duration_seconds=runtime.config.daily_run_duration_seconds,
            )
            self._apply_actions(runtime, actions)
        except Exception as exc:
            self._report_error("_on_daily_schedule_start", exc, pair_name)

    def _on_pair_timer(self, kwargs: Dict[str, Any]) -> None:
        """
        Drive logic timer progression for activation/deadline events.
        """
        pair_name = kwargs.get("pair_name")
        try:
            runtime = self._runtime_by_name[pair_name]
            timer_name = kwargs["timer_name"]
            if timer_name == TIMER_ACTIVATION:
                runtime.activation_timer_handle = None
            elif timer_name == TIMER_DEADLINE:
                runtime.deadline_timer_handle = None
            actions = runtime.logic.on_time_tick(self.datetime())
            self._apply_actions(runtime, actions)
        except Exception as exc:
            self._report_error("_on_pair_timer", exc, pair_name)

    def _on_keepalive_tick(self, kwargs: Dict[str, Any]) -> None:
        """
        Send periodic ON pulse to keep KNX staircase output alive.
        """
        pair_name = kwargs.get("pair_name")
        try:
            runtime = self._runtime_by_name[pair_name]
            # run_every callbacks can still arrive right after cancellation; ignore those.
            if runtime.keepalive_timer_handle is None or runtime.disabled:
                return
            self._turn_fan(runtime, on=True)
        except Exception as exc:
            self._report_error("_on_keepalive_tick", exc, pair_name)

    def _apply_actions(self, runtime: PairRuntime, actions: list[Action]) -> None:
        """
        Translate pure logic actions into AppDaemon side effects.
        """
        if runtime.disabled:
            return
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
        """
        Issue fan switch command and mark expected resulting state.
        """
        if runtime.disabled:
            return
        now = self.datetime()
        if runtime.record_fan_command(now):
            self._disable_pair(runtime)
            return
        service = "switch/turn_on" if on else "switch/turn_off"
        self.log(f"[{runtime.config.name}] Fan {service}")
        self.call_service(service, entity_id=runtime.config.fan_switch_entity)

    def _disable_pair(self, runtime: PairRuntime) -> None:
        """
        Permanently disable a pair due to rate limiting and notify.
        """
        runtime.disabled = True
        runtime.logic.disable()
        self._cancel_timer(runtime, TIMER_ACTIVATION)
        self._cancel_timer(runtime, TIMER_DEADLINE)
        self._stop_keepalive(runtime)
        self.log(
            f"[{runtime.config.name}] DISABLED: fan switch command rate "
            f"limit exceeded ({FAN_CMD_RATE_LIMIT} commands in "
            f"{FAN_CMD_RATE_WINDOW_SECONDS}s). Restart AppDaemon to "
            "re-enable.", level="ERROR")
        self.call_service(
            "persistent_notification/create",
            title="ExtractorFanControl disabled",
            message=(f"Pair '{runtime.config.name}' has been disabled because "
                     f"it sent more than {FAN_CMD_RATE_LIMIT} fan switch "
                     f"commands in {FAN_CMD_RATE_WINDOW_SECONDS} seconds. "
                     "This likely indicates a bug. Restart AppDaemon to "
                     "re-enable."),
        )

    def _report_error(
        self,
        context: str,
        exc: Exception,
        pair_name: Optional[str] = None,
    ) -> None:
        """
        Log an unhandled callback exception, disable the affected pair, and notify Home Assistant.
        """
        tb = traceback.format_exc()
        self.log(f"Unhandled exception in {context}: {type(exc).__name__}: {exc}\n{tb}",
                 level="ERROR")

        if pair_name is not None and pair_name in self._runtime_by_name:
            runtime = self._runtime_by_name[pair_name]
            runtime.disabled = True
            runtime.logic.disable()
            self._cancel_timer(runtime, TIMER_ACTIVATION)
            self._cancel_timer(runtime, TIMER_DEADLINE)
            self._stop_keepalive(runtime)

        self.call_service(
            "persistent_notification/create",
            title="ExtractorFanControl error",
            message=f"{context}\n{type(exc).__name__}: {exc}",
        )

    def _start_keepalive(self, runtime: PairRuntime) -> None:
        """
        Start periodic staircase keepalive pulses.
        """
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
        """
        Stop periodic keepalive pulses.
        """
        if runtime.keepalive_timer_handle is None:
            return
        self.cancel_timer(runtime.keepalive_timer_handle)
        runtime.keepalive_timer_handle = None

    def _set_timer(self, runtime: PairRuntime, action: Action) -> None:
        """
        Set activation/deadline one-shot timer at the requested timestamp.
        """
        if action.timer_name not in (TIMER_ACTIVATION, TIMER_DEADLINE) or action.at is None:
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

    def _cancel_timer(self, runtime: PairRuntime, timer_name: Optional[str]) -> None:
        """
        Cancel activation/deadline timer if currently scheduled.
        """
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
