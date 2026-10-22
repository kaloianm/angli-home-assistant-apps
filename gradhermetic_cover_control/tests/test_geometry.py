import unittest

from gradhermetic_cover_control.geometry import (
    DEFAULT_HEIGHT_STEP_PCT,
    Zone,
    clamp_pct,
    to_whole_pct,
)

# Geometry used throughout the suite: zone [38, 44], epsilon 2, release 46, step 2 real travel
# percent. Span = 6, and the ambiguity band is [36, 46]. Every configured number here -- STEP
# included -- is a whole percent of real blind travel.
UPPER = 44
LOWER = 38
EPSILON = 2
RELEASE = 46
STEP = 2


def _zone(**overrides):
    args = {
        "tilt_zone_upper_pct": UPPER,
        "tilt_zone_lower_pct": LOWER,
        "tilt_zone_epsilon_pct": EPSILON,
        "tilt_zone_release_pct": RELEASE,
        "tilt_step_pct": STEP,
    }
    args.update(overrides)
    return Zone(**args)


class TestLandmarks(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_named_targets(self):
        self.assertEqual(6, self.zone.span)
        self.assertEqual(LOWER - EPSILON, self.zone.dip_target)
        self.assertEqual(RELEASE, self.zone.release_target)
        self.assertEqual(self.zone.dip_target, self.zone.band_low)
        self.assertEqual(self.zone.release_target, self.zone.band_high)

    def test_release_target_is_the_configured_height(self):
        self.assertEqual(RELEASE, self.zone.tilt_zone_release_pct)
        self.assertEqual(RELEASE, self.zone.release_target)

    def test_epsilon_only_pads_the_lower_edge(self):
        # Changing the clearance moves the dip and the bottom of the band, never the release.
        zone = _zone(tilt_zone_epsilon_pct=EPSILON + 1)
        self.assertEqual(LOWER - EPSILON - 1, zone.band_low)
        self.assertEqual(RELEASE, zone.release_target)
        self.assertEqual(RELEASE, zone.band_high)

    def test_landing_defaults_to_the_closed_edge(self):
        self.assertIsNone(self.zone.tilt_zone_enter_pct)
        self.assertEqual(UPPER, self.zone.enter_landing_real)
        # Virtual 0 is the closed edge, which is where the latching rise already ends.
        self.assertEqual(0, self.zone.enter_landing_virtual)

    def test_every_landmark_is_a_whole_percent(self):
        zone = _zone(tilt_zone_enter_pct=41)
        for value in (zone.upper, zone.lower, zone.epsilon, zone.span, zone.dip_target,
                      zone.release_target, zone.enter_landing_real, zone.enter_landing_virtual,
                      zone.band_low, zone.band_high):
            self.assertIs(int, type(value))

    def test_a_configured_landing_is_a_real_position(self):
        zone = _zone(tilt_zone_enter_pct=41)
        self.assertEqual(41, zone.enter_landing_real)
        self.assertEqual(50, zone.enter_landing_virtual)

    def test_the_height_step_defaults_and_is_configurable(self):
        self.assertEqual(DEFAULT_HEIGHT_STEP_PCT, self.zone.height_step_pct)
        self.assertEqual(5, _zone(height_step_pct=5).height_step_pct)


class TestConfiguredReleaseHeight(unittest.TestCase):
    """A release height well above the zone drags the band up with it."""

    def setUp(self):
        self.zone = _zone(tilt_zone_release_pct=55)

    def test_release_target_is_the_configured_height(self):
        self.assertEqual(55, self.zone.release_target)

    def test_band_high_still_coincides_with_the_release_target(self):
        # A latched-but-not-yet-released mechanism can rest anywhere up to the true release height,
        # so the ambiguity band has to reach exactly that far.
        self.assertEqual(self.zone.release_target, self.zone.band_high)
        self.assertEqual(self.zone.dip_target, self.zone.band_low)

    def test_the_band_widens_with_it(self):
        self.assertTrue(self.zone.in_band(50))
        self.assertTrue(self.zone.in_band(55))
        self.assertFalse(self.zone.in_band(56))
        # The zone itself is untouched: those heights are ambiguous, not slat-controllable.
        self.assertFalse(self.zone.in_zone(50))

    def test_snapping_follows_the_widened_band(self):
        self.assertEqual(55, self.zone.snap_normal_target(50))
        self.assertEqual(36, self.zone.snap_normal_target(37))
        self.assertEqual(55, self.zone.snap_normal_target(55))
        self.assertEqual(56, self.zone.snap_normal_target(56))


class TestPredicates(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_band_is_inclusive_at_both_edges(self):
        self.assertTrue(self.zone.in_band(36))
        self.assertTrue(self.zone.in_band(46))
        self.assertTrue(self.zone.in_band(41))

    def test_outside_band(self):
        self.assertFalse(self.zone.in_band(35))
        self.assertFalse(self.zone.in_band(47))
        self.assertFalse(self.zone.in_band(0))
        self.assertFalse(self.zone.in_band(100))

    def test_zone_is_inclusive_at_both_edges(self):
        self.assertTrue(self.zone.in_zone(LOWER))
        self.assertTrue(self.zone.in_zone(UPPER))
        self.assertFalse(self.zone.in_zone(LOWER - 1))
        self.assertFalse(self.zone.in_zone(UPPER + 1))

    def test_band_is_wider_than_zone(self):
        # A position between the zone and band edges is ambiguous but not slat-controllable.
        self.assertTrue(self.zone.in_band(45))
        self.assertFalse(self.zone.in_zone(45))


class TestMapping(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_virtual_extremes_map_to_zone_edges(self):
        self.assertEqual(LOWER, self.zone.virtual_to_real(100))
        self.assertEqual(UPPER, self.zone.virtual_to_real(0))
        self.assertEqual(100, self.zone.real_to_virtual(LOWER))
        self.assertEqual(0, self.zone.real_to_virtual(UPPER))

    def test_midpoint(self):
        self.assertEqual(41, self.zone.virtual_to_real(50))
        self.assertEqual(50, self.zone.real_to_virtual(41))

    def test_a_real_position_survives_the_round_trip(self):
        # The slat scale shows a whole percent for every real position, and reading it back lands on
        # the same real position -- what is published can be sent back without moving the slats.
        for real in range(LOWER, UPPER + 1):
            with self.subTest(real=real):
                self.assertEqual(real, self.zone.virtual_to_real(self.zone.real_to_virtual(real)))

    def test_the_round_trip_holds_on_every_zone(self):
        # The argument: one real percent is at least 100 / span >= 1 virtual percent wide, so a
        # rounded virtual value is always within half a real percent of where it came from.
        for lower in range(2, 98, 7):
            for upper in range(lower + 1, 98, 5):
                zone = _zone(tilt_zone_lower_pct=lower, tilt_zone_upper_pct=upper,
                             tilt_zone_epsilon_pct=1, tilt_zone_release_pct=upper + 1,
                             tilt_step_pct=1, height_step_pct=1)
                for real in range(lower, upper + 1):
                    self.assertEqual(real, zone.virtual_to_real(zone.real_to_virtual(real)))

    def test_every_virtual_position_maps_to_the_nearest_real_one(self):
        # Span 6: each real percent is 16.67 virtual percent wide, halves rounding toward open.
        self.assertEqual(UPPER, self.zone.virtual_to_real(8))
        self.assertEqual(UPPER - 1, self.zone.virtual_to_real(9))
        self.assertEqual(17, self.zone.real_to_virtual(UPPER - 1))
        for virtual in range(0, 101):
            self.assertTrue(self.zone.in_zone(self.zone.virtual_to_real(virtual)))

    def test_mapping_clamps_outside_range(self):
        self.assertEqual(LOWER, self.zone.virtual_to_real(150))
        self.assertEqual(UPPER, self.zone.virtual_to_real(-10))
        self.assertEqual(100, self.zone.real_to_virtual(LOWER - 5))
        self.assertEqual(0, self.zone.real_to_virtual(UPPER + 5))


class TestSnapping(unittest.TestCase):
    """Q2: a normal-mode target inside the band snaps to the nearest band edge."""

    def setUp(self):
        self.zone = _zone()

    def test_targets_outside_band_are_untouched(self):
        for target in (0, 10, 35, 36, 46, 47, 100):
            self.assertEqual(target, self.zone.snap_normal_target(target))

    def test_target_near_lower_edge_snaps_down(self):
        self.assertEqual(36, self.zone.snap_normal_target(37))
        self.assertEqual(36, self.zone.snap_normal_target(40))

    def test_target_near_upper_edge_snaps_up(self):
        self.assertEqual(46, self.zone.snap_normal_target(45))
        self.assertEqual(46, self.zone.snap_normal_target(42))

    def test_tie_rises(self):
        # Exactly midway: rising never needs a latch release first, so ties go up.
        self.assertEqual(46, self.zone.snap_normal_target(41))

    def test_snapping_clamps(self):
        self.assertEqual(100, self.zone.snap_normal_target(120))
        self.assertEqual(0, self.zone.snap_normal_target(-5))

    def test_the_nearest_edge_is_skipped_when_the_blind_already_rests_on_it(self):
        # A slider dragged into the band from an edge asked for a move, so the far edge wins.
        self.assertEqual(46, self.zone.snap_normal_target(37, current=36))
        self.assertEqual(36, self.zone.snap_normal_target(45, current=46))
        # Resting anywhere else, the nearest edge is still the nearest edge.
        self.assertEqual(36, self.zone.snap_normal_target(37, current=80))
        self.assertEqual(46, self.zone.snap_normal_target(45, current=10))

    def test_a_step_target_snaps_in_the_direction_of_travel(self):
        # A step down into the band continues to the bottom of it, a step up to the top.
        self.assertEqual(36, self.zone.snap_step_target(45, rising=False))
        self.assertEqual(46, self.zone.snap_step_target(37, rising=True))
        for target in (0, 36, 46, 100):
            self.assertEqual(target, self.zone.snap_step_target(target, rising=True))
            self.assertEqual(target, self.zone.snap_step_target(target, rising=False))


class TestHelpers(unittest.TestCase):

    def test_clamp(self):
        self.assertEqual(0, clamp_pct(-1))
        self.assertEqual(100, clamp_pct(101))
        self.assertEqual(42, clamp_pct(42))

    def test_to_whole_pct_rounds_half_up_and_clamps(self):
        self.assertEqual(43, to_whole_pct(42.8))
        self.assertEqual(42, to_whole_pct(42.4))
        # Halves go up, not to the nearest even number.
        self.assertEqual(43, to_whole_pct(42.5))
        self.assertEqual(44, to_whole_pct(43.5))
        self.assertEqual(100, to_whole_pct(120))
        self.assertEqual(0, to_whole_pct(-3))
        self.assertIs(int, type(to_whole_pct(42.0)))


class TestValidation(unittest.TestCase):

    def test_valid_geometry_passes(self):
        _zone().validate()

    def test_a_fractional_field_raises(self):
        # The actuator speaks whole percent, so the geometry refuses anything else outright.
        for key in ("tilt_zone_upper_pct", "tilt_zone_lower_pct", "tilt_zone_epsilon_pct",
                    "tilt_zone_release_pct", "tilt_step_pct", "tilt_zone_enter_pct",
                    "height_step_pct"):
            for value in (41.5, 2.0, True):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, f"{key} must be a whole percent"):
                        _zone(**{key: value})

    def test_one_percent_of_epsilon_is_accepted(self):
        zone = _zone(tilt_zone_epsilon_pct=1)
        self.assertEqual(LOWER - 1, zone.dip_target)

    def test_epsilon_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be >= 1"):
            _zone(tilt_zone_epsilon_pct=0)

    def test_lower_not_below_upper_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be smaller"):
            _zone(tilt_zone_lower_pct=44)

    def test_the_minimum_step_is_accepted(self):
        self.assertEqual(1, _zone(tilt_step_pct=1).tilt_step_pct)

    def test_step_wider_than_the_zone_raises(self):
        for step in (7, 20, 100):
            with self.subTest(step=step):
                with self.assertRaisesRegex(ValueError, "tilt_step_pct must be <="):
                    _zone(tilt_step_pct=step)

    def test_a_step_spanning_the_whole_zone_is_accepted(self):
        zone = _zone(tilt_step_pct=UPPER - LOWER)
        self.assertEqual(zone.span, zone.tilt_step_pct)

    def test_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be >= 1"):
            _zone(tilt_step_pct=0)

    def test_height_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "height_step_pct must be >= 1"):
            _zone(height_step_pct=0)

    def test_height_step_above_full_travel_raises(self):
        with self.assertRaisesRegex(ValueError, "height_step_pct must be <= 100"):
            _zone(height_step_pct=101)

    def test_dip_target_at_or_below_zero_raises(self):
        # The band must stop short of the bottom limit: a blind resting fully closed is one the app
        # has to be able to trust as unlatched, so lower - epsilon may not reach 0.
        for lower in (EPSILON, 1):
            with self.subTest(lower=lower):
                with self.assertRaisesRegex(ValueError,
                                            "tilt_zone_lower_pct - tilt_zone_epsilon_pct"):
                    _zone(tilt_zone_lower_pct=lower, tilt_zone_upper_pct=10)

    def test_dip_target_just_above_zero_is_accepted(self):
        zone = _zone(tilt_zone_lower_pct=EPSILON + 1, tilt_zone_upper_pct=10)
        self.assertEqual(1, zone.dip_target)

    def test_release_not_above_the_upper_edge_raises(self):
        # A release at or below the upper edge does not even carry the reported position out of
        # the zone.
        for release in (UPPER, UPPER - 1, 0):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError,
                                            "tilt_zone_release_pct must be > tilt_zone_upper_pct"):
                    _zone(tilt_zone_release_pct=release)

    def test_release_one_whole_percent_above_the_upper_edge_is_accepted(self):
        # The smallest release the actuator can tell apart from the edge; epsilon plays no part.
        zone = _zone(tilt_zone_release_pct=UPPER + 1)
        self.assertEqual(UPPER + 1, zone.release_target)

    def test_a_zone_near_the_top_limit_only_needs_room_for_the_release(self):
        # Epsilon no longer pads the upper edge, so it does not limit how high the zone may sit.
        zone = _zone(tilt_zone_upper_pct=98, tilt_zone_lower_pct=92,
                     tilt_zone_release_pct=99)
        self.assertEqual(99, zone.band_high)

    def test_release_at_or_above_a_hundred_raises(self):
        # The band must stop short of the top limit: a blind resting fully open is one the app has
        # to be able to trust as unlatched.
        for release in (100, 101):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be < 100"):
                    _zone(tilt_zone_release_pct=release)

    def test_release_just_below_a_hundred_is_accepted(self):
        self.assertEqual(99, _zone(tilt_zone_release_pct=99).release_target)

    def test_landing_outside_the_zone_raises(self):
        # The landing is a slat position, so it has to be a real position inside the zone.
        for landing in (LOWER - 1, LOWER - 10, UPPER + 1, UPPER + 10, 0, 100):
            with self.subTest(landing=landing):
                with self.assertRaisesRegex(
                        ValueError, "tilt_zone_enter_pct must be between tilt_zone_lower_pct"):
                    _zone(tilt_zone_enter_pct=landing)

    def test_landing_at_either_zone_edge_is_accepted(self):
        closed = _zone(tilt_zone_enter_pct=UPPER)
        self.assertEqual(UPPER, closed.enter_landing_real)
        self.assertEqual(0, closed.enter_landing_virtual)
        opened = _zone(tilt_zone_enter_pct=LOWER)
        self.assertEqual(LOWER, opened.enter_landing_real)
        self.assertEqual(100, opened.enter_landing_virtual)

    def test_percentages_must_be_in_range(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be between 0 and 100"):
            _zone(tilt_zone_upper_pct=120)
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be between 0 and 100"):
            _zone(tilt_zone_lower_pct=-1)


if __name__ == "__main__":
    unittest.main()
