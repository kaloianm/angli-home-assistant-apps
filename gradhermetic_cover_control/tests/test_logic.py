import unittest
from unittest import mock

from gradhermetic_cover_control.executor import (
    ACTION_ARM_SETTLE_TIMER,
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_NOTIFY,
    ACTION_OPEN_FULL,
    ACTION_PUBLISH_STATE,
    ACTION_STOP,
    MOTION_CLOSING,
    MOTION_IDLE,
    MOTION_OPENING,
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

# Geometry used throughout: zone [38, 44], epsilon 2, step 1.2 real travel percent. Every
# configured number is real blind travel; span 6 makes the 1.2% step 20 on the virtual slat scale.
# Band = [36, 46].
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 1.2
DIP = LOWER - EPSILON
RELEASE = UPPER + EPSILON

_MOVE_KINDS = (ACTION_MOVE_TO, ACTION_OPEN_FULL, ACTION_CLOSE_FULL)


def _config(**overrides):
    args = {
        "tilt_zone_upper_pct": UPPER,
        "tilt_zone_lower_pct": LOWER,
        "tilt_zone_epsilon_pct": EPSILON,
        "tilt_step_pct": STEP,
    }
    args.update(overrides)
    return Zone(**args)


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
    return [a for a in actions if a.kind == ACTION_PUBLISH_STATE]


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

    def test_leave_drives_the_blind_fully_open(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertAlmostEqual(100.0, self.logic.last_position)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_leave_is_satisfied_by_the_release_height_even_if_the_blind_stops_short(self):
        # The blind was sent to its top limit, but clearing RELEASE is all the step needs: the
        # mechanism has provably let go by then.
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = self.logic.on_set_tilt_mode(False)
        self.logic.on_real_position(RELEASE - 1.0, True)
        self.assertTrue(self.logic.has_pending_plan)
        actions.extend(self.logic.on_real_position(RELEASE, False))
        self.assertFalse(self.logic.has_pending_plan)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)
        self.assertAlmostEqual(RELEASE, _published(actions)[-1].position)

    def test_leave_still_accepts_a_configured_release_height(self):
        logic = GradhermeticCoverLogic(_config(tilt_zone_release_pct=55.0))
        logic.seed_state(100.0)
        run_plan(logic, logic.on_set_tilt_mode(True))
        actions = logic.on_set_tilt_mode(False)
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        # A blind passing the bare clearance has not yet cleared the measured release height.
        logic.on_real_position(46.0, False)
        self.assertTrue(logic.has_pending_plan)
        run_plan(logic, actions)
        self.assertEqual(LATCH_UNLATCHED, logic.latch)

    def test_enter_is_idempotent(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual([], self.logic.on_set_tilt_mode(True))

    def test_the_published_state_says_which_mode_the_position_is_on(self):
        # The flag is what a dashboard reads to show the mode, and it has to agree with the scale
        # the position beside it was measured on.
        self.logic.seed_state(100.0)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertTrue(_published(actions)[-1].in_tilt)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)  # virtual: slats closed

        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertFalse(_published(actions)[-1].in_tilt)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)  # real: fully open

    def test_losing_the_latch_belief_publishes_the_mode_as_off(self):
        # Slat control is offered only from a confident LATCHED belief, so an interrupted sequence
        # that leaves the belief uncertain must not still show the blind as being in slat mode.
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = self.logic.on_real_position(80.0, False)  # external motion clear of the band
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)
        self.assertFalse(_published(actions)[-1].in_tilt)

    def test_leave_without_a_latch_belief_is_a_noop(self):
        self.logic.seed_state(41.0)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertEqual([], self.logic.on_set_tilt_mode(False))


