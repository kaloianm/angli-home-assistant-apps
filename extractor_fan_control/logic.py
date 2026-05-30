"""
Pure business logic for extractor fan control.

This module intentionally has no AppDaemon or Home Assistant dependencies.
The runtime integration layer can feed events into ``ExtractorFanPairLogic`` and execute the
returned actions.
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
    MANUAL_OVERRIDE = "manual_override"
    DISABLED = "disabled"

    def is_managed(self) -> bool:
        """
        Whether automation currently owns fan output decisions.
        """
        return self not in (PairState.IDLE, PairState.MANUAL_OVERRIDE, PairState.DISABLED)


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

        - ``min_light_on_for_fan_seconds``: minimum continuous light-on time required before fan
            automation can start.
        - ``short_visit_threshold_seconds``: if the light-on duration is below this value, fan stops
            immediately when light turns off.
        - ``max_post_run_seconds``: upper bound for long-visit post-run time.
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

    Notes:
    - "manual override" is reset when the light next turns off after the override was set.
    - Overlapping occupancy/scheduled demand is merged by latest end time.
    """

    def __init__(
        self,
        config: LogicConfig,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        """
        Create logic state for one light/fan pair.

        ``config`` contains timing thresholds for activation and short-visit detection. The object
        then keeps all runtime state internally and emits declarative actions from public event
        methods.
        """
        config.validate()
        self._config = config
        self._log = log
        self._disabled = False

        # Input/state tracking.
        self._light_is_on = False
        self._light_on_since: Optional[datetime] = None
        self._activation_due_at: Optional[datetime] = None
        self._occupancy_active_while_light_on = False
        self._occupancy_run_until: Optional[datetime] = None
        self._schedule_run_until: Optional[datetime] = None

        # Manual override lifecycle.
        self._manual_override: Optional[bool] = None

        # Output tracking for idempotent action emission.
        self._fan_output_on = False
        self._keepalive_output_on = False
        self._timer_outputs: Dict[str, Optional[datetime]] = {
            TIMER_ACTIVATION: None,
            TIMER_DEADLINE: None,
        }

    @property
    def state(self) -> PairState:
        """
        Current management state.
        """
        if self._disabled:
            return PairState.DISABLED
        if self._manual_override is not None:
            return PairState.MANUAL_OVERRIDE
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
        })

    def on_light_on(self, now: datetime) -> List[Action]:
        """
        Handle a light ON event.

        ``now`` is the event timestamp used for all duration math. This starts the activation timer
        while preserving any active manual override.
        """
        actions: List[Action] = []
        self._log(f"event light_on at {now.isoformat()}")
        if self._light_is_on:
            self._log("ignored duplicate light_on")
            return actions

        previous_state = self.state
        self._light_is_on = True
        self._light_on_since = now
        self._activation_due_at = now + timedelta(seconds=self._config.min_light_on_for_fan_seconds)

        return self._reconcile(now, previous_state)

    def on_light_off(self, now: datetime) -> List[Action]:
        """
        Handle a light OFF event.

        ``now`` is used to compute how long the light stayed on.
        - If activation never happened, fan demand ends immediately.
        - If activation happened:
          - short visit (< threshold): stop fan now
          - long visit (>= threshold): keep fan for light-on duration, capped
            by ``max_post_run_seconds``
        """
        actions: List[Action] = []
        self._log(f"event light_off at {now.isoformat()}")
        if not self._light_is_on:
            self._log("ignored duplicate light_off")
            return actions

        previous_state = self.state
        light_on_since = self._light_on_since
        self._light_is_on = False
        self._light_on_since = None
        self._activation_due_at = None

        if self._manual_override is not None:
            self._manual_override = None
            self._log("manual override cleared after light off")

        if self._occupancy_active_while_light_on and light_on_since is not None:
            duration = now - light_on_since
            if duration < timedelta(seconds=self._config.short_visit_threshold_seconds):
                self._occupancy_run_until = now
                self._log(f"short visit duration={duration}; ending occupancy demand")
            else:
                capped_post_run = min(
                    duration,
                    timedelta(seconds=self._config.max_post_run_seconds),
                )
                self._occupancy_run_until = now + capped_post_run
                self._log(f"long visit duration={duration}; post_run_until="
                          f"{self._occupancy_run_until.isoformat()}")

        self._occupancy_active_while_light_on = False
        return self._reconcile(now, previous_state)

    def on_schedule_started(self, now: datetime, *, duration_seconds: int) -> List[Action]:
        """
        Start or extend a scheduled fan run.

        ``now`` is the schedule trigger time.
        ``duration_seconds`` is how long this scheduled demand should stay active. If another
        scheduled window already exists, the later end time wins.
        """
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")

        previous_state = self.state
        candidate_deadline = now + timedelta(seconds=duration_seconds)
        if self._schedule_run_until is None or candidate_deadline > self._schedule_run_until:
            self._schedule_run_until = candidate_deadline
            self._log(f"schedule run until {self._schedule_run_until.isoformat()}")
        else:
            self._log("ignored schedule run that would not extend deadline")

        return self._reconcile(now, previous_state)

    def on_manual_fan_toggle(self, now: datetime, *, fan_on: bool) -> List[Action]:
        """
        Apply a manual fan override.

        ``fan_on`` is the user-forced target state (True/False).
        Once set, manual override is authoritative for occupancy demand until the next light-off
        event resets it. Scheduled demand still keeps the fan running until the scheduled window
        expires.
        ``now`` is still used for timer progression consistency.
        """
        self._log(f"event manual_fan_toggle fan_on={fan_on} at {now.isoformat()}")
        previous_state = self.state
        self._manual_override = fan_on
        # The callback already reports the fan's physical state. Recording it prevents the state
        # machine from commanding the same state back and amplifying delayed KNX feedback.
        self._fan_output_on = fan_on
        return self._reconcile(now, previous_state)

    def on_time_tick(self, now: datetime) -> List[Action]:
        """
        Advance time-dependent state without a new device event.

        ``now`` is used to expire activation/deadline windows and emit any resulting actions (for
        example fan stop when demand reaches its end).
        """
        self._log(f"event time_tick at {now.isoformat()}")
        return self._reconcile(now)

    def _reconcile(
        self,
        now: datetime,
        previous_state: Optional[PairState] = None,
    ) -> List[Action]:
        """
        Recompute full output state for timestamp ``now``.

        This is the single place where event-driven state changes are turned into externally visible
        actions, keeping behavior deterministic.
        """
        if self._disabled:
            self._log("ignored event while disabled")
            return []

        if previous_state is None:
            previous_state = self.state
        self._advance_time(now)
        target_outputs = self._target_outputs(now)
        actions = self._emit_transitions(target_outputs)
        current_state = self.state
        if current_state != previous_state:
            self._log(f"state {previous_state.value} -> {current_state.value}")
        for action in actions:
            if action.kind == ACTION_SET_TIMER:
                self._log(f"action {action.kind} {action.timer_name} at "
                          f"{action.at.isoformat() if action.at else None}")
            elif action.kind == ACTION_CANCEL_TIMER:
                self._log(f"action {action.kind} {action.timer_name}")
            else:
                self._log(f"action {action.kind}")
        return actions

    def _advance_time(self, now: datetime) -> None:
        """
        Apply time-based state transitions before deciding outputs.

        Important transitions:
        - activation timer expiry promotes current light session to occupancy
          demand (fan is now allowed to run)
        - expired occupancy/schedule deadlines are dropped
        """
        if (self._light_is_on and self._activation_due_at is not None
                and now >= self._activation_due_at):
            # Light has been on long enough to count as real occupancy.
            self._occupancy_active_while_light_on = True
            self._activation_due_at = None
            self._log("activation threshold reached")

        if self._occupancy_run_until is not None and now >= self._occupancy_run_until:
            # Post-run demand window has ended.
            self._occupancy_run_until = None
            self._log("occupancy demand expired")

        if self._schedule_run_until is not None and now >= self._schedule_run_until:
            # Scheduled demand window has ended.
            self._schedule_run_until = None
            self._log("schedule demand expired")

    def _target_outputs(self, now: datetime) -> Dict[str, Optional[datetime] | bool]:
        """
        Compute target fan/keepalive/timer outputs from current state.

        Manual override, when present, wins over occupancy demand. Scheduled demand is stronger than
        manual override and keeps the fan running until the schedule expires.
        """
        occupancy_active = self._occupancy_active_while_light_on or (
            self._occupancy_run_until is not None and now < self._occupancy_run_until)
        schedule_active = (self._schedule_run_until is not None and now < self._schedule_run_until)

        if self._manual_override is not None:
            target_fan_on = schedule_active or self._manual_override
            target_keepalive_on = target_fan_on
        else:
            # Merge demand sources: if either needs fan, fan should run.
            target_fan_on = occupancy_active or schedule_active
            target_keepalive_on = target_fan_on

        activation_timer = (
            # Activation timer exists only while waiting to decide if this light session
            # is long enough to trigger fan behavior.
            self._activation_due_at
            if self._light_is_on and not self._occupancy_active_while_light_on else None)
        # Deadline timer wakes integration layer on next relevant expiry.
        deadline_timer = self._compute_next_deadline(now)

        return {
            "fan_on": target_fan_on,
            "keepalive_on": target_keepalive_on,
            TIMER_ACTIVATION: activation_timer,
            TIMER_DEADLINE: deadline_timer,
        }

    def _compute_next_deadline(self, now: datetime) -> Optional[datetime]:
        """
        Return earliest active demand deadline, or ``None`` if no demand.

        We use the nearest deadline so the caller can schedule one wake-up and then re-evaluate
        state at that time.
        """
        candidates: List[datetime] = []
        if self._occupancy_run_until is not None and now < self._occupancy_run_until:
            candidates.append(self._occupancy_run_until)
        if self._schedule_run_until is not None and now < self._schedule_run_until:
            candidates.append(self._schedule_run_until)
        if not candidates:
            return None
        return min(candidates)

    def _emit_transitions(self, target_outputs: Dict[str,
                                                     Optional[datetime] | bool]) -> List[Action]:
        """
        Emit only changes between previous output and target output.

        This makes the logic idempotent: repeated events/ticks with unchanged target state produce
        no duplicate commands.
        """
        actions: List[Action] = []

        target_fan_on = bool(target_outputs["fan_on"])
        if target_fan_on != self._fan_output_on:
            # Emit edge-triggered fan command only on state transition.
            actions.append(Action(ACTION_FAN_ON if target_fan_on else ACTION_FAN_OFF))
            self._fan_output_on = target_fan_on

        target_keepalive_on = bool(target_outputs["keepalive_on"])
        if target_keepalive_on != self._keepalive_output_on:
            # Keepalive scheduler is also edge-triggered.
            actions.append(
                Action(ACTION_START_KEEPALIVE if target_keepalive_on else ACTION_STOP_KEEPALIVE))
            self._keepalive_output_on = target_keepalive_on

        for timer_name in (TIMER_ACTIVATION, TIMER_DEADLINE):
            target_at = target_outputs[timer_name]
            current_at = self._timer_outputs[timer_name]
            if target_at != current_at:
                # Timer commands are declarative too: set when needed, cancel when no longer needed.
                if target_at is None:
                    actions.append(Action(ACTION_CANCEL_TIMER, timer_name=timer_name))
                else:
                    actions.append(Action(ACTION_SET_TIMER, timer_name=timer_name, at=target_at))
                self._timer_outputs[timer_name] = target_at

        return actions
