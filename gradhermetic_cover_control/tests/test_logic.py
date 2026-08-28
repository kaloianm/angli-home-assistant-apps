import unittest
from unittest import mock

from gradhermetic_cover_control.executor import (
    ACTION_ARM_SETTLE_TIMER,
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_NOTIFY,
    ACTION_OPEN_FULL,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    NOTIFY_INVARIANT,
)
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.logic import GradhermeticCoverLogic
from gradhermetic_cover_control.planner import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    LATCH_LATCHED,
    LATCH_UNKNOWN,
    LATCH_UNLATCHED,
)

# Geometry used throughout: zone [38, 44], epsilon 2, step 20. Span = 6, so the minimum step that
# moves the integer-reporting actuator is 100/6 ~= 16.7; 20 clears it. Band = [36, 46].
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 20.0
DIP = LOWER - EPSILON
RELEASE = UPPER + EPSILON

_MOVE_KINDS = (ACTION_MOVE_TO, ACTION_OPEN_FULL, ACTION_CLOSE_FULL)


def _config():
    return Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP)


def _kinds(actions):
    return [action.kind for action in actions]


def _moves(actions):
    return [action for action in actions if action.kind in _MOVE_KINDS]


def _target_of(action):
    if action.kind == ACTION_MOVE_TO:
        return action.position
    if action.kind == ACTION_OPEN_FULL:
        return 100.0
    return 0.0


def _published(actions):
    return [a for a in actions if a.kind == ACTION_PUBLISH_POSITION]


def run_plan(logic, actions):
    """
    Drive a plan to completion by reporting the blind reaching each commanded position.
    """
    collected = list(actions)
    produced = actions
    for _ in range(20):
        if not logic.has_pending_plan:
            return collected
        moves = _moves(produced)
        if not moves:
            raise AssertionError("a plan is pending but the last actions commanded nothing")
        target = _target_of(moves[-1])
        logic.on_real_position(target, True)
        produced = logic.on_real_position(target, False)
        collected.extend(produced)
    raise AssertionError("plan did not terminate")


class TestOutsideTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_open_opens_fully(self):
        self.logic.seed_state(50.0)
        actions = run_plan(self.logic, self.logic.on_open())
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(100.0, _published(actions)[-1].position)

    def test_close_closes_fully(self):
        self.logic.seed_state(50.0)
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual(ACTION_CLOSE_FULL, _moves(actions)[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(0.0, _published(actions)[-1].position)

    def test_set_position_maps_one_to_one(self):
        self.logic.seed_state(80.0)
        actions = run_plan(self.logic, self.logic.on_set_position(30.0))
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(30.0, _moves(actions)[0].position)
        self.assertAlmostEqual(30.0, self.logic.last_position)
        self.assertAlmostEqual(30.0, _published(actions)[-1].position)

    def test_set_position_inside_the_band_snaps_clear_of_it(self):
        # Q2: normal mode never targets the band interior, where a rise would silently latch.
        self.logic.seed_state(80.0)
        actions = run_plan(self.logic, self.logic.on_set_position(41.0))
        self.assertAlmostEqual(RELEASE, _moves(actions)[0].position)
        self.assertAlmostEqual(RELEASE, _published(actions)[-1].position)

    def test_set_position_to_the_current_position_moves_nothing(self):
        self.logic.seed_state(50.0)
        actions = self.logic.on_set_position(50.0)
        self.assertEqual([], _moves(actions))
        self.assertAlmostEqual(50.0, _published(actions)[-1].position)


class TestEnterLeaveTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_enter_from_fully_open_skips_straight_to_the_dip(self):
        self.logic.seed_state(100.0)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(DIP, _moves(actions)[0].position)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(UPPER, self.logic.last_position)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_enter_from_below_opens_fully_first(self):
        # Q1: the latch percentages are only reliable when referenced from the top limit, so entry
        # always drives fully open before dipping -- there is no hop from below any more.
        self.logic.seed_state(20.0)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual([ACTION_OPEN_FULL, ACTION_MOVE_TO, ACTION_MOVE_TO],
                         _kinds(_moves(actions)))
        self.assertAlmostEqual(DIP, _moves(actions)[1].position)
        self.assertAlmostEqual(UPPER, _moves(actions)[2].position)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_enter_from_inside_the_band_opens_fully_first(self):
        self.logic.seed_state(41.0)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual([ACTION_OPEN_FULL, ACTION_MOVE_TO, ACTION_MOVE_TO],
                         _kinds(_moves(actions)))
        self.assertTrue(self.logic.in_tilt)

    def test_enter_when_position_unknown_opens_fully_first(self):
        self.logic.seed_state(None)
        actions = self.logic.on_set_tilt_mode(True)
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_leave_moves_above_zone(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(RELEASE, _moves(actions)[0].position)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_leave_accepts_an_overshoot(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = self.logic.on_set_tilt_mode(False)
        self.logic.on_real_position(47.0, True)
        actions.extend(self.logic.on_real_position(47.0, False))
        self.assertFalse(self.logic.has_pending_plan)
        self.assertAlmostEqual(47.0, _published(actions)[-1].position)

    def test_enter_is_idempotent(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual([], self.logic.on_set_tilt_mode(True))

    def test_leave_without_a_latch_belief_is_a_noop(self):
        self.logic.seed_state(41.0)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertEqual([], self.logic.on_set_tilt_mode(False))


class TestInsideTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched, virtual 0 / real UPPER.

    def test_open_orients_slats_to_lower_edge(self):
        actions = run_plan(self.logic, self.logic.on_open())
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(LOWER, self.logic.last_position)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_close_orients_slats_to_upper_edge(self):
        run_plan(self.logic, self.logic.on_open())  # move off the closed edge first
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(UPPER, self.logic.last_position)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)

    def test_set_position_interpolates_between_edges(self):
        actions = self.logic.on_set_position(50.0)
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(41.0, _moves(actions)[0].position)  # 44 - 0.5 * 6

    def test_step_up_moves_toward_open(self):
        actions = self.logic.on_knx_short(DIRECTION_UP)
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(UPPER - (STEP / 100.0) * (UPPER - LOWER),
                               _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_down_at_closed_edge_is_noop(self):
        # Already at virtual 0 (closed edge).
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_step_up_at_open_edge_leaves_tilt(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER.
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_UP))
        self.assertAlmostEqual(RELEASE, _target_of(_moves(actions)[0]))
        self.assertFalse(self.logic.in_tilt)


class TestSlatStepHelper(unittest.TestCase):
    """The dedicated ``..._step_up`` / ``..._step_down`` helpers: slats only, never cross zones."""

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched, virtual 0 / real UPPER.

    def test_step_up_moves_toward_open(self):
        actions = self.logic.on_slat_step(DIRECTION_UP)
        self.assertAlmostEqual(UPPER - (STEP / 100.0) * (UPPER - LOWER),
                               _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_down_moves_toward_closed(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER (fully open).
        actions = self.logic.on_slat_step(DIRECTION_DOWN)
        self.assertAlmostEqual(UPPER - ((100.0 - STEP) / 100.0) * (UPPER - LOWER),
                               _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_up_at_open_edge_clamps_and_stays_in_tilt(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER (fully open).
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_UP))
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(LOWER, self.logic.last_position)

    def test_step_down_at_closed_edge_is_noop(self):
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_DOWN))
        self.assertTrue(self.logic.in_tilt)

    def test_step_ignored_outside_tilt(self):
        self.logic.seed_state(80.0)
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_UP))
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_DOWN))

    def test_step_ignored_while_moving(self):
        self.logic.on_real_position(42.0, True)  # blind reports it is travelling.
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_UP))

    def test_step_ignored_while_plan_pending(self):
        self.logic.on_set_position(50.0)  # starts a plan; no feedback yet.
        self.assertTrue(self.logic.has_pending_plan)
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_UP))