class TestEnterLanding(unittest.TestCase):
    """
    ``tilt_enter_landing_pct``: where a deliberate entry finishes, since the rise ends closed.

    The setting is an absolute real position inside the zone, like every other configured
    percentage; only the *published* position is on the inverted virtual slat scale.
    """

    def test_the_default_landing_is_the_closed_edge(self):
        logic = GradhermeticCoverLogic(_config())
        logic.seed_state(100.0)
        actions = run_plan(logic, logic.on_set_tilt_mode(True))
        self.assertAlmostEqual(UPPER, _moves(actions)[-1].position)
        self.assertAlmostEqual(0.0, logic.current_virtual_position())

    def test_a_configured_landing_adds_a_final_in_zone_move(self):
        # Real 41 is the middle of a [38, 44] zone, i.e. virtual 50.
        logic = GradhermeticCoverLogic(_config(tilt_enter_landing_pct=41.0))
        logic.seed_state(100.0)
        actions = run_plan(logic, logic.on_set_tilt_mode(True))
        # Already fully open, so: dip, latching rise to the closed edge, then the landing.
        self.assertEqual([DIP, UPPER, 41.0], [move.position for move in _moves(actions)])
        self.assertTrue(logic.in_tilt)
        self.assertAlmostEqual(50.0, logic.current_virtual_position())

    def test_a_landing_at_the_lower_edge_lands_the_slats_wide_open(self):
        logic = GradhermeticCoverLogic(_config(tilt_enter_landing_pct=LOWER))
        logic.seed_state(100.0)
        actions = run_plan(logic, logic.on_set_tilt_mode(True))
        self.assertAlmostEqual(LOWER, _moves(actions)[-1].position)
        self.assertAlmostEqual(100.0, logic.current_virtual_position())

    def test_a_landing_that_rounds_to_the_closed_edge_adds_no_step(self):
        # 0.3 real percent below the upper edge: the command would repeat the setpoint.
        logic = GradhermeticCoverLogic(_config(tilt_enter_landing_pct=UPPER - 0.3))
        logic.seed_state(100.0)
        actions = run_plan(logic, logic.on_set_tilt_mode(True))
        self.assertEqual([DIP, UPPER], [move.position for move in _moves(actions)])
        self.assertTrue(logic.in_tilt)

    def test_a_landing_outside_the_zone_is_rejected(self):
        # It is a slat position, so it has to be one.
        for landing in (LOWER - 0.1, UPPER + 0.1, 0.0, 100.0):
            with self.subTest(landing=landing):
                with self.assertRaisesRegex(ValueError, "tilt_enter_landing_pct must be between"):
                    _config(tilt_enter_landing_pct=landing)

    def test_the_toggle_lands_on_the_configured_landing_too(self):
        # The tilt helper and the KNX tilt address go through the toggle; it is the same entry.
        logic = GradhermeticCoverLogic(_config(tilt_enter_landing_pct=41.0))
        logic.seed_state(80.0)
        actions = run_plan(logic, logic.on_toggle_tilt_mode())
        self.assertAlmostEqual(41.0, _moves(actions)[-1].position)
        self.assertAlmostEqual(50.0, logic.current_virtual_position())


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
        self.assertAlmostEqual(UPPER - STEP, _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_down_at_closed_edge_is_noop(self):
        # Already at virtual 0 (closed edge).
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_step_up_at_open_edge_leaves_tilt(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER.
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_UP))
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)
        self.assertAlmostEqual(100.0, self.logic.last_position)
        self.assertFalse(self.logic.in_tilt)


