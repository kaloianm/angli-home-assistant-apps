import unittest

from datetime import datetime, timedelta

from extractor_fan_control.config import PairConfig
from extractor_fan_control.logic import ExtractorFanPairLogic, LogicConfig
from extractor_fan_control.runtime import (
    FAN_CMD_RATE_LIMIT,
    FAN_CMD_RATE_WINDOW_SECONDS,
    PairRuntime,
)


class TestPairRuntimeRateLimiting(unittest.TestCase):

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

    def test_commands_under_limit_do_not_trip(self):
        for i in range(FAN_CMD_RATE_LIMIT):
            tripped = self.runtime.record_fan_command(self.t0 + timedelta(seconds=i))
            self.assertFalse(tripped)
        self.assertFalse(self.runtime.disabled)

    def test_exceeding_limit_in_window_trips_and_disables(self):
        # FAN_CMD_RATE_LIMIT commands are allowed; the next one within the window trips.
        for i in range(FAN_CMD_RATE_LIMIT):
            self.assertFalse(self.runtime.record_fan_command(self.t0 + timedelta(milliseconds=i)))
        tripped = self.runtime.record_fan_command(self.t0 + timedelta(seconds=1))
        self.assertTrue(tripped)
        self.assertTrue(self.runtime.disabled)

    def test_old_commands_outside_window_are_pruned(self):
        # Fill the window, then let it fully elapse; subsequent commands start fresh.
        for i in range(FAN_CMD_RATE_LIMIT):
            self.runtime.record_fan_command(self.t0 + timedelta(milliseconds=i))
        later = self.t0 + timedelta(seconds=FAN_CMD_RATE_WINDOW_SECONDS + 1)
        for i in range(FAN_CMD_RATE_LIMIT):
            self.assertFalse(self.runtime.record_fan_command(later + timedelta(milliseconds=i)))
        self.assertFalse(self.runtime.disabled)


if __name__ == "__main__":
    unittest.main()
