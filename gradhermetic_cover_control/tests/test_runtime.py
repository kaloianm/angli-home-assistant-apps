import unittest

from datetime import datetime, timedelta

from gradhermetic_cover_control.config import GradhermeticConfig
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.logic import GradhermeticCoverLogic
from gradhermetic_cover_control.runtime import (
    COMMAND_RATE_LIMIT,
    COMMAND_RATE_WINDOW_SECONDS,
    CoverRuntime,
)


def _runtime():
    zone = Zone(tilt_zone_upper_pct=44.0, tilt_zone_lower_pct=38.0, tilt_zone_epsilon_pct=2.0,
                tilt_step_pct=1.2)
    config = GradhermeticConfig(
        real_cover="cover.living_room_blind",
        virtual_id="living_room",
        virtual_name="Living Room Blind",
        zone=zone,
        knx_move_address=None,
        knx_step_address=None,
    )
    return CoverRuntime(config=config, logic=GradhermeticCoverLogic(zone))


class TestCommandRateLimiting(unittest.TestCase):

    def setUp(self):
        self.runtime = _runtime()
        self.t0 = datetime(2026, 4, 15, 12, 0, 0)

    def test_within_limit_is_allowed(self):
        for i in range(COMMAND_RATE_LIMIT):
            self.assertFalse(self.runtime.record_command(self.t0 + timedelta(seconds=i)))
        self.assertFalse(self.runtime.disabled)

    def test_exceeding_limit_trips_and_disables(self):
        for i in range(COMMAND_RATE_LIMIT):
            self.runtime.record_command(self.t0 + timedelta(seconds=i))
        # One more within the window trips the limit.
        tripped = self.runtime.record_command(self.t0 + timedelta(seconds=COMMAND_RATE_LIMIT))
        self.assertTrue(tripped)
        self.assertTrue(self.runtime.disabled)

    def test_old_commands_slide_out_of_window(self):
        for i in range(COMMAND_RATE_LIMIT):
            self.runtime.record_command(self.t0 + timedelta(seconds=i))
        # A command well past the window resets the effective count.
        later = self.t0 + timedelta(seconds=COMMAND_RATE_WINDOW_SECONDS + 10)
        self.assertFalse(self.runtime.record_command(later))
        self.assertFalse(self.runtime.disabled)


if __name__ == "__main__":
    unittest.main()
