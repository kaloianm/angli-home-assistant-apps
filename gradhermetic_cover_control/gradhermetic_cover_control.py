"""
AppDaemon adapter for GradhermeticCoverControl.

Transport only: listens for template-cover command events, step/tilt button presses, KNX telegrams
and real-cover state changes, feeds them to the pure core (:mod:`logic`), and translates the
resulting :class:`Action` values into service calls and ``set_state`` updates. Makes no decisions.
See README.md for the Home Assistant contract and IMPLEMENTATION.md for the architecture.
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional, Tuple

from gradhermetic_cover_control.config import parse_app_config
from gradhermetic_cover_control.executor import (
    ACTION_ARM_SETTLE_TIMER,
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_NOTIFY,
    ACTION_OPEN_FULL,
    ACTION_PUBLISH_STATE,
    ACTION_STOP,
    MOTION_IDLE,
    NOTIFY_STALL,
    Action,
)
from gradhermetic_cover_control.geometry import to_whole_pct
from gradhermetic_cover_control.logic import GradhermeticCoverLogic
from gradhermetic_cover_control.planner import DIRECTION_DOWN, DIRECTION_UP
from gradhermetic_cover_control.runtime import (
    COMMAND_RATE_LIMIT,
    COMMAND_RATE_WINDOW_SECONDS,
    CoverRuntime,
)

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class _HassBase:
        pass

    class hass:  # type: ignore[no-redef]
        Hass = _HassBase


# Home Assistant event fired by the template cover to carry user commands to this app.
COMMAND_EVENT = "gradhermetic_command"

# Seconds to wait after startup before reading the real cover's position, letting entity state
# settle.
STARTUP_DELAY_SECONDS = 3


class GradhermeticCoverControl(hass.Hass):
    """
    AppDaemon app wrapping a real cover with Gradhermetic tilt-mode control.
    """

    def initialize(self) -> None:
        """
        AppDaemon startup hook.
        """
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        config = parse_app_config(self.args or {})
        self._config = config
        self._position_entity = f"sensor.gradhermetic_{config.virtual_id}_position"
        self._tilt_mode_entity = f"binary_sensor.gradhermetic_{config.virtual_id}_tilt_mode"
        self._step_up_button = f"input_button.gradhermetic_{config.virtual_id}_step_up"
        self._step_down_button = f"input_button.gradhermetic_{config.virtual_id}_step_down"
        self._tilt_button = f"input_button.gradhermetic_{config.virtual_id}_tilt"
        # Commands are ignored until the first position reading has seeded the belief the safety
        # guards are derived from.
        self._ready = False
        # Last state written to the tilt-mode sensor; see _publish_virtual.
        self._last_tilt_state: Optional[str] = None

        logic = GradhermeticCoverLogic(config.zone, log=self.log)
        self._runtime = CoverRuntime(config=config, logic=logic)

        if self.get_state(config.real_cover, default=None) is None:
            self.log(
                f"Configured real_cover '{config.real_cover}' does not exist in Home "
                "Assistant.", level="ERROR")

        self.listen_state(self._on_real_state, config.real_cover, attribute="all")
        self.listen_event(self._on_command, COMMAND_EVENT)

        # Dashboard step/tilt input_button helpers; each press updates the helper's timestamp state.
        self.listen_state(self._on_step_button, self._step_up_button)
        self.listen_state(self._on_step_button, self._step_down_button)
        self.listen_state(self._on_tilt_button, self._tilt_button)

        if config.knx_move_address or config.knx_step_address or config.knx_tilt_address:
            self.listen_event(self._on_knx, "knx_event")

        self.run_in(self._seed_startup_state, STARTUP_DELAY_SECONDS)

        self.log(f"Initialized for '{config.virtual_id}' wrapping {config.real_cover}.")

    # -- Startup -----------------------------------------------------------------------------------

    def _seed_startup_state(self, kwargs: Dict[str, Any]) -> None:
        """
        Seed the logic's belief from the real cover's first position reading.
        """
        try:
            position, is_moving, direction = self._read_real_position()
            self._apply_actions(self._runtime.logic.on_startup(position, is_moving, direction))
            self._ready = True  # pylint: disable=attribute-defined-outside-init
        except Exception as exc:
            self._report_error("_seed_startup_state", exc)

    # -- Command events ----------------------------------------------------------------------------

    def _on_command(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Route a ``gradhermetic_command`` event addressed to this blind into the logic engine.
        """
        try:
            if str(data.get("virtual_id")) != self._config.virtual_id:
                return
            if not self._ready:
                self.log(f"Ignoring command before startup state is seeded: {data!r}")
                return
            self._apply_actions(self._dispatch_command(data))
        except Exception as exc:
            self._report_error(f"_on_command(data={data!r})", exc)

    def _dispatch_command(self, data: Dict[str, Any]) -> List[Action]:
        """
        Translate a command payload into logic events.
        """
        runtime = self._runtime
        command = data.get("command")
        if command == "open":
            return runtime.logic.on_open()
        if command == "close":
            return runtime.logic.on_close()
        if command == "stop":
            return runtime.logic.on_stop()
        if command == "set_position":
            # Any automation can fire this event, so a malformed payload is ignored rather than
            # raised into _report_error, which would disable the blind.
            raw_position = data.get("position")
            position = _parse_pct(raw_position)
            if position is None:
                self.log(f"Ignoring set_position with invalid position {raw_position!r}",
                         level="WARNING")
                return []
            return runtime.logic.on_set_position(position)
        if command == "set_tilt_mode":
            if data.get("enabled") is None:
                self.log("Ignoring set_tilt_mode without 'enabled'", level="WARNING")
                return []
            return runtime.logic.on_set_tilt_mode(_as_bool(data["enabled"]))
        self.log(f"Ignoring unknown command {command!r}", level="WARNING")
        return []

    # -- Real cover feedback -----------------------------------------------------------------------

    def _on_real_state(self, entity: str, attribute: str, old: Any, new: Any,
                       kwargs: Dict[str, Any]) -> None:
        """
        Feed controller position/motion feedback into the logic engine, including a missing
        position when the cover goes unavailable.
        """
        try:
            position, is_moving, direction = _extract_feedback(new)
            self._apply_actions(self._runtime.logic.on_real_position(position, is_moving,
                                                                     direction))
        except Exception as exc:
            self._report_error(f"_on_real_state(entity={entity!r})", exc)

    def _on_settle(self, kwargs: Dict[str, Any]) -> None:
        """
        Hand a settle-timer firing to the logic, with the controller state read fresh, since the
        timer exists for the case where a state update never arrived.
        """
        try:
            self._runtime.settle_timer_handle = None
            position, is_moving, direction = self._read_real_position()
            self._apply_actions(self._runtime.logic.on_settle_timer(position, is_moving, direction))
        except Exception as exc:
            self._report_error("_on_settle", exc)

    # -- KNX ---------------------------------------------------------------------------------------

    def _on_knx(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Route a KNX wall-button telegram to a long/short/tilt logic event.
        """
        try:
            destination = data.get("destination")
            config = self._config
            # Unconfigured addresses are None, which a destination-less telegram would match.
            if destination is None:
                return
            if destination not in (config.knx_move_address, config.knx_step_address,
                                   config.knx_tilt_address):
                return
            if not self._ready:
                self.log("Ignoring KNX press before startup state is seeded")
                return
            logic = self._runtime.logic
            # The tilt address is a directionless toggle trigger.
            if destination == config.knx_tilt_address:
                if not _knx_trigger(data):
                    return
                self._apply_actions(logic.on_toggle_tilt_mode())
                return
            direction = _knx_direction(data)
            if direction is None:
                return
            if destination == config.knx_move_address:
                actions = logic.on_knx_long(direction)
            else:
                actions = logic.on_knx_short(direction)
            self._apply_actions(actions)
        except Exception as exc:
            self._report_error(f"_on_knx(destination={data.get('destination')!r})", exc)

    # -- Dashboard step/tilt buttons ---------------------------------------------------------------

    def _on_step_button(self, entity: str, attribute: str, old: Any, new: Any,
                        kwargs: Dict[str, Any]) -> None:
        """
        Route an ``input_button`` step press to the step logic, up or down by which helper fired.
        """
        try:
            if not self._is_button_press(old, new):
                return
            if not self._ready:
                self.log("Ignoring step press before startup state is seeded")
                return
            direction = DIRECTION_UP if entity == self._step_up_button else DIRECTION_DOWN
            self._apply_actions(self._runtime.logic.on_step(direction))
        except Exception as exc:
            self._report_error(f"_on_step_button(entity={entity!r})", exc)

    def _on_tilt_button(self, entity: str, attribute: str, old: Any, new: Any,
                        kwargs: Dict[str, Any]) -> None:
        """
        Toggle tilt mode from the ``input_button`` tilt helper.
        """
        try:
            if not self._is_button_press(old, new):
                return
            if not self._ready:
                self.log("Ignoring tilt press before startup state is seeded")
                return
            self._apply_actions(self._runtime.logic.on_toggle_tilt_mode())
        except Exception as exc:
            self._report_error("_on_tilt_button", exc)

    @staticmethod
    def _is_button_press(old: Any, new: Any) -> bool:
        """
        Whether an ``input_button`` state change represents a real press.

        A real press changes one timestamp to another. Transitions into or out of
        unknown/unavailable/None (startup, helper reload, reconnect) are not presses.
        """
        if new in (None, "unknown", "unavailable"):
            return False
        if old in (None, "unknown", "unavailable"):
            return False
        return new != old

    # -- Action dispatch ---------------------------------------------------------------------------

    def _apply_actions(self, actions: List[Action]) -> None:
        """
        Translate declarative logic actions one-to-one into AppDaemon side effects.
        """
        runtime = self._runtime
        if runtime.disabled:
            return
        for action in actions:
            if action.kind == ACTION_MOVE_TO:
                self._command(runtime, "cover/set_cover_position", position=action.position)
            elif action.kind == ACTION_OPEN_FULL:
                self._command(runtime, "cover/open_cover")
            elif action.kind == ACTION_CLOSE_FULL:
                self._command(runtime, "cover/close_cover")
            elif action.kind == ACTION_STOP:
                self._command(runtime, "cover/stop_cover")
            elif action.kind == ACTION_PUBLISH_STATE:
                self._publish_virtual(action.position, action.in_tilt, action.motion)
            elif action.kind == ACTION_ARM_SETTLE_TIMER:
                self._arm_settle(runtime, action.seconds)
            elif action.kind == ACTION_CANCEL_SETTLE_TIMER:
                self._cancel_settle(runtime)
            elif action.kind == ACTION_NOTIFY:
                self._notify(runtime, action.notify_kind, action.message)

    def _command(self, runtime: CoverRuntime, service: str, **data: Any) -> None:
        """
        Issue a real-cover service call, enforcing the command rate limit.
        """
        if runtime.disabled:
            return
        if runtime.record_command(self.datetime()):
            self._disable(runtime)
            return
        self.log(f"Cover {_describe_command(service, data)}")
        self.call_service(service, entity_id=runtime.config.real_cover, **data)

    def _notify(self, runtime: CoverRuntime, kind: Optional[str], message: Optional[str]) -> None:
        """
        Render a logic notification as a Home Assistant persistent notification.
        """
        title = ("GradhermeticCoverControl stalled"
                 if kind == NOTIFY_STALL else "GradhermeticCoverControl error")
        self.log(f"{kind}: {message}", level="ERROR")
        self.call_service(
            "persistent_notification/create",
            title=title,
            message=f"Cover '{runtime.config.virtual_id}' {message}",
        )

    def _publish_virtual(self, virtual_position: Optional[int], in_tilt: Optional[bool],
                         motion: Optional[str]) -> None:
        """
        Write the position sensor (with its ``motion`` attribute) and, if it changed, the tilt-mode
        sensor.

        The tilt-mode sensor is only rewritten on change because it is mirrored onto KNX. A fresh
        app instance (every reload, including an HA restart) always writes it once, which
        re-creates it if HA has forgotten it.

        The position is sent as a string: AppDaemon drops falsy values from the request body, so
        an integer 0 would be lost and HA would reject the update.
        """
        self.set_state(
            self._position_entity,
            state="unavailable" if virtual_position is None else str(virtual_position),
            attributes={
                "friendly_name": f"{self._config.virtual_name} Position",
                "unit_of_measurement": "%",
                "motion": motion or MOTION_IDLE,
            },
        )
        tilt_state = "on" if in_tilt else "off"
        if tilt_state == self._last_tilt_state:
            return
        self._last_tilt_state = tilt_state
        self.set_state(
            self._tilt_mode_entity,
            state=tilt_state,
            attributes={
                "friendly_name": f"{self._config.virtual_name} Slat Mode",
                "icon": "mdi:blinds-horizontal",
            },
        )

    # -- Settle timer ------------------------------------------------------------------------------

    def _arm_settle(self, runtime: CoverRuntime, seconds: Optional[float]) -> None:
        """
        (Re)start the settle timer used as a plan-advancement fallback.
        """
        self._cancel_settle(runtime)
        runtime.settle_timer_handle = self.run_in(self._on_settle, seconds)

    def _cancel_settle(self, runtime: CoverRuntime) -> None:
        """
        Cancel the settle timer if scheduled.
        """
        if runtime.settle_timer_handle is not None:
            self.cancel_timer(runtime.settle_timer_handle)
            runtime.settle_timer_handle = None

    # -- Safety ------------------------------------------------------------------------------------

    def _read_real_position(self) -> Tuple[Optional[int], bool, Optional[str]]:
        """
        Read the real cover's current position, motion and direction from Home Assistant.
        """
        return _extract_feedback(self.get_state(self._config.real_cover, attribute="all"))

    def _disable(self, runtime: CoverRuntime) -> None:
        """
        Permanently disable this blind due to rate limiting and notify.
        """
        runtime.disabled = True
        runtime.logic.disable()
        self._cancel_settle(runtime)
        self.log(
            f"DISABLED: real-cover command rate limit exceeded ({COMMAND_RATE_LIMIT} "
            f"commands in {COMMAND_RATE_WINDOW_SECONDS}s). Restart AppDaemon to re-enable.",
            level="ERROR")
        self.call_service(
            "persistent_notification/create",
            title="GradhermeticCoverControl disabled",
            message=(f"Cover '{runtime.config.virtual_id}' has been disabled because it sent more "
                     f"than {COMMAND_RATE_LIMIT} commands in {COMMAND_RATE_WINDOW_SECONDS} "
                     "seconds. This likely indicates a bug. Restart AppDaemon to re-enable."),
        )

    def _report_error(self, context: str, exc: Exception) -> None:
        """
        Log an unhandled callback exception, disable the blind, and notify Home Assistant.
        """
        tb = traceback.format_exc()
        self.log(f"Unhandled exception in {context}: {type(exc).__name__}: {exc}\n{tb}",
                 level="ERROR")
        runtime = getattr(self, "_runtime", None)
        if runtime is not None:
            runtime.disabled = True
            runtime.logic.disable()
            self._cancel_settle(runtime)
        self.call_service(
            "persistent_notification/create",
            title="GradhermeticCoverControl error",
            message=f"{context}\n{type(exc).__name__}: {exc}",
        )


def _describe_command(service: str, data: Dict[str, Any]) -> str:
    """
    Render a real-cover service call as a short phrase for the log.
    """
    if service == "cover/set_cover_position":
        return f"move to {data.get('position')}%"
    if service == "cover/open_cover":
        return "open fully"
    if service == "cover/close_cover":
        return "close fully"
    if service == "cover/stop_cover":
        return "stop"
    return f"{service} {data or ''}".strip()


def _extract_feedback(state: Any) -> Tuple[Optional[int], bool, Optional[str]]:
    """
    Extract (current_position, is_moving, direction) from a full Home Assistant cover state object.

    An unreadable position yields None ("unavailable") rather than raising, so a flaky integration
    cannot disable the blind.
    """
    if not isinstance(state, dict):
        return None, False, None
    raw_position = state.get("attributes", {}).get("current_position")
    cover_state = state.get("state")
    direction = {"opening": DIRECTION_UP, "closing": DIRECTION_DOWN}.get(cover_state)
    is_moving = direction is not None
    return _parse_pct(raw_position), is_moving, direction


def _parse_pct(raw: Any) -> Optional[int]:
    """
    Read a percentage arriving from Home Assistant as a whole percent, or None if it is unreadable.
    """
    if isinstance(raw, bool):
        return None
    try:
        return to_whole_pct(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None


def _knx_direction(data: Dict[str, Any]) -> Optional[str]:
    """
    Decode a KNX up/down telegram into a direction, following the repo convention 0 = up, 1 = down.
    """
    raw = data.get("data")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    # A malformed value is ignored rather than raised into _report_error, which would disable it.
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return DIRECTION_UP if value == 0 else DIRECTION_DOWN


def _knx_trigger(data: Dict[str, Any]) -> bool:
    """
    Whether a KNX tilt telegram is a toggle trigger: only a 1 is.

    Ignoring 0 makes a pushbutton configured as a two-state switch (1 on press, 0 on release)
    toggle once per press instead of twice.
    """
    raw = data.get("data")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return False
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False
    return value == 1


def _as_bool(value: Any) -> bool:
    """
    Coerce a service-call value into a boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "on", "yes", "1")
