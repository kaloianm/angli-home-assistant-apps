"""
Tilt-zone geometry for one Gradhermetic blind.

Zone validation, band predicates, latch-sequence targets, and the only real<->virtual mapping.

- **real** -- the blind's travel position (0-100) as the actuator reports and accepts it. All
  configured numbers are real.
- **virtual** -- what the user-facing cover shows: equal to real outside tilt; inside tilt, one of
  :data:`SLAT_POSITIONS` fixed slat positions spread evenly over the slat window, shown as 0, 33,
  67 and 100 (0 = entry landing, least light; 100 = lower edge, most light). The upper edge, above
  the window, is only where the latching rise ends.

All positions are whole percents. Rounding happens only in :func:`to_whole_pct` (values from Home
Assistant) and the slat positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

# Slat positions offered in tilt mode, spread from the entry landing down to the lower edge.
SLAT_POSITIONS = 4


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
    tilt_zone_enter_pct: int
    height_step_pct: int

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
            if not _is_whole(value):
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
        if not self.tilt_zone_lower_pct <= self.tilt_zone_enter_pct <= self.tilt_zone_upper_pct:
            raise ValueError("tilt_zone_enter_pct must be between tilt_zone_lower_pct and "
                             "tilt_zone_upper_pct")
        if self.tilt_zone_enter_pct - self.tilt_zone_lower_pct < SLAT_POSITIONS - 1:
            raise ValueError(f"tilt_zone_enter_pct - tilt_zone_lower_pct must be >= "
                             f"{SLAT_POSITIONS - 1} so the {SLAT_POSITIONS} slat positions are "
                             "distinct whole percents")
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
    def slat_span(self) -> int:
        """
        Real travel percent of the slat window, from the entry landing down to the lower edge.
        """
        return self.tilt_zone_enter_pct - self.tilt_zone_lower_pct

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
        Real travel position the entry sequence finishes on: slat 0%, the least light tilt mode
        offers.
        """
        return self.tilt_zone_enter_pct

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

    @property
    def slat_positions(self) -> tuple[int, ...]:
        """
        Real travel position of each slat position, from the entry landing (slat 0%) down to the
        lower edge (100%): evenly spread over the slat window, rounded toward more closed (a higher
        real position).
        """
        last = SLAT_POSITIONS - 1
        return tuple(self.enter_landing_real - index * self.slat_span // last
                     for index in range(SLAT_POSITIONS))

    def virtual_to_real(self, virtual: int) -> int:
        """
        Map an in-tilt virtual position (0-100) to the real position of the nearest slat position,
        halves going to the more closed one.
        """
        last = SLAT_POSITIONS - 1
        return self.slat_positions[(2 * clamp_pct(virtual) * last + 99) // 200]

    def real_to_virtual(self, real: int) -> int:
        """
        Map an in-tilt real travel position to the virtual value of the nearest slat position,
        halves going to the more closed one.
        """
        positions = self.slat_positions
        index = min(range(SLAT_POSITIONS), key=lambda i: (abs(positions[i] - real), i))
        last = SLAT_POSITIONS - 1
        return (200 * index + last) // (2 * last)

    def snap_normal_target(self, target: int, current: int | None = None) -> int:
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