class TestKnxLongPress(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_long_up_opens_and_leaves_tilt(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_UP))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_long_down_from_tilt_releases_by_opening_then_descends(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        self.assertEqual([ACTION_OPEN_FULL, ACTION_CLOSE_FULL], _kinds(_moves(actions)))
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)

    def test_long_down_outside_closes_fully(self):
        self.logic.seed_state(80.0)
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        self.assertEqual(ACTION_CLOSE_FULL, _moves(actions)[0].kind)
        self.assertFalse(self.logic.in_tilt)


class TestKnxShortPress(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_short_press_stops_a_moving_blind(self):
        self.logic.seed_state(50.0)
        self.logic.on_open()  # starts a plan
        self.logic.on_real_position(70.0, True)  # now moving
        actions = self.logic.on_knx_short(DIRECTION_UP)
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER], _kinds(actions))
        self.assertFalse(self.logic.has_pending_plan)

    def test_short_down_from_above_enters_closed(self):
        self.logic.seed_state(80.0)
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_DOWN))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_short_up_from_below_enters_open(self):
        self.logic.seed_state(10.0)
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_UP))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, self.logic.current_virtual_position())

    def test_short_up_from_above_does_nothing(self):
        self.logic.seed_state(80.0)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_UP))

    def test_short_down_from_below_does_nothing(self):
        self.logic.seed_state(10.0)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_short_press_ignored_when_position_unknown(self):
        self.logic.seed_state(None)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_short_press_while_idle_inside_the_zone_does_nothing(self):
        # Q3: neither direction points toward a zone the blind already sits in, and it is not
        # believed latched, so there are no slats to step. The long press is the escape hatch.
        self.logic.seed_state(41.0)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_UP))
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))


class TestRecoveryAndMisc(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_recover_opens_fully_and_clears_tilt(self):
        self.logic.seed_state(None)
        actions = run_plan(self.logic, self.logic.on_recover())
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_startup_inside_the_band_recovers_upward(self):
        actions = self.logic.on_startup(41.0)
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)

    def test_startup_with_an_unknown_position_recovers_upward(self):
        actions = self.logic.on_startup(None)
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)

    def test_startup_outside_the_band_resumes_and_publishes(self):
        actions = self.logic.on_startup(80.0)
        self.assertEqual([], _moves(actions))
        self.assertAlmostEqual(80.0, _published(actions)[-1].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_manual_stop_then_rest_publishes(self):
        self.logic.seed_state(60.0)
        self.logic.on_real_position(60.0, True)  # moving (e.g. manual drive)
        actions = self.logic.on_real_position(55.0, False)  # came to rest, no plan
        self.assertEqual([ACTION_PUBLISH_POSITION], _kinds(actions))
        self.assertAlmostEqual(55.0, actions[0].position)

    def test_disabled_logic_ignores_events(self):
        self.logic.seed_state(50.0)
        self.logic.disable()
        self.assertEqual([], self.logic.on_open())
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))
        self.assertEqual([], self.logic.on_real_position(50.0, False))
        self.assertEqual([], self.logic.on_settle_timer(50.0, False))

    def test_stop_clears_pending_plan(self):
        self.logic.seed_state(50.0)
        self.logic.on_open()
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER], _kinds(self.logic.on_stop()))
        # With the plan cleared, position feedback no longer advances anything.
        self.assertEqual([], self.logic.on_real_position(70.0, True))


