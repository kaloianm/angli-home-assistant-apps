import unittest

from gradhermetic_cover_control.executor import (
    ACTION_ARM_SETTLE_TIMER,
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_NOTIFY,
    ACTION_OPEN_FULL,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    DEVIATION_TOLERANCE_PCT,
    NOTIFY_STALL,
    SETTLE_TIMEOUT_SECONDS,
    STATUS_ABANDONED,
    STATUS_COMPLETED,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_STALLED,
    Executor,
)
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.planner import (
    COMMAND_CLOSE,
    COMMAND_OPEN,
    COMMAND_POSITION,
    LATCH_LATCHED,
    LATCH_UNLATCHED,
    PLAN_ENTER,
    PLAN_NORMAL,
    PLAN_SLAT,
    STEP_MOVE_TO,
    STEP_RISE_TO_AT_LEAST,
    Plan,
    Step,
)

UPPER = 44.0
LOWER = 38.0
ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=2.0,
            tilt_step_pct=1.2)


def _kinds(outcome):
    return [action.kind for action in outcome.actions]


def _of(outcome, kind):
    return [action for action in outcome.actions if action.kind == kind]


def _enter_plan():
    return Plan(PLAN_ENTER, (
        Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
        Step(STEP_MOVE_TO, 36.0),
        Step(STEP_MOVE_TO, UPPER),
    ), LATCH_LATCHED)


def _move_plan(target, command="position"):
    return Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, target, command),), LATCH_UNLATCHED)


class TestActivation(unittest.TestCase):

    def setUp(self):
        self.executor = Executor(ZONE)

    def test_first_command_is_issued_and_the_timer_armed(self):
        outcome = self.executor.start(_enter_plan(), 80.0, False)
        self.assertEqual(STATUS_RUNNING, outcome.status)
        self.assertEqual([ACTION_OPEN_FULL, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertEqual(SETTLE_TIMEOUT_SECONDS, _of(outcome, ACTION_ARM_SETTLE_TIMER)[0].seconds)

    def test_satisfied_step_is_skipped(self):
        # Already fully open: the enter sequence starts at the dip, with no no-op waypoint that the
        # actuator would never acknowledge.
        outcome = self.executor.start(_enter_plan(), 100.0, False)
        self.assertEqual([ACTION_MOVE_TO, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertAlmostEqual(36.0, _of(outcome, ACTION_MOVE_TO)[0].position)

    def test_satisfied_step_is_not_skipped_while_still_travelling(self):
        # Passing through 100 is not resting at 100, so the command still goes out.
        outcome = self.executor.start(_enter_plan(), 100.0, True)
        self.assertEqual([ACTION_OPEN_FULL, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))

    def test_a_wholly_satisfied_plan_completes_at_once(self):
        outcome = self.executor.start(_move_plan(50.0), 50.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_PUBLISH_POSITION], _kinds(outcome))
        self.assertFalse(self.executor.has_plan)

    def test_rounding_decides_satisfaction(self):
        # The actuator speaks whole percent: 42.8 is commanded as 43 and satisfied by 43.
        outcome = self.executor.start(_move_plan(42.8), 43.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)


class TestArrival(unittest.TestCase):

    def setUp(self):
        self.executor = Executor(ZONE)
        self.executor.start(_enter_plan(), 80.0, False)  # commanded the full open

    def test_motion_feedback_does_not_advance(self):
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(100.0, True).status)

    def test_arrival_issues_the_next_command_and_rearms(self):
        outcome = self.executor.on_feedback(100.0, False)
        self.assertEqual(STATUS_RUNNING, outcome.status)
        self.assertEqual([ACTION_MOVE_TO, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertAlmostEqual(36.0, _of(outcome, ACTION_MOVE_TO)[0].position)

    def test_feedback_short_of_the_target_does_nothing(self):
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(90.0, False).status)

    def test_stale_feedback_at_the_send_time_position_cannot_advance(self):
        # The step was only commanded because the predicate did not hold at 80, so a duplicate
        # report of 80 can never complete it -- however small the step.
        for _ in range(3):
            self.assertEqual(STATUS_IDLE, self.executor.on_feedback(80.0, False).status)
        self.assertTrue(self.executor.has_plan)

    def test_duplicate_arrival_after_the_step_completed_is_ignored(self):
        self.executor.on_feedback(100.0, False)  # completes step 1, commands the dip to 36
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(100.0, False).status)

    def test_completion_cancels_the_timer_and_publishes(self):
        self.executor.on_feedback(100.0, False)
        self.executor.on_feedback(36.0, False)
        outcome = self.executor.on_feedback(UPPER, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_PUBLISH_POSITION], _kinds(outcome))
        self.assertFalse(self.executor.has_plan)

    def test_feedback_without_a_plan_is_idle(self):
        executor = Executor(ZONE)
        self.assertEqual(STATUS_IDLE, executor.on_feedback(50.0, False).status)

    def test_unreadable_position_does_not_advance(self):
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(None, False).status)
        self.assertTrue(self.executor.has_plan)


