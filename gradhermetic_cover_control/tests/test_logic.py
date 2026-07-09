import unittest

from gradhermetic_cover_control.logic import (
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_OPEN_FULL,
    ACTION_PERSIST_STATE,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    DIRECTION_DOWN,
    DIRECTION_UP,
    GradhermeticCoverLogic,
    LogicConfig,
)

# Geometry used throughout: zone [38, 44], epsilon 2, step 20. Span = 6, so the minimum step that
# moves the integer-reporting actuator is 100/6 ~= 16.7; 20 clears it.
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 20.0


def _config():
    return LogicConfig(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                       tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP)


def _kinds(actions):
    return [action.kind for action in actions]


def _target_of(action):
    if action.kind == ACTION_MOVE_TO:
        return action.position
    if action.kind == ACTION_OPEN_FULL:
        return 100.0
    return 0.0


_MOVE_KINDS = (ACTION_MOVE_TO, ACTION_OPEN_FULL, ACTION_CLOSE_FULL)


def run_plan(logic, actions):
    """
    Drive a plan to completion by simulating the blind reaching each commanded waypoint.
    """
    collected = list(actions)
    while collected and collected[-1].kind in _MOVE_KINDS:
        target = _target_of(collected[-1])
        logic.on_real_position(target, True)
        collected.extend(logic.on_real_position(target, False))
    return collected


def _persisted(actions):
    return [a for a in actions if a.kind == ACTION_PERSIST_STATE]


def _published(actions):
    return [a for a in actions if a.kind == ACTION_PUBLISH_POSITION]


class TestOutsideTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_open_opens_fully(self):
        self.logic.seed_state(50.0, False)
        actions = run_plan(self.logic, self.logic.on_open())
        self.assertEqual(ACTION_OPEN_FULL, actions[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(100.0, _published(actions)[-1].position)

    def test_close_closes_fully(self):
        self.logic.seed_state(50.0, False)
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual(ACTION_CLOSE_FULL, actions[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertEqual(0.0, _published(actions)[-1].position)

    def test_set_position_maps_one_to_one(self):
        self.logic.seed_state(80.0, False)
        actions = run_plan(self.logic, self.logic.on_set_position(30.0))
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(30.0, actions[0].position)
        self.assertAlmostEqual(30.0, self.logic.last_position)
        self.assertAlmostEqual(30.0, _published(actions)[-1].position)


class TestEnterLeaveTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_enter_from_above_dips_then_latches_closed(self):
        self.logic.seed_state(100.0, False)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        # First waypoint is the pre-dip below the lower edge.
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(LOWER - EPSILON, actions[0].position)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(UPPER, self.logic.last_position)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_enter_from_below_hops_above_edge_then_dips(self):
        self.logic.seed_state(20.0, False)
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        # From below the lower edge the sequence first hops just above it so the dip crosses the
        # edge downward, guaranteeing a genuine down-then-up latch motion.
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(LOWER + EPSILON, actions[0].position)
        self.assertAlmostEqual(LOWER - EPSILON, actions[1].position)
        self.assertAlmostEqual(UPPER, actions[2].position)
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_enter_when_position_unknown_recovers_first(self):
        self.logic.seed_state(None, False)
        actions = self.logic.on_set_tilt_mode(True)
        self.assertEqual(ACTION_OPEN_FULL, actions[0].kind)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_leave_moves_above_zone(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_set_tilt_mode(False))
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(UPPER + EPSILON, actions[0].position)
        self.assertFalse(self.logic.in_tilt)

    def test_enter_is_idempotent(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.assertEqual([], self.logic.on_set_tilt_mode(True))


class TestInsideTilt(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched, virtual 0 / real UPPER.

    def test_open_orients_slats_to_lower_edge(self):
        actions = run_plan(self.logic, self.logic.on_open())
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(LOWER, self.logic.last_position)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_close_orients_slats_to_upper_edge(self):
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(UPPER, self.logic.last_position)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)

    def test_set_position_interpolates_between_edges(self):
        actions = self.logic.on_set_position(50.0)
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(41.0, actions[0].position)  # 44 - 0.5 * 6

    def test_step_up_moves_toward_open(self):
        actions = self.logic.on_knx_short(DIRECTION_UP)
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(UPPER - (STEP / 100.0) * (UPPER - LOWER), actions[0].position)
        run_plan(self.logic, actions)
        self.assertTrue(self.logic.in_tilt)

    def test_step_down_at_closed_edge_is_noop(self):
        # Already at virtual 0 (closed edge).
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_step_up_at_open_edge_leaves_tilt(self):
        run_plan(self.logic, self.logic.on_open())  # virtual 100 / real LOWER.
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_UP))
        self.assertAlmostEqual(UPPER + EPSILON, _target_of(actions[0]))
        self.assertFalse(self.logic.in_tilt)


class TestKnxLongPress(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_long_up_opens_and_leaves_tilt(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_UP))
        self.assertEqual(ACTION_OPEN_FULL, actions[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_long_down_from_tilt_rises_out_then_descends(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        # First rise above the zone to release the latch, then close fully.
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(UPPER + EPSILON, actions[0].position)
        self.assertIn(ACTION_CLOSE_FULL, _kinds(actions))
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, _published(actions)[-1].position)

    def test_long_down_outside_closes_fully(self):
        self.logic.seed_state(80.0, False)
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        self.assertEqual(ACTION_CLOSE_FULL, actions[0].kind)
        self.assertFalse(self.logic.in_tilt)


class TestKnxShortPress(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_short_press_stops_a_moving_blind(self):
        self.logic.seed_state(50.0, False)
        self.logic.on_open()  # starts a plan
        self.logic.on_real_position(70.0, True)  # now moving
        actions = self.logic.on_knx_short(DIRECTION_UP)
        self.assertEqual([ACTION_STOP], _kinds(actions))

    def test_short_down_from_above_enters_closed(self):
        self.logic.seed_state(80.0, False)
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_DOWN))
        self.assertAlmostEqual(LOWER - EPSILON, _target_of(actions[0]))
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.current_virtual_position())

    def test_short_up_from_below_enters_open(self):
        self.logic.seed_state(10.0, False)
        actions = run_plan(self.logic, self.logic.on_knx_short(DIRECTION_UP))
        # From below, the latch sequence hops above the lower edge before dipping across it.
        self.assertAlmostEqual(LOWER + EPSILON, _target_of(actions[0]))
        self.assertTrue(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, self.logic.current_virtual_position())

    def test_short_up_from_above_does_nothing(self):
        self.logic.seed_state(80.0, False)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_UP))

    def test_short_down_from_below_does_nothing(self):
        self.logic.seed_state(10.0, False)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_short_press_ignored_when_position_unknown(self):
        self.logic.seed_state(None, False)
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))


