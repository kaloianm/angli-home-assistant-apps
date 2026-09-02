"""
AppDaemon entry point for GradhermeticCoverControl.

The virtual cover is surfaced to Home Assistant without MQTT: a small template cover (defined in the
HA config) forwards user commands to this app as ``gradhermetic_command`` events. AppDaemon cannot
register a controllable cover entity by itself, so that template is the one irreducible HA-side
shim; everything else the app owns. The displayed position is not an ``input_number`` helper the
user must declare -- the app publishes it directly via ``set_state`` onto
``sensor.gradhermetic_<id>_position``, which the template cover reads.

Slat stepping and tilt engagement are exposed as dumb ``input_button`` helpers the app listens on:
``..._step_up`` / ``..._step_down`` step the slats within the tilt zone while latched (clamped at
both edges) and otherwise enter the zone when the press points toward it, and ``..._tilt`` toggles
tilt mode. Unlike a KNX wall-button short press, the step helpers never *leave* the zone and never
stop a move in flight -- that is the tilt helper's and the cover's job.

Every decision -- which sequence to run, when a waypoint is reached, when the settle timer is armed
or cancelled, when a stall is declared -- is made in the pure core. What is left here is transport:
listening and filtering, gating commands until startup recovery has run, decoding KNX telegrams and
button presses, the command rate limit, the callback error boundary, and a one-to-one translation of
:class:`Action` values into service calls.
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
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    NOTIFY_STALL,
    Action,
)
from gradhermetic_cover_control.geometry import to_command
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

# Seconds to wait after startup before running position recovery, letting entity state settle.
RECOVERY_DELAY_SECONDS = 3


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
        self._step_up_button = f"input_button.gradhermetic_{config.virtual_id}_step_up"
        self._step_down_button = f"input_button.gradhermetic_{config.virtual_id}_step_down"
        self._tilt_button = f"input_button.gradhermetic_{config.virtual_id}_tilt"
        # Commands are ignored until startup recovery has established a known-safe state; a command
        # arriving in the recovery window would run against unseeded state and could clobber the
        # in-flight recovery plan.
        self._ready = False

        logic = GradhermeticCoverLogic(config.zone, log=self.log)
        self._runtime = CoverRuntime(config=config, logic=logic)

        if self.get_state(config.real_cover, default=None) is None:
            self.log(
                f"Configured real_cover '{config.real_cover}' does not exist in Home "
                "Assistant.", level="ERROR")

        self.listen_state(self._on_real_state, config.real_cover, attribute="all")
        self.listen_event(self._on_command, COMMAND_EVENT)

        # Dashboard step/tilt controls: dumb input_button helpers whose presses route into the same
        # logic the KNX wall button uses. Each press updates the helper's timestamp state.
        self.listen_state(self._on_step_button, self._step_up_button)
        self.listen_state(self._on_step_button, self._step_down_button)
        self.listen_state(self._on_tilt_button, self._tilt_button)

        if config.knx_move_address or config.knx_step_address:
            self.listen_event(self._on_knx, "knx_event")

        self.register_service("gradhermetic_cover_control/set_tilt_mode", self._on_set_tilt_mode)

        self.run_in(self._run_recovery, RECOVERY_DELAY_SECONDS)

        self.log(f"GradhermeticCoverControl initialized for '{config.virtual_id}' "
                 f"wrapping {config.real_cover}.")

    # -- Recovery ----------------------------------------------------------------------------------

    def _run_recovery(self, kwargs: Dict[str, Any]) -> None:
        """
        Establish a known-safe state at startup.

        State is not persisted across restarts, so the logic decides from the first position reading
        whether whole-height control can resume or the blind has to be recovered upward.
        """
        try:
            position, is_moving = self._read_real_position()
            self._apply_actions(self._runtime.logic.on_startup(position, is_moving))
            self._ready = True  # pylint: disable=attribute-defined-outside-init
        except Exception as exc:
            self._report_error("_run_recovery", exc)

    # -- Command events ----------------------------------------------------------------------------

    def _on_command(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Route a ``gradhermetic_command`` event addressed to this blind into the logic engine.
        """
        try:
            if str(data.get("virtual_id")) != self._config.virtual_id:
                return
            if not self._ready:
                self.log(f"Ignoring command during startup recovery: {data!r}")
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
            # The event bus is open to any HA automation, so a malformed payload is bad input, not
            # an app bug: ignore it rather than letting it reach _report_error and disable it.
            raw_position = data.get("position")
            try:
                position = float(raw_position)
            except (TypeError, ValueError):
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
        Feed controller position/motion feedback into the logic engine.

        A missing position (the cover went unavailable) is forwarded too, so the logic drops its
        stale position and motion beliefs rather than reasoning from them indefinitely.
        """
        try:
            position, is_moving = _extract_position(new)
            self._apply_actions(self._runtime.logic.on_real_position(position, is_moving))
        except Exception as exc:
            self._report_error(f"_on_real_state(entity={entity!r})", exc)

    def _on_settle(self, kwargs: Dict[str, Any]) -> None:
        """
        Hand a settle-timer firing to the logic, with the controller state read as it fired.

        Reading here rather than relying on the last feedback event keeps the fallback honest even
        if a state update never reached us -- which is the situation the timer exists for.
        """
        try:
            self._runtime.settle_timer_handle = None
            position, is_moving = self._read_real_position()
            self._apply_actions(self._runtime.logic.on_settle_timer(position, is_moving))
        except Exception as exc:
            self._report_error("_on_settle", exc)

    # -- KNX ---------------------------------------------------------------------------------------

    def _on_knx(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Route a KNX wall-button telegram to a long/short logic event.
        """
        try:
            destination = data.get("destination")
            config = self._config
            # Guard against a telegram with no destination matching a None-valued address attribute
            # when only one of the two KNX addresses is configured.
            if destination is None:
                return
            if destination not in (config.knx_move_address, config.knx_step_address):
                return
            if not self._ready:
                self.log("Ignoring KNX press during startup recovery")
                return
            direction = _knx_direction(data)
            if direction is None:
                return
            if destination == config.knx_move_address:
                actions = self._runtime.logic.on_knx_long(direction)
            else:
                actions = self._runtime.logic.on_knx_short(direction)
            self._apply_actions(actions)
        except Exception as exc:
            self._report_error(f"_on_knx(destination={data.get('destination')!r})", exc)

    # -- Dashboard step/tilt buttons ---------------------------------------------------------------

    def _on_step_button(self, entity: str, attribute: str, old: Any, new: Any,
                        kwargs: Dict[str, Any]) -> None:
        """
        Route an ``input_button`` step press to the slat-step logic.

        The direction follows which helper fired. While latched these helpers adjust slats within
        the tilt zone, clamping at both edges; while not latched they enter the zone when the press
        points toward it, exactly as a KNX wall-button short press would. What they never do is
        leave tilt or stop a move in flight -- leaving is the dedicated tilt helper's job.
        """
        try:
            if not self._is_button_press(old, new):
                return
            if not self._ready:
                self.log("Ignoring step press during startup recovery")
                return
            direction = DIRECTION_UP if entity == self._step_up_button else DIRECTION_DOWN
            self._apply_actions(self._runtime.logic.on_slat_step(direction))
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
                self.log("Ignoring tilt press during startup recovery")
                return
            logic = self._runtime.logic
            self._apply_actions(logic.on_set_tilt_mode(not logic.in_tilt))
        except Exception as exc:
            self._report_error("_on_tilt_button", exc)

    @staticmethod
    def _is_button_press(old: Any, new: Any) -> bool:
        """
        Whether an ``input_button`` state change represents a real press.

        A real press is a timestamp -> different-timestamp transition. Transitions into or out of
        unknown/unavailable/None (startup, HA helper reload, reconnect state restore) carry a fresh
        timestamp on one side but are not presses, so both the old and the new state must be real
        timestamps.
        """
        if new in (None, "unknown", "unavailable"):
            return False
        if old in (None, "unknown", "unavailable"):
            return False
        return new != old

    # -- Custom service ----------------------------------------------------------------------------

    def _on_set_tilt_mode(self, namespace: str, domain: str, service: str, data: Dict[str,
                                                                                      Any]) -> None:
        """
        Handle ``gradhermetic_cover_control.set_tilt_mode``.
        """
        try:
            if not self._service_targets_me(data):
                return
            if not self._ready:
                self.log("Ignoring set_tilt_mode during startup recovery")
                return
            if data.get("enabled") is None:
                self.log("Ignoring set_tilt_mode without 'enabled'", level="WARNING")
                return
            self._apply_actions(self._runtime.logic.on_set_tilt_mode(_as_bool(data["enabled"])))
        except Exception as exc:
            self._report_error("_on_set_tilt_mode", exc)

    def _service_targets_me(self, data: Dict[str, Any]) -> bool:
        """
        Whether a set_tilt_mode call is addressed to this instance.

        A call with neither ``virtual_id`` nor ``entity_id`` applies to every instance; otherwise it
        only applies when the target matches this blind.
        """
        config = self._config
        target_virtual_id = data.get("virtual_id")
        if target_virtual_id is not None:
            return str(target_virtual_id) == config.virtual_id
        target_entity = data.get("entity_id")
        if target_entity is not None:
            expected = f"cover.gradhermetic_{config.virtual_id}"
            if isinstance(target_entity, (list, tuple)):
                return any(str(entity) == expected for entity in target_entity)
            return str(target_entity) == expected
        return True

    # -- Action dispatch ---------------------------------------------------------------------------

    def _apply_actions(self, actions: List[Action]) -> None:
        """
        Translate declarative logic actions into AppDaemon side effects.

        This is a mechanical one-to-one translation: every decision, including when the settle timer
        is armed or cancelled, was made in the pure core.
        """
        runtime = self._runtime
        if runtime.disabled:
            return
        for action in actions:
            if action.kind == ACTION_MOVE_TO:
                self._command(runtime, "cover/set_cover_position",
                              position=to_command(action.position))
            elif action.kind == ACTION_OPEN_FULL:
                self._command(runtime, "cover/open_cover")
            elif action.kind == ACTION_CLOSE_FULL:
                self._command(runtime, "cover/close_cover")
            elif action.kind == ACTION_STOP:
                self._command(runtime, "cover/stop_cover")
            elif action.kind == ACTION_PUBLISH_POSITION:
                self._publish_virtual(action.position)
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

    def _publish_virtual(self, virtual_position: Optional[float]) -> None:
        """
        Publish the virtual cover position the template cover displays.

        The app owns this value directly via ``set_state`` -- no user-declared ``input_number``
        helper is required -- creating ``sensor.gradhermetic_<id>_position`` in Home Assistant.
        """
        if virtual_position is None:
            return
        self.set_state(
            self._position_entity,
            state=to_command(virtual_position),
            attributes={
                "friendly_name": f"{self._config.virtual_name} Position",
                "unit_of_measurement": "%",
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

    def _read_real_position(self) -> Tuple[Optional[float], bool]:
        """
        Read the real cover's current position and motion from Home Assistant.
        """
        return _extract_position(self.get_state(self._config.real_cover, attribute="all"))

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
    Render a real-cover service call as the short phrase the log shows.

    The raw ``service + payload`` form is noisy and unreadable in the log; each of the four services
    the app issues gets a human-readable description instead.
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


def _extract_position(state: Any) -> Tuple[Optional[float], bool]:
    """
    Extract (current_position, is_moving) from a full Home Assistant cover state object.

    A missing or unreadable position yields None, which the logic treats as "the cover is
    unavailable": bad state from a flaky integration must not reach the error boundary and disable
    the blind.
    """
    if not isinstance(state, dict):
        return None, False
    raw_position = state.get("attributes", {}).get("current_position")
    is_moving = state.get("state") in ("opening", "closing")
    try:
        return float(raw_position), is_moving
    except (TypeError, ValueError):
        return None, is_moving


def _knx_direction(data: Dict[str, Any]) -> Optional[str]:
    """
    Decode a KNX up/down telegram into a direction, following the repo convention 0 = up, 1 = down.
    """
    raw = data.get("data")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    # A malformed telegram value is bad external input, not an app bug: ignore it (the caller treats
    # None as "no direction") rather than raising into _report_error and disabling the blind.
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return DIRECTION_UP if value == 0 else DIRECTION_DOWN


def _as_bool(value: Any) -> bool:
    """
    Coerce a service-call value into a boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "on", "yes", "1")
