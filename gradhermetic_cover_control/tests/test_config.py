import unittest

from gradhermetic_cover_control.config import parse_app_config


def _valid_args(**overrides):
    args = {
        "real_cover": "cover.living_room_blind",
        "virtual_id": "living_room",
        "virtual_name": "Living Room Blind",
        "tilt_zone_upper_pct": 44.0,
        "tilt_zone_lower_pct": 38.0,
        "tilt_zone_epsilon_pct": 2.0,
        "tilt_step_pct": 20.0,
    }
    args.update(overrides)
    return args


class TestConfigParsing(unittest.TestCase):

    def test_valid_config_parses(self):
        config = parse_app_config(_valid_args(knx_move_address="1/2/3", knx_step_address="1/2/4"))
        self.assertEqual("cover.living_room_blind", config.real_cover)
        self.assertEqual("living_room", config.virtual_id)
        self.assertEqual("Living Room Blind", config.virtual_name)
        self.assertEqual(44.0, config.zone.tilt_zone_upper_pct)
        self.assertEqual(38.0, config.zone.tilt_zone_lower_pct)
        self.assertEqual("1/2/3", config.knx_move_address)
        self.assertEqual("1/2/4", config.knx_step_address)

    def test_the_release_and_landing_keys_are_optional(self):
        # Absent means "keep the geometric default": the bare clearance, and the closed edge.
        config = parse_app_config(_valid_args())
        self.assertIsNone(config.zone.tilt_zone_release_pct)
        self.assertIsNone(config.zone.tilt_enter_landing_pct)
        self.assertEqual(46.0, config.zone.release_target)
        self.assertEqual(0.0, config.zone.enter_landing)

    def test_release_and_landing_are_parsed(self):
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct=52.0, tilt_enter_landing_pct=15.0))
        self.assertEqual(52.0, config.zone.tilt_zone_release_pct)
        self.assertEqual(52.0, config.zone.release_target)
        self.assertEqual(15.0, config.zone.enter_landing)
        # The band follows the release height, so it must be visible in the summary log line.
        self.assertIn("tilt_zone_release_pct=52.0", str(config))
        self.assertIn("tilt_enter_landing_pct=15.0", str(config))

    def test_release_and_landing_accept_strings(self):
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct="52", tilt_enter_landing_pct="15"))
        self.assertEqual(52.0, config.zone.release_target)
        self.assertEqual(15.0, config.zone.enter_landing)

    def test_an_explicitly_null_key_falls_back_to_the_default(self):
        config = parse_app_config(
            _valid_args(tilt_zone_release_pct=None, tilt_enter_landing_pct=None))
        self.assertEqual(46.0, config.zone.release_target)
        self.assertEqual(0.0, config.zone.enter_landing)

    def test_release_below_the_bare_clearance_raises(self):
        # Delegated to geometry: it must clear upper + epsilon, which is what a release at all means.
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be >="):
            parse_app_config(_valid_args(tilt_zone_release_pct=45.0))

    def test_release_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_release_pct=120.0))

    def test_non_numeric_release_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_release_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_release_pct="high"))

    def test_landing_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_enter_landing_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_enter_landing_pct=101.0))

    def test_non_numeric_landing_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_enter_landing_pct must be a number"):
            parse_app_config(_valid_args(tilt_enter_landing_pct="ajar"))

    def test_knx_addresses_are_optional(self):
        config = parse_app_config(_valid_args())
        self.assertIsNone(config.knx_move_address)
        self.assertIsNone(config.knx_step_address)

    def test_blank_knx_address_becomes_none(self):
        config = parse_app_config(_valid_args(knx_move_address="  "))
        self.assertIsNone(config.knx_move_address)

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

    def test_epsilon_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be > 0"):
            parse_app_config(_valid_args(tilt_zone_epsilon_pct=0.0))

    def test_predip_below_zero_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_lower_pct - tilt_zone_epsilon_pct"):
            parse_app_config(_valid_args(tilt_zone_lower_pct=1.0, tilt_zone_epsilon_pct=2.0))

    def test_leave_target_above_hundred_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct \\+ tilt_zone_epsilon_pct"):
            parse_app_config(_valid_args(tilt_zone_upper_pct=99.0, tilt_zone_epsilon_pct=2.0))

    def test_percentage_out_of_range_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be between 0 and 100"):
            parse_app_config(_valid_args(tilt_zone_upper_pct=120.0))

    def test_step_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be > 0"):
            parse_app_config(_valid_args(tilt_step_pct=0.0))

    def test_epsilon_below_minimum_raises(self):
        # Delegated to geometry: a margin under one whole percent cannot carry the rounded command
        # clear of the zone edge it must cross.
        with self.assertRaisesRegex(ValueError, "tilt_zone_epsilon_pct must be >="):
            parse_app_config(_valid_args(tilt_zone_epsilon_pct=0.5))

    def test_step_must_move_actuator(self):
        # Span is 6, so any step below 100/6 ~= 16.7 never moves the integer-reporting actuator.
        with self.assertRaisesRegex(ValueError, "tilt_step_pct must be >="):
            parse_app_config(_valid_args(tilt_step_pct=1.0))

    def test_non_numeric_percentage_raises(self):
        with self.assertRaisesRegex(ValueError, "tilt_zone_upper_pct must be a number"):
            parse_app_config(_valid_args(tilt_zone_upper_pct="high"))


if __name__ == "__main__":
    unittest.main()
