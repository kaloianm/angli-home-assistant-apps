"""
AppDaemon entry point for GradhermeticCoverControl.

The virtual cover is surfaced to Home Assistant without MQTT: a small template cover (defined in the
HA config) forwards user commands to this app as ``gradhermetic_command`` events. AppDaemon cannot
register a controllable cover entity by itself, so that template is the one irreducible HA-side
shim; everything else the app owns. The displayed position is not an ``input_number`` helper the
user must declare -- the app publishes it directly via ``set_state`` onto
``sensor.gradhermetic_<id>_position``, which the template cover reads. Beside it goes
``binary_sensor.gradhermetic_<id>_tilt_mode``: whether that position is a slat angle rather than a
height. Nothing in HA can derive that -- a real position inside the tilt zone is neither necessary
nor sufficient for being latched, which is the whole reason the app event-sources a latch belief --
so the app publishing its own belief is the only honest source, and it is what lets a dashboard show
which mode the blind is in.

Stepping and tilt engagement are exposed as dumb ``input_button`` helpers the app listens on:
``..._step_up`` / ``..._step_down`` stop a moving blind, step the slats while latched, and step the
height otherwise -- the same rule a KNX stop/step object follows -- and ``..._tilt`` toggles tilt
mode (cancelling an entry or exit in flight).

The position sensor also carries a ``motion`` attribute (``opening`` / ``closing`` / ``idle``) so
the template cover can show travel as it happens; the app publishes on every feedback event, not
only when a move ends.

Every decision -- which sequence to run, when a waypoint is reached, when the settle timer is armed
or cancelled, when a stall is declared -- is made in the pure core. What is left here is transport:
listening and filtering, gating commands until the startup state is seeded, decoding KNX telegrams
and button presses, the command rate limit, the callback error boundary, and a one-to-one
translation of :class:`Action` values into service calls.
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
        # Commands are ignored until the first position reading has seeded the belief; a command
        # arriving before that would run against unseeded state, and the guards that keep the
        # mechanism safe are all derived from the belief.
        self._ready = False
        # The tilt-mode sensor's last written state ("on"/"off"), so a publish that only moves the
        # position does not rewrite it too; see _publish_virtual. None on a fresh instance, which is
        # what makes the very first publish always write it -- AppDaemon builds a fresh instance on
        # every reload, including a Home Assistant restart, which is what re-creates the entity if
        # Home Assistant has forgotten it.
        self._last_tilt_state: Optional[str] = None

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

        if config.knx_move_address or config.knx_step_address or config.knx_tilt_address:
            self.listen_event(self._on_knx, "knx_event")

        self.run_in(self._seed_startup_state, STARTUP_DELAY_SECONDS)

        self.log(f"GradhermeticCoverControl initialized for '{config.virtual_id}' "
                 f"wrapping {config.real_cover}.")

    # -- Startup -----------------------------------------------------------------------------------

    def _seed_startup_state(self, kwargs: Dict[str, Any]) -> None:
        """
        Seed the logic's belief from the real cover's first position reading.

        State is not persisted across restarts, so the belief follows from that reading alone. No
        movement is commanded here: an ambiguous position simply leaves the latch belief unknown,
        and the first action that needs a trusted position re-references the actuator itself.
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
            position, is_moving, direction = _extract_feedback(new)
            self._apply_actions(
                self._runtime.logic.on_real_position(position, is_moving, direction))
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
            position, is_moving, direction = self._read_real_position()
            self._apply_actions(
                self._runtime.logic.on_settle_timer(position, is_moving, direction))
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
            # Guard against a telegram with no destination matching a None-valued address attribute
            # when only some of the three KNX addresses are configured.
            if destination is None:
                return
            if destination not in (config.knx_move_address, config.knx_step_address,
                                   config.knx_tilt_address):
                return
            if not self._ready:
                self.log("Ignoring KNX press before startup state is seeded")
                return
            logic = self._runtime.logic
            # The tilt address carries no direction, so it is handled before any direction is
            # decoded: it is a trigger, and what it does depends only on the current mode.
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
        Route an ``input_button`` step press to the step logic.

        The direction follows which helper fired. The logic decides what a step means: stop while
        anything is moving, a slat step while latched, a height step otherwise.
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

    def _publish_virtual(self, virtual_position: Optional[float], in_tilt: Optional[bool],
                         motion: Optional[str]) -> None:
        """
        Publish what the virtual cover shows: its position, which scale that position is on, and
        whether it is travelling.

        The app owns both entities directly via ``set_state`` -- no user-declared helpers are
        required -- creating ``sensor.gradhermetic_<id>_position`` and
        ``binary_sensor.gradhermetic_<id>_tilt_mode`` in Home Assistant. Both come from one action
        so a dashboard can never read a slat angle as though it were a height, but they are not
        rewritten at the same rate: the logic now publishes on every feedback event, which is what
        makes the position sensor track travel percent by percent, while the mode changes only a
        few times per plan. Rewriting the binary sensor with the value it already holds on every one
        of those position updates is pure churn -- it is also mirrored onto the KNX bus by an
        expose -- so it is skipped whenever the mode has not actually changed since the last write.
        ``self._last_tilt_state`` is instance state, not Home Assistant state: AppDaemon builds a
        fresh instance of this app whenever it reloads it (including every Home Assistant restart),
        and a fresh instance starts with nothing to compare against, so its first publish always
        writes the sensor -- which is what re-creates it if Home Assistant has forgotten it. The
        motion rides on the position sensor as an attribute, which the template cover's state
        template reads to report ``opening`` / ``closing``. An unknown position publishes the sensor
        as ``unavailable`` rather than leaving a stale number in it.

        The position goes out as a *string*. Home Assistant stores every state as one anyway and
        the template cover reads it back through ``| int(0)``, but the integer 0 -- what the closed
        slat edge publishes -- does not survive the trip: the write comes back ``400 Bad Request``
        carrying the attributes and no state at all, and the sensor keeps the number it last held.
        Sending text puts every value on the same path.
        """
        self.set_state(
            self._position_entity,
            state=("unavailable"
                   if virtual_position is None else str(to_command(virtual_position))),
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

    def _read_real_position(self) -> Tuple[Optional[float], bool, Optional[str]]:
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


def _extract_feedback(state: Any) -> Tuple[Optional[float], bool, Optional[str]]:
    """
    Extract (current_position, is_moving, direction) from a full Home Assistant cover state object.

    The direction is the real one the cover integration reports through its ``opening`` /
    ``closing`` state, or None when it is not moving.

    A missing or unreadable position yields None, which the logic treats as "the cover is
    unavailable": bad state from a flaky integration must not reach the error boundary and disable
    the blind.
    """
    if not isinstance(state, dict):
        return None, False, None
    raw_position = state.get("attributes", {}).get("current_position")
    cover_state = state.get("state")
    direction = {"opening": DIRECTION_UP, "closing": DIRECTION_DOWN}.get(cover_state)
    is_moving = direction is not None
    try:
        return float(raw_position), is_moving, direction
    except (TypeError, ValueError):
        return None, is_moving, direction


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


def _knx_trigger(data: Dict[str, Any]) -> bool:
    """
    Decode a KNX tilt telegram into "act" or "ignore", acting only on a 1.

    The tilt object is a stateless trigger, not a mode level: what it means is "toggle", and the app
    toggles off its own latch belief exactly as the dashboard tilt helper does. Ignoring 0 is what
    makes it correct on a momentary pushbutton parameterized as a two-state switch, which sends 1 on
    press and 0 on release -- toggling on both edges would cancel out to nothing per press.
    """
    raw = data.get("data")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return False
    # As in _knx_direction, a malformed telegram value is bad external input rather than an app bug.
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
