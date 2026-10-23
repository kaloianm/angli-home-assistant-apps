"""
Tilt-zone geometry for one Gradhermetic blind.

Zone validation, band predicates, latch-sequence targets, and the only real<->virtual mapping.

- **real** -- the blind's travel position (0-100) as the actuator reports and accepts it. All
  configured numbers are real.
- **virtual** -- what the user-facing cover shows: equal to real outside tilt; inside tilt, the zone
  inverted onto 0-100 (100 = lower edge, most light; 0 = upper edge, least light).

All positions are whole percents. Rounding happens only in :func:`to_whole_pct` (values from Home
Assistant) and the virtual mapping, which round-trips real positions exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Optional

# Default real travel of one height step: a short nudge on a ~15 s full travel.
DEFAULT_HEIGHT_STEP_PCT = 2


def clamp_pct(value: int) -> int:
    """
    Clamp a whole percentage into [0, 100].
    """
    return max(0, min(100, value))


def to_whole_pct(value: float) -> int:
    """
    Round a percentage from outside the app to the nearest whole percent, halves rounding up.
    """
    return clamp_pct(math.floor(value + 0.5))


def _is_whole(value: object) -> bool:
    """
    Whether a value is an ``int`` proper. ``bool`` is an ``int`` subclass, but never a percentage.
    """
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Zone:
    """
    Tilt-zone geometry for one blind.

    Field names match the ``apps.yaml`` keys (documented in README.md) so validation errors name the
    setting to fix. All are whole percents of real travel. Construction validates.
    """

    tilt_zone_upper_pct: int
    tilt_zone_lower_pct: int
    tilt_zone_epsilon_pct: int
    tilt_zone_release_pct: int
    tilt_step_pct: int
    tilt_zone_enter_pct: Optional[int] = None
    height_step_pct: int = DEFAULT_HEIGHT_STEP_PCT

    def __post_init__(self) -> None:
        """
        Reject an unusable geometry at construction time.
        """
        self.validate()

    def validate(self) -> None:
        """
        Validate the zone numbers, raising ``ValueError`` naming the offending setting.
        """
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None and not _is_whole(value):
                raise ValueError(f"{field.name} must be a whole percent, not {value!r}")
        if not 0 <= self.tilt_zone_lower_pct <= 100:
            raise ValueError("tilt_zone_lower_pct must be between 0 and 100")
        if not 0 <= self.tilt_zone_upper_pct <= 100:
            raise ValueError("tilt_zone_upper_pct must be between 0 and 100")
        if self.tilt_zone_lower_pct >= self.tilt_zone_upper_pct:
            raise ValueError("tilt_zone_lower_pct must be smaller than tilt_zone_upper_pct")
        if self.tilt_zone_epsilon_pct < 1:
            raise ValueError("tilt_zone_epsilon_pct must be >= 1 so the dip target clears the "
                             "lower edge")
        # Both travel limits must lie outside the ambiguity band, so a blind on an end stop is
        # known to be unlatched.
        if self.tilt_zone_lower_pct - self.tilt_zone_epsilon_pct <= 0:
            raise ValueError("tilt_zone_lower_pct - tilt_zone_epsilon_pct must be > 0 so the "
                             "ambiguity band stops short of the fully closed position")
        if self.tilt_zone_release_pct <= self.tilt_zone_upper_pct:
            raise ValueError("tilt_zone_release_pct must be > tilt_zone_upper_pct")
        if self.tilt_zone_release_pct >= 100:
            raise ValueError("tilt_zone_release_pct must be < 100 so the ambiguity band stops "
                             "short of the fully open position")
        landing = self.tilt_zone_enter_pct
        if landing is not None:
            if not self.tilt_zone_lower_pct <= landing <= self.tilt_zone_upper_pct:
                raise ValueError("tilt_zone_enter_pct must be between tilt_zone_lower_pct and "
                                 "tilt_zone_upper_pct")
        if self.tilt_step_pct < 1:
            raise ValueError("tilt_step_pct must be >= 1 so one step moves the actuator")
        if self.tilt_step_pct > self.span:
            raise ValueError("tilt_step_pct must be <= tilt_zone_upper_pct - tilt_zone_lower_pct, "
                             "the real travel the whole tilt zone spans")
        if self.height_step_pct < 1:
            raise ValueError("height_step_pct must be >= 1 so one step moves the actuator")
        if self.height_step_pct > 100:
            raise ValueError("height_step_pct must be <= 100, the blind's whole travel")

    # -- Named landmarks ---------------------------------------------------------------------------

    @property
    def upper(self) -> int:
        """
        Upper zone edge: slats parallel / closed / least light.
        """
        return self.tilt_zone_upper_pct

    @property
    def lower(self) -> int:
        """
        Lower zone edge: slats perpendicular / open / most light.
        """
        return self.tilt_zone_lower_pct

    @property
    def epsilon(self) -> int:
        """
        Clearance margin used to cleanly cross the lower zone edge on the dip.
        """
        return self.tilt_zone_epsilon_pct

    @property
    def span(self) -> int:
        """
        Real travel percent between the two zone edges.
        """
        return self.tilt_zone_upper_pct - self.tilt_zone_lower_pct

    @property
    def dip_target(self) -> int:
        """
        Real position just below the lower edge, dipped to before the latching rise.
        """
        return self.lower - self.epsilon

    @property
    def release_target(self) -> int:
        """
        Measured real position at which the latch has let go, a few percent above the upper edge.
        """
        return self.tilt_zone_release_pct

    @property
    def enter_landing_real(self) -> int:
        """
        Real travel position the entry sequence finishes on; defaults to the upper (closed) edge.
        """
        if self.tilt_zone_enter_pct is not None:
            return self.tilt_zone_enter_pct
        return self.upper

    @property
    def enter_landing_virtual(self) -> int:
        """
        The same landing on the virtual slat scale, as the planner's enter intent carries it.
        """
        return self.real_to_virtual(self.enter_landing_real)

    @property
    def band_low(self) -> int:
        """
        Lower end of the latch-ambiguity band. Coincides with :attr:`dip_target`.
        """
        return self.dip_target

    @property
    def band_high(self) -> int:
        """
        Upper end of the latch-ambiguity band: :attr:`release_target`, since a latched blind can
        rest anywhere up to its release height.
        """
        return self.release_target

    # -- Predicates --------------------------------------------------------------------------------

    def in_band(self, position: int) -> bool:
        """
        Whether a real position falls inside the inclusive latch-ambiguity band. A blind resting
        outside it cannot be latched.
        """
        return self.band_low <= position <= self.band_high

    def in_zone(self, position: int) -> bool:
        """
        Whether a real position falls inside the inclusive tilt zone ``[lower, upper]``.
        """
        return self.lower <= position <= self.upper

    # -- Mapping -----------------------------------------------------------------------------------

    def virtual_to_real(self, virtual: int) -> int:
        """
        Map an in-tilt virtual position (0-100) to the nearest real travel position within the zone.

        Integer arithmetic with halves rounding up: the offset below the upper edge is
        ``virtual * span / 100``, rounded.
        """
        offset = (2 * clamp_pct(virtual) * self.span + 100) // 200
        return self.upper - offset

    def real_to_virtual(self, real: int) -> int:
        """
        Map an in-tilt real travel position to the nearest virtual position (0-100).

        The inverse of :meth:`virtual_to_real`, rounded the same way. Positions outside the zone
        clamp to the nearer end of the slat scale.
        """
        depth = self.upper - real
        return clamp_pct((200 * depth + self.span) // (2 * self.span))

    def snap_normal_target(self, target: int, current: Optional[int] = None) -> int:
        """
        Snap a whole-height target inside the ambiguity band to the nearest band edge, since rising
        into the band would silently latch. Ties rise.

        If the nearest edge is ``current`` (where the blind rests), the far edge is used so the
        request still moves the blind.
        """
        target = clamp_pct(target)
        if not self.band_low < target < self.band_high:
            return target
        if target - self.band_low < self.band_high - target:
            nearest, farthest = self.band_low, self.band_high
        else:
            nearest, farthest = self.band_high, self.band_low
        if current is not None and nearest == current:
            return farthest
        return nearest

    def snap_step_target(self, target: int, rising: bool) -> int:
        """
        Snap a height-step target inside the ambiguity band to the band edge in the step's direction.
        """
        target = clamp_pct(target)
        if not self.band_low < target < self.band_high:
            return target
        return self.band_high if rising else self.band_low
