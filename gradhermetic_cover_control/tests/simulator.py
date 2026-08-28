"""
A ground-truth model of the physical Gradhermetic blind.

The app's tests can only prove the app consistent with itself. This simulator supplies the other
half: an independent model of the mechanism that records a **violation** whenever it is asked to
travel below the lower zone edge while latched. Tests assert zero violations, which is the safety
proof.

The latch model is deliberately the most pessimistic one consistent with the hardware:

- *any* upward crossing of the lower edge that starts below it engages the latch;
- only an upward crossing clear of the upper edge releases it;
- downward travel never changes it.

Plans that are correct under this model are also correct under milder, reversal-only models, because
they never rely on a crossing *not* latching.

An optional calibration-drift knob models the fact the whole redesign turns on: the actuator's
reported percentage may not match the blind's true position, and only reaching the top limit
re-references it. With drift enabled, a sequence that starts from a full open still lands exactly
where it intends, while a short rise to a merely *reported* percentage does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from gradhermetic_cover_control.geometry import Zone, clamp_pct, to_command

# How far the blind travels between two position reports, in real percent. One whole percent is what
# a KNX actuator reports; a coarser stride is useful for tests that inject at every report.
DEFAULT_STRIDE_PCT = 1.0

_EPSILON = 1e-9


@dataclass(frozen=True)
class Report:
    """
    One position update as the controller would publish it.
    """

    position: Optional[float]
    is_moving: bool


@dataclass
class Quirks:
    """
    Feedback behaviours real controllers exhibit, each exercised by the model tests.
    """

    # Emit a report for every step of travel, not just the final one.
    report_intermediate: bool = True
    # Carry is_moving=True while travelling. Some integrations only ever publish positions.
    report_motion_state: bool = True
    # Repeat the final settled report, as an attribute-level state listener sees it.
    duplicate_settled: bool = False
    # Re-publish the position the blind is still standing on when a move starts. A state listener
    # watching every attribute sees this whenever the entity changes before the position does.
    echo_before_moving: bool = False
    # Real percent of calibration error each position-commanded move adds between limit switches.
    drift_per_move: float = 0.0


class BlindSimulator:
    """
    The physical blind: a true position, a latch, and an actuator that reports whole percent.
    """

    def __init__(self, zone: Zone, position: float = 100.0, latched: bool = False,
                 quirks: Optional[Quirks] = None, stride: float = DEFAULT_STRIDE_PCT) -> None:
        """
        Create a blind resting at ``position``, optionally already latched.
        """
        self._zone = zone
        self._stride = stride
        self.quirks = quirks or Quirks()
        self.physical = float(position)
        self.latched = bool(latched)
        self.drift = 0.0
        self.violations: List[str] = []
        self.commands: List[str] = []
        self._target: Optional[float] = None
        self._outbox: List[Report] = []

    # -- Observation -------------------------------------------------------------------------------

    @property
    def reported(self) -> int:
        """
        The whole-percent position the actuator publishes, calibration error included.
        """
        return to_command(self.physical + self.drift)

    @property
    def busy(self) -> bool:
        """
        Whether travel is in progress or a report is still waiting to be delivered.
        """
        return self._target is not None or bool(self._outbox)

    @property
    def is_moving(self) -> bool:
        """
        Whether the blind is travelling right now.
        """
        return self._target is not None

    # -- Commands ----------------------------------------------------------------------------------

    def open_cover(self) -> None:
        """
        ``cover.open_cover``: drive to the top limit, which re-references the actuator.
        """
        self.commands.append("open")
        self._travel_to(100.0)

    def close_cover(self) -> None:
        """
        ``cover.close_cover``: drive to the bottom limit.
        """
        self.commands.append("close")
        self._travel_to(0.0)

    def set_position(self, reported_target: int) -> None:
        """
        ``cover.set_cover_position``: drive until the *reported* position matches the setpoint.

        The actuator always reaches its own setpoint, so the report on arrival is exact whatever the
        calibration error; what the error moves is where the blind physically ends up.
        """
        self.commands.append(f"position={reported_target}")
        drift = self.drift + self.quirks.drift_per_move
        if self._travel_to(reported_target - drift):
            self.drift = drift

    def stop_cover(self) -> None:
        """
        ``cover.stop_cover``: halt where the blind is and report the resting position.
        """
        self.commands.append("stop")
        self.jam()

    def jam(self) -> None:
        """
        Halt travel short of the setpoint, as a mechanical obstruction would.
        """
        if self._target is None:
            return
        self._target = None
        self._emit_settled()

    # -- Travel ------------------------------------------------------------------------------------

    def tick(self) -> List[Report]:
        """
        Deliver any queued report, else advance travel by one stride.
        """
        if self._outbox:
            reports, self._outbox = self._outbox, []
            return reports
        if self._target is None:
            return []

        previous = self.physical
        remaining = self._target - self.physical
        stride = min(abs(remaining), self._stride)
        self.physical = clamp_pct(self.physical + (stride if remaining > 0 else -stride))
        self._apply_latch(previous, self.physical)
        if self.latched and self.physical < self._zone.lower - _EPSILON:
            self.violations.append(
                f"travelled to {self.physical} below the lower edge {self._zone.lower} while latched")

        if abs(self._target - self.physical) < _EPSILON:
            self._target = None
            self._settle()
            self._emit_settled()
        elif self.quirks.report_intermediate:
            self._outbox.append(Report(float(self.reported), self.quirks.report_motion_state))

        reports, self._outbox = self._outbox, []
        return reports

    def _travel_to(self, physical_target: float) -> bool:
        """
        Begin travelling to a true position, recording a violation if that means descending latched.

        Returns whether the blind actually has anywhere to go.
        """
        physical_target = clamp_pct(physical_target)
        if self.latched and physical_target < self._zone.lower - _EPSILON:
            self.violations.append(
                f"commanded to {physical_target} below the lower edge {self._zone.lower} while "
                "latched")
        if abs(physical_target - self.physical) < _EPSILON:
            # The actuator is already on its setpoint: it neither moves nor says anything.
            self._target = None
            return False
        self._target = physical_target
        if self.quirks.echo_before_moving:
            self._outbox.append(Report(float(self.reported), self.quirks.report_motion_state))
        return True

    def _apply_latch(self, start: float, end: float) -> None:
        """
        Update the latch for one step of travel, under the conservative crossing model.
        """
        if end <= start:
            return
        if start < self._zone.lower <= end:
            self.latched = True
        if start <= self._zone.upper < end:
            self.latched = False

    def _settle(self) -> None:
        """
        Finish a move: reaching a limit switch re-references the actuator and clears the error.

        Only the top limit matters to the app -- it is the one direction that is always safe to
        take -- but a real blind is re-referenced at either end.
        """
        if self.physical >= 100.0 - _EPSILON or self.physical <= _EPSILON:
            self.drift = 0.0

    def _emit_settled(self) -> None:
        """
        Queue the settled report(s) for the position the blind came to rest at.
        """
        self._outbox.append(Report(float(self.reported), False))
        if self.quirks.duplicate_settled:
            self._outbox.append(Report(float(self.reported), False))
