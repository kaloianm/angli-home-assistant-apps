import unittest

from gradhermetic_cover_control.config import parse_app_config


# Zone [38, 44], epsilon 2. Every key is real blind travel, so a 1.2% step is one slat step of the
# blind's own movement -- a fifth of this 6%-wide zone.
def _valid_args(**overrides):
    args = {
        "real_cover": "cover.living_room_blind",
        "virtual_id": "living_room",
        "virtual_name": "Living Room Blind",
        "tilt_zone_upper_pct": 44.0,
        "tilt_zone_lower_pct": 38.0,
        "tilt_zone_release_pct": 46.0,
        "tilt_step_pct": 1.2,
    }
    args.update(overrides)
    return args


class TestConfigParsing(unittest.TestCase):

    def test_valid_config_parses(self):
        config = parse_app_config(
            _valid_args(knx_move_address="1/2/3", knx_step_address="1/2/4",
                        knx_tilt_address="1/2/5"))
        self.assertEqual("cover.living_room_blind", config.real_cover)
        self.assertEqual("living_room", config.virtual_id)
        self.assertEqual("Living Room Blind", config.virtual_name)
        self.assertEqual(44.0, config.zone.tilt_zone_upper_pct)
        self.assertEqual(38.0, config.zone.tilt_zone_lower_pct)
        self.assertEqual("1/2/3", config.knx_move_address)
        self.assertEqual("1/2/4", config.knx_step_address)
        self.assertEqual("1/2/5", config.knx_tilt_address)

    def test_the_landing_key_is_optional(self):
        # Absent means "keep the geometric default": the closed edge the latching rise reaches.
        config = parse_app_config(_valid_args())
        self.assertIsNone(config.zone.tilt_enter_landing_pct)
        self.assertEqual(44.0, config.zone.enter_landing_real)
        self.assertEqual(0.0, config.zone.enter_landing_virtual)

    def test_the_release_key_is_required(self):
        # It is a measured property of the mechanism; there is nothing honest to default it to.
        args = _valid_args()
        del args["tilt_zone_release_pct"]
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct is required"):
            parse_app_config(args)

    def test_release_and_landing_are_parsed(self):
        # Both are real travel positions; the landing has to lie inside the zone.
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct=52.0, tilt_enter_landing_pct=41.0))
        self.assertEqual(52.0, config.zone.tilt_zone_release_pct)
        self.assertEqual(52.0, config.zone.release_target)
        self.assertEqual(41.0, config.zone.enter_landing_real)
        self.assertEqual(50.0, config.zone.enter_landing_virtual)
        # The band follows the release height, so it must be visible in the summary log line.
        self.assertIn("tilt_zone_release_pct=52.0", str(config))
        self.assertIn("tilt_enter_landing_pct=41.0", str(config))

    def test_release_and_landing_accept_strings(self):
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct="52", tilt_enter_landing_pct="41"))
        self.assertEqual(52.0, config.zone.release_target)
        self.assertEqual(41.0, config.zone.enter_landing_real)

    def test_the_step_is_real_travel(self):
        # The planner steps on the virtual scale, but the config states the blind's own movement.
        config = parse_app_config(_valid_args(tilt_step_pct=3.0))
        self.assertEqual(3.0, config.zone.tilt_step_pct)
        self.assertEqual(50.0, config.zone.step)
        self.assertIn("tilt_step_pct=3.0", str(config))

    def test_the_height_step_is_optional_and_parsed(self):
        self.assertEqual(2.0, parse_app_config(_valid_args()).zone.height_step_pct)
        config = parse_app_config(_valid_args(height_step_pct="5"))
        self.assertEqual(5.0, config.zone.height_step_pct)
        self.assertIn("height_step_pct=5.0", str(config))

    def test_height_step_must_move_the_actuator(self):
        with self.assertRaisesRegex(ValueError, "height_step_pct must be >="):
            parse_app_config(_valid_args(height_step_pct=0.5))
        with self.assertRaisesRegex(ValueError, "height_step_pct must be > 0"):
            parse_app_config(_valid_args(height_step_pct=0))

    def test_an_explicitly_null_key_falls_back_to_the_default(self):
        config = parse_app_config(_valid_args(tilt_enter_landing_pct=None))
        self.assertEqual(44.0, config.zone.enter_landing_real)

    def test_release_not_clear_of_the_upper_edge_raises(self):
        # Delegated to geometry: a release that rounds to the upper edge has not cleared it.
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must round"):
            parse_app_config(_valid_args(tilt_zone_release_pct=44.0))

    def test_release_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=120.0))

    def test_release_at_a_hundred_raises(self):
        # Delegated to geometry: the band may not reach the top limit.
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be < 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=100.0))

    def test_non_numeric_release_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_release_pct="high"))

    def test_landing_out_of_range_raises(self):
        # Not even a percentage: rejected by this module before the geometry sees it.
        with self.assertRaisesRegex(ValueError, "tilt_enter_landing_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_enter_landing_pct=101.0))

    def test_landing_outside_the_zone_raises(self):
        # Delegated to geometry: a landing is a slat position, so it must lie inside the zone.
        for landing in (37.9, 44.1, 0.0, 100.0):
            with self.subTest(landing=landing):
                with self.assertRaisesRegex(
                        ValueError, "tilt_enter_landing_pct must be between tilt_zone_lower_pct"):
                    parse_app_config(_valid_args(tilt_enter_landing_pct=landing))

    def test_landing_at_either_zone_edge_is_accepted(self):
        for landing in (38.0, 44.0):
            with self.subTest(landing=landing):
                config = parse_app_config(_valid_args(tilt_enter_landing_pct=landing))
                self.assertEqual(landing, config.zone.enter_landing_real)

    def test_non_numeric_landing_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_enter_landing_pct must be a number"):
            parse_app_config(_valid_args(tilt_enter_landing_pct="ajar"))

    def test_knx_addresses_are_optional(self):
        config = parse_app_config(_valid_args())
        self.assertIsNone(config.knx_move_address)
        self.assertIsNone(config.knx_step_address)
        self.assertIsNone(config.knx_tilt_address)

    def test_blank_knx_address_becomes_none(self):
        config = parse_app_config(_valid_args(knx_move_address="  ", knx_tilt_address="  "))
        self.assertIsNone(config.knx_move_address)
        self.assertIsNone(config.knx_tilt_address)

    def test_missing_real_cover_raises(self):
        args = _valid_args()
        del args["real_cover"]
        with self.assertRaisesRegex(ValueError, "real_cover is required"):
            parse_app_config(args)

    def test_missing_virtual_id_raises(self):
        args = _valid_args()
        del args["virtual_id"]
        with self.assertRaisesRegex(ValueError, "virtual_id is required"):
            parse_app_config(args)

    def test_lower_not_below_upper_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be smaller"):
            parse_app_config(_valid_args(tilt_zone_lower_pct=44.0, tilt_zone_upper_pct=44.0))

    def test_a_band_reaching_the_bottom_limit_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct must be > 1"):
            parse_app_config(_valid_args(tilt_zone_lower_pct=1.0))

    def test_release_above_hundred_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be < 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=100.0))

    def test_percentage_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_upper_pct=120.0))

    def test_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be > 0"):
            parse_app_config(_valid_args(tilt_step_pct=0.0))

    def test_step_must_move_actuator(self):
        # The actuator reports whole percent, so real travel below 1.0 never moves it at all.
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be >="):
            parse_app_config(_valid_args(tilt_step_pct=0.5))

    def test_a_step_of_exactly_one_percent_is_accepted(self):
        config = parse_app_config(_valid_args(tilt_step_pct=1.0))
        self.assertEqual(1.0, config.zone.tilt_step_pct)

    def test_step_wider_than_the_zone_raises(self):
        # Span is 6, so a step of 20 real percent would jump the whole zone and then some.
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be <="):
            parse_app_config(_valid_args(tilt_step_pct=20.0))

    def test_a_step_spanning_the_whole_zone_is_accepted(self):
        config = parse_app_config(_valid_args(tilt_step_pct=6.0))
        self.assertEqual(100.0, config.zone.step)

    def test_non_numeric_percentage_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_upper_pct="high"))


if __name__ == "__main__":
    unittest.main()
