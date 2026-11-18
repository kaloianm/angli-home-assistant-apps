import unittest

from gradhermetic_cover_control.actions import ActionKind
from gradhermetic_cover_control.actuator import (
    SETTLED_RECHECK_SECONDS,
    SETTLE_TIMEOUT_SECONDS,
    Actuator,
    Arrived,
    CommandKind,
    Direction,
    Move,
    Stalled,
)


def _kinds(actions):
    return [action.kind for action in actions]


def _to(position, command=CommandKind.POSITION, **kwargs):
    return Move(command, position, position, **kwargs)


def _started(position, move, is_moving=False):
    actuator = Actuator()
    actuator.seed(position, is_moving)
    actuator.start(move)
    return actuator


class TestCommands(unittest.TestCase):

    def test_a_move_is_commanded_and_the_timer_armed(self):
        actions = Actuator().start(_to(30))
        self.assertEqual([ActionKind.MOVE_TO, ActionKind.ARM_SETTLE_TIMER], _kinds(actions))
        self.assertEqual(30, actions[0].position)
        self.assertEqual(SETTLE_TIMEOUT_SECONDS, actions[1].seconds)

    def test_open_and_close_use_the_limit_switch_commands(self):
        self.assertEqual(ActionKind.OPEN_FULL, Actuator().start(_to(100, CommandKind.OPEN))[0].kind)
        self.assertEqual(ActionKind.CLOSE_FULL, Actuator().start(_to(0, CommandKind.CLOSE))[0].kind)

    def test_the_command_carries_the_position_not_the_target(self):
        actions = Actuator().start(Move(CommandKind.POSITION, 48, 46, at_least=True))
        self.assertEqual(48, actions[0].position)

    def test_release_forgets_the_move_and_cancels_the_timer(self):
        actuator = _started(80, _to(30))
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER], _kinds(actuator.release()))
        self.assertIsNone(actuator.move)


class TestArrival(unittest.TestCase):

    def setUp(self):
        self.actuator = _started(80, _to(30))

    def test_motion_does_not_arrive(self):
        self.assertIsNone(self.actuator.on_report(30, True).verdict)

    def test_a_settled_report_on_target_arrives(self):
        self.assertEqual(Arrived(30), self.actuator.on_report(30, False).verdict)

    def test_stale_feedback_at_the_start_position_cannot_arrive(self):
        # The move was only started because 80 did not satisfy it, so a duplicate report of 80 can
        # never complete it -- however small the move.
        for _ in range(3):
            feedback = self.actuator.on_report(80, False)
            self.assertIsNone(feedback.verdict)
            self.assertEqual([], feedback.actions)

    def test_an_unreadable_position_does_not_arrive(self):
        self.assertIsNone(self.actuator.on_report(None, False).verdict)
        self.assertIsNotNone(self.actuator.move)

    def test_without_a_move_reports_only_update_the_state(self):
        actuator = Actuator()
        feedback = actuator.on_report(50, False)
        self.assertEqual(([], None), (feedback.actions, feedback.verdict))
        self.assertEqual(50, actuator.position)

    def test_at_least_arrives_anywhere_past_its_target(self):
        actuator = _started(44, Move(CommandKind.OPEN, 100, 46, at_least=True))
        self.assertIsNone(actuator.on_report(45, False).verdict)
        self.assertEqual(Arrived(47), actuator.on_report(47, False).verdict)

    def test_exact_arrival_judges_the_target_not_the_command(self):
        actuator = _started(80, Move(CommandKind.POSITION, 28, 30))
        self.assertIsNone(actuator.on_report(28, False).verdict)
        self.assertEqual(Arrived(30), actuator.on_report(30, False).verdict)


class TestSettledRecheck(unittest.TestCase):
    """A cover seen moving and then settled short is judged after a short recheck, not 45 s."""

    def setUp(self):
        self.actuator = _started(80, _to(30))

    def test_settling_short_after_motion_arms_the_recheck(self):
        self.actuator.on_report(60, True)
        feedback = self.actuator.on_report(41, False)
        self.assertIsNone(feedback.verdict)
        self.assertEqual([ActionKind.ARM_SETTLE_TIMER], _kinds(feedback.actions))
        self.assertEqual(SETTLED_RECHECK_SECONDS, feedback.actions[0].seconds)

    def test_the_recheck_is_armed_once_per_stop(self):
        self.actuator.on_report(60, True)
        self.actuator.on_report(41, False)
        self.assertEqual([], self.actuator.on_report(41, False).actions)

    def test_a_settled_report_before_any_motion_is_not_a_stop(self):
        # The cover has not started yet; the inactivity timeout keeps waiting for it.
        self.assertEqual([], self.actuator.on_report(80, False).actions)

    def test_the_recheck_then_judges_the_stop(self):
        self.actuator.on_report(60, True)
        self.actuator.on_report(41, False)
        self.assertIsInstance(self.actuator.on_timer(41, False).verdict, Stalled)


