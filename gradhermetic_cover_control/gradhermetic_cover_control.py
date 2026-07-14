"""
AppDaemon entry point for GradhermeticCoverControl.

The virtual cover is surfaced to Home Assistant without MQTT: a small template cover (defined in the
HA config) forwards user commands to this app as ``gradhermetic_command`` events. AppDaemon cannot
register a controllable cover entity by itself, so that template is the one irreducible HA-side shim;
everything else the app owns. The displayed position is not an ``input_number`` helper the user must
declare -- the app publishes it directly via ``set_state`` onto ``sensor.gradhermetic_<id>_position``,
which the template cover reads.

Slat stepping and tilt engagement are exposed as dumb ``input_button`` helpers the app listens on:
``..._step_up`` / ``..._step_down`` adjust slats only (step within the tilt zone, clamped at both
edges; a no-op when not latched), and ``..._tilt`` toggles tilt mode. Unlike a KNX wall-button short
press, the step helpers never enter or leave the zone -- that is the tilt helper's job. All
decision-making stays in the pure logic engine; this adapter only wires transport and drives the
real cover.
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional, Tuple

from gradhermetic_cover_control.config import parse_app_config
from gradhermetic_cover_control.logic import (
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_OPEN_FULL,
    ACTION_PERSIST_STATE,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    DIRECTION_DOWN,
    DIRECTION_UP,
    Action,
    GradhermeticCoverLogic,
    LogicConfig,
)
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

# Fallback used to advance a movement plan if a final position update is never observed.
SETTLE_TIMEOUT_SECONDS = 45

_MOVE_SERVICES = ("cover/set_cover_position", "cover/open_cover", "cover/close_cover")


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

        logic = GradhermeticCoverLogic(
            LogicConfig(
                tilt_zone_upper_pct=config.tilt_zone_upper_pct,
                tilt_zone_lower_pct=config.tilt_zone_lower_pct,
                tilt_zone_epsilon_pct=config.tilt_zone_epsilon_pct,
                tilt_step_pct=config.tilt_step_pct,
            ),
            log=lambda message: self.log(f"[{config.virtual_id}] {message}"),
        )
        self._runtime = CoverRuntime(config=config, logic=logic)

        if self.get_state(config.real_cover, default=None) is None:
            self.log(
                f"[{config.virtual_id}] Configured real_cover '{config.real_cover}' does not "
                "exist in Home Assistant.", level="ERROR")

        self._runtime.state_listener_handle = self.listen_state(self._on_real_state,
                                                                config.real_cover, attribute="all")
        self._runtime.command_listener_handle = self.listen_event(self._on_command, COMMAND_EVENT)

        # Dashboard step/tilt controls: dumb input_button helpers whose presses route into the same
        # logic the KNX wall button uses. Each press updates the helper's timestamp state.
        self._runtime.step_up_listener_handle = self.listen_state(self._on_step_button,
                                                                  self._step_up_button)
        self._runtime.step_down_listener_handle = self.listen_state(self._on_step_button,
                                                                    self._step_down_button)
        self._runtime.tilt_listener_handle = self.listen_state(self._on_tilt_button,
                                                               self._tilt_button)

        if config.knx_move_address or config.knx_step_address:
            self._runtime.knx_listener_handle = self.listen_event(self._on_knx, "knx_event")

        self.register_service("gradhermetic_cover_control/set_tilt_mode", self._on_set_tilt_mode)

        self.run_in(self._run_recovery, RECOVERY_DELAY_SECONDS)

        self.log(f"GradhermeticCoverControl initialized for '{config.virtual_id}' "
                 f"wrapping {config.real_cover}.")

    # -- Recovery ----------------------------------------------------------------------------------

    def _run_recovery(self, kwargs: Dict[str, Any]) -> None:
        """
        Establish a known-safe state at startup.

        State is not persisted across restarts. If the blind's real position is outside the tilt
        zone it cannot be latched, so it is safe to resume whole-height control. If it is inside
        (or near) the zone the latch state is ambiguous, so recover upward to a known state,
        honoring the upward-only protection rule.
        """
        runtime = self._runtime
        try:
            position, _ = self._read_real_position()
            low = runtime.config.tilt_zone_lower_pct - runtime.config.tilt_zone_epsilon_pct
            high = runtime.config.tilt_zone_upper_pct + runtime.config.tilt_zone_epsilon_pct
            ambiguous = position is None or low <= position <= high
            runtime.logic.seed_state(position, False)
            if ambiguous:
                self.log(f"[{runtime.config.virtual_id}] Startup position {position} is unknown or "
                         "near the tilt zone; recovering fully open.")
                self._apply_actions(runtime.logic.on_recover())
            else:
                self.log(f"[{runtime.config.virtual_id}] Startup position {position} is outside "
                         "the tilt zone; resuming whole-height control.")
                self._publish_virtual(runtime.logic.current_virtual_position())
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
                self.log(f"[{self._config.virtual_id}] Ignoring command during startup recovery: "
                         f"{data!r}")
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
            # The event bus is open to any HA automation, so a malformed payload is bad input, not an
            # app bug: ignore it rather than letting it reach _report_error and disable the blind.
            raw_position = data.get("position")
            try:
                position = float(raw_position)
            except (TypeError, ValueError):
                self.log(
                    f"[{runtime.config.virtual_id}] Ignoring set_position with invalid position "
                    f"{raw_position!r}", level="WARNING")
                return []
            return runtime.logic.on_set_position(position)
        if command == "set_tilt_mode":
            if data.get("enabled") is None:
                self.log(f"[{runtime.config.virtual_id}] Ignoring set_tilt_mode without 'enabled'",
                         level="WARNING")
                return []
            return runtime.logic.on_set_tilt_mode(_as_bool(data["enabled"]))
        self.log(f"[{runtime.config.virtual_id}] Ignoring unknown command {command!r}",
                 level="WARNING")
        return []

    # -- Real cover feedback -----------------------------------------------------------------------

    def _on_real_state(self, entity: str, attribute: str, old: Any, new: Any,
                       kwargs: Dict[str, Any]) -> None:
        """
        Feed controller position/motion feedback into the logic engine.
        """
        try:
            position, is_moving = _extract_position(new)
            if position is None:
                return
            self._apply_actions(self._runtime.logic.on_real_position(position, is_moving))
        except Exception as exc:
            self._report_error(f"_on_real_state(entity={entity!r})", exc)

    def _on_settle(self, kwargs: Dict[str, Any]) -> None:
        """
        Advance a stalled plan using the controller's current reported position.
        """
        try:
            self._runtime.settle_timer_handle = None
            logic = self._runtime.logic
            position, is_moving = self._read_real_position()
            if position is None:
                # An unreadable position (cover unavailable) with a plan still pending is a genuine
                # stall; surface it rather than leaving the plan hanging forever with no notice.
                if logic.has_pending_plan:
                    self._report_stall(None)
                return
            had_plan = logic.has_pending_plan
            actions = logic.on_real_position(position, is_moving)
            self._apply_actions(actions)
            # A healthy plan either completes or advances to the next waypoint (which re-arms the
            # settle timer). If it did neither, the current waypoint was not reached yet.
            if had_plan and logic.has_pending_plan and not actions:
                # The settle timeout is an inactivity timeout, not a hard travel-time cap: while the
                # blind is still reporting motion the move is simply long, so re-arm and keep waiting
                # rather than stopping it and raising a false obstruction alert.
                if is_moving:
                    self._restart_settle(self._runtime)
                    return
                # Settled but short of target: surface it instead of pending forever, and stop the
                # blind where it is.
                self._report_stall(position)
        except Exception as exc:
            self._report_error("_on_settle", exc)

    def _report_stall(self, position: Optional[float]) -> None:
        """
        Abandon a stalled movement plan and notify Home Assistant.
        """
        runtime = self._runtime
        self.log(
            f"[{runtime.config.virtual_id}] Plan stalled: waypoint not reached within "
            f"{SETTLE_TIMEOUT_SECONDS}s (position {position}). Stopping and clearing the plan.",
            level="ERROR")
        self._apply_actions(runtime.logic.on_stop())
        self.call_service(
            "persistent_notification/create",
            title="GradhermeticCoverControl stalled",
            message=(
                f"Cover '{runtime.config.virtual_id}' did not reach its target position within "
                f"{SETTLE_TIMEOUT_SECONDS} seconds and was stopped. Check the blind for a "
                "mechanical obstruction or a misconfigured tilt zone."),
        )

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
                self.log(f"[{config.virtual_id}] Ignoring KNX press during startup recovery")
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

        The direction follows which helper fired. Unlike a KNX wall-button short press, these helpers
        only adjust slats within the tilt zone (clamping at both edges) and do nothing when the blind
        is not latched -- entering and leaving tilt is the dedicated tilt helper's job.
        """
        try:
            if not self._is_button_press(old, new):
                return
            if not self._ready:
                self.log(f"[{self._config.virtual_id}] Ignoring step press during startup recovery")
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
                self.log(f"[{self._config.virtual_id}] Ignoring tilt press during startup recovery")
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
                self.log(f"[{self._config.virtual_id}] Ignoring set_tilt_mode during startup "
                         "recovery")
                return
            if data.get("enabled") is None:
                self.log(f"[{self._config.virtual_id}] Ignoring set_tilt_mode without 'enabled'",
                         level="WARNING")
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
        """
        runtime = self._runtime
        if runtime.disabled:
            return
        for action in actions:
            if action.kind == ACTION_MOVE_TO:
                self._command(runtime, "cover/set_cover_position",
                              position=int(round(_clamp_pct(action.position))))
            elif action.kind == ACTION_OPEN_FULL:
                self._command(runtime, "cover/open_cover")
            elif action.kind == ACTION_CLOSE_FULL:
                self._command(runtime, "cover/close_cover")
            elif action.kind == ACTION_STOP:
                self._command(runtime, "cover/stop_cover")
                self._cancel_settle(runtime)
            elif action.kind == ACTION_PUBLISH_POSITION:
                self._publish_virtual(action.position)
            elif action.kind == ACTION_PERSIST_STATE:
                # State is intentionally not persisted; startup recovery re-establishes safe state.
                pass

    def _command(self, runtime: CoverRuntime, service: str, **data: Any) -> None:
        """
        Issue a real-cover service call, enforcing the command rate limit.
        """
        if runtime.disabled:
            return
        if runtime.record_command(self.datetime()):
            self._disable(runtime)
            return
        self.log(f"[{runtime.config.virtual_id}] {service} {data or ''}")
        self.call_service(service, entity_id=runtime.config.real_cover, **data)
        if service in _MOVE_SERVICES:
            self._restart_settle(runtime)

    def _publish_virtual(self, virtual_position: Optional[float]) -> None:
        """
        Publish the virtual cover position the template cover displays.

        The app owns this value directly via ``set_state`` -- no user-declared ``input_number`` helper
        is required -- creating/updating ``sensor.gradhermetic_<id>_position`` in Home Assistant.
        """
        if virtual_position is None:
            return
        self.set_state(
            self._position_entity,
            state=int(round(_clamp_pct(virtual_position))),
            attributes={
                "friendly_name": f"{self._config.virtual_name} Position",
                "unit_of_measurement": "%",
            },
        )

    # -- Settle timer ------------------------------------------------------------------------------

    def _restart_settle(self, runtime: CoverRuntime) -> None:
        """
        (Re)start the settle timer used as a plan-advancement fallback.
        """
        self._cancel_settle(runtime)
        runtime.settle_timer_handle = self.run_in(self._on_settle, SETTLE_TIMEOUT_SECONDS)

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
            f"[{runtime.config.virtual_id}] DISABLED: real-cover command rate limit exceeded "
            f"({COMMAND_RATE_LIMIT} commands in {COMMAND_RATE_WINDOW_SECONDS}s). Restart "
            "AppDaemon to re-enable.", level="ERROR")
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


def _extract_position(state: Any) -> Tuple[Optional[float], bool]:
    """
    Extract (current_position, is_moving) from a full Home Assistant cover state object.
    """
    if not isinstance(state, dict):
        return None, False
    raw_position = state.get("attributes", {}).get("current_position")
    is_moving = state.get("state") in ("opening", "closing")
    if raw_position is None:
        return None, is_moving
    return float(raw_position), is_moving


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


def _clamp_pct(value: float) -> float:
    """
    Clamp a percentage into [0, 100].
    """
    return max(0.0, min(100.0, value))
