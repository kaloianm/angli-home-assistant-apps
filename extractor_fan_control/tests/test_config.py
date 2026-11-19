import unittest

from extractor_fan_control.config import ExtractorFanConfig, parse_app_config


def _args(**overrides):
    args = {
        "name": "bathroom",
        "light_entity": "light.bathroom",
        "fan_switch_entity": "switch.bathroom_fan",
        "min_light_on_for_fan_seconds": 15,
        "short_visit_threshold_seconds": 60,
        "staircase_interval_seconds": 30,
        "pulse_guard_seconds": 5,
    }
    args.update(overrides)
    return {key: value for key, value in args.items() if value is not None}


class TestConfigParsing(unittest.TestCase):

    def test_parse_valid_config_with_required_values(self):
        cfg = parse_app_config(_args(daily_run_time="07:30", daily_run_duration_seconds=600))

        self.assertEqual(30, cfg.staircase_interval_seconds)
        self.assertEqual(5, cfg.pulse_guard_seconds)
        self.assertEqual(25, cfg.keepalive_pulse_interval_seconds)
        self.assertEqual("bathroom", cfg.name)
        self.assertEqual(15, cfg.min_light_on_for_fan_seconds)
        self.assertEqual(60, cfg.short_visit_threshold_seconds)
        self.assertEqual("sensor.extractor_fan_bathroom_off_at", cfg.off_at_sensor_entity)
        self.assertEqual("07:30", cfg.daily_run_time)
        self.assertEqual(600, cfg.daily_run_duration_seconds)

    def test_missing_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "name is required"):
            parse_app_config(_args(name=None))

    def test_invalid_daily_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "daily_run_time must be HH:MM"):
            parse_app_config(_args(daily_run_time="25:99", daily_run_duration_seconds=600))

    def test_missing_daily_duration_disables_daily_run(self):
        cfg = parse_app_config(_args(daily_run_time="07:30"))
        self.assertIsNone(cfg.daily_run_time)
        self.assertIsNone(cfg.daily_run_duration_seconds)

    def test_missing_daily_time_disables_daily_run(self):
        cfg = parse_app_config(_args(daily_run_duration_seconds=600))
        self.assertIsNone(cfg.daily_run_time)
        self.assertIsNone(cfg.daily_run_duration_seconds)

    def test_pulse_guard_must_be_smaller_than_staircase_interval(self):
        with self.assertRaisesRegex(ValueError, "pulse_guard_seconds must be smaller"):
            parse_app_config(_args(pulse_guard_seconds=30))

    def test_missing_staircase_interval_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "staircase_interval_seconds is required"):
            parse_app_config(_args(staircase_interval_seconds=None))

    def test_missing_min_light_on_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "min_light_on_for_fan_seconds is required"):
            parse_app_config(_args(min_light_on_for_fan_seconds=None))

    def test_min_light_on_can_be_zero(self):
        cfg = parse_app_config(_args(min_light_on_for_fan_seconds=0))
        self.assertEqual(0, cfg.min_light_on_for_fan_seconds)

    def test_short_visit_threshold_cannot_be_zero(self):
        with self.assertRaisesRegex(ValueError, "short_visit_threshold_seconds must be > 0"):
            parse_app_config(_args(min_light_on_for_fan_seconds=0, short_visit_threshold_seconds=0))

    def test_min_light_on_cannot_exceed_short_visit_threshold(self):
        with self.assertRaisesRegex(
                ValueError,
                "min_light_on_for_fan_seconds must be <= short_visit_threshold_seconds"):
            parse_app_config(_args(min_light_on_for_fan_seconds=61))

    def test_off_at_sensor_entity_can_be_overridden(self):
        cfg = parse_app_config(
            _args(off_at_sensor_entity="sensor.courtesy_bathroom_air_extractor_off_at"))
        self.assertEqual("sensor.courtesy_bathroom_air_extractor_off_at", cfg.off_at_sensor_entity)

    def test_off_at_sensor_entity_must_be_a_sensor(self):
        with self.assertRaisesRegex(ValueError, "off_at_sensor_entity must start with 'sensor.'"):
            parse_app_config(_args(off_at_sensor_entity="input_datetime.bathroom_fan_off_at"))

    def test_empty_off_at_sensor_entity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "off_at_sensor_entity is required"):
            parse_app_config(_args(off_at_sensor_entity="  "))

    def test_config_string_representation_is_readable(self):
        config = ExtractorFanConfig(
            name="guestroom_bathroom",
            light_entity="light.guestroom_bathroom_ceiling_light",
            fan_switch_entity="switch.guestroom_bathroom_air_extractor",
            off_at_sensor_entity="sensor.guestroom_bathroom_air_extractor_off_at",
            min_light_on_for_fan_seconds=15,
            short_visit_threshold_seconds=60,
            daily_run_time=None,
            daily_run_duration_seconds=None,
            staircase_interval_seconds=30,
            pulse_guard_seconds=5,
        )
        rendered = str(config)
        self.assertIn("ExtractorFanConfig(", rendered)
        self.assertIn("name=guestroom_bathroom", rendered)
        self.assertIn("off_at_sensor_entity=sensor.guestroom_bathroom_air_extractor_off_at",
                      rendered)
        self.assertIn("daily_run_time=None", rendered)


if __name__ == "__main__":
    unittest.main()