class TestRecoveryAndMisc(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_recover_opens_fully_and_clears_tilt(self):
        self.logic.seed_state(None, True)
        actions = run_plan(self.logic, self.logic.on_recover())
        self.assertEqual(ACTION_OPEN_FULL, actions[0].kind)
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(100.0, _published(actions)[-1].position)

    def test_manual_stop_then_rest_publishes_and_persists(self):
        self.logic.seed_state(60.0, False)
        self.logic.on_real_position(60.0, True)  # moving (e.g. manual drive)
        actions = self.logic.on_real_position(55.0, False)  # came to rest, no plan
        self.assertIn(ACTION_PUBLISH_POSITION, _kinds(actions))
        self.assertEqual(1, len(_persisted(actions)))
        self.assertAlmostEqual(55.0, _persisted(actions)[0].position)

    def test_disabled_logic_ignores_events(self):
        self.logic.seed_state(50.0, False)
        self.logic.disable()
        self.assertEqual([], self.logic.on_open())
        self.assertEqual([], self.logic.on_knx_short(DIRECTION_DOWN))

    def test_stop_clears_pending_plan(self):
        self.logic.seed_state(50.0, False)
        self.logic.on_open()
        self.assertEqual([ACTION_STOP], _kinds(self.logic.on_stop()))
        # With the plan cleared, position feedback no longer advances anything.
        self.assertEqual([], self.logic.on_real_position(70.0, True))


class TestLatchSafetyGuard(unittest.TestCase):
    """
    Any downward move while the blind might be latched must first release the latch upward. This
    protects against a stale in_tilt belief (interrupted latch sequence, external move, startup).
    """

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())
        # Simulate an interrupted latch sequence: the blind physically sits inside the tilt band but
        # the logic believes it is in NORMAL mode (in_tilt False).
        self.logic.seed_state(41.0, False)

    def test_close_releases_upward_before_descending(self):
        actions = run_plan(self.logic, self.logic.on_close())
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(UPPER + EPSILON, actions[0].position)
        self.assertIn(ACTION_CLOSE_FULL, _kinds(actions))
        self.assertFalse(self.logic.in_tilt)
        self.assertAlmostEqual(0.0, self.logic.last_position)

    def test_long_down_releases_upward_before_descending(self):
        actions = run_plan(self.logic, self.logic.on_knx_long(DIRECTION_DOWN))
        self.assertAlmostEqual(UPPER + EPSILON, _target_of(actions[0]))
        self.assertIn(ACTION_CLOSE_FULL, _kinds(actions))
        self.assertFalse(self.logic.in_tilt)

    def test_set_position_downward_releases_upward_first(self):
        actions = run_plan(self.logic, self.logic.on_set_position(10.0))
        self.assertAlmostEqual(UPPER + EPSILON, _target_of(actions[0]))
        self.assertAlmostEqual(10.0, self.logic.last_position)

    def test_set_position_upward_does_not_release(self):
        # Rising past the upper edge self-releases the latch, so no explicit release step is needed.
        actions = self.logic.on_set_position(90.0)
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(90.0, actions[0].position)

    def test_no_release_when_clearly_outside_band(self):
        self.logic.seed_state(80.0, False)
        actions = self.logic.on_close()
        self.assertEqual(ACTION_CLOSE_FULL, actions[0].kind)

    def test_unknown_position_is_treated_as_maybe_latched(self):
        self.logic.seed_state(None, False)
        actions = self.logic.on_close()
        self.assertEqual(ACTION_MOVE_TO, actions[0].kind)
        self.assertAlmostEqual(UPPER + EPSILON, actions[0].position)


class TestFeedbackRefresh(unittest.TestCase):

    def setUp(self):
        self.logic = GradhermeticCoverLogic(_config())

    def test_external_move_outside_band_clears_in_tilt(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))  # latched, real UPPER
        self.assertTrue(self.logic.in_tilt)
        # The real cover is driven externally well below the zone; the latch must have released.
        self.logic.on_real_position(10.0, True)
        self.logic.on_real_position(10.0, False)
        self.assertFalse(self.logic.in_tilt)

    def test_feedback_inside_band_keeps_in_tilt(self):
        self.logic.seed_state(100.0, False)
        run_plan(self.logic, self.logic.on_set_tilt_mode(True))
        self.logic.on_real_position(UPPER, False)  # still inside the band
        self.assertTrue(self.logic.in_tilt)


class TestConfigValidation(unittest.TestCase):

    def test_epsilon_not_exceeding_tolerance_raises(self):
        with self.assertRaises(ValueError):
            LogicConfig(tilt_zone_upper_pct=44.0, tilt_zone_lower_pct=38.0,
                        tilt_zone_epsilon_pct=1.0, tilt_step_pct=STEP).validate()

    def test_step_below_actuator_resolution_raises(self):
        with self.assertRaises(ValueError):
            LogicConfig(tilt_zone_upper_pct=44.0, tilt_zone_lower_pct=38.0,
                        tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=1.0).validate()

    def test_valid_geometry_passes(self):
        LogicConfig(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                    tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP).validate()


if __name__ == "__main__":
    unittest.main()