class TestStepHelperWhileLatched(unittest.TestCase):
    """
    The ``..._step_up`` / ``..._step_down`` helpers while latched: slat steps clamped at both
    edges, and a stop whenever anything is moving.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched, virtual 0 / real UPPER.

    def test_step_up_moves_toward_open(self):
        actions = self.logic.on_step(DIRECTION_UP)
        self.assertAlmostEqual(UPPER - STEP, _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_down_moves_toward_closed(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER (fully open).
        actions = self.logic.on_step(DIRECTION_DOWN)
        self.assertAlmostEqual(LOWER + STEP, _moves(actions)[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_up_at_open_edge_clamps_and_stays_in_tilt(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER (fully open).
        self.assertEqual([], self.logic.on_step(DIRECTION_UP))
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(LOWER, self.logic.last_position)

    def test_step_down_at_closed_edge_is_noop(self):
        self.assertEqual([], self.logic.on_step(DIRECTION_DOWN))
        self.assertTrue(self.logic.in_tilt)

    def test_a_step_while_moving_stops_the_blind(self):
        self.logic.on_real_position(42.0, True)  # blind reports it is travelling.
        actions = self.logic.on_step(DIRECTION_UP)
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertEqual([], _moves(actions))

    def test_a_step_while_a_slat_move_is_pending_stops_it_and_keeps_the_latch_belief(self):
        self.logic.on_set_position(50.0)  # starts a slat plan; no feedback yet.
        self.assertTrue(self.logic.has_pending_plan)
        actions = self.logic.on_step(DIRECTION_UP)
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertFalse(self.logic.has_pending_plan)
        self.assertEqual(LATCH_LATCHED, self.logic.latch)


class TestStepHelperWhileNotLatched(unittest.TestCase):
    """
    The same helpers outside tilt step the height by ``height_step_pct``, in either direction,
    from anywhere -- and stop the blind if it is moving. Entering tilt is the tilt helper's job.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_a_step_moves_the_height_by_one_increment(self):
        cases = ((80.0, DIRECTION_DOWN, 78.0), (80.0, DIRECTION_UP, 82.0),
                 (10.0, DIRECTION_UP, 12.0), (10.0, DIRECTION_DOWN, 8.0))
        for start, direction, expected in cases:
            with self.subTest(start=start, direction=direction):
                logic = GradhermeticCoverLogic(_config())
                logic.seed_state(start)
                actions = run_plan(logic, logic.on_step(direction))
                self.assertEqual([ACTION_MOVE_TO], _kinds(_moves(actions)))
                self.assertAlmostEqual(expected, _moves(actions)[0].position)
                self.assertAlmostEqual(expected, _published(actions)[-1].position)
                self.assertFalse(_published(actions)[-1].in_tilt)
                self.assertEqual(LATCH_UNLATCHED, logic.latch)

    def test_the_step_size_is_configurable(self):
        logic = GradhermeticCoverLogic(_config(height_step_pct=5.0))
        logic.seed_state(80.0)
        self.assertAlmostEqual(75.0, _moves(logic.on_step(DIRECTION_DOWN))[0].position)

    def test_a_step_at_a_travel_limit_does_nothing_and_says_so(self):
        entries = []
        logic = GradhermeticCoverLogic(_config(), log=lambda m, level="INFO": entries.append(m))
        logic.seed_state(100.0)
        self.assertEqual([], logic.on_step(DIRECTION_UP))
        logic.seed_state(0.0)
        self.assertEqual([], logic.on_step(DIRECTION_DOWN))
        self.assertTrue(any("top limit" in m for m in entries))
        self.assertTrue(any("bottom limit" in m for m in entries))

    def test_a_step_into_the_band_crosses_to_the_far_edge(self):
        self.logic.seed_state(47.0)
        actions = run_plan(self.logic, self.logic.on_step(DIRECTION_DOWN))
        self.assertAlmostEqual(DIP, _moves(actions)[0].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)
        actions = run_plan(self.logic, self.logic.on_step(DIRECTION_UP))
        self.assertAlmostEqual(RELEASE, _moves(actions)[0].position)
        # Rising from below the lower edge to exactly the release height proves nothing about the
        # latch, so the belief is honest rather than optimistic...
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        # ...and clears itself the moment the blind rests clear of the band.
        actions = run_plan(self.logic, self.logic.on_step(DIRECTION_UP))
        self.assertAlmostEqual(RELEASE + 2.0, _moves(actions)[0].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_a_step_down_from_the_band_with_no_latch_belief_releases_first(self):
        self.logic.seed_state(41.0)
        actions = run_plan(self.logic, self.logic.on_step(DIRECTION_DOWN))
        self.assertEqual([ACTION_OPEN_FULL, ACTION_MOVE_TO], _kinds(_moves(actions)))
        self.assertAlmostEqual(DIP, _moves(actions)[1].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_a_step_up_from_the_band_with_no_latch_belief_rises_directly(self):
        self.logic.seed_state(41.0)
        actions = run_plan(self.logic, self.logic.on_step(DIRECTION_UP))
        self.assertEqual([ACTION_MOVE_TO], _kinds(_moves(actions)))
        self.assertAlmostEqual(RELEASE, _moves(actions)[0].position)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_a_step_with_an_unknown_position_does_nothing(self):
        self.logic.seed_state(None)
        self.assertEqual([], self.logic.on_step(DIRECTION_DOWN))
        self.assertEqual([], self.logic.on_step(DIRECTION_UP))

    def test_a_step_mid_plan_stops_the_plan(self):
        self.logic.seed_state(80.0)
        self.logic.on_close()
        actions = self.logic.on_step(DIRECTION_DOWN)
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertFalse(self.logic.has_pending_plan)

    def test_a_step_while_moving_externally_stops_the_blind(self):
        self.logic.seed_state(80.0)
        self.logic.on_real_position(70.0, True)
        actions = self.logic.on_step(DIRECTION_DOWN)
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertEqual([], _moves(actions))

    def test_a_step_never_enters_tilt(self):
        for start in (80.0, 10.0):
            for direction in (DIRECTION_UP, DIRECTION_DOWN):
                with self.subTest(start=start, direction=direction):
                    logic = GradhermeticCoverLogic(_config())
                    logic.seed_state(start)
                    actions = run_plan(logic, logic.on_step(direction))
                    self.assertEqual([ACTION_MOVE_TO], _kinds(_moves(actions)))
                    self.assertFalse(logic.in_tilt)


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
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER], _kinds(actions)[:2])
        self.assertFalse(self.logic.has_pending_plan)

    def test_short_press_steps_the_height_when_not_latched(self):
        # The same rule as the dashboard step helpers: a nudge, never a tilt entry.
        self.logic.seed_state(80.0)
        self.assertAlmostEqual(78.0, _moves(self.logic.on_knx_short(DIRECTION_DOWN))[0].position)
        logic = GradhermeticCoverLogic(_config())
        logic.seed_state(10.0)
        self.assertAlmostEqual(12.0, _moves(logic.on_knx_short(DIRECTION_UP))[0].position)
        self.assertFalse(logic.in_tilt)

    def test_short_press_ignored_when_position_unknown(self):
        self.logic.seed_state(None)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_short_press_inside_the_band_without_a_latch_belief_steps_the_height(self):
        # Resting in the band with the latch unknown (after a restart, say) used to make both
        # buttons dead. Now up rises out of the band and down pays the release first.
        self.logic.seed_state(41.0)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertAlmostEqual(RELEASE, _moves(self.logic.on_knx_short(DIRECTION_UP))[0].position)
        logic = GradhermeticCoverLogic(_config())
        logic.seed_state(41.0)
        self.assertEqual(ACTION_OPEN_FULL, _moves(logic.on_knx_short(DIRECTION_DOWN))[0].kind)


