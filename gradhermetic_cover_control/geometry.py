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
meet: :meth:`Zone.virtual_to_real` and :meth:`Zone.real_to_virtual` are the only mapping between
them, and nothing outside this module converts.

**Every position is a whole percent.** The actuator accepts and reports integers, so every
configured number, every target and every belief the app holds is an ``int`` too, and :class:`Zone`
rejects anything else. Rounding happens in exactly two places: values arriving from Home Assistant
(:func:`to_whole_pct`), and the virtual mapping, because a zone a few percent wide is shown as a
0-100 slat scale. The mapping rounds to the nearest whole percent in both directions, and since a
zone is always narrower than the full travel, a real position survives the round trip through the
virtual scale unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Optional

# Real travel one height step moves the blind while it is not latched, unless apps.yaml says
# otherwise. Two percent is a short nudge on a blind whose full travel takes about fifteen seconds.
DEFAULT_HEIGHT_STEP_PCT = 2


def clamp_pct(value: int) -> int:
    """
    Clamp a whole percentage into [0, 100].
    """
    return max(0, min(100, value))


def to_whole_pct(value: float) -> int:
    """
    Round a percentage from outside the app to the nearest whole percent, halves rounding up.

    Home Assistant reports cover positions as integers already; this is the boundary that makes sure
    nothing else -- a template cover, an automation's event payload -- ever gets further in.
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

    Field names match the ``apps.yaml`` keys so validation errors name the setting the user has to
    fix, and every one of them is a **whole percent of real travel** -- the numbers the actuator
    reports and accepts. Construction validates, so a ``Zone`` instance is always a usable geometry.

    - ``tilt_step_pct`` -- how far one slat step moves the blind, in real travel. At least one
      percent, and at most the zone's own width (a step larger than the zone is meaningless).
    - ``height_step_pct`` -- how far one step press moves the blind while it is *not* latched, in
      real travel. At least one percent; it defaults to :data:`DEFAULT_HEIGHT_STEP_PCT`.
    - ``tilt_zone_epsilon_pct`` -- the clearance below the lower edge that the entry sequence dips
      to before the latching rise. It only ever applies to the lower edge; the upper side of the
      band is ``tilt_zone_release_pct``.
    - ``tilt_zone_release_pct`` -- how far the blind really has to rise before the latch lets go,
      as an absolute real position strictly above the upper edge. It is required because it can
      only be measured: the mechanism may need several percent of real travel past the reported
      upper edge before it disengages. Leaving tilt travels past it to the top limit; what the
      height decides is when the release has provably happened, and how far up the ambiguity band
      reaches.

    The optional field is the one a real installation may want to tune:

    - ``tilt_zone_enter_pct`` -- the absolute real position the entry sequence finishes on, which
      being a slat position must lie inside ``[lower, upper]``. The latching rise necessarily ends
      at the upper (closed) edge, where on some blinds the slats are not visibly open yet, so entry
      can be told to continue to a slightly-open angle. It defaults to ``upper``: the closed edge
      the rise already reaches, i.e. no extra entry step at all.
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
        # The ambiguity band must leave both travel limits outside it. A blind resting fully closed
        # or fully open provably cannot be latched, and the app relies on that: at startup those two
        # positions are the ones it can be surest about, and the latch belief they seed is what
        # decides whether the next move has to re-reference the actuator first. A band reaching
        # either limit would make the app doubt a blind that is sitting on its own end stop.
        if self.tilt_zone_lower_pct - self.tilt_zone_epsilon_pct <= 0:
            raise ValueError("tilt_zone_lower_pct - tilt_zone_epsilon_pct must be > 0 so the "
                             "ambiguity band stops short of the fully closed position")
        # A release at or below the upper edge would not even carry the reported position out of
        # the zone, and one at or above 100 would put the top limit inside the band.
        if self.tilt_zone_release_pct <= self.tilt_zone_upper_pct:
            raise ValueError("tilt_zone_release_pct must be > tilt_zone_upper_pct")
        if self.tilt_zone_release_pct >= 100:
            raise ValueError("tilt_zone_release_pct must be < 100 so the ambiguity band stops "
                             "short of the fully open position")
        landing = self.tilt_zone_enter_pct
        if landing is not None:
            # The landing is a slat position, so it has to be one: a real travel position inside the
            # zone. Anything outside is either not a slat angle at all, or would cross an edge.
            if not self.tilt_zone_lower_pct <= landing <= self.tilt_zone_upper_pct:
                raise ValueError("tilt_zone_enter_pct must be between tilt_zone_lower_pct and "
                                 "tilt_zone_upper_pct")
        if self.tilt_step_pct < 1:
            raise ValueError("tilt_step_pct must be >= 1 so one step moves the actuator")
        # A step wider than the zone itself always lands on an edge, which is not a step.
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
        Real position the blind must reach for the latch to have let go.

        Clearing the *reported* upper edge is not enough: disengaging the mechanism itself can take
        several percent more of real travel. ``tilt_zone_release_pct`` is that measured height.

        This is a threshold, not a destination: the tilt exit drives all the way to the top limit
        and merely uses this height to know the mechanism has released. Where it does act as a
        position is :attr:`band_high`, the top of the range a latched blind could be resting in.
        """
        return self.tilt_zone_release_pct

    @property
    def enter_landing_real(self) -> int:
        """
        Real travel position the entry sequence finishes on.

        Defaults to the upper (closed) edge, which is where the latching rise itself ends -- so an
        unconfigured blind gets no extra entry step, exactly as before the setting existed.
        """
        if self.tilt_zone_enter_pct is not None:
            return self.tilt_zone_enter_pct
        return self.upper

    @property
    def enter_landing_virtual(self) -> int:
        """
        The same landing on the virtual slat scale (0 = closed edge, 100 = open edge).

        The setting is configured as a real position, like everything else in ``apps.yaml``, but the
        planner's enter intent carries a virtual slat position. Converting here keeps the
        virtual<->real mapping in this module alone, and the round trip returns the configured
        landing exactly.
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
        Upper end of the latch-ambiguity band. Coincides with :attr:`release_target`.

        The two must coincide: a mechanism that is latched but has not yet been released can
        physically rest anywhere up to the true release height, so the band of positions from which
        "latched" cannot be ruled out reaches exactly that far. Configuring a higher
        ``tilt_zone_release_pct`` therefore widens the band with it, and everything derived from the
        band -- :meth:`in_band`, :meth:`snap_normal_target`, the startup latch belief and
        latch-belief clearing -- widens automatically.
        """
        return self.release_target

    # -- Predicates --------------------------------------------------------------------------------

    def in_band(self, position: int) -> bool:
        """
        Whether a real position falls inside the inclusive latch-ambiguity band.

        The band runs from the dip target up to the release target: below it the blind is clear of
        the lower edge, and above it the mechanism must have let go, because a still-latched
        mechanism can rest anywhere up to the height at which it releases. A blind resting outside
        the band provably cannot be latched, which is what makes feedback able to clear a latch
        belief.
        """
        return self.band_low <= position <= self.band_high

    def in_zone(self, position: int) -> bool:
        """
        Whether a real position falls inside the inclusive tilt zone ``[lower, upper]``.

        Travel that stays within the zone is slat rotation: it can neither engage the latch (which
        needs a rise across the lower edge from below) nor release it (a rise above the upper edge).
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
        Snap a whole-height target that falls inside the ambiguity band to the nearest band edge.

        Rising from below into the band silently engages the latch, so a normal-mode move that
        aimed there would leave the app doing height control on a latched mechanism -- belief and
        reality diverging. Snapping costs a couple of percent of blind travel and makes "normal mode
        never targets the band interior" an invariant instead of a hazard. Ties rise, because an
        upward move never needs a latch release first.

        ``current`` is where the blind rests now. When the nearest edge is that very position the
        far edge is chosen instead: a slider dragged into the band from an edge asked for a move,
        and snapping it straight back would move nothing and look broken.

        A configured ``tilt_zone_release_pct`` raises :attr:`band_high` and so widens the range of
        heights this refuses to stop at: the cost of the snap grows with the distance the mechanism
        genuinely needs to release.
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
        Snap a height-step target that falls inside the ambiguity band to the edge ahead of it.

        A step has a direction, so unlike a slider target it never snaps back the way it came: a
        step down into the band continues to :attr:`band_low`, a step up continues to
        :attr:`band_high`. The band is crossed in one step instead of being a place the blind can
        get stuck in front of.
        """
        target = clamp_pct(target)
        if not self.band_low < target < self.band_high:
            return target
        return self.band_high if rising else self.band_low
