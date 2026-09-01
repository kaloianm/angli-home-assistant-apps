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

**Every configured number is real travel percent.** The virtual scale is an internal presentation
detail -- it is what the cover entity's position slider shows while tilt mode is engaged, and what
the planner's enter/set-position intents carry -- so this module is the only place the two scales
meet: :attr:`Zone.enter_landing_virtual` and :attr:`Zone.step` convert the two configured slat
numbers into the virtual terms the planner works in, and nothing outside this module maps between
the scales.

The actuator speaks whole percent: commands are rounded to integers and its feedback is the
integer setpoint it reached. All arrival tests therefore happen in that integer domain (see
:func:`to_command`), which is why the geometry validates that every target the planner can emit
rounds to an integer distinct from the edge it is meant to clear.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Smallest usable clearance margin, in real travel percent. The margin only has to carry the
# actuator's reported integer position clear of a zone edge, so one whole percent suffices.
MIN_EPSILON_PCT = 1.0

# Smallest usable slat step, in real travel percent. The actuator speaks whole percent, so a step
# that moves the blind less than one reported percent rounds back to the current setpoint and moves
# nothing at all.
MIN_STEP_PCT = 1.0


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
    fix, and every one of them is **real travel percent** -- the numbers the actuator reports and
    accepts. Construction validates, so a ``Zone`` instance is always a usable geometry.

    - ``tilt_step_pct`` -- how far one slat step moves the blind, in real travel. It has to be at
      least one whole reported percent (or the rounded command repeats the current setpoint) and at
      most the zone's own width (a step larger than the zone is meaningless).

    The two optional fields are the ones a real installation has to be calibrated for:

    - ``tilt_zone_release_pct`` -- how far the blind really has to rise before the latch lets go.
      The clearance margin ``tilt_zone_epsilon_pct`` is only large enough to carry the *reported*
      position clear of the upper edge; the mechanism itself may need several percent more. It
      defaults to ``upper + epsilon``, which is the behaviour that predates the setting.
    - ``tilt_enter_landing_pct`` -- the absolute real position the entry sequence finishes on, which
      being a slat position must lie inside ``[lower, upper]``. The latching rise necessarily ends
      at the upper (closed) edge, where on some blinds the slats are not visibly open yet, so entry
      can be told to continue to a slightly-open angle. It defaults to ``upper``: the closed edge
      the rise already reaches, i.e. no extra entry step at all.
    """

    tilt_zone_upper_pct: float
    tilt_zone_lower_pct: float
    tilt_zone_epsilon_pct: float
    tilt_step_pct: float
    tilt_zone_release_pct: Optional[float] = None
    tilt_enter_landing_pct: Optional[float] = None

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
        if self.tilt_zone_release_pct is not None:
            # A release below upper + epsilon would not even carry the reported position clear of
            # the upper edge, and one above 100 is unreachable.
            if self.tilt_zone_release_pct < self.tilt_zone_upper_pct + self.tilt_zone_epsilon_pct:
                raise ValueError(
                    "tilt_zone_release_pct must be >= tilt_zone_upper_pct + tilt_zone_epsilon_pct")
            if self.tilt_zone_release_pct > 100.0:
                raise ValueError("tilt_zone_release_pct must be <= 100")
        landing = self.tilt_enter_landing_pct
        if landing is not None:
            # The landing is a slat position, so it has to be one: a real travel position inside the
            # zone. Anything outside is either not a slat angle at all, or would cross an edge.
            if not self.tilt_zone_lower_pct <= landing <= self.tilt_zone_upper_pct:
                raise ValueError("tilt_enter_landing_pct must be between tilt_zone_lower_pct and "
                                 "tilt_zone_upper_pct")
        if self.tilt_step_pct <= 0.0:
            raise ValueError("tilt_step_pct must be > 0")
        # The step is real travel, and the actuator speaks whole percent: a step below one reported
        # percent rounds back to the current setpoint and the slats never change.
        if self.tilt_step_pct < MIN_STEP_PCT:
            raise ValueError(
                f"tilt_step_pct must be >= {MIN_STEP_PCT} so one step moves the actuator at least "
                "one reported percent of real travel")
        # A step wider than the zone itself always lands on an edge, which is not a step.
        if self.tilt_step_pct > self.span:
            raise ValueError("tilt_step_pct must be <= tilt_zone_upper_pct - tilt_zone_lower_pct, "
                             "the real travel the whole tilt zone spans")

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
        Slat step size on the virtual scale, converted from the configured real travel.

        ``tilt_step_pct`` is real travel like every other configured percentage, but the planner
        steps the slats on the virtual scale, where the whole zone is 100 wide. Validation caps the
        step at the zone's span, so this never exceeds 100.
        """
        return self.tilt_step_pct / self.span * 100.0

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
        Real position risen to in order to release the latch.

        ``tilt_zone_upper_pct + tilt_zone_epsilon_pct`` only clears the *reported* upper edge, which
        is all the geometry needs; disengaging the mechanism itself can take several percent more of
        real travel. ``tilt_zone_release_pct`` is that measured height, and defaults to the bare
        clearance so an unconfigured blind behaves exactly as before.
        """
        if self.tilt_zone_release_pct is not None:
            return self.tilt_zone_release_pct
        return self.upper + self.epsilon

    @property
    def enter_landing_real(self) -> float:
        """
        Real travel position the entry sequence finishes on.

        Defaults to the upper (closed) edge, which is where the latching rise itself ends -- so an
        unconfigured blind gets no extra entry step, exactly as before the setting existed.
        """
        if self.tilt_enter_landing_pct is not None:
            return self.tilt_enter_landing_pct
        return self.upper

    @property
    def enter_landing_virtual(self) -> float:
        """
        The same landing on the virtual slat scale (0 = closed edge, 100 = open edge).

        The setting is configured as a real position, like everything else in ``apps.yaml``, but the
        planner's enter intent carries a virtual slat position -- that is also what the wall
        button's near-edge rule produces. Converting here keeps the virtual<->real mapping in this
        module alone.
        """
        return self.real_to_virtual(self.enter_landing_real)

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

        The two must coincide: a mechanism that is latched but has not yet been released can
        physically rest anywhere up to the true release height, so the band of positions from which
        "latched" cannot be ruled out reaches exactly that far. Configuring a higher
        ``tilt_zone_release_pct`` therefore widens the band with it, and everything derived from the
        band -- :meth:`in_band`, :meth:`snap_normal_target`, startup recovery and latch-belief
        clearing -- widens automatically.
        """
        return self.release_target

    # -- Predicates --------------------------------------------------------------------------------

    def in_band(self, position: float) -> bool:
        """
        Whether a real position falls inside the inclusive latch-ambiguity band.

        The band runs from the dip target up to the release target: below it the blind is clear of
        the lower edge, and above it the mechanism must have let go, because a still-latched
        mechanism can rest anywhere up to the height at which it releases. A blind resting outside
        the band provably cannot be latched, which is what makes feedback able to clear a latch
        belief.
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

        A configured ``tilt_zone_release_pct`` raises :attr:`band_high` and so widens the range of
        heights this refuses to stop at: the cost of the snap grows with the distance the mechanism
        genuinely needs to release.
        """
        target = clamp_pct(target)
        if not self.band_low < target < self.band_high:
            return target
        if target - self.band_low < self.band_high - target:
            return self.band_low
        return self.band_high
