import unittest

from gradhermetic_cover_control.actions import ActionKind, NotifyKind
from gradhermetic_cover_control.actuator import Actuator, CommandKind
from gradhermetic_cover_control.executor import (
    DEVIATION_TOLERANCE_PCT,
    Executor,
    Outcome,
    Status,
    virtual_position,
)
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.planner import Latch, Plan, PlanKind, Step, StepKind

UPPER = 44
LOWER = 38
RELEASE = 46
ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=2,
            tilt_zone_release_pct=RELEASE, tilt_zone_enter_pct=UPPER, height_step_pct=2)


def _kinds(outcome):
    return [action.kind for action in outcome.actions]


def _of(outcome, kind):
    return [action for action in outcome.actions if action.kind == kind]


def _enter_plan():
    return Plan(PlanKind.ENTER, (
        Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
        Step(StepKind.MOVE_TO, 36),
        Step(StepKind.MOVE_TO, UPPER),
    ), Latch.LATCHED)


def _move_plan(target, command=CommandKind.POSITION):
    return Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, target, command), ), Latch.UNLATCHED)


class _Rig:
    """
    An executor on a real actuator, fed the way the logic feeds it.
    """

    def __init__(self, position, is_moving=False):
        self.actuator = Actuator()
        self.actuator.seed(position, is_moving)
        self.executor = Executor(ZONE, self.actuator)

    def start(self, movement):
        return self.executor.start(movement)

    def report(self, position, is_moving=False):
        return self._feed(self.actuator.on_report(position, is_moving))

    def timer(self, position, is_moving=False):
        return self._feed(self.actuator.on_timer(position, is_moving))

    def _feed(self, feedback):
        outcome = self.executor.on_verdict(feedback.verdict)
        return Outcome(feedback.actions + outcome.actions, outcome.status, outcome.plan)


class TestActivation(unittest.TestCase):

    def test_first_command_is_issued_and_the_timer_armed(self):
        outcome = _Rig(80).start(_enter_plan())
        self.assertEqual(Status.RUNNING, outcome.status)
        self.assertEqual([ActionKind.OPEN_FULL, ActionKind.ARM_SETTLE_TIMER], _kinds(outcome))

    def test_satisfied_step_is_skipped(self):
        # Already fully open: the enter sequence starts at the dip, with no no-op waypoint that the
        # actuator would never acknowledge.
        outcome = _Rig(100).start(_enter_plan())
        self.assertEqual([ActionKind.MOVE_TO, ActionKind.ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertEqual(36, _of(outcome, ActionKind.MOVE_TO)[0].position)

    def test_satisfied_step_is_not_skipped_while_still_travelling(self):
        # Passing through 100 is not resting at 100, so the command still goes out.
        outcome = _Rig(100, is_moving=True).start(_enter_plan())
        self.assertEqual([ActionKind.OPEN_FULL, ActionKind.ARM_SETTLE_TIMER], _kinds(outcome))

    def test_a_wholly_satisfied_plan_completes_at_once(self):
        rig = _Rig(50)
        outcome = rig.start(_move_plan(50))
        self.assertEqual(Status.COMPLETED, outcome.status)
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER], _kinds(outcome))
        self.assertFalse(rig.executor.has_plan)

    def test_the_current_step_is_exposed_while_a_plan_runs(self):
        rig = _Rig(80)
        self.assertIsNone(rig.executor.current_step)
        rig.start(_enter_plan())
        self.assertEqual(CommandKind.OPEN, rig.executor.current_step.command)
        rig.report(100)
        self.assertEqual(36, rig.executor.current_step.target)

    def test_satisfaction_is_exact(self):
        # The actuator reports the whole percent it reached, so arrival is plain equality.
        self.assertEqual(Status.COMPLETED, _Rig(43).start(_move_plan(43)).status)
        self.assertEqual(Status.RUNNING, _Rig(42).start(_move_plan(43)).status)


