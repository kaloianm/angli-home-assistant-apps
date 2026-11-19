"""
AppDaemon entry point for ExtractorFanControl.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from typing import Any

from appdaemon_common.hassapi import Hass
from appdaemon_common.rate_limiter import RateLimiter
from extractor_fan_control.config import parse_app_config
from extractor_fan_control.logic import (
    Action,
    ActionKind,
    ExtractorFanPairLogic,
    LogicConfig,
    TimerName,
)

# More fan switch commands than this (keepalive pulses included) within the window disables the fan
# until AppDaemon restarts.
FAN_CMD_RATE_LIMIT = 5
FAN_CMD_RATE_WINDOW_SECONDS = 30


class ExtractorFanControl(Hass):
    """
    AppDaemon app wiring extractor fan control logic for one light/fan pair.
    """

    def initialize(self) -> None:
        """
        AppDaemon startup hook.
        """
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        config = parse_app_config(self.args or {})
        self._config = config
        self.log(f"Processing config: {config}", level="DEBUG")
        self._validate_entities()

        # Set by the rate limit or an unhandled exception; actions are dropped from then on.
        self._disabled = False

        self._logic = ExtractorFanPairLogic(
            LogicConfig(
                min_light_on_for_fan_seconds=config.min_light_on_for_fan_seconds,
                short_visit_threshold_seconds=config.short_visit_threshold_seconds,
            ),
            log=self.log,
        )
        self._rate_limiter = RateLimiter(FAN_CMD_RATE_LIMIT, FAN_CMD_RATE_WINDOW_SECONDS)
        self._activation_timer_handle: Any | None = None
        self._deadline_timer_handle: Any | None = None
        self._keepalive_timer_handle: Any | None = None

        # Creates the sensor and clears any countdown left from before a restart.
        self._publish_off_deadline(None)

        self.listen_state(self._on_light_state, config.light_entity)
        if config.daily_run_time and config.daily_run_duration_seconds:
            self.run_daily(self._on_daily_schedule_start, self.parse_time(config.daily_run_time))

        self.log(f"Initialized '{config.name}'. keepalive_pulse_interval_seconds="
                 f"{config.keepalive_pulse_interval_seconds}")

    def _validate_entities(self) -> None:
        """
        Log configuration errors for missing entities without failing startup.
        """
        entities = (
            ("light_entity", self._config.light_entity),
            ("fan_switch_entity", self._config.fan_switch_entity),
        )
        for key, entity_id in entities:
            if self.get_state(entity_id, default=None) is None:
                self.log(
                    f"Configured {key} '{entity_id}' does not exist in Home Assistant.",
                    level="ERROR",
                )

    def _on_light_state(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: dict[str, Any],
    ) -> None:
        """
        Process light ON/OFF state transitions.
        """
        try:
            if new == old:
                return
            if new not in ("on", "off"):
                return

            now = self.datetime()
            if new == "on":
                actions = self._logic.on_light_on(now)
            else:
                actions = self._logic.on_light_off(now)
            self._apply_actions(actions)
        except Exception as exc:
            self._report_error(f"_on_light_state(entity={entity!r}, new={new!r})", exc)

    def _on_daily_schedule_start(self, kwargs: dict[str, Any]) -> None:
        """
        Trigger the scheduled run.
        """
        try:
            if self._config.daily_run_duration_seconds is None:
                return
            actions = self._logic.on_schedule_started(
                self.datetime(),
                duration_seconds=self._config.daily_run_duration_seconds,
            )
            self._apply_actions(actions)
        except Exception as exc:
            self._report_error("_on_daily_schedule_start", exc)

    def _on_timer(self, kwargs: dict[str, Any]) -> None:
        """
        Drive logic timer progression for activation/deadline events.
        """
        try:
            timer_name = kwargs["timer_name"]
            match timer_name:
                case TimerName.ACTIVATION:
                    self._activation_timer_handle = None
                case TimerName.DEADLINE:
                    self._deadline_timer_handle = None
            actions = self._logic.on_time_tick(self.datetime())
            self._apply_actions(actions)
        except Exception as exc:
            self._report_error("_on_timer", exc)

    def _on_keepalive_tick(self, kwargs: dict[str, Any]) -> None:
        """
        Send periodic ON pulse to keep KNX staircase output alive.
        """
        try:
            # run_every callbacks can still arrive right after cancellation; ignore those.
            if self._keepalive_timer_handle is None or self._disabled:
                return
            self._turn_fan(on=True, keepalive=True)
        except Exception as exc:
            self._report_error("_on_keepalive_tick", exc)

    def _apply_actions(self, actions: list[Action]) -> None:
        """
        Translate pure logic actions into AppDaemon side effects.
        """
        if self._disabled:
            return
        for action in actions:
            match action.kind:
                case ActionKind.FAN_ON:
                    self._turn_fan(on=True)
                case ActionKind.FAN_OFF:
                    self._turn_fan(on=False)
                case ActionKind.START_KEEPALIVE:
                    self._start_keepalive()
                case ActionKind.STOP_KEEPALIVE:
                    self._stop_keepalive()
                case ActionKind.SET_TIMER:
                    self._set_timer(action)
                case ActionKind.CANCEL_TIMER:
                    self._cancel_timer(action.timer_name)
                case ActionKind.SET_OFF_DEADLINE:
                    self._publish_off_deadline(action.at)

    def _turn_fan(self, *, on: bool, keepalive: bool = False) -> None:
        """
        Issue a rate-limited fan switch command. Keepalive pulses log at ``DEBUG``.
        """
        if self._disabled:
            return
        if self._rate_limiter.record(self.datetime()):
            self._disable()
            return
        service = "switch/turn_on" if on else "switch/turn_off"
        self.log(f"Fan {service}{' (keepalive)' if keepalive else ''}",
                 level="DEBUG" if keepalive else "INFO")
        self.call_service(service, entity_id=self._config.fan_switch_entity)

    def _publish_off_deadline(self, at: datetime | None) -> None:
        """
        Publish when the fan is going to turn off, or ``unknown``. Not rate-limited: it is a state
        write, not a fan command.
        """
        entity_id = self._config.off_at_sensor_entity
        state = "unknown" if at is None else self._to_local_aware(at).isoformat()
        self.set_state(
            entity_id,
            state=state,
            attributes={
                "device_class": "timestamp",
                "friendly_name": _friendly_name_from_entity_id(entity_id),
                "icon": "mdi:fan-clock",
            },
        )

    def _to_local_aware(self, naive_local: datetime) -> datetime:
        """
        Attach the current AppDaemon timezone to a naive local datetime, as
        ``device_class: timestamp`` needs. Deadlines are minutes away, so DST is not a concern.
        """
        return naive_local.replace(tzinfo=self.datetime(aware=True).tzinfo)

    def _disable(self) -> None:
        """
        Permanently disable this fan due to rate limiting and notify.
        """
        self._shutdown()
        self.log(
            f"DISABLED: fan switch command rate limit exceeded ({FAN_CMD_RATE_LIMIT} commands in "
            f"{FAN_CMD_RATE_WINDOW_SECONDS}s). Restart AppDaemon to re-enable.", level="ERROR")
        self.call_service(
            "persistent_notification/create",
            title="ExtractorFanControl disabled",
            message=(f"Fan '{self._config.name}' has been disabled because it sent more than "
                     f"{FAN_CMD_RATE_LIMIT} fan switch commands in {FAN_CMD_RATE_WINDOW_SECONDS} "
                     "seconds. This likely indicates a bug. Restart AppDaemon to re-enable."),
        )

    def _report_error(self, context: str, exc: Exception) -> None:
        """
        Log an unhandled callback exception, disable the fan, and notify Home Assistant.
        """
        tb = traceback.format_exc()
        self.log(f"Unhandled exception in {context}: {type(exc).__name__}: {exc}\n{tb}",
                 level="ERROR")
        self._shutdown()
        self.call_service(
            "persistent_notification/create",
            title="ExtractorFanControl error",
            message=f"{context}\n{type(exc).__name__}: {exc}",
        )

    def _shutdown(self) -> None:
        """
        Disable both the adapter and the logic, cancel every timer and clear the countdown.

        The countdown is cleared directly because the logic's actions are ignored from now on.
        """
        self._disabled = True
        self._logic.disable()
        self._cancel_timer(TimerName.ACTIVATION)
        self._cancel_timer(TimerName.DEADLINE)
        self._stop_keepalive()
        self._publish_off_deadline(None)

    def _start_keepalive(self) -> None:
        """
        Start periodic staircase keepalive pulses.
        """
        if self._keepalive_timer_handle is not None:
            return
        interval = self._config.keepalive_pulse_interval_seconds
        self._keepalive_timer_handle = self.run_every(
            self._on_keepalive_tick,
            self.datetime() + timedelta(seconds=interval),
            interval,
        )

    def _stop_keepalive(self) -> None:
        """
        Stop periodic keepalive pulses.
        """
        if self._keepalive_timer_handle is None:
            return
        self.cancel_timer(self._keepalive_timer_handle)
        self._keepalive_timer_handle = None

    def _set_timer(self, action: Action) -> None:
        """
        Set activation/deadline one-shot timer at the requested timestamp.
        """
        if action.timer_name not in (TimerName.ACTIVATION, TimerName.DEADLINE) or action.at is None:
            return
        self._cancel_timer(action.timer_name)
        handle = self.run_at(self._on_timer, action.at, timer_name=action.timer_name)
        if action.timer_name == TimerName.ACTIVATION:
            self._activation_timer_handle = handle
        else:
            self._deadline_timer_handle = handle

    def _cancel_timer(self, timer_name: str | None) -> None:
        """
        Cancel activation/deadline timer if currently scheduled.
        """
        match timer_name:
            case TimerName.ACTIVATION:
                handle = self._activation_timer_handle
                self._activation_timer_handle = None
            case TimerName.DEADLINE:
                handle = self._deadline_timer_handle
                self._deadline_timer_handle = None
            case _:
                return

        if handle is not None:
            self.cancel_timer(handle)


def _friendly_name_from_entity_id(entity_id: str) -> str:
    """
    Turn ``sensor.courtesy_bathroom_air_extractor_off_at`` into a readable name.
    """
    object_id = entity_id.split(".", 1)[-1]
    return object_id.replace("_", " ").title()
