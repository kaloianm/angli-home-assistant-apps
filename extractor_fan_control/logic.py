"""
State machine for one light/fan pair. No AppDaemon dependencies: the adapter feeds events in and
performs the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional

ACTION_FAN_ON = "fan_on"
ACTION_FAN_OFF = "fan_off"
ACTION_START_KEEPALIVE = "start_keepalive"
ACTION_STOP_KEEPALIVE = "stop_keepalive"
ACTION_SET_TIMER = "set_timer"
ACTION_CANCEL_TIMER = "cancel_timer"
ACTION_SET_OFF_DEADLINE = "set_off_deadline"

TIMER_ACTIVATION = "activation"
TIMER_DEADLINE = "deadline"


class PairState(Enum):
    """
    Human-readable state for one light/fan pair.
    """

    IDLE = "idle"
    WAITING_FOR_ACTIVATION = "waiting_for_activation"
    RUNNING_LIGHT = "running_light"
    POST_RUN = "post_run"
    SCHEDULED_RUN = "scheduled_run"
    COMBINED_RUN = "combined_run"
    DISABLED = "disabled"

    def is_managed(self) -> bool:
        """
        Whether automation currently owns fan output decisions.
        """
        return self not in (PairState.IDLE, PairState.DISABLED)


@dataclass(frozen=True)
class LogicConfig:
    """
    Per light/fan pair timing configuration.
    """

    min_light_on_for_fan_seconds: int = 10
    short_visit_threshold_seconds: int = 60
    max_post_run_seconds: int = 600

    def validate(self) -> None:
        """
        Validate config values.
        """
        if self.min_light_on_for_fan_seconds < 0:
            raise ValueError("min_light_on_for_fan_seconds must be >= 0")
        if self.short_visit_threshold_seconds <= 0:
            raise ValueError("short_visit_threshold_seconds must be > 0")
        if self.min_light_on_for_fan_seconds > self.short_visit_threshold_seconds:
            raise ValueError(
                "min_light_on_for_fan_seconds must be <= short_visit_threshold_seconds")
        if self.max_post_run_seconds <= 0:
            raise ValueError("max_post_run_seconds must be > 0")


@dataclass(frozen=True)
class Action:
    """
    Declarative action produced by the logic engine.
    """

    kind: str
    timer_name: Optional[str] = None
    at: Optional[datetime] = None


class ExtractorFanPairLogic:
    """
    State machine for one light/fan pair.
    """

    def __init__(
        self,
        config: LogicConfig,
        log: Callable[..., None] = lambda *_args, **_kwargs: None,
    ) -> None:
        """
        Create logic state for one light/fan pair.
        """
        config.validate()
        self._config = config
        self._log = log
        self._disabled = False

        self._light_is_on = False
        self._light_on_since: Optional[datetime] = None
        self._activation_due_at: Optional[datetime] = None
        self._occupancy_active_while_light_on = False
        self._occupancy_run_until: Optional[datetime] = None
        self._schedule_run_until: Optional[datetime] = None

        # Last emitted outputs, so only changes produce actions.
        self._fan_output_on = False
        self._keepalive_output_on = False
        self._timer_outputs: Dict[str, Optional[datetime]] = {
            TIMER_ACTIVATION: None,
            TIMER_DEADLINE: None,
        }
        self._off_deadline_output: Optional[datetime] = None

    @property
    def state(self) -> PairState:
        """
        Current management state.
        """
        if self._disabled:
            return PairState.DISABLED
        schedule_active = self._schedule_run_until is not None
        occupancy_post_active = self._occupancy_run_until is not None
        if self._light_is_on and not self._occupancy_active_while_light_on:
            return PairState.WAITING_FOR_ACTIVATION
        if self._light_is_on and self._occupancy_active_while_light_on:
            return PairState.COMBINED_RUN if schedule_active else PairState.RUNNING_LIGHT
        if occupancy_post_active:
            return PairState.COMBINED_RUN if schedule_active else PairState.POST_RUN
        if schedule_active:
            return PairState.SCHEDULED_RUN
        return PairState.IDLE

    def disable(self) -> List[Action]:
        """
        Permanently stop automation decisions for this pair until restart.
        """
        self._disabled = True
        self._log("disabled")
        return self._emit_transitions({
            "fan_on": False,
            "keepalive_on": False,
            TIMER_ACTIVATION: None,
            TIMER_DEADLINE: None,
            "off_deadline": None,
        })

    def on_light_on(self, now: datetime) -> List[Action]:
        """
        Handle a light ON event, starting the activation timer.
        """
        actions: List[Action] = []
        self._log(f"event light_on at {now.isoformat()}", level="DEBUG")
        if self._light_is_on:
            self._log("ignored duplicate light_on", level="DEBUG")
            return actions

        previous_state = self.state
        self._light_is_on = True
        self._light_on_since = now
        self._activation_due_at = now + timedelta(seconds=self._config.min_light_on_for_fan_seconds)

        return self._reconcile(now, previous_state)

    def on_light_off(self, now: datetime) -> List[Action]:
        """
        Handle a light OFF event. After a short visit (or no activation) occupancy demand ends now;
        after a long one the fan keeps running for the light-on duration, capped by
        ``max_post_run_seconds``.
        """
        actions: List[Action] = []
        self._log(f"event light_off at {now.isoformat()}", level="DEBUG")
        if not self._light_is_on:
            self._log("ignored duplicate light_off", level="DEBUG")
            return actions

        previous_state = self.state
        light_on_since = self._light_on_since
        self._light_is_on = False
        self._light_on_since = None
        self._activation_due_at = None

        if self._occupancy_active_while_light_on and light_on_since is not None:
            duration = now - light_on_since
            if duration < timedelta(seconds=self._config.short_visit_threshold_seconds):
                self._occupancy_run_until = now
                self._log(f"short visit duration={duration}; ending occupancy demand",
                          level="DEBUG")
            else:
                capped_post_run = min(
                    duration,
                    timedelta(seconds=self._config.max_post_run_seconds),
                )
                self._occupancy_run_until = now + capped_post_run
                self._log(
                    f"long visit duration={duration}; post_run_until="
                    f"{self._occupancy_run_until.isoformat()}", level="DEBUG")

        self._occupancy_active_while_light_on = False
        return self._reconcile(now, previous_state)

    def on_schedule_started(self, now: datetime, *, duration_seconds: int) -> List[Action]:
        """
        Start or extend a scheduled fan run; if one is already active, the later end wins.
        """
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")

        previous_state = self.state
        candidate_deadline = now + timedelta(seconds=duration_seconds)
        if self._schedule_run_until is None or candidate_deadline > self._schedule_run_until:
            self._schedule_run_until = candidate_deadline
            self._log(f"schedule run until {self._schedule_run_until.isoformat()}")
        else:
            self._log("ignored schedule run that would not extend deadline", level="DEBUG")

        return self._reconcile(now, previous_state)

    def on_time_tick(self, now: datetime) -> List[Action]:
        """
        Advance time-dependent state (activation, expiring deadlines) without a device event.
        """
        self._log(f"event time_tick at {now.isoformat()}", level="DEBUG")
        return self._reconcile(now)

    def _reconcile(
        self,
        now: datetime,
        previous_state: Optional[PairState] = None,
    ) -> List[Action]:
        """
        Recompute all outputs at ``now`` and emit actions for the ones that changed.
        """
        if self._disabled:
            self._log("ignored event while disabled", level="DEBUG")
            return []

        if previous_state is None:
            previous_state = self.state
        self._advance_time(now)
        target_outputs = self._target_outputs(now)
        actions = self._emit_transitions(target_outputs)
        current_state = self.state
        if current_state != previous_state:
            off_deadline = self._compute_off_deadline(now)
            until = f" until {off_deadline.isoformat()}" if off_deadline is not None else ""
            self._log(f"state {previous_state.value} -> {current_state.value}{until}")
        for action in actions:
            if action.kind == ACTION_SET_TIMER:
                self._log(
                    f"action {action.kind} {action.timer_name} at "
                    f"{action.at.isoformat() if action.at else None}", level="DEBUG")
            elif action.kind == ACTION_CANCEL_TIMER:
                self._log(f"action {action.kind} {action.timer_name}", level="DEBUG")
            elif action.kind == ACTION_SET_OFF_DEADLINE:
                self._log(
                    f"action {action.kind} at "
                    f"{action.at.isoformat() if action.at else None}", level="DEBUG")
            else:
                self._log(f"action {action.kind}", level="DEBUG")
        return actions

    def _advance_time(self, now: datetime) -> None:
        """
        Promote a light session to occupancy once activation is due, and drop expired deadlines.
        """
        if (self._light_is_on and self._activation_due_at is not None
                and now >= self._activation_due_at):
            self._occupancy_active_while_light_on = True
            self._activation_due_at = None
            self._log("activation threshold reached", level="DEBUG")

        if self._occupancy_run_until is not None and now >= self._occupancy_run_until:
            self._occupancy_run_until = None
            self._log("occupancy demand expired", level="DEBUG")

        if self._schedule_run_until is not None and now >= self._schedule_run_until:
            self._schedule_run_until = None
            self._log("schedule demand expired", level="DEBUG")

    def _target_outputs(self, now: datetime) -> Dict[str, Optional[datetime] | bool]:
        """
        Compute target outputs. The fan runs if occupancy or schedule demand is active, and the
        keepalive follows the fan.
        """
        occupancy_active = self._occupancy_active_while_light_on or (
            self._occupancy_run_until is not None and now < self._occupancy_run_until)
        schedule_active = (self._schedule_run_until is not None and now < self._schedule_run_until)

        target_fan_on = occupancy_active or schedule_active
        target_keepalive_on = target_fan_on

        activation_timer = (self._activation_due_at
                            if self._light_is_on and not self._occupancy_active_while_light_on else
                            None)
        deadline_timer = self._compute_next_deadline(now)

        return {
            "fan_on": target_fan_on,
            "keepalive_on": target_keepalive_on,
            TIMER_ACTIVATION: activation_timer,
            TIMER_DEADLINE: deadline_timer,
            "off_deadline": self._compute_off_deadline(now),
        }

    def _compute_next_deadline(self, now: datetime) -> Optional[datetime]:
        """
        Return the earliest active demand deadline (the next wake-up), or ``None`` if no demand.
        """
        candidates: List[datetime] = []
        if self._occupancy_run_until is not None and now < self._occupancy_run_until:
            candidates.append(self._occupancy_run_until)
        if self._schedule_run_until is not None and now < self._schedule_run_until:
            candidates.append(self._schedule_run_until)
        if not candidates:
            return None
        return min(candidates)

    def _compute_off_deadline(self, now: datetime) -> Optional[datetime]:
        """
        Return when the fan will turn off: the latest active deadline. ``None`` while the light is
        on with occupancy, since the post-run is only computed at light-off.
        """
        if self._occupancy_active_while_light_on:
            return None
        candidates: List[datetime] = []
        if self._occupancy_run_until is not None and now < self._occupancy_run_until:
            candidates.append(self._occupancy_run_until)
        if self._schedule_run_until is not None and now < self._schedule_run_until:
            candidates.append(self._schedule_run_until)
        if not candidates:
            return None
        return max(candidates)

    def _emit_transitions(self, target_outputs: Dict[str,
                                                     Optional[datetime] | bool]) -> List[Action]:
        """
        Emit actions only for outputs that differ from the last emitted ones.
        """
        actions: List[Action] = []

        target_fan_on = bool(target_outputs["fan_on"])
        if target_fan_on != self._fan_output_on:
            actions.append(Action(ACTION_FAN_ON if target_fan_on else ACTION_FAN_OFF))
            self._fan_output_on = target_fan_on

        target_keepalive_on = bool(target_outputs["keepalive_on"])
        if target_keepalive_on != self._keepalive_output_on:
            actions.append(
                Action(ACTION_START_KEEPALIVE if target_keepalive_on else ACTION_STOP_KEEPALIVE))
            self._keepalive_output_on = target_keepalive_on

        for timer_name in (TIMER_ACTIVATION, TIMER_DEADLINE):
            target_at = target_outputs[timer_name]
            current_at = self._timer_outputs[timer_name]
            if target_at != current_at:
                if target_at is None:
                    actions.append(Action(ACTION_CANCEL_TIMER, timer_name=timer_name))
                else:
                    actions.append(Action(ACTION_SET_TIMER, timer_name=timer_name, at=target_at))
                self._timer_outputs[timer_name] = target_at

        target_off_deadline = target_outputs["off_deadline"]
        if target_off_deadline != self._off_deadline_output:
            # at=None clears the sensor.
            actions.append(Action(ACTION_SET_OFF_DEADLINE, at=target_off_deadline))
            self._off_deadline_output = target_off_deadline

        return actions
