import unittest

from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.planner import (
    COMMAND_CLOSE,
    COMMAND_OPEN,
    COMMAND_POSITION,
    DIRECTION_DOWN,
    DIRECTION_UP,
    INTENT_CLOSE,
    INTENT_ENTER_TILT,
    INTENT_ENTER_TOWARD_ZONE,
    INTENT_LEAVE_TILT,
    INTENT_LONG_PRESS,
    INTENT_OPEN,
    INTENT_RECOVER,
    INTENT_SET_POSITION,
    INTENT_SLAT_STEP,
    LATCH_LATCHED,
    LATCH_UNKNOWN,
    LATCH_UNLATCHED,
    NEAR_EDGE_CLOSED,
    NEAR_EDGE_OPEN,
    PLAN_ENTER,
    PLAN_LEAVE,
    PLAN_NORMAL,
    PLAN_RECOVER,
    PLAN_SLAT,
    STEP_MOVE_TO,
    STEP_RISE_TO_AT_LEAST,
    Belief,
    Intent,
    Plan,
    Step,
    can_change_latch,
    check_plan,
    plan,
)

# Zone [38, 44], epsilon 2, step 20 -> band [36, 46].
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 20.0
DIP = LOWER - EPSILON
RELEASE = UPPER + EPSILON

ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
            tilt_step_pct=STEP)

# Representative starts: above, at the band edges, inside the zone, below, and unknown.
STARTS = (100.0, 80.0, 47.0, RELEASE, 45.0, UPPER, 41.0, LOWER, DIP, 30.0, 0.0, None)


def _belief(position, latch=LATCH_UNLATCHED, is_moving=False):
    return Belief(position=position, latch=latch, is_moving=is_moving)


def _targets(movement):
    return [step.target for step in movement.steps]


def _commands(movement):
    return [step.command for step in movement.steps]


class TestEnterTilt(unittest.TestCase):
    """One canonical sequence from any start: full open, dip, latching rise."""

    def test_same_sequence_from_every_start(self):
        for start in STARTS:
            for latch in (LATCH_UNLATCHED, LATCH_UNKNOWN):
                with self.subTest(start=start, latch=latch):
                    movement = plan(ZONE, _belief(start, latch),
                                    Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_CLOSED))
                    self.assertEqual(PLAN_ENTER, movement.kind)
                    self.assertEqual([100.0, DIP, UPPER], _targets(movement))
                    self.assertEqual([COMMAND_OPEN, COMMAND_POSITION, COMMAND_POSITION],
                                     _commands(movement))
                    self.assertEqual(LATCH_LATCHED, movement.final_latch)

    def test_near_open_edge_continues_to_the_lower_edge(self):
        movement = plan(ZONE, _belief(30.0), Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_OPEN))
        self.assertEqual([100.0, DIP, UPPER, LOWER], _targets(movement))

    def test_the_dip_is_a_pure_descent_from_fully_open(self):
        # Nothing in the sequence rises into the zone before the dip, so the dip cannot be made
        # while latched no matter where the blind started.
        movement = plan(ZONE, _belief(None, LATCH_UNKNOWN), Intent(INTENT_ENTER_TILT))
        self.assertEqual(COMMAND_OPEN, movement.steps[0].command)
        self.assertLess(movement.steps[1].target, LOWER)


class TestLeaveTilt(unittest.TestCase):

    def test_latched_uses_the_cheap_upward_exit(self):
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED), Intent(INTENT_LEAVE_TILT))
        self.assertEqual(PLAN_LEAVE, movement.kind)
        self.assertEqual([RELEASE], _targets(movement))
        self.assertEqual(STEP_RISE_TO_AT_LEAST, movement.steps[0].kind)
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

    def test_uncertain_belief_has_nothing_to_leave(self):
        for latch in (LATCH_UNLATCHED, LATCH_UNKNOWN):
            self.assertIsNone(plan(ZONE, _belief(41.0, latch), Intent(INTENT_LEAVE_TILT)))


