import unittest

from datetime import datetime, timedelta

from extractor_fan_control.config import PairConfig
from extractor_fan_control.logic import ExtractorFanPairLogic, LogicConfig
from extractor_fan_control.runtime import PairRuntime


class TestPairRuntimeExpectedStateTracking(unittest.TestCase):

    def setUp(self):
        self.runtime = PairRuntime(
            config=PairConfig(
                name="service_closet",
                light_entity="light.service_closet_light",
                fan_switch_entity="switch.service_closet_air_extractor",
                min_light_on_for_fan_seconds=5,
                short_visit_threshold_seconds=60,
                daily_run_time="16:00",
                daily_run_duration_seconds=900,
            ),
            logic=ExtractorFanPairLogic(LogicConfig()),
        )
        self.t0 = datetime(2026, 4, 18, 16, 15, 0)

    def test_matches_automation_feedback_out_of_order(self):
        self.runtime.record_expected_fan_state("off", self.t0)
        self.runtime.record_expected_fan_state("on", self.t0 + timedelta(milliseconds=20))

        # Simulate feedback arriving out of command order from the bus.
        self.assertTrue(
            self.runtime.consume_expected_fan_state("on", self.t0 + timedelta(milliseconds=100)))
        self.assertTrue(
            self.runtime.consume_expected_fan_state("off", self.t0 + timedelta(milliseconds=120)))

    def test_expired_expected_state_is_not_matched(self):
        self.runtime.record_expected_fan_state("on", self.t0)
        self.assertFalse(
            self.runtime.consume_expected_fan_state("on", self.t0 + timedelta(seconds=3)))


if __name__ == "__main__":
    unittest.main()