class TestArrival(unittest.TestCase):

    def setUp(self):
        self.rig = _Rig(80)
        self.rig.start(_enter_plan())  # commanded the full open

    def test_motion_feedback_does_not_advance(self):
        self.assertEqual(Status.IDLE, self.rig.report(100, True).status)

    def test_arrival_issues_the_next_command_and_rearms(self):
        outcome = self.rig.report(100)
        self.assertEqual(Status.RUNNING, outcome.status)
        self.assertEqual([ActionKind.MOVE_TO, ActionKind.ARM_SETTLE_TIMER], _kinds(outcome))
        self.assertEqual(36, _of(outcome, ActionKind.MOVE_TO)[0].position)

    def test_feedback_short_of_the_target_does_nothing(self):
        self.assertEqual(Status.IDLE, self.rig.report(90).status)

    def test_stale_feedback_at_the_send_time_position_cannot_advance(self):
        for _ in range(3):
            self.assertEqual(Status.IDLE, self.rig.report(80).status)
        self.assertTrue(self.rig.executor.has_plan)

    def test_duplicate_arrival_after_the_step_completed_is_ignored(self):
        self.rig.report(100)  # completes step 1, commands the dip to 36
        self.assertEqual(Status.IDLE, self.rig.report(100).status)

    def test_completion_cancels_the_timer_and_leaves_publishing_to_the_caller(self):
        self.rig.report(100)
        self.rig.report(36)
        outcome = self.rig.report(UPPER)
        self.assertEqual(Status.COMPLETED, outcome.status)
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER], _kinds(outcome))
        self.assertFalse(self.rig.executor.has_plan)
        self.assertIsNone(self.rig.actuator.move)

    def test_no_verdict_without_a_plan(self):
        self.assertEqual(Status.IDLE, Executor(ZONE, Actuator()).on_verdict(None).status)


class TestRiseToAtLeast(unittest.TestCase):

    def setUp(self):
        self.plan = Plan(PlanKind.LEAVE, (Step(StepKind.RISE_TO_AT_LEAST, 46), ), Latch.UNLATCHED)

    def test_resting_past_the_target_satisfies_the_step(self):
        rig = _Rig(UPPER)
        rig.start(self.plan)
        self.assertEqual(Status.COMPLETED, rig.report(47).status)

    def test_short_of_the_target_does_not(self):
        rig = _Rig(UPPER)
        rig.start(self.plan)
        self.assertEqual(Status.IDLE, rig.report(45).status)

    def test_already_above_is_skipped(self):
        self.assertEqual(Status.COMPLETED, _Rig(50).start(self.plan).status)


class TestCommandDistinctFromTarget(unittest.TestCase):
    """A step may aim past what satisfies it; the command follows the aim, arrival the target."""

    def setUp(self):
        # The shape the tilt exit uses: satisfied at 46, commanded to 48.
        self.plan = Plan(
            PlanKind.LEAVE,
            (Step(StepKind.RISE_TO_AT_LEAST, 46, CommandKind.POSITION, command_pct=48), ),
            Latch.UNLATCHED)
        self.rig = _Rig(UPPER)

    def test_the_command_carries_the_command_position(self):
        outcome = self.rig.start(self.plan)
        self.assertEqual(48, _of(outcome, ActionKind.MOVE_TO)[0].position)

    def test_arrival_is_judged_by_the_target_not_the_command(self):
        self.rig.start(self.plan)
        self.assertEqual(Status.COMPLETED, self.rig.report(46).status)

    def test_activation_skips_on_the_target_not_the_command(self):
        outcome = _Rig(46).start(self.plan)
        self.assertEqual(Status.COMPLETED, outcome.status)
        self.assertEqual([], _of(outcome, ActionKind.MOVE_TO))

    def test_the_settle_timer_accepts_on_the_target(self):
        self.rig.start(self.plan)
        self.assertEqual(Status.COMPLETED, self.rig.timer(47).status)


class TestStall(unittest.TestCase):

    def test_a_stall_names_the_target_the_step_needed(self):
        rig = _Rig(UPPER)
        rig.start(
            Plan(PlanKind.LEAVE,
                 (Step(StepKind.RISE_TO_AT_LEAST, 46, CommandKind.POSITION, command_pct=48), ),
                 Latch.UNLATCHED))
        outcome = rig.timer(45)
        self.assertEqual(Status.STALLED, outcome.status)
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER, ActionKind.NOTIFY], _kinds(outcome))
        notify = _of(outcome, ActionKind.NOTIFY)[0]
        self.assertEqual(NotifyKind.STALL, notify.notify_kind)
        self.assertIn("did not reach 46%", notify.message)
        self.assertIn("45", notify.message)
        self.assertFalse(rig.executor.has_plan)
        self.assertIsNone(rig.actuator.move)

    def test_an_unreadable_position_stops_then_stalls(self):
        rig = _Rig(80)
        rig.start(_move_plan(0, CommandKind.CLOSE))
        outcome = rig.timer(None)
        self.assertEqual(Status.STALLED, outcome.status)
        self.assertEqual([ActionKind.STOP, ActionKind.CANCEL_SETTLE_TIMER, ActionKind.NOTIFY],
                         _kinds(outcome))
        self.assertIn("unreadable", _of(outcome, ActionKind.NOTIFY)[0].message)


