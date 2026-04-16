import unittest

from extractor_fan_control.config import PairConfig, parse_app_config


class TestConfigParsing(unittest.TestCase):

    def test_parse_valid_config_with_required_values(self):
        cfg = parse_app_config({
            "staircase_interval_seconds": 30,
            "pulse_guard_seconds": 5,
            "pairs": [{
                "light_entity": "light.bathroom",
                "fan_switch_entity": "switch.bathroom_fan",
                "min_light_on_for_fan_seconds": 15,
                "short_visit_threshold_seconds": 60,
                "daily_run_time": "07:30",
                "daily_run_duration_seconds": 600,
            }]
        })

        self.assertEqual(30, cfg.staircase_interval_seconds)
        self.assertEqual(5, cfg.pulse_guard_seconds)
        self.assertEqual(25, cfg.keepalive_pulse_interval_seconds)
        self.assertEqual(1, len(cfg.pairs))
        self.assertEqual(15, cfg.pairs[0].min_light_on_for_fan_seconds)
        self.assertEqual(60, cfg.pairs[0].short_visit_threshold_seconds)
        self.assertTrue(cfg.pairs[0].name.startswith("pair_0_light.bathroom_"))
        self.assertEqual("07:30", cfg.pairs[0].daily_run_time)
        self.assertEqual(600, cfg.pairs[0].daily_run_duration_seconds)

    def test_duplicate_pair_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate pair name"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": [
                    {
                        "name": "bathroom",
                        "light_entity": "light.bathroom",
                        "fan_switch_entity": "switch.bathroom_fan",
                        "min_light_on_for_fan_seconds": 15,
                        "short_visit_threshold_seconds": 60,
                        "daily_run_time": "07:30",
                        "daily_run_duration_seconds": 600,
                    },
                    {
                        "name": "bathroom",
                        "light_entity": "light.wc",
                        "fan_switch_entity": "switch.wc_fan",
                        "min_light_on_for_fan_seconds": 15,
                        "short_visit_threshold_seconds": 60,
                        "daily_run_time": "08:00",
                        "daily_run_duration_seconds": 600,
                    },
                ]
            })

    def test_invalid_daily_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "daily_run_time must be HH:MM"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "min_light_on_for_fan_seconds": 15,
                    "short_visit_threshold_seconds": 60,
                    "daily_run_time": "25:99",
                    "daily_run_duration_seconds": 600,
                }]
            })

    def test_missing_daily_duration_disables_daily_run(self):
        cfg = parse_app_config({
            "staircase_interval_seconds": 30,
            "pulse_guard_seconds": 5,
            "pairs": [{
                "light_entity": "light.bathroom",
                "fan_switch_entity": "switch.bathroom_fan",
                "min_light_on_for_fan_seconds": 15,
                "short_visit_threshold_seconds": 60,
                "daily_run_time": "07:30",
            }]
        })
        self.assertIsNone(cfg.pairs[0].daily_run_time)
        self.assertIsNone(cfg.pairs[0].daily_run_duration_seconds)

    def test_missing_daily_time_disables_daily_run(self):
        cfg = parse_app_config({
            "staircase_interval_seconds": 30,
            "pulse_guard_seconds": 5,
            "pairs": [{
                "light_entity": "light.bathroom",
                "fan_switch_entity": "switch.bathroom_fan",
                "min_light_on_for_fan_seconds": 15,
                "short_visit_threshold_seconds": 60,
                "daily_run_duration_seconds": 600,
            }]
        })
        self.assertIsNone(cfg.pairs[0].daily_run_time)
        self.assertIsNone(cfg.pairs[0].daily_run_duration_seconds)

    def test_pulse_guard_must_be_smaller_than_staircase_interval(self):
        with self.assertRaisesRegex(ValueError, "pulse_guard_seconds must be smaller"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 30,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "min_light_on_for_fan_seconds": 15,
                    "short_visit_threshold_seconds": 60,
                    "daily_run_time": "07:30",
                    "daily_run_duration_seconds": 600,
                }],
            })

    def test_pairs_must_be_non_empty_list(self):
        with self.assertRaisesRegex(ValueError, "pairs must be a non-empty list"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": []
            })

    def test_missing_required_top_level_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "staircase_interval_seconds is required"):
            parse_app_config({
                "pulse_guard_seconds": 5,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "min_light_on_for_fan_seconds": 15,
                    "short_visit_threshold_seconds": 60,
                }]
            })

    def test_missing_required_pair_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError,
                                    "pairs\\[0\\]\\.min_light_on_for_fan_seconds is required"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "short_visit_threshold_seconds": 60,
                }]
            })

    def test_min_light_on_can_be_zero(self):
        cfg = parse_app_config({
            "staircase_interval_seconds": 30,
            "pulse_guard_seconds": 5,
            "pairs": [{
                "light_entity": "light.bathroom",
                "fan_switch_entity": "switch.bathroom_fan",
                "min_light_on_for_fan_seconds": 0,
                "short_visit_threshold_seconds": 60,
            }]
        })
        self.assertEqual(0, cfg.pairs[0].min_light_on_for_fan_seconds)

    def test_short_visit_threshold_cannot_be_zero(self):
        with self.assertRaisesRegex(ValueError,
                                    "pairs\\[0\\]\\.short_visit_threshold_seconds must be > 0"):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "min_light_on_for_fan_seconds": 0,
                    "short_visit_threshold_seconds": 0,
                }]
            })

    def test_min_light_on_cannot_exceed_short_visit_threshold(self):
        with self.assertRaisesRegex(
                ValueError,
                "pairs\\[0\\]\\.min_light_on_for_fan_seconds must be <= "
                "pairs\\[0\\]\\.short_visit_threshold_seconds",
        ):
            parse_app_config({
                "staircase_interval_seconds": 30,
                "pulse_guard_seconds": 5,
                "pairs": [{
                    "light_entity": "light.bathroom",
                    "fan_switch_entity": "switch.bathroom_fan",
                    "min_light_on_for_fan_seconds": 61,
                    "short_visit_threshold_seconds": 60,
                }]
            })

    def test_pair_config_string_representation_is_readable(self):
        pair = PairConfig(
            name="guestroom_bathroom",
            light_entity="light.guestroom_bathroom_ceiling_light",
            fan_switch_entity="switch.guestroom_bathroom_air_extractor",
            min_light_on_for_fan_seconds=15,
            short_visit_threshold_seconds=60,
            daily_run_time=None,
            daily_run_duration_seconds=None,
        )
        rendered = str(pair)
        self.assertIn("PairConfig(", rendered)
        self.assertIn("name=guestroom_bathroom", rendered)
        self.assertIn("daily_run_time=None", rendered)


if __name__ == "__main__":
    unittest.main()