class TestLatchSafetyGuard(unittest.TestCase):
    """
    Any downward move while the blind might be latched must first release the latch, and the release
    is a full open: an uncertain latch belief also means an uncertain calibration, so a rise to a
    merely reported ``upper + epsilon`` cannot be trusted.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        # An interrupted latch sequence: the blind physically sits inside the tilt band and the app
        # has no idea whether it is latched.
        self.logic.seed_state(41.0)

    def test_close_releases_by_opening_before_descending(self):
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual([ACTION_OPEN_FULL, ACTION_CLOSE_FULL], _kinds(_moves(actions)))
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.last_position)

    def test_long_down_releases_by_opening_before_descending(self):
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        self.assertEqual([ACTION_OPEN_FULL, ACTION_CLOSE_FULL], _kinds(_moves(actions)))
        self.assertFalse(self.logic.in_tilt)

    def test_set_position_downward_releases_first(self):
        actions = run_plan(self.logic, self.logic.on_set_position(10.0))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertAlmostEqual(10.0, self.logic.last_position)

    def test_set_position_upward_does_not_release(self):
        # Rising past the upper edge self-releases the latch, so no explicit release step is needed.
        actions = self.logic.on_set_position(90.0)
        self.assertEqual(ACTION_MOVE_TO, _moves(actions)[0].kind)
        self.assertAlmostEqual(90.0, _moves(actions)[0].position)

    def test_no_release_when_clearly_outside_band(self):
        self.logic.seed_state(80.0)
        actions = self.logic.on_close()
        self.assertEqual(ACTION_CLOSE_FULL, _moves(actions)[0].kind)

    def test_unknown_position_is_treated_as_maybe_latched(self):
        self.logic.seed_state(None)
        actions = self.logic.on_close()
        self.assertEqual([ACTION_OPEN_FULL], _kinds(_moves(actions)))


class TestBeliefTransitions(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def _latch(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))

    def test_only_a_completed_enter_sequence_latches(self):
        self.logic.seed_state(100.0)
        self.logic.on_set_tilt_mode(True)  # already fully open, so the dip is commanded first
        actions = self.logic.on_real_position(DIP, False)
        self.assertFalse(self.logic.in_tilt)  # the latching rise has not finished yet
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_external_move_outside_band_clears_the_latch_belief(self):
        self._latch()
        self.logic.on_real_position(10.0, True)
        self.logic.on_real_position(10.0, False)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_external_move_ending_inside_the_band_makes_the_belief_unknown(self):
        self._latch()
        self.logic.on_real_position(43.0, True)  # something else is driving the blind
        self.logic.on_real_position(42.0, False)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_settled_duplicate_feedback_does_not_change_the_belief(self):
        self._latch()
        self.logic.on_real_position(UPPER, False)  # still inside the band, still at rest
        self.assertEqual(LATCH_LATCHED, self.logic.latch)

    def test_interrupting_a_slat_move_keeps_the_latch_belief(self):
        # Every target of a slat plan lies inside the zone, so it can neither engage nor release the
        # latch: stopping one must not drop the blind out of tilt mode.
        self._latch()
        self.logic.on_slat_step(DIRECTION_UP)
        self.logic.on_real_position(43.0, True)
        self.logic.on_stop()
        self.assertEqual(LATCH_LATCHED, self.logic.latch)

    def test_interrupting_a_latch_sequence_makes_the_belief_unknown(self):
        self.logic.seed_state(80.0)
        self.logic.on_set_tilt_mode(True)
        self.logic.on_real_position(100.0, False)  # the full open completed; the dip is running
        self.logic.on_real_position(DIP, False)  # the dip completed; the latching rise is running
        self.logic.on_real_position(40.0, True)  # mid-rise, which physically latches
        self.logic.on_stop()
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_interrupting_a_plan_outside_the_band_leaves_it_released(self):
        self.logic.seed_state(80.0)
        self.logic.on_close()
        self.logic.on_real_position(60.0, True)
        self.logic.on_stop()
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)


class TestCommandReplacesPlan(unittest.TestCase):
    """Q4: a command arriving mid-plan replaces it, re-planned from the belief after the abort."""

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_replacement_reruns_the_safety_guards(self):
        self.logic.seed_state(80.0)
        self.logic.on_set_tilt_mode(True)  # full open, then the dip, then the latching rise
        self.logic.on_real_position(100.0, False)
        self.logic.on_real_position(DIP, False)
        self.logic.on_real_position(40.0, True)  # mid-rise: physically latched
        # Closing now must not simply descend: the abandoned sequence leaves the latch unknown.
        actions = self.logic.on_close()
        self.assertEqual([ACTION_OPEN_FULL], _kinds(_moves(actions)))

    def test_replacement_drops_the_old_plan(self):
        self.logic.seed_state(80.0)
        self.logic.on_set_tilt_mode(True)
        run_plan(self.logic, self.logic.on_open())
        self.assertFalse(self.logic.has_pending_plan)
        self.assertAlmostEqual(100.0, self.logic.last_position)
        self.assertFalse(self.logic.in_tilt)

    def test_a_noop_intent_leaves_a_running_plan_alone(self):
        self.logic.seed_state(80.0)
        self.logic.on_close()
        self.assertEqual([], self.logic.on_slat_step(DIRECTION_UP))
        self.assertTrue(self.logic.has_pending_plan)


class TestSettleTimer(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(80.0)

    def test_a_long_move_rearms_rather_than_stalling(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(40.0, True)
        self.assertEqual([ACTION_ARM_SETTLE_TIMER], _kinds(actions))
        self.assertTrue(self.logic.has_pending_plan)

    def test_settled_short_stalls_and_notifies(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(50.0, False)
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER, ACTION_NOTIFY], _kinds(actions))
        self.assertFalse(self.logic.has_pending_plan)

    def test_an_unreadable_position_stalls(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(None, False)
        self.assertEqual(ACTION_NOTIFY, _kinds(actions)[-1])
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_a_stray_firing_without_a_plan_does_nothing(self):
        self.assertEqual([], self.logic.on_settle_timer(80.0, False))

    def test_the_timer_can_complete_a_plan_the_actuator_never_reported(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(0.0, False)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_PUBLISH_POSITION], _kinds(actions))
        self.assertFalse(self.logic.has_pending_plan)


class TestInvariantFailure(unittest.TestCase):

    def test_a_failed_safety_check_disables_the_blind_and_notifies(self):
        logic = GradhermeticCoverLogic(_config())
        logic.seed_state(80.0)
        with mock.patch("gradhermetic_cover_control.planner.check_plan", return_value="L1: boom"):
            actions = logic.on_close()
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_NOTIFY], _kinds(actions))
        self.assertEqual(NOTIFY_INVARIANT, actions[-1].notify_kind)
        self.assertIn("L1: boom", actions[-1].message)
        self.assertEqual([], logic.on_open())  # disabled from here on


class TestConfirmedBugRegressions(unittest.TestCase):
    """The four bugs the redesign exists to remove; see REDESIGN_PLAN section 1.1."""

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_close_after_leaving_tilt_starts_immediately(self):
        # B1: resting at the release target (inside the ambiguity band) but known unlatched, the
        # descent is the very first command -- no no-op waypoint that stalls until the fallback
        # timer, and no full-open detour either.
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertAlmostEqual(RELEASE, self.logic.last_position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

        actions = self.logic.on_close()
        self.assertEqual(ACTION_CLOSE_FULL, actions[0].kind)

    def test_slat_step_ignores_stale_feedback(self):
        # B2: a 20% slat step is 1.2 real percent, less than the old 1.5% arrival tolerance, so a
        # duplicate report of the pre-step position used to complete the plan instantly.
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched at real UPPER, virtual 0.
        self.logic.on_slat_step(DIRECTION_UP)

        actions = self.logic.on_real_position(UPPER, False)
        self.assertEqual([], actions)
        self.assertTrue(self.logic.has_pending_plan)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_unavailable_clears_motion_belief(self):
        # B3: the position and motion beliefs used to survive the cover going unavailable, so a
        # short press would "stop" a blind that was not moving and guards reasoned from a stale
        # position.
        self.logic.seed_state(80.0)
        self.logic.on_real_position(70.0, True)
        self.assertTrue(self.logic.is_moving)

        self.logic.on_real_position(None, True)
        self.assertFalse(self.logic.is_moving)
        self.assertIsNone(self.logic.last_position)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertEqual([ACTION_OPEN_FULL], _kinds(_moves(self.logic.on_close())))

    def test_settle_timer_cancelled_on_completion(self):
        # B4: the timer was only cancelled on stop/disable/error, so a stray firing followed every
        # completed plan.
        self.logic.seed_state(80.0)
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual(ACTION_CANCEL_SETTLE_TIMER, _kinds(actions)[-2])
        self.assertEqual(ACTION_PUBLISH_POSITION, _kinds(actions)[-1])
        self.assertEqual(1, _kinds(actions).count(ACTION_CANCEL_SETTLE_TIMER))


if __name__ == "__main__":
    unittest.main()