class TestRiseToAtLeast(unittest.TestCase):

    def setUp(self):
        self.executor = Executor(ZONE)
        self.plan = Plan("leave", (Step(STEP_RISE_TO_AT_LEAST, 46.0),), LATCH_UNLATCHED)

    def test_overshoot_satisfies_the_step(self):
        self.executor.start(self.plan, UPPER, False)
        outcome = self.executor.on_feedback(47.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertAlmostEqual(47.0, _of(outcome, ACTION_PUBLISH_POSITION)[0].position)

    def test_short_of_the_target_does_not(self):
        self.executor.start(self.plan, UPPER, False)
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(45.0, False).status)

    def test_already_above_is_skipped(self):
        outcome = self.executor.start(self.plan, 50.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)


class TestCommandDistinctFromTarget(unittest.TestCase):
    """A step may aim past what satisfies it; the command follows the aim, arrival the target."""

    def setUp(self):
        self.executor = Executor(ZONE)
        # The shape the tilt exit uses: satisfied at 46, commanded to 48.
        self.plan = Plan("leave", (Step(STEP_RISE_TO_AT_LEAST, 46.0, COMMAND_POSITION,
                                        command_pct=48.0),), LATCH_UNLATCHED)

    def test_the_command_carries_the_command_position(self):
        outcome = self.executor.start(self.plan, UPPER, False)
        self.assertEqual([ACTION_MOVE_TO, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertAlmostEqual(48.0, _of(outcome, ACTION_MOVE_TO)[0].position)

    def test_arrival_is_judged_by_the_target_not_the_command(self):
        # Settling two percent short of the commanded 48 still clears the 46 the step needs.
        self.executor.start(self.plan, UPPER, False)
        outcome = self.executor.on_feedback(46.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertAlmostEqual(46.0, _of(outcome, ACTION_PUBLISH_POSITION)[0].position)

    def test_short_of_the_target_still_does_not_arrive(self):
        self.executor.start(self.plan, UPPER, False)
        self.assertEqual(STATUS_IDLE, self.executor.on_feedback(45.0, False).status)
        self.assertTrue(self.executor.has_plan)

    def test_activation_skips_on_the_target_not_the_command(self):
        # Already resting at 46: the step is satisfied, so no command goes out at all.
        outcome = self.executor.start(self.plan, 46.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertEqual([], _of(outcome, ACTION_MOVE_TO))

    def test_the_settle_timer_accepts_on_the_target(self):
        self.executor.start(self.plan, UPPER, False)
        self.assertEqual(STATUS_COMPLETED, self.executor.on_timer(47.0, False).status)

    def test_a_stall_names_the_target_the_step_needed(self):
        self.executor.start(self.plan, UPPER, False)
        outcome = self.executor.on_timer(45.0, False)
        self.assertEqual(STATUS_STALLED, outcome.status)
        message = _of(outcome, ACTION_NOTIFY)[0].message
        self.assertIn("did not reach 46%", message)
        self.assertIn("45", message)

    def test_a_move_step_can_carry_a_command_position_too(self):
        executor = Executor(ZONE)
        movement = _move_plan(30.0)
        movement = Plan(movement.kind,
                        (Step(STEP_MOVE_TO, 30.0, COMMAND_POSITION, command_pct=28.0),),
                        LATCH_UNLATCHED)
        outcome = executor.start(movement, 80.0, False)
        self.assertAlmostEqual(28.0, _of(outcome, ACTION_MOVE_TO)[0].position)
        # Exact-equality arrival still measures the target, so 28 does not complete it.
        self.assertEqual(STATUS_IDLE, executor.on_feedback(28.0, False).status)
        self.assertEqual(STATUS_COMPLETED, executor.on_feedback(30.0, False).status)


class TestPublishedPosition(unittest.TestCase):

    def test_normal_plans_publish_the_real_position(self):
        executor = Executor(ZONE)
        outcome = executor.start(_move_plan(30.0), 30.0, False)
        self.assertAlmostEqual(30.0, _of(outcome, ACTION_PUBLISH_POSITION)[0].position)

    def test_slat_plans_publish_the_inverted_zone_mapping(self):
        executor = Executor(ZONE)
        movement = Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, 41.0),), LATCH_LATCHED)
        outcome = executor.start(movement, 41.0, False)
        self.assertAlmostEqual(50.0, _of(outcome, ACTION_PUBLISH_POSITION)[0].position)

    def test_published_value_follows_the_blind_not_the_setpoint(self):
        # A slat step commanded 42.8 -> 43; the honest virtual position is the one for 43.
        executor = Executor(ZONE)
        movement = Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, 42.8),), LATCH_LATCHED)
        executor.start(movement, UPPER, False)
        outcome = executor.on_feedback(43.0, False)
        self.assertAlmostEqual(ZONE.real_to_virtual(43.0),
                               _of(outcome, ACTION_PUBLISH_POSITION)[0].position)