class TestStartupAndMisc(unittest.TestCase):
    """
    Startup only ever seeds the belief. Re-referencing the actuator is left to the first move that
    needs a trusted position, so a power cut at night does not raise the blind by itself.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_startup_inside_the_band_seeds_an_unknown_latch_without_moving(self):
        actions = self.logic.on_startup(41.0)
        self.assertEqual([], _moves(actions))
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertFalse(self.logic.has_pending_plan)
        # The position itself is known, so the sensor is corrected straight away.
        self.assertAlmostEqual(41.0, _published(actions)[-1].position)

    def test_startup_with_an_unknown_position_seeds_an_unknown_latch_without_moving(self):
        actions = self.logic.on_startup(None)
        # Nothing moves; the sensor is published as unavailable rather than left to a stale value.
        self.assertEqual([ACTION_PUBLISH_STATE], _kinds(actions))
        self.assertIsNone(actions[0].position)
        self.assertFalse(actions[0].in_tilt)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)
        self.assertFalse(self.logic.has_pending_plan)

    def test_startup_defers_the_reference_to_the_first_descent(self):
        # The lazy half of the deferral: the guarded descent buys the reference when it needs it.
        self.logic.on_startup(41.0)
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual([ACTION_OPEN_FULL, ACTION_CLOSE_FULL], _kinds(_moves(actions)))

    def test_startup_defers_the_reference_to_a_descending_set_position(self):
        self.logic.on_startup(None)
        actions = self.logic.on_set_position(10.0)
        self.assertEqual(ACTION_OPEN_FULL, _moves(actions)[0].kind)

    def test_startup_leaves_a_later_open_a_plain_full_open(self):
        # Opening is the reference: it needs no detour of its own.
        self.logic.on_startup(41.0)
        actions = run_plan(self.logic, self.logic.on_open())
        self.assertEqual([ACTION_OPEN_FULL], _kinds(_moves(actions)))
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_startup_outside_the_band_resumes_and_publishes(self):
        actions = self.logic.on_startup(80.0)
        self.assertEqual([], _moves(actions))
        self.assertAlmostEqual(80.0, _published(actions)[-1].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_the_first_known_reading_publishes(self):
        # Started while the real cover was still unavailable: nothing else would correct the sensor
        # until a plan completed, and startup no longer runs one.
        self.logic.on_startup(None)
        actions = self.logic.on_real_position(60.0, False)
        self.assertEqual([ACTION_PUBLISH_STATE], _kinds(actions))
        self.assertAlmostEqual(60.0, actions[0].position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

    def test_a_reading_while_already_known_and_idle_publishes_nothing(self):
        self.logic.on_startup(60.0)
        self.assertEqual([], self.logic.on_real_position(60.0, False))

    def test_manual_stop_then_rest_publishes(self):
        self.logic.seed_state(60.0)
        self.logic.on_real_position(60.0, True)  # moving (e.g. manual drive)
        actions = self.logic.on_real_position(55.0, False)  # came to rest, no plan
        self.assertEqual([ACTION_PUBLISH_STATE], _kinds(actions))
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
        actions = self.logic.on_stop()
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER], _kinds(actions)[:2])
        self.assertFalse(self.logic.has_pending_plan)
        # With the plan cleared, position feedback only updates what the cover shows.
        self.assertEqual([ACTION_PUBLISH_STATE], _kinds(self.logic.on_real_position(70.0, True)))


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
        self.logic.on_step(DIRECTION_UP)
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
        # Leaving tilt from a known release plans nothing, so the close keeps running.
        self.logic.seed_state(80.0)
        self.logic.on_close()
        self.assertEqual([], self.logic.on_set_tilt_mode(False))
        self.assertTrue(self.logic.has_pending_plan)


class TestSettleTimer(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(80.0)

    def test_a_long_move_rearms_rather_than_stalling(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(40.0, True)
        self.assertIn(ACTION_ARM_SETTLE_TIMER, _kinds(actions))
        self.assertNotIn(ACTION_NOTIFY, _kinds(actions))
        self.assertTrue(self.logic.has_pending_plan)

    def test_settled_short_stalls_and_notifies_without_a_stop(self):
        # The blind is at rest: a stop has nothing to stop, and on a KNX actuator without a stop
        # object it would be a step telegram instead.
        self.logic.on_close()
        actions = self.logic.on_settle_timer(50.0, False)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_NOTIFY, ACTION_PUBLISH_STATE],
                         _kinds(actions))
        self.assertFalse(self.logic.has_pending_plan)

    def test_an_unreadable_position_stalls_and_stops(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(None, False)
        self.assertEqual([ACTION_STOP, ACTION_CANCEL_SETTLE_TIMER, ACTION_NOTIFY],
                         _kinds(actions)[:3])
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_a_stray_firing_without_a_plan_does_nothing(self):
        self.assertEqual([], self.logic.on_settle_timer(80.0, False))

    def test_the_timer_can_complete_a_plan_the_actuator_never_reported(self):
        self.logic.on_close()
        actions = self.logic.on_settle_timer(0.0, False)
        self.assertEqual([ACTION_CANCEL_SETTLE_TIMER, ACTION_PUBLISH_STATE], _kinds(actions))
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
        # B1: known unlatched, the descent is the very first command -- no no-op waypoint that
        # stalls until the fallback timer, and no full-open detour either. (The exit now ends at
        # the top limit rather than inside the band; the band-interior case the bug was found in is
        # covered by the planner, which can be handed that belief directly.)
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertAlmostEqual(100.0, self.logic.last_position)
        self.assertEqual(LATCH_UNLATCHED, self.logic.latch)

        actions = self.logic.on_close()
        self.assertEqual(ACTION_CLOSE_FULL, actions[0].kind)

    def test_slat_step_ignores_stale_feedback(self):
        # B2: a 20% slat step is 1.2 real percent, less than the old 1.5% arrival tolerance, so a
        # duplicate report of the pre-step position used to complete the plan instantly.
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched at real UPPER, virtual 0.
        self.logic.on_step(DIRECTION_UP)

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
        self.assertEqual(ACTION_PUBLISH_STATE, _kinds(actions)[-1])
        self.assertEqual(1, _kinds(actions).count(ACTION_CANCEL_SETTLE_TIMER))


class TestLogging(unittest.TestCase):

    def test_step_trace_is_debug_and_plan_completion_is_info(self):
        entries = []

        def log(message, level="INFO"):
            entries.append((level, message))

        logic = GradhermeticCoverLogic(_config(), log=log)
        logic.seed_state(80.0)
        run_plan(logic, logic.on_set_position(30.0))

        info = [message for level, message in entries if level == "INFO"]
        debug = [message for level, message in entries if level == "DEBUG"]

        self.assertIn("plan complete: latch=unlatched position=30.0", info)
        self.assertTrue(any("commanding move_to 30.0 (satisfied at 30.0) from 80.0" in message
                            for message in debug))
        self.assertFalse(any("plan complete" in message for message in debug))

    def test_interrupting_a_plan_logs_the_latch_transition_at_info(self):
        entries = []

        def log(message, level="INFO"):
            entries.append((level, message))

        logic = GradhermeticCoverLogic(_config(), log=log)
        logic.seed_state(100.0)
        run_plan(logic, logic.on_set_tilt_mode(True))  # latched, resting at the upper edge.
        logic.on_set_tilt_mode(False)  # leave-tilt plan in flight.
        logic.on_stop()

        info = [message for level, message in entries if level == "INFO"]
        self.assertTrue(any("latch belief latched -> unknown: leave plan interrupted" in message
                            for message in info))

    def test_a_violating_plan_is_logged_at_error(self):
        entries = []

        def log(message, level="INFO"):
            entries.append((level, message))

        logic = GradhermeticCoverLogic(_config(), log=log)
        logic.seed_state(80.0)
        with mock.patch("gradhermetic_cover_control.planner.check_plan", return_value="L1: boom"):
            logic.on_close()

        self.assertIn(("ERROR", "refusing a plan that violates L1: boom"), entries)


class TestPublishing(unittest.TestCase):
    """
    What the virtual cover shows, and when: every event publishes whatever changed, so travel is
    visible as it happens rather than only where it ended.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_a_height_move_publishes_its_progress(self):
        self.logic.seed_state(80.0)
        actions = self.logic.on_close()
        # Commanded but not yet reported moving: the cover already shows what is about to happen.
        self.assertEqual(MOTION_CLOSING, _published(actions)[-1].motion)
        for position in (70.0, 50.0, 30.0):
            actions = self.logic.on_real_position(position, True)
            self.assertAlmostEqual(position, _published(actions)[-1].position)
            self.assertEqual(MOTION_CLOSING, _published(actions)[-1].motion)
        actions = self.logic.on_real_position(0.0, False)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_IDLE, _published(actions)[-1].motion)

    def test_an_external_move_publishes_its_progress_too(self):
        self.logic.seed_state(20.0)
        actions = self.logic.on_real_position(30.0, True, DIRECTION_UP)
        self.assertAlmostEqual(30.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_OPENING, _published(actions)[-1].motion)

    def test_the_direction_is_read_off_the_trend_when_the_controller_gives_none(self):
        self.logic.seed_state(20.0)
        self.logic.on_real_position(20.0, True)
        actions = self.logic.on_real_position(18.0, True)
        self.assertEqual(MOTION_CLOSING, _published(actions)[-1].motion)

    def test_a_slat_move_publishes_on_the_slat_scale(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched at UPPER, virtual 0.
        actions = self.logic.on_open()  # slats toward the lower edge: the blind itself descends
        published = _published(actions)[-1]
        self.assertTrue(published.in_tilt)
        self.assertEqual(MOTION_OPENING, published.motion)
        actions = self.logic.on_real_position(41.0, True, DIRECTION_DOWN)
        published = _published(actions)[-1]
        self.assertAlmostEqual(50.0, published.position)
        self.assertTrue(published.in_tilt)
        self.assertEqual(MOTION_OPENING, published.motion)

    def test_an_entry_shows_height_mode_until_it_latches(self):
        self.logic.seed_state(80.0)
        actions = self.logic.on_set_tilt_mode(True)
        self.assertFalse(_published(actions)[-1].in_tilt)
        self.assertEqual(MOTION_OPENING, _published(actions)[-1].motion)
        actions = self.logic.on_real_position(90.0, True, DIRECTION_UP)
        self.assertAlmostEqual(90.0, _published(actions)[-1].position)
        self.assertFalse(_published(actions)[-1].in_tilt)
        self.logic.on_real_position(100.0, False)  # the full open is done; the dip is commanded
        actions = self.logic.on_real_position(60.0, True, DIRECTION_DOWN)
        self.assertAlmostEqual(60.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_CLOSING, _published(actions)[-1].motion)
        self.assertFalse(_published(actions)[-1].in_tilt)
        self.logic.on_real_position(DIP, False)  # the dip is done; the latching rise is commanded
        actions = self.logic.on_real_position(UPPER, False)
        published = _published(actions)[-1]
        self.assertTrue(published.in_tilt)
        self.assertAlmostEqual(0.0, published.position)
        self.assertEqual(MOTION_IDLE, published.motion)

    def test_an_exit_shows_height_mode_from_the_start(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = self.logic.on_set_tilt_mode(False)
        published = _published(actions)[-1]
        self.assertFalse(published.in_tilt)
        self.assertAlmostEqual(UPPER, published.position)  # the real height, not a slat angle
        self.assertEqual(MOTION_OPENING, published.motion)

    def test_the_rest_after_a_stop_publishes_idle(self):
        self.logic.seed_state(80.0)
        self.logic.on_close()
        self.logic.on_real_position(60.0, True, DIRECTION_DOWN)
        self.logic.on_stop()
        actions = self.logic.on_real_position(58.0, False)
        self.assertAlmostEqual(58.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_IDLE, _published(actions)[-1].motion)

    def test_a_stop_on_a_controller_without_motion_state_still_publishes_the_rest(self):
        # No opening/closing state ever arrives, so nothing but the stop itself can say "idle".
        self.logic.seed_state(80.0)
        self.logic.on_close()
        self.logic.on_real_position(70.0, False)
        actions = self.logic.on_stop()
        self.assertAlmostEqual(70.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_IDLE, _published(actions)[-1].motion)

    def test_a_stall_publishes_where_the_blind_rests(self):
        self.logic.seed_state(80.0)
        self.logic.on_close()
        actions = self.logic.on_settle_timer(50.0, False)
        self.assertIn(ACTION_NOTIFY, _kinds(actions))
        self.assertAlmostEqual(50.0, _published(actions)[-1].position)
        self.assertEqual(MOTION_IDLE, _published(actions)[-1].motion)

    def test_unavailable_publishes_as_such_and_drops_the_mode(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = self.logic.on_real_position(None, False)
        published = _published(actions)[-1]
        self.assertIsNone(published.position)
        self.assertFalse(published.in_tilt)
        self.assertEqual(MOTION_IDLE, published.motion)

    def test_nothing_is_published_twice(self):
        self.logic.on_startup(60.0)
        self.assertEqual([], self.logic.on_real_position(60.0, False))
        self.assertEqual([], self.logic.on_real_position(60.0, False))


class TestTiltToggle(unittest.TestCase):
    """The stateless toggle behind the tilt helper and the KNX tilt address."""

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_the_toggle_enters_and_leaves(self):
        self.logic.seed_state(80.0)
        run_plan(self.logic, self.logic.on_toggle_tilt_mode())
        self.assertTrue(self.logic.in_tilt)
        run_plan(self.logic, self.logic.on_toggle_tilt_mode())
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, self.logic.last_position)

    def test_the_toggle_cancels_an_entry_in_flight(self):
        self.logic.seed_state(80.0)
        self.logic.on_toggle_tilt_mode()
        self.logic.on_real_position(90.0, True, DIRECTION_UP)
        actions = self.logic.on_toggle_tilt_mode()
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertEqual([], _moves(actions))
        self.assertFalse(self.logic.has_pending_plan)

    def test_the_toggle_cancels_an_exit_in_flight(self):
        self.logic.seed_state(100.0)
        run_plan(self.logic, self.logic.on_toggle_tilt_mode())
        self.logic.on_toggle_tilt_mode()
        self.logic.on_real_position(RELEASE - 1.0, True, DIRECTION_UP)
        actions = self.logic.on_toggle_tilt_mode()
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertFalse(self.logic.has_pending_plan)
        self.assertEqual(LATCH_UNKNOWN, self.logic.latch)

    def test_repeating_a_request_in_flight_does_not_restart_it(self):
        # Each restart would be another real-cover command against the rate limit.
        self.logic.seed_state(80.0)
        first = self.logic.on_set_tilt_mode(True)
        self.assertEqual([ACTION_OPEN_FULL], _kinds(_moves(first)))
        self.assertEqual([], self.logic.on_set_tilt_mode(True))
        self.assertTrue(self.logic.has_pending_plan)
        run_plan(self.logic, first)
        self.logic.on_set_tilt_mode(False)
        self.assertEqual([], self.logic.on_set_tilt_mode(False))
        self.assertTrue(self.logic.has_pending_plan)

    def test_asking_to_leave_during_an_entry_stops_it(self):
        self.logic.seed_state(80.0)
        self.logic.on_set_tilt_mode(True)
        actions = self.logic.on_set_tilt_mode(False)
        self.assertEqual(ACTION_STOP, _kinds(actions)[0])
        self.assertFalse(self.logic.has_pending_plan)


class TestStopIsOnlySentWhenSomethingMoves(unittest.TestCase):
    """
    Every command reaches the actuator through this app, and on a KNX actuator without a stop
    object a stop is carried on the step object -- which nudges an idle blind.
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(80.0)

    def test_stop_on_an_idle_blind_sends_no_stop_command(self):
        self.assertNotIn(ACTION_STOP, _kinds(self.logic.on_stop()))

    def test_stop_with_a_plan_pending_sends_it(self):
        self.logic.on_close()
        self.assertIn(ACTION_STOP, _kinds(self.logic.on_stop()))

    def test_stop_while_moving_externally_sends_it(self):
        self.logic.on_real_position(70.0, True)
        self.assertIn(ACTION_STOP, _kinds(self.logic.on_stop()))


if __name__ == "__main__":
    unittest.main()