class TestWholeHeight(unittest.TestCase):

    def test_open_drives_fully_open_with_the_open_command(self):
        movement = plan(ZONE, _belief(50.0), Intent(INTENT_OPEN))
        self.assertEqual(PLAN_NORMAL, movement.kind)
        self.assertEqual([100.0], _targets(movement))
        self.assertEqual([COMMAND_OPEN], _commands(movement))

    def test_close_from_a_known_release_descends_directly(self):
        movement = plan(ZONE, _belief(80.0, LATCH_UNLATCHED), Intent(INTENT_CLOSE))
        self.assertEqual([0.0], _targets(movement))
        self.assertEqual([COMMAND_CLOSE], _commands(movement))

    def test_close_right_after_leaving_tilt_descends_directly(self):
        # Resting at the release target, known unlatched: no full-open detour, no no-op waypoint.
        movement = plan(ZONE, _belief(RELEASE, LATCH_UNLATCHED), Intent(INTENT_CLOSE))
        self.assertEqual([0.0], _targets(movement))

    def test_close_from_an_uncertain_belief_releases_by_opening_fully(self):
        movement = plan(ZONE, _belief(41.0, LATCH_UNKNOWN), Intent(INTENT_CLOSE))
        self.assertEqual([100.0, 0.0], _targets(movement))
        self.assertEqual([COMMAND_OPEN, COMMAND_CLOSE], _commands(movement))

    def test_close_with_an_unknown_position_releases_by_opening_fully(self):
        movement = plan(ZONE, _belief(None, LATCH_UNKNOWN), Intent(INTENT_CLOSE))
        self.assertEqual([100.0, 0.0], _targets(movement))

    def test_set_position_upward_needs_no_release(self):
        movement = plan(ZONE, _belief(41.0, LATCH_UNKNOWN), Intent(INTENT_SET_POSITION,
                                                                   virtual_pct=90.0))
        self.assertEqual([90.0], _targets(movement))

    def test_set_position_downward_from_an_uncertain_belief_releases_first(self):
        movement = plan(ZONE, _belief(41.0, LATCH_UNKNOWN), Intent(INTENT_SET_POSITION,
                                                                   virtual_pct=10.0))
        self.assertEqual([100.0, 10.0], _targets(movement))
        self.assertEqual(COMMAND_OPEN, movement.steps[0].command)

    def test_set_position_downward_from_a_known_release_descends_directly(self):
        movement = plan(ZONE, _belief(80.0, LATCH_UNLATCHED), Intent(INTENT_SET_POSITION,
                                                                     virtual_pct=10.0))
        self.assertEqual([10.0], _targets(movement))

    def test_set_position_inside_the_band_snaps_to_an_edge(self):
        movement = plan(ZONE, _belief(80.0, LATCH_UNLATCHED), Intent(INTENT_SET_POSITION,
                                                                     virtual_pct=37.0))
        self.assertEqual([DIP], _targets(movement))
        movement = plan(ZONE, _belief(80.0, LATCH_UNLATCHED), Intent(INTENT_SET_POSITION,
                                                                     virtual_pct=45.0))
        self.assertEqual([RELEASE], _targets(movement))

    def test_set_position_to_the_current_position_is_not_a_descent(self):
        # Equal targets must not trip the descent guard; the executor skips the step instead.
        movement = plan(ZONE, _belief(50.0, LATCH_UNKNOWN), Intent(INTENT_SET_POSITION,
                                                                   virtual_pct=50.0))
        self.assertEqual([50.0], _targets(movement))

    def test_long_up_opens_fully(self):
        movement = plan(ZONE, _belief(41.0, LATCH_LATCHED),
                        Intent(INTENT_LONG_PRESS, direction=DIRECTION_UP))
        self.assertEqual([100.0], _targets(movement))
        self.assertEqual([COMMAND_OPEN], _commands(movement))

    def test_long_down_while_latched_releases_by_opening_fully(self):
        movement = plan(ZONE, _belief(41.0, LATCH_LATCHED),
                        Intent(INTENT_LONG_PRESS, direction=DIRECTION_DOWN))
        self.assertEqual([100.0, 0.0], _targets(movement))

    def test_long_down_outside_the_band_closes_directly(self):
        movement = plan(ZONE, _belief(80.0, LATCH_UNLATCHED),
                        Intent(INTENT_LONG_PRESS, direction=DIRECTION_DOWN))
        self.assertEqual([0.0], _targets(movement))

    def test_recover_is_a_single_full_open(self):
        movement = plan(ZONE, _belief(None, LATCH_UNKNOWN), Intent(INTENT_RECOVER))
        self.assertEqual(PLAN_RECOVER, movement.kind)
        self.assertEqual([100.0], _targets(movement))
        self.assertEqual([COMMAND_OPEN], _commands(movement))


