import unittest

from gradhermetic_cover_control.config import parse_app_config


# Zone [38, 44], epsilon 2. Every key is a whole percent of real blind travel, so a 2% step is one
# slat step of the blind's own movement -- a third of this 6%-wide zone.
def _valid_args(**overrides):
    args = {
        "real_cover": "cover.living_room_blind",
        "virtual_id": "living_room",
        "virtual_name": "Living Room Blind",
        "tilt_zone_upper_pct": 44,
        "tilt_zone_lower_pct": 38,
        "tilt_zone_epsilon_pct": 2,
        "tilt_zone_release_pct": 46,
        "tilt_step_pct": 2,
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
        self.assertEqual(44, config.zone.tilt_zone_upper_pct)
        self.assertEqual(38, config.zone.tilt_zone_lower_pct)
        self.assertEqual("1/2/3", config.knx_move_address)
        self.assertEqual("1/2/4", config.knx_step_address)
        self.assertEqual("1/2/5", config.knx_tilt_address)

    def test_the_landing_key_is_optional(self):
        # Absent means "keep the geometric default": the closed edge.
        config = parse_app_config(_valid_args())
        self.assertIsNone(config.zone.tilt_zone_enter_pct)
        self.assertEqual(44, config.zone.enter_landing_real)
        self.assertEqual(0, config.zone.enter_landing_virtual)

    def test_release_and_landing_are_parsed(self):
        # Both are real travel positions; the landing has to lie inside the zone.
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct=52, tilt_zone_enter_pct=41))
        self.assertEqual(52, config.zone.tilt_zone_release_pct)
        self.assertEqual(52, config.zone.release_target)
        self.assertEqual(41, config.zone.enter_landing_real)
        self.assertEqual(50, config.zone.enter_landing_virtual)
        # The band follows the release height, so it must be visible in the summary log line.
        self.assertIn("tilt_zone_release_pct=52", str(config))
        self.assertIn("tilt_zone_enter_pct=41", str(config))

    def test_release_and_landing_accept_strings(self):
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct="52", tilt_zone_enter_pct="41"))
        self.assertEqual(52, config.zone.release_target)
        self.assertEqual(41, config.zone.enter_landing_real)

    def test_the_step_is_real_travel(self):
        # The config states the blind's own movement, in whole percent.
        config = parse_app_config(_valid_args(tilt_step_pct=3))
        self.assertEqual(3, config.zone.tilt_step_pct)
        self.assertIn("tilt_step_pct=3", str(config))

    def test_every_number_is_parsed_as_a_whole_percent(self):
        # YAML reads 44.0 as a float and a quoted value as a string; both mean a whole percent.
        config = parse_app_config(_valid_args(tilt_zone_upper_pct=44.0, tilt_zone_lower_pct="38",
                                              tilt_zone_release_pct="46.0", tilt_step_pct=2.0,
                                              tilt_zone_enter_pct=41.0, height_step_pct="3"))
        zone = config.zone
        for value in (zone.tilt_zone_upper_pct, zone.tilt_zone_lower_pct,
                      zone.tilt_zone_epsilon_pct, zone.tilt_zone_release_pct, zone.tilt_step_pct,
                      zone.tilt_zone_enter_pct, zone.height_step_pct):
            self.assertIs(int, type(value))
        self.assertIn("tilt_zone_upper_pct=44,", str(config))

    def test_a_fractional_percentage_is_rejected_not_rounded(self):
        # No actuator position corresponds to a fraction, and silently rounding a calibrated edge
        # would hide the mistake.
        for key in ("tilt_zone_upper_pct", "tilt_zone_lower_pct", "tilt_zone_epsilon_pct",
                    "tilt_zone_release_pct", "tilt_step_pct", "tilt_zone_enter_pct",
                    "height_step_pct"):
            for value in (1.5, "40.5"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, f"{key} must be a whole percent"):
                        parse_app_config(_valid_args(**{key: value}))

    def test_the_height_step_is_optional_and_parsed(self):
        self.assertEqual(2, parse_app_config(_valid_args()).zone.height_step_pct)
        config = parse_app_config(_valid_args(height_step_pct="5"))
        self.assertEqual(5, config.zone.height_step_pct)
        self.assertIn("height_step_pct=5", str(config))

    def test_height_step_must_move_the_actuator(self):
        with self.assertRaisesRegex(ValueError, "height_step_pct must be > 0"):
            parse_app_config(_valid_args(height_step_pct=0))

    def test_an_explicitly_null_landing_falls_back_to_the_default(self):
        config = parse_app_config(_valid_args(tilt_zone_enter_pct=None))
        self.assertEqual(44, config.zone.enter_landing_real)

    def test_the_release_height_is_required(self):
        # It can only be measured on the blind, so there is no geometric default to fall back to.
        args = _valid_args()
        del args["tilt_zone_release_pct"]
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct is required"):
            parse_app_config(args)
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct is required"):
            parse_app_config(_valid_args(tilt_zone_release_pct=None))

    def test_the_summary_shows_the_configured_release_height(self):
        self.assertIn("tilt_zone_release_pct=46", str(parse_app_config(_valid_args())))

    def test_release_not_above_the_upper_edge_raises(self):
        # Delegated to geometry: a release has to carry the reported position out of the zone.
        for release in (44, 40):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError,
                                            "tilt_zone_release_pct must be > tilt_zone_upper_pct"):
                    parse_app_config(_valid_args(tilt_zone_release_pct=release))

    def test_release_one_percent_above_the_upper_edge_is_accepted(self):
        # Epsilon no longer constrains the upper side: only the lower edge is padded by it.
        config = parse_app_config(_valid_args(tilt_zone_release_pct=45))
        self.assertEqual(45, config.zone.release_target)

    def test_release_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=120))

    def test_release_at_a_hundred_raises(self):
        # Delegated to geometry: the band may not reach the top limit.
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be < 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=100))

    def test_non_numeric_release_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_release_pct="high"))

    def test_landing_out_of_range_raises(self):
        # Not even a percentage: rejected by this module before the geometry sees it.
        with self.assertRaisesRegex(ValueError, "tilt_zone_enter_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_enter_pct=101))

    def test_landing_outside_the_zone_raises(self):
        # Delegated to geometry: a landing is a slat position, so it must lie inside the zone.
        for landing in (37, 45, 0, 100):
            with self.subTest(landing=landing):
                with self.assertRaisesRegex(
                        ValueError, "tilt_zone_enter_pct must be between tilt_zone_lower_pct"):
                    parse_app_config(_valid_args(tilt_zone_enter_pct=landing))

    def test_landing_at_either_zone_edge_is_accepted(self):
        for landing in (38, 44):
            with self.subTest(landing=landing):
                config = parse_app_config(_valid_args(tilt_zone_enter_pct=landing))
                self.assertEqual(landing, config.zone.enter_landing_real)

    def test_non_numeric_landing_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_enter_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_enter_pct="ajar"))

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
            parse_app_config(_valid_args(tilt_zone_lower_pct=44, tilt_zone_upper_pct=44))

    def test_epsilon_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be > 0"):
            parse_app_config(_valid_args(tilt_zone_epsilon_pct=0))

    def test_predip_below_zero_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct - tilt_zone_epsilon_pct"):
            parse_app_config(_valid_args(tilt_zone_lower_pct=1, tilt_zone_epsilon_pct=2))

    def test_percentage_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_upper_pct=120))

    def test_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be > 0"):
            parse_app_config(_valid_args(tilt_step_pct=0))

    def test_a_step_of_exactly_one_percent_is_accepted(self):
        config = parse_app_config(_valid_args(tilt_step_pct=1))
        self.assertEqual(1, config.zone.tilt_step_pct)

    def test_step_wider_than_the_zone_raises(self):
        # Span is 6, so a step of 20 real percent would jump the whole zone and then some.
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be <="):
            parse_app_config(_valid_args(tilt_step_pct=20))

    def test_a_step_spanning_the_whole_zone_is_accepted(self):
        config = parse_app_config(_valid_args(tilt_step_pct=6))
        self.assertEqual(6, config.zone.tilt_step_pct)

    def test_non_numeric_percentage_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_upper_pct="high"))


if __name__ == "__main__":
    unittest.main()
