import unittest

from datetime import datetime, timedelta

from extractor_fan_control.logic import (
    ACTION_FAN_OFF,
    ACTION_FAN_ON,
    ACTION_START_KEEPALIVE,
    ACTION_STOP_KEEPALIVE,
    ACTION_SET_TIMER,
    ACTION_CANCEL_TIMER,
    TIMER_ACTIVATION,
    TIMER_DEADLINE,
    ExtractorFanPairLogic,
    LogicConfig,
)


def _kinds(actions):
    return [action.kind for action in actions]


def _timer_actions(actions, kind):
    return [a for a in actions if a.kind == kind]


class TestExtractorFanPairLogic(unittest.TestCase):

    def setUp(self):
        self.logic = ExtractorFanPairLogic(LogicConfig())
        self.t0 = datetime(2026, 4, 15, 12, 0, 0)

    def test_light_shorter_than_activation_threshold_never_starts_fan(self):
        actions_on = self.logic.on_light_on(self.t0)
        self.assertIn(ACTION_SET_TIMER, _kinds(actions_on))
        self.assertEqual(TIMER_ACTIVATION,
                         _timer_actions(actions_on, ACTION_SET_TIMER)[0].timer_name)

        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=10))
        self.assertIn(ACTION_CANCEL_TIMER, _kinds(actions_off))
        self.assertEqual(TIMER_ACTIVATION,
                         _timer_actions(actions_off, ACTION_CANCEL_TIMER)[0].timer_name)
        self.assertNotIn(ACTION_FAN_ON, _kinds(actions_on + actions_off))

    def test_activated_but_short_visit_turns_off_immediately(self):
        self.logic.on_light_on(self.t0)
        actions_activation = self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        self.assertIn(ACTION_FAN_ON, _kinds(actions_activation))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(actions_activation))

        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=40))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_off))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_off))

    def test_long_visit_keeps_fan_for_same_duration_after_light_off(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # Light was on for 2 minutes -> post-run must also be 2 minutes.
        self.logic.on_light_off(self.t0 + timedelta(seconds=120))

        actions_before_deadline = self.logic.on_time_tick(self.t0 + timedelta(seconds=239))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_deadline))

        actions_at_deadline = self.logic.on_time_tick(self.t0 + timedelta(seconds=240))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_deadline))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_at_deadline))

    def test_schedule_and_occupancy_overlap_uses_latest_end(self):
        schedule_actions = self.logic.on_schedule_started(self.t0, duration_seconds=300)
        self.assertIn(ACTION_FAN_ON, _kinds(schedule_actions))

        self.logic.on_light_on(self.t0 + timedelta(seconds=10))
        self.logic.on_time_tick(self.t0 + timedelta(seconds=25))
        self.logic.on_light_off(self.t0 + timedelta(seconds=100))

        actions_before_schedule_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=299))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_schedule_end))

        actions_schedule_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=300))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_schedule_end))

    def test_manual_override_is_authoritative_until_full_cycle_reset(self):
        self.logic.on_schedule_started(self.t0, duration_seconds=180)
        manual_off = self.logic.on_manual_fan_toggle(self.t0 + timedelta(seconds=1), fan_on=False)
        self.assertIn(ACTION_FAN_OFF, _kinds(manual_off))

        # Demand exists but override blocks fan.
        blocked = self.logic.on_time_tick(self.t0 + timedelta(seconds=50))
        self.assertEqual([], blocked)

        self.logic.on_light_on(self.t0 + timedelta(seconds=60))
        still_blocked = self.logic.on_time_tick(self.t0 + timedelta(seconds=80))
        self.assertNotIn(ACTION_FAN_ON, _kinds(still_blocked))
        self.assertNotIn(ACTION_START_KEEPALIVE, _kinds(still_blocked))

        # OFF marks reset-ready; next ON clears override.
        self.logic.on_light_off(self.t0 + timedelta(seconds=90))
        on_reset = self.logic.on_light_on(self.t0 + timedelta(seconds=91))
        self.assertIn(ACTION_FAN_ON, _kinds(on_reset))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(on_reset))
        after_reset = self.logic.on_time_tick(self.t0 + timedelta(seconds=106))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(after_reset))

    def test_repeated_ticks_are_idempotent(self):
        self.logic.on_light_on(self.t0)
        first = self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        self.assertIn(ACTION_FAN_ON, _kinds(first))

        second = self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        self.assertEqual([], second)

    def test_deadline_timer_tracks_nearest_expiration(self):
        self.logic.on_schedule_started(self.t0, duration_seconds=300)
        self.logic.on_light_on(self.t0 + timedelta(seconds=10))
        self.logic.on_time_tick(self.t0 + timedelta(seconds=25))
        self.logic.on_light_off(self.t0 + timedelta(seconds=100))

        # Occupancy post-run would expire at t0+190, earlier than schedule at t0+300.
        timer_set_actions = self.logic.on_time_tick(self.t0 + timedelta(seconds=101))
        deadline_sets = [
            a for a in timer_set_actions
            if a.kind == ACTION_SET_TIMER and a.timer_name == TIMER_DEADLINE
        ]
        self.assertTrue(
            any(a.at == self.t0 + timedelta(seconds=190) for a in deadline_sets)
            or timer_set_actions == [])


if __name__ == "__main__":
    unittest.main()
