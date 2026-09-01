import unittest

from gradhermetic_cover_control.geometry import (
    MIN_EPSILON_PCT,
    MIN_STEP_PCT,
    Zone,
    clamp_pct,
    to_command,
)

# Geometry used throughout the suite: zone [38, 44], epsilon 2, step 1.2 real travel percent. Span
# = 6, so one step is 20 on the virtual scale, and the ambiguity band is [36, 46]. Every configured
# number here -- STEP included -- is real blind travel.
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 1.2
VIRTUAL_STEP = 20.0


def _zone(**overrides):
    args = {
        "tilt_zone_upper_pct": UPPER,
        "tilt_zone_lower_pct": LOWER,
        "tilt_zone_epsilon_pct": EPSILON,
        "tilt_step_pct": STEP,
    }
    args.update(overrides)
    return Zone(**args)


class TestLandmarks(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_named_targets(self):
        self.assertAlmostEqual(6.0, self.zone.span)
        self.assertAlmostEqual(LOWER - EPSILON, self.zone.dip_target)
        self.assertAlmostEqual(UPPER + EPSILON, self.zone.release_target)
        self.assertAlmostEqual(self.zone.dip_target, self.zone.band_low)
        self.assertAlmostEqual(self.zone.release_target, self.zone.band_high)

    def test_release_defaults_to_the_bare_clearance(self):
        self.assertIsNone(self.zone.tilt_zone_release_pct)
        self.assertAlmostEqual(UPPER + EPSILON, self.zone.release_target)

    def test_landing_defaults_to_the_closed_edge(self):
        self.assertIsNone(self.zone.tilt_enter_landing_pct)
        self.assertAlmostEqual(UPPER, self.zone.enter_landing_real)
        # Virtual 0 is the closed edge, which is where the latching rise already ends.
        self.assertAlmostEqual(0.0, self.zone.enter_landing_virtual)

    def test_the_step_converts_real_travel_to_the_virtual_scale(self):
        # The config states real travel; the planner steps on the virtual scale, where the whole
        # zone is 100 wide.
        self.assertAlmostEqual(STEP, self.zone.tilt_step_pct)
        self.assertAlmostEqual(VIRTUAL_STEP, self.zone.step)
        self.assertAlmostEqual(STEP, self.zone.step / 100.0 * self.zone.span)

    def test_a_configured_landing_is_a_real_position(self):
        zone = _zone(tilt_enter_landing_pct=41.0)
        self.assertAlmostEqual(41.0, zone.enter_landing_real)
        self.assertAlmostEqual(50.0, zone.enter_landing_virtual)


class TestConfiguredReleaseHeight(unittest.TestCase):
    """A measured release height replaces the bare clearance and drags the band up with it."""

    def setUp(self):
        self.zone = _zone(tilt_zone_release_pct=55.0)

    def test_release_target_is_the_configured_height(self):
        self.assertAlmostEqual(55.0, self.zone.release_target)

    def test_band_high_still_coincides_with_the_release_target(self):
        # A latched-but-not-yet-released mechanism can rest anywhere up to the true release height,
        # so the ambiguity band has to reach exactly that far.
        self.assertAlmostEqual(self.zone.release_target, self.zone.band_high)
        self.assertAlmostEqual(self.zone.dip_target, self.zone.band_low)

    def test_the_band_widens_with_it(self):
        self.assertTrue(self.zone.in_band(50.0))
        self.assertTrue(self.zone.in_band(55.0))
        self.assertFalse(self.zone.in_band(55.1))
        # The zone itself is untouched: those heights are ambiguous, not slat-controllable.
        self.assertFalse(self.zone.in_zone(50.0))

    def test_snapping_follows_the_widened_band(self):
        self.assertAlmostEqual(55.0, self.zone.snap_normal_target(50.0))
        self.assertAlmostEqual(36.0, self.zone.snap_normal_target(37.0))
        self.assertAlmostEqual(55.0, self.zone.snap_normal_target(55.0))
        self.assertAlmostEqual(56.0, self.zone.snap_normal_target(56.0))


class TestPredicates(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_band_is_inclusive_at_both_edges(self):
        self.assertTrue(self.zone.in_band(36.0))
        self.assertTrue(self.zone.in_band(46.0))
        self.assertTrue(self.zone.in_band(41.0))

    def test_outside_band(self):
        self.assertFalse(self.zone.in_band(35.9))
        self.assertFalse(self.zone.in_band(46.1))
        self.assertFalse(self.zone.in_band(0.0))
        self.assertFalse(self.zone.in_band(100.0))

    def test_zone_is_inclusive_at_both_edges(self):
        self.assertTrue(self.zone.in_zone(LOWER))
        self.assertTrue(self.zone.in_zone(UPPER))
        self.assertFalse(self.zone.in_zone(LOWER - 0.1))
        self.assertFalse(self.zone.in_zone(UPPER + 0.1))

    def test_band_is_wider_than_zone(self):
        # A position between the zone and band edges is ambiguous but not slat-controllable.
        self.assertTrue(self.zone.in_band(45.0))
        self.assertFalse(self.zone.in_zone(45.0))


class TestMapping(unittest.TestCase):

    def setUp(self):
        self.zone = _zone()

    def test_virtual_extremes_map_to_zone_edges(self):
        self.assertAlmostEqual(LOWER, self.zone.virtual_to_real(100.0))
        self.assertAlmostEqual(UPPER, self.zone.virtual_to_real(0.0))
        self.assertAlmostEqual(100.0, self.zone.real_to_virtual(LOWER))
        self.assertAlmostEqual(0.0, self.zone.real_to_virtual(UPPER))

    def test_midpoint(self):
        self.assertAlmostEqual(41.0, self.zone.virtual_to_real(50.0))
        self.assertAlmostEqual(50.0, self.zone.real_to_virtual(41.0))

    def test_round_trip(self):
        for virtual in range(0, 101):
            self.assertAlmostEqual(float(virtual),
                                   self.zone.real_to_virtual(self.zone.virtual_to_real(virtual)))

    def test_mapping_clamps_outside_range(self):
        self.assertAlmostEqual(LOWER, self.zone.virtual_to_real(150.0))
        self.assertAlmostEqual(UPPER, self.zone.virtual_to_real(-10.0))
        self.assertAlmostEqual(100.0, self.zone.real_to_virtual(LOWER - 5.0))
        self.assertAlmostEqual(0.0, self.zone.real_to_virtual(UPPER + 5.0))


class TestSnapping(unittest.TestCase):
    """Q2: a normal-mode target inside the band snaps to the nearest band edge."""

    def setUp(self):
        self.zone = _zone()

    def test_targets_outside_band_are_untouched(self):
        for target in (0.0, 10.0, 35.0, 36.0, 46.0, 47.0, 100.0):
            self.assertAlmostEqual(target, self.zone.snap_normal_target(target))

    def test_target_near_lower_edge_snaps_down(self):
        self.assertAlmostEqual(36.0, self.zone.snap_normal_target(37.0))
        self.assertAlmostEqual(36.0, self.zone.snap_normal_target(40.9))

    def test_target_near_upper_edge_snaps_up(self):
        self.assertAlmostEqual(46.0, self.zone.snap_normal_target(45.0))
        self.assertAlmostEqual(46.0, self.zone.snap_normal_target(41.1))

    def test_tie_rises(self):
        # Exactly midway: rising never needs a latch release first, so ties go up.
        self.assertAlmostEqual(46.0, self.zone.snap_normal_target(41.0))

    def test_snapping_clamps(self):
        self.assertAlmostEqual(100.0, self.zone.snap_normal_target(120.0))
        self.assertAlmostEqual(0.0, self.zone.snap_normal_target(-5.0))


class TestHelpers(unittest.TestCase):

    def test_clamp(self):
        self.assertAlmostEqual(0.0, clamp_pct(-1.0))
        self.assertAlmostEqual(100.0, clamp_pct(101.0))
        self.assertAlmostEqual(42.0, clamp_pct(42.0))

    def test_to_command_rounds_and_clamps(self):
        self.assertEqual(43, to_command(42.8))
        self.assertEqual(42, to_command(42.4))
        self.assertEqual(100, to_command(120.0))
        self.assertEqual(0, to_command(-3.0))


class TestValidation(unittest.TestCase):

    def test_valid_geometry_passes(self):
        _zone().validate()

    def test_epsilon_below_minimum_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be >="):
            _zone(tilt_zone_epsilon_pct=MIN_EPSILON_PCT / 2.0)

    def test_minimum_epsilon_is_accepted(self):
        # One whole percent is enough to carry the rounded command clear of the edge.
        zone = _zone(tilt_zone_epsilon_pct=MIN_EPSILON_PCT)
        self.assertNotEqual(to_command(zone.lower), to_command(zone.dip_target))
        self.assertNotEqual(to_command(zone.upper), to_command(zone.release_target))

    def test_epsilon_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be > 0"):
            _zone(tilt_zone_epsilon_pct=0.0)

    def test_lower_not_below_upper_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be smaller"):
            _zone(tilt_zone_lower_pct=44.0)

    def test_step_below_actuator_resolution_raises(self):
        # Real travel below one whole percent rounds back to the current setpoint: no movement.
        for step in (0.5, MIN_STEP_PCT - 0.01):
            with self.subTest(step=step):
                with self.assertRaisesRegex(ValueError, "tilt_step_pct must be >="):
                    _zone(tilt_step_pct=step)

    def test_the_minimum_step_is_accepted(self):
        zone = _zone(tilt_step_pct=MIN_STEP_PCT)
        self.assertAlmostEqual(MIN_STEP_PCT, zone.tilt_step_pct)
        # One whole percent of a 6% zone is one sixth of the virtual scale.
        self.assertAlmostEqual(100.0 / 6.0, zone.step)

    def test_step_wider_than_the_zone_raises(self):
        for step in (6.1, 20.0, 100.0):
            with self.subTest(step=step):
                with self.assertRaisesRegex(ValueError, "tilt_step_pct must be <="):
                    _zone(tilt_step_pct=step)

    def test_a_step_spanning_the_whole_zone_is_accepted(self):
        zone = _zone(tilt_step_pct=UPPER - LOWER)
        self.assertAlmostEqual(100.0, zone.step)

    def test_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be > 0"):
            _zone(tilt_step_pct=0.0)

    def test_dip_target_below_zero_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct - tilt_zone_epsilon_pct"):
            _zone(tilt_zone_lower_pct=1.0, tilt_zone_upper_pct=10.0)

    def test_release_target_above_hundred_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct \\+ tilt_zone_epsilon_pct"):
            _zone(tilt_zone_upper_pct=99.0)

    def test_release_below_the_bare_clearance_raises(self):
        # Anything below upper + epsilon does not even carry the reported position clear of the
        # upper edge, so it cannot be the height the mechanism releases at.
        for release in (UPPER + EPSILON - 0.1, UPPER, 0.0):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be >="):
                    _zone(tilt_zone_release_pct=release)

    def test_release_exactly_at_the_bare_clearance_is_accepted(self):
        zone = _zone(tilt_zone_release_pct=UPPER + EPSILON)
        self.assertAlmostEqual(UPPER + EPSILON, zone.release_target)

    def test_release_above_a_hundred_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be <= 100"):
            _zone(tilt_zone_release_pct=101.0)

    def test_release_at_a_hundred_is_accepted(self):
        self.assertAlmostEqual(100.0, _zone(tilt_zone_release_pct=100.0).release_target)

    def test_landing_outside_the_zone_raises(self):
        # The landing is a slat position, so it has to be a real position inside the zone.
        for landing in (LOWER - 0.1, LOWER - 10.0, UPPER + 0.1, UPPER + 10.0, 0.0, 100.0):
            with self.subTest(landing=landing):
                with self.assertRaisesRegex(
                        ValueError, "tilt_enter_landing_pct must be between tilt_zone_lower_pct"):
                    _zone(tilt_enter_landing_pct=landing)

    def test_landing_at_either_zone_edge_is_accepted(self):
        closed = _zone(tilt_enter_landing_pct=UPPER)
        self.assertAlmostEqual(UPPER, closed.enter_landing_real)
        self.assertAlmostEqual(0.0, closed.enter_landing_virtual)
        opened = _zone(tilt_enter_landing_pct=LOWER)
        self.assertAlmostEqual(LOWER, opened.enter_landing_real)
        self.assertAlmostEqual(100.0, opened.enter_landing_virtual)

    def test_percentages_must_be_in_range(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be between 0 and 100"):
            _zone(tilt_zone_upper_pct=120.0)
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be between 0 and 100"):
            _zone(tilt_zone_lower_pct=-1.0)


if __name__ == "__main__":
    unittest.main()