class TestTimer(unittest.TestCase):

    def setUp(self):
        self.logged = []
        self.actuator = Actuator(
            log=lambda message, level="INFO": self.logged.append((level, message)))
        self.actuator.seed(80)
        self.actuator.start(_to(0, CommandKind.CLOSE))

    def test_no_move_means_nothing_to_time(self):
        feedback = Actuator().on_timer(50, False)
        self.assertEqual(([], None), (feedback.actions, feedback.verdict))

    def test_still_moving_rearms(self):
        feedback = self.actuator.on_timer(40, True)
        self.assertIsNone(feedback.verdict)
        self.assertEqual([ActionKind.ARM_SETTLE_TIMER], _kinds(feedback.actions))
        self.assertEqual(SETTLE_TIMEOUT_SECONDS, feedback.actions[0].seconds)

    def test_settled_on_target_arrives(self):
        # Covers a controller that reported no intermediate states at all.
        self.assertEqual(Arrived(0), self.actuator.on_timer(0, False).verdict)

    def test_settled_short_stalls_without_stopping_a_cover_at_rest(self):
        # There is nothing to stop, and on a KNX actuator with no stop object a stop sent to an
        # idle cover is a step telegram.
        feedback = self.actuator.on_timer(50, False)
        self.assertEqual(Stalled("it settled at 50%"), feedback.verdict)
        self.assertEqual([], feedback.actions)

    def test_an_unreadable_position_stalls_and_stops(self):
        # It may well still be travelling, so this is the one stall that sends a stop.
        feedback = self.actuator.on_timer(None, False)
        self.assertEqual(Stalled("its position is unreadable"), feedback.verdict)
        self.assertEqual([ActionKind.STOP], _kinds(feedback.actions))

    def test_settled_within_the_tolerance_arrives_with_a_warning(self):
        actuator = Actuator(log=lambda message, level="INFO": self.logged.append((level, message)))
        actuator.seed(80)
        actuator.start(_to(50, tolerance_pct=2))
        self.assertEqual(Arrived(52), actuator.on_timer(52, False).verdict)
        self.assertIn(("WARNING", "accepting 52 for target 50: settled within 2% of the "
                       "setpoint"), self.logged)

    def test_beyond_the_tolerance_stalls(self):
        actuator = _started(80, _to(50, tolerance_pct=2))
        self.assertIsInstance(actuator.on_timer(53, False).verdict, Stalled)

    def test_the_tolerance_applies_only_once_stopped(self):
        # A settled report within tolerance waits for the timer, in case the cover creeps on.
        actuator = _started(80, _to(50, tolerance_pct=2))
        self.assertIsNone(actuator.on_report(52, False).verdict)


class TestMayBeTravelling(unittest.TestCase):
    """
    Whether a stop has anything to stop. A move in progress alone is not enough: the settled-short
    recheck keeps one open for seconds after the cover has reported coming to rest.
    """

    def test_idle_with_no_move(self):
        actuator = Actuator()
        actuator.seed(50)
        self.assertFalse(actuator.may_be_travelling)

    def test_true_while_reporting_motion_even_without_a_move(self):
        actuator = Actuator()
        actuator.on_report(50, True)
        self.assertTrue(actuator.may_be_travelling)

    def test_true_from_the_moment_a_command_goes_out(self):
        self.assertTrue(_started(80, _to(30)).may_be_travelling)

    def test_a_settled_report_short_of_the_target_answers_the_command(self):
        actuator = _started(80, _to(30))
        actuator.on_report(60, True)
        actuator.on_report(41, False)
        self.assertIsNotNone(actuator.move)
        self.assertFalse(actuator.may_be_travelling)

    def test_the_next_command_makes_it_true_again(self):
        actuator = _started(80, _to(30))
        actuator.on_report(60, True)
        actuator.on_report(41, False)
        actuator.start(_to(20))
        self.assertTrue(actuator.may_be_travelling)

    def test_a_controller_that_never_reports_motion_counts_while_a_move_is_out(self):
        # Its settled-looking reports prove nothing about whether it is still travelling.
        actuator = _started(80, _to(30))
        actuator.on_report(70, False)
        actuator.on_report(60, False)
        self.assertTrue(actuator.may_be_travelling)

    def test_releasing_clears_it(self):
        actuator = _started(80, _to(30))
        actuator.release()
        self.assertFalse(actuator.may_be_travelling)


class TestStop(unittest.TestCase):

    def test_a_cover_that_may_be_travelling_is_stopped(self):
        actuator = _started(80, _to(30))
        self.assertEqual([ActionKind.STOP, ActionKind.CANCEL_SETTLE_TIMER], _kinds(actuator.stop()))
        self.assertIsNone(actuator.move)

    def test_an_idle_cover_gets_no_stop(self):
        actuator = Actuator()
        actuator.seed(50)
        self.assertEqual([ActionKind.CANCEL_SETTLE_TIMER], _kinds(actuator.stop()))


class TestDirectionAndHeading(unittest.TestCase):

    def test_the_reported_direction_wins(self):
        actuator = Actuator()
        actuator.on_report(50, True, Direction.UP)
        self.assertEqual(Direction.UP, actuator.direction)
        self.assertEqual(Direction.UP, actuator.heading)

    def test_the_direction_is_read_off_the_trend_when_the_controller_gives_none(self):
        actuator = Actuator()
        actuator.seed(20)
        actuator.on_report(20, True)
        actuator.on_report(18, True)
        self.assertEqual(Direction.DOWN, actuator.direction)

    def test_a_settled_report_clears_the_direction(self):
        actuator = Actuator()
        actuator.on_report(50, True, Direction.UP)
        actuator.on_report(55, False)
        self.assertIsNone(actuator.direction)

    def test_heading_points_at_the_commanded_position_while_a_command_is_out(self):
        self.assertEqual(Direction.DOWN, _started(80, _to(30)).heading)
        self.assertEqual(Direction.UP, _started(20, _to(100, CommandKind.OPEN)).heading)

    def test_no_heading_once_settled_short(self):
        actuator = _started(80, _to(30))
        actuator.on_report(60, True)
        actuator.on_report(41, False)
        self.assertIsNone(actuator.heading)

    def test_an_unreadable_position_clears_motion(self):
        actuator = Actuator()
        actuator.on_report(50, True, Direction.UP)
        actuator.on_report(None, False)
        self.assertEqual((None, False, None),
                         (actuator.position, actuator.is_moving, actuator.direction))


if __name__ == "__main__":
    unittest.main()
