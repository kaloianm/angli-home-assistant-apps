import unittest

from datetime import datetime, timedelta

from extractor_fan_control.logic import (
    ACTION_FAN_OFF,
    ACTION_FAN_ON,
    ACTION_START_KEEPALIVE,
    ACTION_STOP_KEEPALIVE,
    ACTION_SET_TIMER,
    ACTION_CANCEL_TIMER,
    ACTION_SET_OFF_DEADLINE,
    TIMER_ACTIVATION,
    TIMER_DEADLINE,
    ExtractorFanPairLogic,
    LogicConfig,
    PairState,
)


def _kinds(actions):
    return [action.kind for action in actions]


def _timer_actions(actions, kind):
    return [a for a in actions if a.kind == kind]


def _off_deadlines(actions):
    return [a.at for a in actions if a.kind == ACTION_SET_OFF_DEADLINE]


class TestExtractorFanPairLogic(unittest.TestCase):

    def setUp(self):
        self.logic = ExtractorFanPairLogic(LogicConfig())
        self.t0 = datetime(2026, 4, 15, 12, 0, 0)

    def test_light_shorter_than_activation_threshold_never_starts_fan(self):
        actions_on = self.logic.on_light_on(self.t0)
        self.assertIn(ACTION_SET_TIMER, _kinds(actions_on))
        self.assertEqual(TIMER_ACTIVATION,
                         _timer_actions(actions_on, ACTION_SET_TIMER)[0].timer_name)
        self.assertEqual(PairState.WAITING_FOR_ACTIVATION, self.logic.state)

        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=10))
        self.assertIn(ACTION_CANCEL_TIMER, _kinds(actions_off))
        self.assertEqual(TIMER_ACTIVATION,
                         _timer_actions(actions_off, ACTION_CANCEL_TIMER)[0].timer_name)
        self.assertNotIn(ACTION_FAN_ON, _kinds(actions_on + actions_off))
        self.assertEqual(PairState.IDLE, self.logic.state)

    def test_activated_but_short_visit_turns_off_immediately(self):
        self.logic.on_light_on(self.t0)
        actions_activation = self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        self.assertIn(ACTION_FAN_ON, _kinds(actions_activation))
        self.assertIn(ACTION_START_KEEPALIVE, _kinds(actions_activation))
        self.assertEqual(PairState.RUNNING_LIGHT, self.logic.state)

        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=40))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_off))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_off))
        self.assertEqual(PairState.IDLE, self.logic.state)

    def test_long_visit_keeps_fan_for_same_duration_after_light_off(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # Light was on for 2 minutes -> post-run must also be 2 minutes.
        self.logic.on_light_off(self.t0 + timedelta(seconds=120))
        self.assertEqual(PairState.POST_RUN, self.logic.state)

        actions_before_deadline = self.logic.on_time_tick(self.t0 + timedelta(seconds=239))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_deadline))

        actions_at_deadline = self.logic.on_time_tick(self.t0 + timedelta(seconds=240))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_deadline))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_at_deadline))
        self.assertEqual(PairState.IDLE, self.logic.state)

    def test_long_visit_post_run_is_capped_to_ten_minutes_by_default(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # 30-minute light usage would normally imply 30-minute post-run, but
        # max_post_run_seconds caps it to 10 minutes by default.
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
        self.assertEqual(PairState.COMBINED_RUN, self.logic.state)

        # Light turns off at t+700 after 700s on-time.
        # Capped post-run is 600s -> occupancy end t+1300.
        self.logic.on_light_off(self.t0 + timedelta(seconds=700))

        # Remaining schedule at light-off is 700s -> schedule end t+1400.
        # Effective fan end is t+1400.
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

    def test_light_off_after_short_visit_never_starts_fan(self):
        # Regression for the "fan starts up the moment I turn the light off" symptom.
        # A visit shorter than the activation threshold must never turn the fan on.
        self.logic.on_light_on(self.t0)
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=3))
        self.assertNotIn(ACTION_FAN_ON, _kinds(actions_off))
        self.assertNotIn(ACTION_START_KEEPALIVE, _kinds(actions_off))
        self.assertEqual(PairState.IDLE, self.logic.state)

    def test_light_off_from_off_fan_never_emits_fan_on(self):
        # Broader invariant: while the fan is off, no light-off transition may command it on.
        # Activated long visit -> fan on -> short follow-up light cycle that never activates.
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        # Short visit turns the fan off immediately.
        self.logic.on_light_off(self.t0 + timedelta(seconds=40))
        self.assertFalse(self.logic.state.is_managed())

        # A subsequent quick light blip (below activation) must leave the fan off.
        self.logic.on_light_on(self.t0 + timedelta(seconds=100))
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=104))
        self.assertNotIn(ACTION_FAN_ON, _kinds(actions_off))
        self.assertEqual(PairState.IDLE, self.logic.state)

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
        self.assertEqual(PairState.SCHEDULED_RUN, self.logic.state)

        deadline_sets = _timer_actions(actions_start, ACTION_SET_TIMER)
        self.assertTrue(
            any(a.timer_name == TIMER_DEADLINE and a.at == self.t0 + timedelta(seconds=900)
                for a in deadline_sets))

        actions_before = self.logic.on_time_tick(self.t0 + timedelta(seconds=899))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before))

        actions_at = self.logic.on_time_tick(self.t0 + timedelta(seconds=900))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at))
        self.assertIn(ACTION_STOP_KEEPALIVE, _kinds(actions_at))
        self.assertEqual(PairState.IDLE, self.logic.state)

    def test_schedule_with_light_uses_later_end(self):
        self.logic.on_schedule_started(self.t0, duration_seconds=300)
        self.logic.on_light_on(self.t0 + timedelta(seconds=10))
        self.logic.on_time_tick(self.t0 + timedelta(seconds=25))

        # Light off at t+200 after 190s on (long visit).
        # Capped post-run = 190s -> occupancy end = t+390.
        # Schedule end = t+300, so fan should stay until t+390.
        self.logic.on_light_off(self.t0 + timedelta(seconds=200))

        actions_at_schedule_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=300))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_at_schedule_end))

        actions_before_occ_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=389))
        self.assertNotIn(ACTION_FAN_OFF, _kinds(actions_before_occ_end))

        actions_at_occ_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=390))
        self.assertIn(ACTION_FAN_OFF, _kinds(actions_at_occ_end))

    def test_no_off_deadline_while_light_driven_occupancy_is_active(self):
        # While the light is on and activated the off time is genuinely unknown, and the value
        # never left its initial "no deadline" state, so nothing is published at all.
        actions_on = self.logic.on_light_on(self.t0)
        actions_activation = self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        self.assertEqual(PairState.RUNNING_LIGHT, self.logic.state)
        self.assertEqual([], _off_deadlines(actions_on + actions_activation))

        # A short visit ends demand immediately, so it must not publish a spurious clear either.
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=40))
        self.assertEqual([], _off_deadlines(actions_off))

    def test_long_visit_publishes_post_run_deadline_and_clears_at_expiry(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # Light on for 2 minutes -> post-run of 2 minutes -> fan off at t0+240.
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=120))
        self.assertEqual([self.t0 + timedelta(seconds=240)], _off_deadlines(actions_off))

        actions_before = self.logic.on_time_tick(self.t0 + timedelta(seconds=239))
        self.assertEqual([], _off_deadlines(actions_before))

        actions_at_deadline = self.logic.on_time_tick(self.t0 + timedelta(seconds=240))
        self.assertEqual([None], _off_deadlines(actions_at_deadline))

    def test_published_deadline_respects_post_run_cap(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))

        # 30 minutes of light would imply a 30-minute post-run; the cap makes it 10 minutes.
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=1800))
        self.assertEqual([self.t0 + timedelta(seconds=2400)], _off_deadlines(actions_off))

    def test_scheduled_run_publishes_and_clears_its_deadline(self):
        actions_start = self.logic.on_schedule_started(self.t0, duration_seconds=900)
        self.assertEqual([self.t0 + timedelta(seconds=900)], _off_deadlines(actions_start))

        actions_before = self.logic.on_time_tick(self.t0 + timedelta(seconds=899))
        self.assertEqual([], _off_deadlines(actions_before))

        actions_at_end = self.logic.on_time_tick(self.t0 + timedelta(seconds=900))
        self.assertEqual([None], _off_deadlines(actions_at_end))

    def test_overlapping_demand_publishes_latest_end_once_light_is_off(self):
        actions_start = self.logic.on_schedule_started(self.t0, duration_seconds=300)
        self.assertEqual([self.t0 + timedelta(seconds=300)], _off_deadlines(actions_start))

        # Light-driven occupancy makes the off time unknown again while the light is on.
        self.logic.on_light_on(self.t0 + timedelta(seconds=10))
        actions_activation = self.logic.on_time_tick(self.t0 + timedelta(seconds=25))
        self.assertEqual(PairState.COMBINED_RUN, self.logic.state)
        self.assertEqual([None], _off_deadlines(actions_activation))

        # Light off at t0+200 after 190s -> occupancy end t0+390, later than schedule end t0+300.
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=200))
        self.assertEqual([self.t0 + timedelta(seconds=390)], _off_deadlines(actions_off))

    def test_light_back_on_during_post_run_keeps_deadline_until_activation(self):
        self.logic.on_light_on(self.t0)
        self.logic.on_time_tick(self.t0 + timedelta(seconds=15))
        actions_off = self.logic.on_light_off(self.t0 + timedelta(seconds=120))
        self.assertEqual([self.t0 + timedelta(seconds=240)], _off_deadlines(actions_off))

        # Back in the room: until the light-on counts as occupancy the post-run deadline still
        # holds, so nothing is republished.
        actions_on_again = self.logic.on_light_on(self.t0 + timedelta(seconds=150))
        self.assertEqual(PairState.WAITING_FOR_ACTIVATION, self.logic.state)
        self.assertEqual([], _off_deadlines(actions_on_again))

        actions_activation = self.logic.on_time_tick(self.t0 + timedelta(seconds=160))
        self.assertEqual(PairState.RUNNING_LIGHT, self.logic.state)
        self.assertEqual([None], _off_deadlines(actions_activation))

    def test_disable_clears_a_published_deadline(self):
        self.logic.on_schedule_started(self.t0, duration_seconds=900)
        self.assertEqual([None], _off_deadlines(self.logic.disable()))
        self.assertEqual(PairState.DISABLED, self.logic.state)

    def test_disable_without_a_deadline_publishes_nothing(self):
        self.assertEqual([], _off_deadlines(self.logic.disable()))

    def test_logic_logs_state_transitions_and_actions(self):
        messages = []
        logic = ExtractorFanPairLogic(LogicConfig(), log=messages.append)

        logic.on_light_on(self.t0)
        logic.on_time_tick(self.t0 + timedelta(seconds=10))

        self.assertTrue(any("state idle -> waiting_for_activation" in msg for msg in messages))
        self.assertTrue(
            any("state waiting_for_activation -> running_light" in msg for msg in messages))
        self.assertTrue(any("action fan_on" in msg for msg in messages))


if __name__ == "__main__":
    unittest.main()
