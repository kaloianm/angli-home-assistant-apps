"""
Tilt-zone geometry for one Gradhermetic blind.

Everything numeric about the tilt zone lives here: the virtual<->real position mapping, the band
predicates, the named targets the latch sequences aim at, and the validation of the configured
numbers. ``config.py`` and ``planner.py`` both delegate to this module, so each rule has exactly
one definition.

Two coordinate systems are in play:

- **real** -- the blind's absolute travel position (0-100) as the actuator reports and accepts it.
- **virtual** -- what the user-facing cover shows. Outside the tilt zone it equals the real
  position; inside the zone it is inverted between the edges, so virtual 100 is the lower edge
  (slats perpendicular / most light) and virtual 0 is the upper edge (slats parallel / least
  light).

The actuator speaks whole percent: commands are rounded to integers and its feedback is the
integer setpoint it reached. All arrival tests therefore happen in that integer domain (see
:func:`to_command`), which is why the geometry validates that every target the planner can emit
rounds to an integer distinct from the edge it is meant to clear.
"""

from __future__ import annotations

from dataclasses import dataclass

# Smallest usable clearance margin, in real travel percent. The margin only has to carry the
# actuator's reported integer position clear of a zone edge, so one whole percent suffices.
MIN_EPSILON_PCT = 1.0


def clamp_pct(value: float) -> float:
    """
    Clamp a percentage into [0, 100].
    """
    return max(0.0, min(100.0, value))


def to_command(value: float) -> int:
    """
    Round a real travel percentage to the integer position the actuator actually speaks.
    """
    return int(round(clamp_pct(value)))


@dataclass(frozen=True)
class Zone:
    """
    Tilt-zone geometry for one blind.

    Field names match the ``apps.yaml`` keys so validation errors name the setting the user has to
    fix. Construction validates, so a ``Zone`` instance is always a usable geometry.
    """

    tilt_zone_upper_pct: float
    tilt_zone_lower_pct: float
    tilt_zone_epsilon_pct: float
    tilt_step_pct: float

    def __post_init__(self) -> None:
        """
        Reject an unusable geometry at construction time.
        """
        self.validate()

    def validate(self) -> None:
        """
        Validate the zone numbers, raising ``ValueError`` naming the offending setting.
        """
        if not 0.0 <= self.tilt_zone_lower_pct <= 100.0:
            raise ValueError("tilt_zone_lower_pct must be between 0 and 100")
        if not 0.0 <= self.tilt_zone_upper_pct <= 100.0:
            raise ValueError("tilt_zone_upper_pct must be between 0 and 100")
        if self.tilt_zone_lower_pct >= self.tilt_zone_upper_pct:
            raise ValueError("tilt_zone_lower_pct must be smaller than tilt_zone_upper_pct")
        if self.tilt_zone_epsilon_pct <= 0.0:
            raise ValueError("tilt_zone_epsilon_pct must be > 0")
        if self.tilt_zone_epsilon_pct < MIN_EPSILON_PCT:
            raise ValueError(
                f"tilt_zone_epsilon_pct must be >= {MIN_EPSILON_PCT} so the dip and release "
                "targets round to integers distinct from the zone edges they must clear")
        if self.tilt_zone_lower_pct - self.tilt_zone_epsilon_pct < 0.0:
            raise ValueError("tilt_zone_lower_pct - tilt_zone_epsilon_pct must be >= 0")
        if self.tilt_zone_upper_pct + self.tilt_zone_epsilon_pct > 100.0:
            raise ValueError("tilt_zone_upper_pct + tilt_zone_epsilon_pct must be <= 100")
        if self.tilt_step_pct <= 0.0:
            raise ValueError("tilt_step_pct must be > 0")
        # A step must map to at least one whole reported percent of real travel, otherwise the
        # rounded position command repeats the current position and the slats never change.
        min_step = 100.0 / self.span
        if self.tilt_step_pct < min_step:
            raise ValueError(
                f"tilt_step_pct must be >= {min_step:.2f} so one step moves the actuator at least "
                "one reported percent within the tilt zone")

    # -- Named landmarks ---------------------------------------------------------------------------

    @property
    def upper(self) -> float:
        """
        Upper zone edge: slats parallel / closed / least light.
        """
        return self.tilt_zone_upper_pct

    @property
    def lower(self) -> float:
        """
        Lower zone edge: slats perpendicular / open / most light.
        """
        return self.tilt_zone_lower_pct

    @property
    def epsilon(self) -> float:
        """
        Clearance margin used to cleanly cross a zone edge.
        """
        return self.tilt_zone_epsilon_pct

    @property
    def step(self) -> float:
        """
        Slat step size in virtual percent.
        """
        return self.tilt_step_pct

    @property
    def span(self) -> float:
        """
        Real travel percent between the two zone edges.
        """
        return self.tilt_zone_upper_pct - self.tilt_zone_lower_pct

    @property
    def dip_target(self) -> float:
        """
        Real position just below the lower edge, dipped to before the latching rise.
        """
        return self.lower - self.epsilon

    @property
    def release_target(self) -> float:
        """
        Real position just above the upper edge, risen to in order to release the latch.
        """
        return self.upper + self.epsilon

    @property
    def band_low(self) -> float:
        """
        Lower end of the latch-ambiguity band. Coincides with :attr:`dip_target`.
        """
        return self.dip_target

    @property
    def band_high(self) -> float:
        """
        Upper end of the latch-ambiguity band. Coincides with :attr:`release_target`.
        """
        return self.release_target

    # -- Predicates --------------------------------------------------------------------------------

    def in_band(self, position: float) -> bool:
        """
        Whether a real position falls inside the inclusive latch-ambiguity band.

        The band is the zone widened by the clearance margin on both sides. A blind resting outside
        it provably cannot be latched, which is what makes feedback able to clear a latch belief.
        """
        return self.band_low <= position <= self.band_high

    def in_zone(self, position: float) -> bool:
        """
        Whether a real position falls inside the inclusive tilt zone ``[lower, upper]``.

        Travel that stays within the zone is slat rotation: it can neither engage the latch (which
        needs a rise across the lower edge from below) nor release it (a rise above the upper edge).
        """
        return self.lower <= position <= self.upper

    # -- Mapping -----------------------------------------------------------------------------------

    def virtual_to_real(self, virtual: float) -> float:
        """
        Map an in-tilt virtual position (0-100) to a real travel position within the zone.
        """
        return self.upper - (clamp_pct(virtual) / 100.0) * self.span

    def real_to_virtual(self, real: float) -> float:
        """
        Map an in-tilt real travel position within the zone to a virtual position (0-100).
        """
        return clamp_pct((self.upper - real) / self.span * 100.0)

    def snap_normal_target(self, target: float) -> float:
        """
        Snap a whole-height target that falls inside the ambiguity band to the nearest band edge.

        Rising from below into the band silently engages the latch, so a normal-mode move that
        aimed there would leave the app doing height control on a latched mechanism -- belief and
        reality diverging. Snapping costs a couple of percent of blind travel and makes "normal mode
        never targets the band interior" an invariant instead of a hazard. Ties rise, because an
        upward move never needs a latch release first.
        """
        target = clamp_pct(target)
        if not self.band_low < target < self.band_high:
            return target
        if target - self.band_low < self.band_high - target:
            return self.band_low
        return self.band_high