class TestSettleTimer(unittest.TestCase):

    def setUp(self):
        self.executor = Executor(ZONE)
        self.executor.start(_move_plan(0.0, COMMAND_CLOSE), 80.0, False)

    def test_no_plan_means_nothing_to_time(self):
        executor = Executor(ZONE)
        self.assertEqual([], executor.on_timer(50.0, False).actions)

    def test_still_moving_rearms(self):
        outcome = self.executor.on_timer(40.0, True)
        self.assertEqual(STATUS_RUNNING, outcome.status)
        self.assertEqual([ACTION_ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertTrue(self.executor.has_plan)

    def test_settled_at_the_target_completes(self):
        # Covers an actuator that reported no intermediate states at all.
        outcome = self.executor.on_timer(0.0, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_PUBLISH_POSITION], _kinds(outcome))

    def test_settled_within_the_deviation_tolerance_is_accepted(self):
        logged = []

        def log(message, level="INFO"):
            logged.append((level, message))

        executor = Executor(ZONE, log=log)
        executor.start(_move_plan(50.0), 80.0, False)
        outcome = executor.on_timer(50.0 + DEVIATION_TOLERANCE_PCT, False)
        self.assertEqual(STATUS_COMPLETED, outcome.status)
        self.assertIn(("WARNING", "accepting 52.0 for target 50.0: settled within 2.0% of the "
                                  "setpoint"), logged)

    def test_settled_short_stalls(self):
        outcome = self.executor.on_timer(50.0, False)
        self.assertEqual(STATUS_STALLED, outcome.status)
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER, ACTION_NOTIFY], _kinds(outcome))
        self.assertEqual(NOTIFY_STALL, _of(outcome, ACTION_NOTIFY)[0].notify_kind)
        self.assertIn("0%", _of(outcome, ACTION_NOTIFY)[0].message)
        self.assertFalse(self.executor.has_plan)

    def test_unreadable_position_stalls(self):
        outcome = self.executor.on_timer(None, False)
        self.assertEqual(STATUS_STALLED, outcome.status)
        self.assertIn("unreadable", _of(outcome, ACTION_NOTIFY)[0].message)
        self.assertFalse(self.executor.has_plan)

    def test_a_deviation_accepted_mid_plan_continues(self):
        executor = Executor(ZONE)
        executor.start(_enter_plan(), 80.0, False)
        outcome = executor.on_timer(99.0, False)  # a percent short of fully open
        self.assertEqual(STATUS_RUNNING, outcome.status)
        self.assertEqual([ACTION_MOVE_TO, ACTION_ARM_SETTLE_TIMER], _kinds(outcome))

    def test_deviation_tolerance_does_not_apply_to_rise_steps(self):
        executor = Executor(ZONE)
        executor.start(Plan("leave", (Step(STEP_RISE_TO_AT_LEAST, 46.0),), LATCH_UNLATCHED), 41.0,
                       False)
        self.assertEqual(STATUS_STALLED, executor.on_timer(45.0, False).status)


class TestAbandon(unittest.TestCase):

    def test_abandon_cancels_the_timer_and_reports_the_plan(self):
        executor = Executor(ZONE)
        movement = _enter_plan()
        executor.start(movement, 80.0, False)
        outcome = executor.abandon()
        self.assertEqual(STATUS_ABANDONED, outcome.status)
        self.assertIs(movement, outcome.plan)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER], _kinds(outcome))
        self.assertFalse(executor.has_plan)

    def test_abandoning_nothing_does_nothing(self):
        executor = Executor(ZONE)
        outcome = executor.abandon()
        self.assertEqual(STATUS_IDLE, outcome.status)
        self.assertEqual([], outcome.actions)


if __name__ == "__main__":
    unittest.main()