class TestSlatMoves(unittest.TestCase):

    def test_open_and_close_become_in_zone_slat_moves(self):
        latched = _belief(UPPER, LATCH_LATCHED)
        self.assertEqual([LOWER], _targets(plan(ZONE, latched, Intent(INTENT_OPEN))))
        self.assertEqual([UPPER], _targets(plan(ZONE, latched, Intent(INTENT_CLOSE))))
        self.assertEqual(PLAN_SLAT, plan(ZONE, latched, Intent(INTENT_OPEN)).kind)

    def test_set_position_interpolates_between_the_edges(self):
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                        Intent(INTENT_SET_POSITION, virtual_pct=50.0))
        self.assertEqual(PLAN_SLAT, movement.kind)
        self.assertAlmostEqual(41.0, movement.steps[0].target)

    def test_step_up_moves_toward_the_open_edge(self):
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                        Intent(INTENT_SLAT_STEP, direction=DIRECTION_UP))
        self.assertAlmostEqual(UPPER - (STEP / 100.0) * (UPPER - LOWER), movement.steps[0].target)

    def test_step_down_moves_toward_the_closed_edge(self):
        movement = plan(ZONE, _belief(LOWER, LATCH_LATCHED),
                        Intent(INTENT_SLAT_STEP, direction=DIRECTION_DOWN))
        self.assertAlmostEqual(UPPER - ((100.0 - STEP) / 100.0) * (UPPER - LOWER),
                               movement.steps[0].target)

    def test_step_down_at_the_closed_edge_clamps(self):
        self.assertIsNone(
            plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                 Intent(INTENT_SLAT_STEP, direction=DIRECTION_DOWN)))

    def test_step_up_at_the_open_edge_clamps_for_the_helper(self):
        self.assertIsNone(
            plan(ZONE, _belief(LOWER, LATCH_LATCHED),
                 Intent(INTENT_SLAT_STEP, direction=DIRECTION_UP)))

    def test_step_up_at_the_open_edge_leaves_tilt_for_a_wall_button(self):
        movement = plan(
            ZONE, _belief(LOWER, LATCH_LATCHED),
            Intent(INTENT_SLAT_STEP, direction=DIRECTION_UP, cross_open_edge=True))
        self.assertEqual(PLAN_LEAVE, movement.kind)

    def test_no_slat_move_without_a_latch_belief(self):
        for latch in (LATCH_UNLATCHED, LATCH_UNKNOWN):
            self.assertIsNone(
                plan(ZONE, _belief(41.0, latch), Intent(INTENT_SLAT_STEP, direction=DIRECTION_UP)))

    def test_no_slat_move_without_a_position(self):
        self.assertIsNone(
            plan(ZONE, _belief(None, LATCH_LATCHED), Intent(INTENT_SLAT_STEP,
                                                            direction=DIRECTION_UP)))


class TestEnterTowardZone(unittest.TestCase):
    """Short press from outside the zone: enter only when the press points toward it."""

    def test_down_from_above_enters_at_the_closed_edge(self):
        movement = plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TOWARD_ZONE,
                                                    direction=DIRECTION_DOWN))
        self.assertEqual([100.0, DIP, UPPER], _targets(movement))

    def test_up_from_below_enters_at_the_open_edge(self):
        movement = plan(ZONE, _belief(10.0), Intent(INTENT_ENTER_TOWARD_ZONE,
                                                    direction=DIRECTION_UP))
        self.assertEqual([100.0, DIP, UPPER, LOWER], _targets(movement))

    def test_press_pointing_away_does_nothing(self):
        self.assertIsNone(
            plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TOWARD_ZONE, direction=DIRECTION_UP)))
        self.assertIsNone(
            plan(ZONE, _belief(10.0), Intent(INTENT_ENTER_TOWARD_ZONE, direction=DIRECTION_DOWN)))

    def test_press_while_resting_inside_the_zone_does_nothing(self):
        # Q3: neither direction points toward a zone the blind is already in; the long press and the
        # tilt helper remain the escape hatches.
        for direction in (DIRECTION_UP, DIRECTION_DOWN):
            self.assertIsNone(
                plan(ZONE, _belief(41.0, LATCH_UNKNOWN),
                     Intent(INTENT_ENTER_TOWARD_ZONE, direction=direction)))

    def test_press_with_an_unknown_position_does_nothing(self):
        self.assertIsNone(
            plan(ZONE, _belief(None, LATCH_UNKNOWN),
                 Intent(INTENT_ENTER_TOWARD_ZONE, direction=DIRECTION_DOWN)))


class TestCanChangeLatch(unittest.TestCase):

    def test_in_zone_plans_cannot(self):
        self.assertFalse(can_change_latch(ZONE, plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                                                     Intent(INTENT_OPEN))))

    def test_sequences_crossing_an_edge_can(self):
        self.assertTrue(
            can_change_latch(ZONE, plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TILT))))
        self.assertTrue(
            can_change_latch(ZONE, plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                                        Intent(INTENT_LEAVE_TILT))))
        self.assertTrue(can_change_latch(ZONE, plan(ZONE, _belief(80.0), Intent(INTENT_CLOSE))))


