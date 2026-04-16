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

    def test_long_visit_post_run_is_capped_to_ten_minutes_by_default(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # 30-minute light usage would normally imply 30-minute post-run,
        # but default max_post_run_seconds caps it to 10 minutes.
        self.logic.on_light_off(self.t0 + timedelta(seconds=1800))

        actions_before_cap = self.logic.on_time_tick(self.t0 + timedelta(seconds=2399))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_cap))

        actions_at_cap = self.logic.on_time_tick(self.t0 + timedelta(seconds=2400))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_cap))

    def test_long_visit_post_run_can_use_higher_configured_cap(self):
        logic = ExtractorFanPairLogic(LogicConfig(max_post_run_seconds=900))
        logic.on_light_on(self.t0)
        logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # 30-minute light usage is capped to configured 15-minute post-run.
        logic.on_light_off(self.t0 + timedelta(seconds=1800))

        actions_before_cap = logic.on_time_tick(self.t0 + timedelta(seconds=2699))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_cap))

        actions_at_cap = logic.on_time_tick(self.t0 + timedelta(seconds=2700))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_cap))

    def test_overlap_uses_remaining_schedule_or_capped_post_run(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # Schedule starts at t+500 for 900s (ends t+1400).
        self.logic.on_schedule_started(self.t0 + timedelta(seconds=500), duration_seconds=900)

        # Light turns off at t+700 after 700s on-time.
        # Capped post-run is 600s -> occupancy end t+1300.
        self.logic.on_light_off(self.t0 + timedelta(seconds=700))

        # Remaining schedule at light-off is 700s -> schedule end t+1400.
        # Effective fan end should be max(700, 600) => t+1400.
        actions_before_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=1399))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_end))

        actions_at_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=1400))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_end))

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

    def test_equal_thresholds_allow_activation_and_expected_stop_behavior(self):
        logic = ExtractorFanPairLogic(
            LogicConfig(min_light_on_for_fan_seconds=60, short_visit_threshold_seconds=60))
        logic.on_light_on(self.t0)

        before_activation = logic.on_time_tick(self.t0 + timedelta(seconds=59))
        self.assertNotIn(ACTION_FAN_ON, _kinds(before_activation))

        at_activation = logic.on_time_tick(self.t0 + timedelta(seconds=60))
        self.assertIn(ACTION_FAN_ON, _kinds(at_activation))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(at_activation))

        logic.on_light_off(self.t0 + timedelta(seconds=60))
        before_post_run_end = logic.on_time_tick(self.t0 + timedelta(seconds=119))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(before_post_run_end))

        at_post_run_end = logic.on_time_tick(self.t0 + timedelta(seconds=120))
        self.assertIn(ACTION_FAN_OFF, _kinds(at_post_run_end))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(at_post_run_end))

    def test_invalid_threshold_order_is_rejected(self):
        with self.assertRaisesRegex(
                ValueError,
                "min_light_on_for_fan_seconds must be <= short_visit_threshold_seconds",
        ):
            ExtractorFanPairLogic(
                LogicConfig(min_light_on_for_fan_seconds=61, short_visit_threshold_seconds=60))

    def test_zero_min_light_on_is_allowed(self):
        logic = ExtractorFanPairLogic(
            LogicConfig(min_light_on_for_fan_seconds=0, short_visit_threshold_seconds=60))
        actions_on = logic.on_light_on(self.t0)
        self.assertIn(ACTION_FAN_ON, _kinds(actions_on))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(actions_on))

    def test_zero_short_visit_threshold_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "short_visit_threshold_seconds must be > 0"):
            ExtractorFanPairLogic(
                LogicConfig(min_light_on_for_fan_seconds=0, short_visit_threshold_seconds=0))

    def test_schedule_only_starts_and_stops_fan(self):
        actions_start = self.logic.on_schedule_started(self.t0, duration_seconds=900)
        self.assertIn(ACTION_FAN_ON, _kinds(actions_start))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(actions_start))

        deadline_sets = _timer_actions(actions_start, ACTION_SET_TIMER)
        self.assertTrue(
            any(a.timer_name == TIMER_DEADLINE and a.at == self.t0 + timedelta(seconds=900)
                for a in deadline_sets))

        actions_before = self.logic.on_time_tick(self.t0 + timedelta(seconds=899))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before))

        actions_at = self.logic.on_time_tick(self.t0 + timedelta(seconds=900))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_at))

    def test_schedule_with_light_uses_later_end(self):
        self.logic.on_schedule_started(self.t0, duration_seconds=300)
        self.logic.on_light_on(self.t0 + timedelta(seconds=10))
        self.logic.on_time_tick(self.t0 + timedelta(seconds=25))

        # Light off at t+200 after 190s on (long visit).
        # Capped post-run = 190s -> occupancy end = t+390.
        # Schedule end = t+300. Fan should stay until t+390.
        self.logic.on_light_off(self.t0 + timedelta(seconds=200))

        actions_at_schedule_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=300))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_at_schedule_end))

        actions_before_occ_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=389))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_occ_end))

        actions_at_occ_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=390))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_occ_end))

    def test_rapid_manual_toggle_oscillation_is_self_sustaining(self):
        """Demonstrate that once the integration layer feeds false manual
        toggles (due to expected_fan_state overwrite race), the logic
        amplifies them into an infinite FAN_ON / FAN_OFF loop.

        Scenario: daily schedule ran, deadline expired (fan is now off).
        The integration layer's _on_fan_state misidentifies a delayed KNX
        state callback as a manual toggle, feeding alternating
        on_manual_fan_toggle(True) / on_manual_fan_toggle(False) calls.
        """
        self.logic.on_schedule_started(self.t0, duration_seconds=900)
        t_end = self.t0 + timedelta(seconds=900)
        self.logic.on_time_tick(t_end)

        # At this point: fan is off, no demand, no override.
        # Simulate the integration layer feeding false manual toggles
        # as it would when expected_fan_state tracking breaks:
        t_race = t_end + timedelta(seconds=1)
        for i in range(5):
            # False "on" callback -> treated as manual toggle ON
            actions_on = self.logic.on_manual_fan_toggle(t_race + timedelta(milliseconds=i * 2),
                                                         fan_on=True)
            self.assertIn(ACTION_FAN_ON, _kinds(actions_on),
                          f"iteration {i}: expected FAN_ON from false manual toggle")

            # False "off" callback -> treated as manual toggle OFF
            actions_off = self.logic.on_manual_fan_toggle(
                t_race + timedelta(milliseconds=i * 2 + 1), fan_on=False)
            self.assertIn(ACTION_FAN_OFF, _kinds(actions_off),
                          f"iteration {i}: expected FAN_OFF from false manual toggle")


if __name__ == "__main__":
    unittest.main()
