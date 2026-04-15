"""Pure business logic for extractor fan control.

This module intentionally has no AppDaemon or Home Assistant dependencies.
The runtime integration layer can feed events into ``ExtractorFanPairLogic``
and execute the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

ACTION_FAN_ON = "fan_on"
ACTION_FAN_OFF = "fan_off"
ACTION_START_KEEPALIVE = "start_keepalive"
ACTION_STOP_KEEPALIVE = "stop_keepalive"
ACTION_SET_TIMER = "set_timer"
ACTION_CANCEL_TIMER = "cancel_timer"

TIMER_ACTIVATION = "activation"
TIMER_DEADLINE = "deadline"


@dataclass(frozen=True)
class LogicConfig:
    """Per light/fan pair timing configuration."""

    min_light_on_for_fan_seconds: int = 15
    short_visit_threshold_seconds: int = 60

    def validate(self) -> None:
        """Validate config values.

        - ``min_light_on_for_fan_seconds``: minimum continuous light-on time
          required before fan automation can start.
        - ``short_visit_threshold_seconds``: if the light-on duration is below
          this value, fan stops immediately when light turns off.
        """
        if self.min_light_on_for_fan_seconds <= 0:
            raise ValueError("min_light_on_for_fan_seconds must be > 0")
        if self.short_visit_threshold_seconds <= 0:
            raise ValueError("short_visit_threshold_seconds must be > 0")


@dataclass(frozen=True)
class Action:
    """Declarative action produced by the logic engine."""

    kind: str
    timer_name: Optional[str] = None
    at: Optional[datetime] = None


class ExtractorFanPairLogic:
    """State machine for one light/fan pair.

    Notes:
    - "manual override" is authoritative and is reset only after a full
      light OFF -> ON transition after the override was set.
    - Overlapping occupancy/scheduled demand is merged by latest end time.
    """

    def __init__(self, config: LogicConfig) -> None:
        """Create logic state for one light/fan pair.

        ``config`` contains timing thresholds for activation and short-visit
        detection. The object then keeps all runtime state internally and
        emits declarative actions from public event methods.
        """
        config.validate()
        self._config = config

        # Input/state tracking.
        self._light_is_on = False
        self._light_on_since: Optional[datetime] = None
        self._activation_due_at: Optional[datetime] = None
        self._occupancy_active_while_light_on = False
        self._occupancy_run_until: Optional[datetime] = None
        self._schedule_run_until: Optional[datetime] = None

        # Manual override lifecycle.
        self._manual_override: Optional[bool] = None
        self._override_reset_ready = False

        # Output tracking for idempotent action emission.
        self._fan_output_on = False
        self._keepalive_output_on = False
        self._timer_outputs: Dict[str, Optional[datetime]] = {
            TIMER_ACTIVATION: None,
            TIMER_DEADLINE: None,
        }

    def on_light_on(self, now: datetime) -> List[Action]:
        """Handle a light ON event.

        ``now`` is the event timestamp used for all duration math. This starts
        the activation timer and may clear a manual override if an OFF->ON
        reset cycle had completed.
        """
        actions: List[Action] = []
        if self._light_is_on:
            return actions

        self._light_is_on = True
        self._light_on_since = now
        self._activation_due_at = now + timedelta(
            seconds=self._config.min_light_on_for_fan_seconds)

        # Manual override is cleared only after full OFF -> ON transition.
        if self._manual_override is not None and self._override_reset_ready:
            self._manual_override = None
            self._override_reset_ready = False

        return self._reconcile(now)

    def on_light_off(self, now: datetime) -> List[Action]:
        """Handle a light OFF event.

        ``now`` is used to compute how long the light stayed on.
        - If activation never happened, fan demand ends immediately.
        - If activation happened:
          - short visit (< threshold): stop fan now
          - long visit (>= threshold): keep fan for same duration as light-on
        """
        actions: List[Action] = []
        if not self._light_is_on:
            return actions

        light_on_since = self._light_on_since
        self._light_is_on = False
        self._light_on_since = None
        self._activation_due_at = None

        if self._manual_override is not None:
            self._override_reset_ready = True

        if self._occupancy_active_while_light_on and light_on_since is not None:
            duration = now - light_on_since
            if duration < timedelta(
                    seconds=self._config.short_visit_threshold_seconds):
                self._occupancy_run_until = now
            else:
                self._occupancy_run_until = now + duration

        self._occupancy_active_while_light_on = False
        return self._reconcile(now)

    def on_schedule_started(self, now: datetime, *,
                            duration_seconds: int) -> List[Action]:
        """Start or extend a scheduled fan run.

        ``now`` is the schedule trigger time.
        ``duration_seconds`` is how long this scheduled demand should stay
        active. If another scheduled window already exists, the later end time
        wins.
        """
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")

        candidate_deadline = now + timedelta(seconds=duration_seconds)
        if self._schedule_run_until is None or candidate_deadline > self._schedule_run_until:
            self._schedule_run_until = candidate_deadline

        return self._reconcile(now)

    def on_manual_fan_toggle(self, now: datetime, *,
                             fan_on: bool) -> List[Action]:
        """Apply a manual fan override.

        ``fan_on`` is the user-forced target state (True/False).
        Once set, manual override is authoritative and suppresses automation
        decisions until a full light OFF->ON cycle resets it.
        ``now`` is still used for timer progression consistency.
        """
        self._manual_override = fan_on
        self._override_reset_ready = False
        return self._reconcile(now)

    def on_time_tick(self, now: datetime) -> List[Action]:
        """Advance time-dependent state without a new device event.

        ``now`` is used to expire activation/deadline windows and emit any
        resulting actions (for example fan stop when demand reaches its end).
        """
        return self._reconcile(now)

    def _reconcile(self, now: datetime) -> List[Action]:
        """Recompute full output state for timestamp ``now``.

        This is the single place where event-driven state changes are turned
        into externally visible actions, keeping behavior deterministic.
        """
        self._advance_time(now)
        target_outputs = self._target_outputs(now)
        return self._emit_transitions(target_outputs)

    def _advance_time(self, now: datetime) -> None:
        """Apply time-based state transitions before deciding outputs.

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

        if self._occupancy_run_until is not None and now >= self._occupancy_run_until:
            # Post-run demand window has ended.
            self._occupancy_run_until = None

        if self._schedule_run_until is not None and now >= self._schedule_run_until:
            # Scheduled demand window has ended.
            self._schedule_run_until = None

    def _target_outputs(self,
                        now: datetime) -> Dict[str, Optional[datetime] | bool]:
        """Compute target fan/keepalive/timer outputs from current state.

        Manual override, when present, always wins over automatic demand.
        Without override, fan runs if either occupancy or schedule demand is
        currently active.
        """
        if self._manual_override is not None:
            # Manual override is authoritative by design.
            target_fan_on = self._manual_override
            target_keepalive_on = self._manual_override
        else:
            occupancy_active = self._occupancy_active_while_light_on or (
                self._occupancy_run_until is not None
                and now < self._occupancy_run_until)
            schedule_active = (self._schedule_run_until is not None
                               and now < self._schedule_run_until)
            # Merge demand sources: if either needs fan, fan should run.
            target_fan_on = occupancy_active or schedule_active
            target_keepalive_on = target_fan_on

        activation_timer = (
            # Activation timer exists only while waiting to decide if this light
            # session is long enough to trigger fan behavior.
            self._activation_due_at if self._light_is_on
            and not self._occupancy_active_while_light_on else None)
        # Deadline timer wakes integration layer on next relevant expiry.
        deadline_timer = self._compute_next_deadline(now)

        return {
            "fan_on": target_fan_on,
            "keepalive_on": target_keepalive_on,
            TIMER_ACTIVATION: activation_timer,
            TIMER_DEADLINE: deadline_timer,
        }

    def _compute_next_deadline(self, now: datetime) -> Optional[datetime]:
        """Return earliest active demand deadline, or ``None`` if no demand.

        We use the nearest deadline so the caller can schedule one wake-up and
        then re-evaluate state at that time.
        """
        candidates: List[datetime] = []
        if self._occupancy_run_until is not None and now < self._occupancy_run_until:
            candidates.append(self._occupancy_run_until)
        if self._schedule_run_until is not None and now < self._schedule_run_until:
            candidates.append(self._schedule_run_until)
        if not candidates:
            return None
        return min(candidates)

    def _emit_transitions(
            self,
            target_outputs: Dict[str,
                                 Optional[datetime] | bool]) -> List[Action]:
        """Emit only changes between previous output and target output.

        This makes the logic idempotent: repeated events/ticks with unchanged
        target state produce no duplicate commands.
        """
        actions: List[Action] = []

        target_fan_on = bool(target_outputs["fan_on"])
        if target_fan_on != self._fan_output_on:
            # Emit edge-triggered fan command only on state transition.
            actions.append(
                Action(ACTION_FAN_ON if target_fan_on else ACTION_FAN_OFF))
            self._fan_output_on = target_fan_on

        target_keepalive_on = bool(target_outputs["keepalive_on"])
        if target_keepalive_on != self._keepalive_output_on:
            # Keepalive scheduler is also edge-triggered.
            actions.append(
                Action(ACTION_START_KEEPALIVE
                       if target_keepalive_on else ACTION_STOP_KEEPALIVE))
            self._keepalive_output_on = target_keepalive_on

        for timer_name in (TIMER_ACTIVATION, TIMER_DEADLINE):
            target_at = target_outputs[timer_name]
            current_at = self._timer_outputs[timer_name]
            if target_at != current_at:
                # Timer commands are declarative too: set when needed, cancel
                # when no longer needed.
                if target_at is None:
                    actions.append(
                        Action(ACTION_CANCEL_TIMER, timer_name=timer_name))
                else:
                    actions.append(
                        Action(ACTION_SET_TIMER,
                               timer_name=timer_name,
                               at=target_at))
                self._timer_outputs[timer_name] = target_at

        return actions