class TestDeviationTolerance(unittest.TestCase):
    """Which steps may settle a percent or so off target: only height moves clear of the band."""

    def test_an_ordinary_height_move_tolerates_it(self):
        rig = _Rig(80)
        rig.start(_move_plan(50))
        self.assertEqual(Status.COMPLETED, rig.timer(50 + DEVIATION_TOLERANCE_PCT).status)

    def test_a_deviation_accepted_mid_plan_continues(self):
        rig = _Rig(80)
        rig.start(_enter_plan())
        outcome = rig.timer(99)  # a percent short of fully open
        self.assertEqual(Status.RUNNING, outcome.status)
        self.assertEqual([ActionKind.MOVE_TO, ActionKind.ARM_SETTLE_TIMER], _kinds(outcome))

    def test_not_for_a_target_in_the_band(self):
        # A percent short of the release height leaves the mechanism latched; a percent short of
        # the enter dip never clears the lower edge. Those must land or stall.
        rig = _Rig(20)
        rig.start(_move_plan(RELEASE))
        self.assertEqual(Status.STALLED, rig.timer(RELEASE - 1).status)
        rig = _Rig(100)
        rig.start(_enter_plan())  # commanded the dip to 36
        self.assertEqual(Status.STALLED, rig.timer(38).status)

    def test_not_for_slat_steps(self):
        # A slat step is smaller than the tolerance: accepting the pre-step position would silently
        # complete a move that never happened.
        rig = _Rig(UPPER)
        rig.start(Plan(PlanKind.SLAT, (Step(StepKind.MOVE_TO, 43), ), Latch.LATCHED))
        self.assertEqual(Status.STALLED, rig.timer(UPPER).status)

    def test_not_for_rise_steps(self):
        rig = _Rig(41)
        rig.start(Plan(PlanKind.LEAVE, (Step(StepKind.RISE_TO_AT_LEAST, 46), ), Latch.UNLATCHED))
        self.assertEqual(Status.STALLED, rig.timer(45).status)


class TestVirtualPosition(unittest.TestCase):
    """The mapping the logic publishes with: a height outside tilt, a slat angle inside it."""

    def test_unlatched_and_unknown_publish_the_real_position(self):
        self.assertEqual(30, virtual_position(ZONE, Latch.UNLATCHED, 30))
        self.assertEqual(41, virtual_position(ZONE, Latch.UNKNOWN, 41))

    def test_latched_publishes_the_inverted_zone_mapping(self):
        self.assertEqual(67, virtual_position(ZONE, Latch.LATCHED, 40))
        self.assertEqual(ZONE.real_to_virtual(43), virtual_position(ZONE, Latch.LATCHED, 43))

    def test_the_executor_never_publishes(self):
        # Publishing needs the belief, which only the logic holds; the executor reports outcomes.
        rig = _Rig(80)
        outcomes = [rig.start(_enter_plan()), rig.report(100), rig.report(36), rig.report(UPPER)]
        self.assertEqual([], [a for o in outcomes for a in _of(o, ActionKind.PUBLISH_STATE)])


class TestAbandon(unittest.TestCase):

    def test_abandon_cancels_the_timer_and_reports_the_plan(self):
        rig = _Rig(80)
        movement = _enter_plan()
        rig.start(movement)
        outcome = rig.executor.abandon()
        self.assertEqual(Status.ABANDONED, outcome.status)
        self.assertIs(movement, outcome.plan)
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER], _kinds(outcome))
        self.assertFalse(rig.executor.has_plan)
        self.assertIsNone(rig.actuator.move)

    def test_abandoning_nothing_does_nothing(self):
        outcome = Executor(ZONE, Actuator()).abandon()
        self.assertEqual(Status.IDLE, outcome.status)
        self.assertEqual([], outcome.actions)


if __name__ == "__main__":
    unittest.main()
