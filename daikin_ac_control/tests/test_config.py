import unittest

from daikin_ac_control.config import parse_app_config

VALID_ARGS = {
    "ac_mode": "select.climate_mode",
    "ac_entities": ["climate.living_room_ac", "climate.bedroom_ac"],
    "settings": {
        "off_hysteresis": 0.7,
        "on_hysteresis": 0.3,
    },
}


class TestConfigParsing(unittest.TestCase):

    def test_parse_valid_config(self):
        cfg = parse_app_config(VALID_ARGS)
        self.assertEqual("select.climate_mode", cfg.ac_mode_entity)
        self.assertEqual(["climate.living_room_ac", "climate.bedroom_ac"], cfg.ac_entities)
        self.assertAlmostEqual(0.7, cfg.settings.off_hysteresis)
        self.assertAlmostEqual(0.3, cfg.settings.on_hysteresis)

    def test_ac_mode_as_single_element_list(self):
        args = {**VALID_ARGS, "ac_mode": ["select.climate_mode"]}
        cfg = parse_app_config(args)
        self.assertEqual("select.climate_mode", cfg.ac_mode_entity)

    def test_ac_mode_list_with_multiple_elements_is_rejected(self):
        args = {**VALID_ARGS, "ac_mode": ["select.a", "select.b"]}
        with self.assertRaisesRegex(ValueError, "exactly one entity"):
            parse_app_config(args)

    def test_missing_ac_mode_is_rejected(self):
        args = {k: v for k, v in VALID_ARGS.items() if k != "ac_mode"}
        with self.assertRaisesRegex(ValueError, "ac_mode is required"):
            parse_app_config(args)

    def test_empty_ac_mode_string_is_rejected(self):
        args = {**VALID_ARGS, "ac_mode": "   "}
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            parse_app_config(args)

    def test_missing_ac_entities_is_rejected(self):
        args = {k: v for k, v in VALID_ARGS.items() if k != "ac_entities"}
        with self.assertRaisesRegex(ValueError, "ac_entities must be a non-empty list"):
            parse_app_config(args)

    def test_empty_ac_entities_list_is_rejected(self):
        args = {**VALID_ARGS, "ac_entities": []}
        with self.assertRaisesRegex(ValueError, "ac_entities must be a non-empty list"):
            parse_app_config(args)

    def test_blank_entity_in_ac_entities_is_rejected(self):
        args = {**VALID_ARGS, "ac_entities": ["climate.room", "  "]}
        with self.assertRaisesRegex(ValueError, "ac_entities\\[1\\] must not be empty"):
            parse_app_config(args)

    def test_missing_settings_is_rejected(self):
        args = {k: v for k, v in VALID_ARGS.items() if k != "settings"}
        with self.assertRaisesRegex(ValueError, "settings must be a mapping"):
            parse_app_config(args)

    def test_settings_as_list_is_rejected(self):
        args = {**VALID_ARGS, "settings": [{"off_hysteresis": 0.7}]}
        with self.assertRaisesRegex(ValueError, "settings must be a mapping"):
            parse_app_config(args)

    def test_missing_off_hysteresis_is_rejected(self):
        args = {**VALID_ARGS, "settings": {"on_hysteresis": 0.3}}
        with self.assertRaisesRegex(ValueError, "off_hysteresis is required"):
            parse_app_config(args)

    def test_missing_on_hysteresis_is_rejected(self):
        args = {**VALID_ARGS, "settings": {"off_hysteresis": 0.7}}
        with self.assertRaisesRegex(ValueError, "on_hysteresis is required"):
            parse_app_config(args)

    def test_non_positive_off_hysteresis_is_rejected(self):
        args = {**VALID_ARGS, "settings": {"off_hysteresis": 0, "on_hysteresis": 0.3}}
        with self.assertRaisesRegex(ValueError, "off_hysteresis must be > 0"):
            parse_app_config(args)

    def test_non_positive_on_hysteresis_is_rejected(self):
        args = {**VALID_ARGS, "settings": {"off_hysteresis": 0.7, "on_hysteresis": 0}}
        with self.assertRaisesRegex(ValueError, "on_hysteresis must be > 0"):
            parse_app_config(args)

    def test_non_numeric_hysteresis_is_rejected(self):
        args = {**VALID_ARGS, "settings": {"off_hysteresis": "warm", "on_hysteresis": 0.3}}
        with self.assertRaisesRegex(ValueError, "off_hysteresis must be a number"):
            parse_app_config(args)

    def test_float_values_accepted_as_strings(self):
        args = {**VALID_ARGS, "settings": {"off_hysteresis": "0.7", "on_hysteresis": "0.3"}}
        cfg = parse_app_config(args)
        self.assertAlmostEqual(0.7, cfg.settings.off_hysteresis)
        self.assertAlmostEqual(0.3, cfg.settings.on_hysteresis)

    def test_single_ac_entity_accepted(self):
        args = {**VALID_ARGS, "ac_entities": ["climate.only_room_ac"]}
        cfg = parse_app_config(args)
        self.assertEqual(["climate.only_room_ac"], cfg.ac_entities)


if __name__ == "__main__":
    unittest.main()