class TestInvariantsHoldForEveryPlan(unittest.TestCase):
    """Every plan the planner can produce satisfies check_plan, over the whole state space."""

    def test_sweep(self):
        intents = [
            Intent(INTENT_OPEN),
            Intent(INTENT_CLOSE),
            Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_CLOSED),
            Intent(INTENT_ENTER_TILT, near_edge=NEAR_EDGE_OPEN),
            Intent(INTENT_LEAVE_TILT),
            Intent(INTENT_RECOVER),
        ]
        intents += [Intent(INTENT_SET_POSITION, virtual_pct=float(v)) for v in range(0, 101, 5)]
        for direction in (DIRECTION_UP, DIRECTION_DOWN):
            intents += [
                Intent(INTENT_LONG_PRESS, direction=direction),
                Intent(INTENT_ENTER_TOWARD_ZONE, direction=direction),
                Intent(INTENT_SLAT_STEP, direction=direction),
                Intent(INTENT_SLAT_STEP, direction=direction, cross_open_edge=True),
            ]

        checked = 0
        for position in [None] + [float(p) for p in range(0, 101)]:
            for latch in (LATCH_LATCHED, LATCH_UNLATCHED, LATCH_UNKNOWN):
                # A latched mechanism can only rest inside the zone.
                if latch == LATCH_LATCHED and (position is None or not ZONE.in_zone(position)):
                    continue
                belief = _belief(position, latch)
                for intent in intents:
                    movement = plan(ZONE, belief, intent)
                    if movement is None:
                        continue
                    checked += 1
                    violation = check_plan(ZONE, belief, movement)
                    self.assertIsNone(violation,
                                      f"{intent} from {belief}: {violation} ({_targets(movement)})")
        self.assertGreater(checked, 1000)


class TestInvariantRejections(unittest.TestCase):
    """check_plan must actually reject the shapes it exists to forbid."""

    def test_n1_rejects_a_normal_target_inside_the_band(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 41.0),), LATCH_UNLATCHED)
        self.assertIn("N1", check_plan(ZONE, _belief(80.0), movement))

    def test_n1_allows_the_band_edges(self):
        for target in (DIP, RELEASE):
            movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, target),), LATCH_UNLATCHED)
            self.assertIsNone(check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_t1_rejects_a_slat_move_without_a_latch_belief(self):
        movement = Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, 41.0),), LATCH_LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))

    def test_t1_rejects_a_slat_target_outside_the_zone(self):
        movement = Plan(PLAN_SLAT, (Step(STEP_MOVE_TO, 30.0),), LATCH_LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41.0, LATCH_LATCHED), movement))

    def test_l1_rejects_an_unguarded_descent_from_an_uncertain_belief(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 0.0, COMMAND_CLOSE),), LATCH_UNLATCHED)
        self.assertIn("L1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))
        self.assertIn("L1", check_plan(ZONE, _belief(41.0, LATCH_LATCHED), movement))

    def test_l1_allows_a_descent_from_a_known_release(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 0.0, COMMAND_CLOSE),), LATCH_UNLATCHED)
        self.assertIsNone(check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_e1_rejects_latching_outside_the_canonical_sequence(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 41.0),), LATCH_LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(41.0, LATCH_UNLATCHED), movement))

    def test_e1_rejects_an_enter_sequence_that_does_not_start_fully_open(self):
        # README's old from-above entry: dip straight down without re-referencing at the top.
        movement = Plan(PLAN_ENTER, (
            Step(STEP_MOVE_TO, RELEASE),
            Step(STEP_MOVE_TO, DIP),
            Step(STEP_MOVE_TO, UPPER),
        ), LATCH_LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_e1_rejects_a_dip_that_does_not_clear_the_lower_edge(self):
        movement = Plan(PLAN_ENTER, (
            Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
            Step(STEP_MOVE_TO, LOWER),
            Step(STEP_MOVE_TO, UPPER),
        ), LATCH_LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_x1_rejects_the_cheap_exit_from_an_uncertain_belief(self):
        movement = Plan(PLAN_LEAVE, (Step(STEP_RISE_TO_AT_LEAST, RELEASE),), LATCH_UNLATCHED)
        self.assertIn("X1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))

    def test_x1_rejects_an_exit_that_does_not_clear_the_upper_edge(self):
        movement = Plan(PLAN_LEAVE, (Step(STEP_RISE_TO_AT_LEAST, UPPER),), LATCH_UNLATCHED)
        self.assertIn("X1", check_plan(ZONE, _belief(UPPER, LATCH_LATCHED), movement))

    def test_r1_rejects_a_recovery_that_is_not_a_full_open(self):
        movement = Plan(PLAN_RECOVER, (Step(STEP_MOVE_TO, 100.0),), LATCH_UNLATCHED)
        self.assertIn("R1", check_plan(ZONE, _belief(None, LATCH_UNKNOWN), movement))


if __name__ == "__main__":
    unittest.main()
