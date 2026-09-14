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
    INTENT_HEIGHT_STEP,
    INTENT_LEAVE_TILT,
    INTENT_LONG_PRESS,
    INTENT_OPEN,
    INTENT_SET_POSITION,
    INTENT_SLAT_STEP,
    LATCH_LATCHED,
    LATCH_UNKNOWN,
    LATCH_UNLATCHED,
    PLAN_ENTER,
    PLAN_LEAVE,
    PLAN_NORMAL,
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

# Zone [38, 44], epsilon 2, step 1.2 real travel percent -> band [36, 46]. Every configured number
# is real blind travel; span 6 makes the 1.2% step 20 on the virtual scale the planner works in.
UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 1.2
DIP = LOWER - EPSILON
RELEASE = UPPER + EPSILON

ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
            tilt_step_pct=STEP)

# The same zone with a measured release height well above the bare clearance, and an entry that
# finishes at a slightly-open slat angle (real 41.6, i.e. virtual 40) instead of the closed edge.
# Band = [36, 55].
CUSTOM_RELEASE = 55.0
CUSTOM_LANDING = 41.6
CUSTOM_ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                   tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP,
                   tilt_zone_release_pct=CUSTOM_RELEASE, tilt_enter_landing_pct=CUSTOM_LANDING)

# Representative starts: above, at the band edges, inside the zone, below, and unknown.
STARTS = (100.0, 80.0, 47.0, RELEASE, 45.0, UPPER, 41.0, LOWER, DIP, 30.0, 0.0, None)


def _belief(position, latch=LATCH_UNLATCHED, is_moving=False):
    return Belief(position=position, latch=latch, is_moving=is_moving)


def _targets(movement):
    return [step.target for step in movement.steps]


def _commanded(movement):
    return [step.command_position for step in movement.steps]


def _commands(movement):
    return [step.command for step in movement.steps]


def _leave_plan(target, command=COMMAND_POSITION, command_pct=None):
    """A hand-built exit plan, for the X1 checks that need shapes the planner will not produce."""
    step = Step(STEP_RISE_TO_AT_LEAST, target, command, command_pct=command_pct)
    return Plan(PLAN_LEAVE, (step,), LATCH_UNLATCHED)


class TestEnterTilt(unittest.TestCase):
    """One canonical sequence from any start: full open, dip, latching rise."""

    def test_same_sequence_from_every_start(self):
        for start in STARTS:
            for latch in (LATCH_UNLATCHED, LATCH_UNKNOWN):
                with self.subTest(start=start, latch=latch):
                    movement = plan(ZONE, _belief(start, latch),
                                    Intent(INTENT_ENTER_TILT))
                    self.assertEqual(PLAN_ENTER, movement.kind)
                    self.assertEqual([100.0, DIP, UPPER], _targets(movement))
                    self.assertEqual([COMMAND_OPEN, COMMAND_POSITION, COMMAND_POSITION],
                                     _commands(movement))
                    self.assertEqual(LATCH_LATCHED, movement.final_latch)

    def test_near_open_edge_continues_to_the_lower_edge(self):
        movement = plan(ZONE, _belief(30.0), Intent(INTENT_ENTER_TILT, landing_virtual=100.0))
        self.assertEqual([100.0, DIP, UPPER, LOWER], _targets(movement))

    def test_a_landing_of_zero_is_the_bare_three_step_sequence(self):
        # Virtual 0 is the closed edge, which is exactly where the latching rise ends.
        movement = plan(ZONE, _belief(80.0),
                        Intent(INTENT_ENTER_TILT, landing_virtual=0.0))
        self.assertEqual([100.0, DIP, UPPER], _targets(movement))

    def test_a_mid_zone_landing_adds_one_in_zone_step(self):
        movement = plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TILT, landing_virtual=50.0))
        self.assertEqual([100.0, DIP, UPPER, 41.0], _targets(movement))
        self.assertEqual(
            [COMMAND_OPEN, COMMAND_POSITION, COMMAND_POSITION, COMMAND_POSITION],
            _commands(movement))
        self.assertEqual(LATCH_LATCHED, movement.final_latch)
        self.assertTrue(ZONE.in_zone(movement.steps[3].target))

    def test_a_landing_that_rounds_to_the_upper_edge_adds_nothing(self):
        # 6% of travel per 100 virtual percent, so virtual 5 is 0.3 real percent -- it rounds to the
        # setpoint the rise already reached, and a command that moves nothing is not worth sending.
        movement = plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TILT, landing_virtual=5.0))
        self.assertEqual([100.0, DIP, UPPER], _targets(movement))

    def test_a_landing_of_a_hundred_is_the_open_edge(self):
        movement = plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TILT, landing_virtual=100.0))
        self.assertEqual([100.0, DIP, UPPER, LOWER], _targets(movement))

    def test_the_dip_is_a_pure_descent_from_fully_open(self):
        # Nothing in the sequence rises into the zone before the dip, so the dip cannot be made
        # while latched no matter where the blind started.
        movement = plan(ZONE, _belief(None, LATCH_UNKNOWN), Intent(INTENT_ENTER_TILT))
        self.assertEqual(COMMAND_OPEN, movement.steps[0].command)
        self.assertLess(movement.steps[1].target, LOWER)


class TestLeaveTilt(unittest.TestCase):

    def test_latched_leaves_by_driving_fully_open(self):
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED), Intent(INTENT_LEAVE_TILT))
        self.assertEqual(PLAN_LEAVE, movement.kind)
        self.assertEqual([COMMAND_OPEN], _commands(movement))
        self.assertEqual([100.0], _commanded(movement))
        self.assertEqual(STEP_RISE_TO_AT_LEAST, movement.steps[0].kind)
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

    def test_the_exit_is_satisfied_at_the_release_height_it_travels_past(self):
        # The blind runs to the top limit, but the step has done its job the moment the reported
        # position clears the height at which the mechanism lets go.
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED), Intent(INTENT_LEAVE_TILT))
        self.assertEqual([RELEASE], _targets(movement))
        self.assertGreater(movement.steps[0].command_position, movement.steps[0].target)
        self.assertTrue(movement.steps[0].satisfied_by(RELEASE))
        self.assertTrue(movement.steps[0].satisfied_by(100.0))
        self.assertFalse(movement.steps[0].satisfied_by(RELEASE - 1.0))

    def test_the_exit_accepts_no_lower_than_a_configured_release_height(self):
        movement = plan(CUSTOM_ZONE, _belief(UPPER, LATCH_LATCHED), Intent(INTENT_LEAVE_TILT))
        self.assertEqual([CUSTOM_RELEASE], _targets(movement))
        self.assertEqual([100.0], _commanded(movement))
        self.assertFalse(movement.steps[0].satisfied_by(CUSTOM_RELEASE - 1.0))

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

    def test_set_position_from_a_band_edge_into_the_band_goes_to_the_far_edge(self):
        # Snapping back to the edge the blind already rests on would move nothing.
        movement = plan(ZONE, _belief(DIP, LATCH_UNLATCHED),
                        Intent(INTENT_SET_POSITION, virtual_pct=37.0))
        self.assertEqual([RELEASE], _targets(movement))
        movement = plan(ZONE, _belief(RELEASE, LATCH_UNLATCHED),
                        Intent(INTENT_SET_POSITION, virtual_pct=45.0))
        self.assertEqual([DIP], _targets(movement))

    def test_a_rise_ending_on_the_release_height_does_not_claim_a_release(self):
        # From below the lower edge the rise latches on the way up and reaches the release height
        # with no margin at all; a position command cannot vouch for the release, so the belief it
        # commits is unknown and the next descent buys the full-open release.
        for start, latch in ((20.0, LATCH_UNLATCHED), (DIP, LATCH_UNLATCHED),
                             (41.0, LATCH_UNKNOWN), (None, LATCH_UNKNOWN)):
            with self.subTest(start=start, latch=latch):
                movement = plan(ZONE, _belief(start, latch),
                                Intent(INTENT_SET_POSITION, virtual_pct=45.0))
                self.assertEqual(RELEASE, movement.steps[-1].target)
                if movement.steps[0].command == COMMAND_OPEN:
                    # An unknown position is guarded, which references the actuator: honest.
                    self.assertEqual(LATCH_UNLATCHED, movement.final_latch)
                else:
                    self.assertEqual(LATCH_UNKNOWN, movement.final_latch)

    def test_a_rise_ending_on_the_release_height_from_a_known_release_at_the_lower_edge_is_clear(
            self):
        # It never crosses the lower edge from below, so it cannot have latched.
        movement = plan(ZONE, _belief(LOWER, LATCH_UNLATCHED),
                        Intent(INTENT_SET_POSITION, virtual_pct=45.0))
        self.assertEqual([RELEASE], _targets(movement))
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

    def test_a_rise_past_the_release_height_commits_a_release(self):
        movement = plan(ZONE, _belief(20.0, LATCH_UNLATCHED),
                        Intent(INTENT_SET_POSITION, virtual_pct=60.0))
        self.assertEqual([60.0], _targets(movement))
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

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
        # The configured step is real travel, so an up step is exactly that far down the blind.
        movement = plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                        Intent(INTENT_SLAT_STEP, direction=DIRECTION_UP))
        self.assertAlmostEqual(UPPER - STEP, movement.steps[0].target)

    def test_step_down_moves_toward_the_closed_edge(self):
        movement = plan(ZONE, _belief(LOWER, LATCH_LATCHED),
                        Intent(INTENT_SLAT_STEP, direction=DIRECTION_DOWN))
        self.assertAlmostEqual(LOWER + STEP, movement.steps[0].target)

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


class TestHeightStep(unittest.TestCase):
    """A step press outside tilt nudges the height by ``height_step_pct``, skipping the band."""

    def _step(self, position, direction, latch=LATCH_UNLATCHED, zone=ZONE):
        return plan(zone, _belief(position, latch), Intent(INTENT_HEIGHT_STEP, direction=direction))

    def test_a_step_is_one_configured_increment(self):
        self.assertEqual([82.0], _targets(self._step(80.0, DIRECTION_UP)))
        self.assertEqual([78.0], _targets(self._step(80.0, DIRECTION_DOWN)))
        self.assertEqual([12.0], _targets(self._step(10.0, DIRECTION_UP)))
        self.assertEqual([8.0], _targets(self._step(10.0, DIRECTION_DOWN)))
        wide = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                    tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP, height_step_pct=5.0)
        self.assertEqual([75.0], _targets(self._step(80.0, DIRECTION_DOWN, zone=wide)))

    def test_a_step_is_a_normal_plan_that_commits_a_release(self):
        movement = self._step(80.0, DIRECTION_DOWN)
        self.assertEqual(PLAN_NORMAL, movement.kind)
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

    def test_a_step_at_a_travel_limit_is_nothing(self):
        self.assertIsNone(self._step(100.0, DIRECTION_UP))
        self.assertIsNone(self._step(0.0, DIRECTION_DOWN))
        self.assertEqual([100.0], _targets(self._step(99.0, DIRECTION_UP)))
        self.assertEqual([0.0], _targets(self._step(1.0, DIRECTION_DOWN)))

    def test_a_step_into_the_band_continues_to_the_edge_ahead(self):
        # Down from just above the band: 47 - 2 = 45 is inside, so the step lands on the bottom.
        self.assertEqual([DIP], _targets(self._step(47.0, DIRECTION_DOWN)))
        # Up from just below: 35 + 2 = 37 is inside, so the step lands on the top.
        self.assertEqual([RELEASE], _targets(self._step(35.0, DIRECTION_UP)))
        # From the band edges themselves, the next step crosses the whole band.
        self.assertEqual([RELEASE], _targets(self._step(DIP, DIRECTION_UP)))
        self.assertEqual([DIP], _targets(self._step(RELEASE, DIRECTION_DOWN)))

    def test_a_step_up_that_crosses_the_lower_edge_commits_no_release(self):
        # Rising from below the lower edge to exactly the release height may leave the mechanism
        # latched; only a step that provably stayed above the lower edge keeps a known release.
        self.assertEqual(LATCH_UNKNOWN, self._step(35.0, DIRECTION_UP).final_latch)
        self.assertEqual(LATCH_UNKNOWN, self._step(DIP, DIRECTION_UP).final_latch)
        self.assertEqual(LATCH_UNKNOWN,
                         self._step(41.0, DIRECTION_UP, latch=LATCH_UNKNOWN).final_latch)
        self.assertEqual(LATCH_UNLATCHED, self._step(RELEASE, DIRECTION_UP).final_latch)

    def test_a_step_down_while_possibly_latched_is_guarded(self):
        movement = self._step(41.0, DIRECTION_DOWN, latch=LATCH_UNKNOWN)
        self.assertEqual([100.0, DIP], _targets(movement))
        self.assertEqual(COMMAND_OPEN, movement.steps[0].command)
        self.assertEqual(LATCH_UNLATCHED, movement.final_latch)

    def test_a_step_up_while_possibly_latched_needs_no_guard(self):
        self.assertEqual([RELEASE], _targets(self._step(41.0, DIRECTION_UP, latch=LATCH_UNKNOWN)))

    def test_no_height_step_while_latched_or_without_a_position(self):
        self.assertIsNone(self._step(41.0, DIRECTION_UP, latch=LATCH_LATCHED))
        self.assertIsNone(self._step(None, DIRECTION_UP, latch=LATCH_UNKNOWN))
        self.assertIsNone(self._step(None, DIRECTION_DOWN, latch=LATCH_UNKNOWN))


class TestCanChangeLatch(unittest.TestCase):

    def test_in_zone_plans_cannot(self):
        self.assertFalse(can_change_latch(ZONE, plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                                                     Intent(INTENT_OPEN))))

    def test_a_step_commanding_outside_the_zone_can(self):
        # The blind travels to the commanded position, so an in-zone target does not make a plan
        # harmless if what it actually sends leaves the zone.
        movement = Plan(PLAN_SLAT,
                        (Step(STEP_MOVE_TO, 41.0, COMMAND_POSITION, command_pct=RELEASE),),
                        LATCH_LATCHED)
        self.assertTrue(can_change_latch(ZONE, movement))

    def test_sequences_crossing_an_edge_can(self):
        self.assertTrue(
            can_change_latch(ZONE, plan(ZONE, _belief(80.0), Intent(INTENT_ENTER_TILT))))
        self.assertTrue(
            can_change_latch(ZONE, plan(ZONE, _belief(UPPER, LATCH_LATCHED),
                                        Intent(INTENT_LEAVE_TILT))))
        self.assertTrue(can_change_latch(ZONE, plan(ZONE, _belief(80.0), Intent(INTENT_CLOSE))))


class TestInvariantsHoldForEveryPlan(unittest.TestCase):
    """Every plan the planner can produce satisfies check_plan, over the whole state space."""

    # Every geometry the config can express in kind: the default one, one whose release height is
    # measured well above the bare clearance (so the band is much wider than the zone), and one
    # whose entry lands mid-zone (so an enter plan has a fourth step that is neither zone edge).
    # The release can go no higher than 99: the band has to stop short of the top limit.
    ZONES = [
        ("default", ZONE),
        ("custom_release_and_landing", CUSTOM_ZONE),
        ("release_just_below_full_travel",
         Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
              tilt_step_pct=STEP, tilt_zone_release_pct=99.0, tilt_enter_landing_pct=LOWER)),
    ]

    @staticmethod
    def _intents(zone):
        intents = [
            Intent(INTENT_OPEN),
            Intent(INTENT_CLOSE),
            Intent(INTENT_ENTER_TILT),
            Intent(INTENT_ENTER_TILT, landing_virtual=zone.enter_landing_virtual),
            Intent(INTENT_LEAVE_TILT),
        ]
        intents += [Intent(INTENT_ENTER_TILT, landing_virtual=float(v)) for v in range(0, 101, 5)]
        intents += [Intent(INTENT_SET_POSITION, virtual_pct=float(v)) for v in range(0, 101, 5)]
        for direction in (DIRECTION_UP, DIRECTION_DOWN):
            intents += [
                Intent(INTENT_LONG_PRESS, direction=direction),
                Intent(INTENT_HEIGHT_STEP, direction=direction),
                Intent(INTENT_SLAT_STEP, direction=direction),
                Intent(INTENT_SLAT_STEP, direction=direction, cross_open_edge=True),
            ]
        return intents

    def test_sweep(self):
        for label, zone in self.ZONES:
            intents = self._intents(zone)
            checked = 0
            for position in [None] + [float(p) for p in range(0, 101)]:
                for latch in (LATCH_LATCHED, LATCH_UNLATCHED, LATCH_UNKNOWN):
                    # A latched mechanism can only rest inside the zone.
                    if latch == LATCH_LATCHED and (position is None or not zone.in_zone(position)):
                        continue
                    belief = _belief(position, latch)
                    for intent in intents:
                        movement = plan(zone, belief, intent)
                        if movement is None:
                            continue
                        checked += 1
                        violation = check_plan(zone, belief, movement)
                        self.assertIsNone(
                            violation,
                            f"{label}: {intent} from {belief}: {violation} ({_targets(movement)})")
            self.assertGreater(checked, 1000, label)


class TestInvariantRejections(unittest.TestCase):
    """check_plan must actually reject the shapes it exists to forbid."""

    def test_n1_rejects_a_normal_target_inside_the_band(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 41.0),), LATCH_UNLATCHED)
        self.assertIn("N1", check_plan(ZONE, _belief(80.0), movement))

    def test_n1_allows_the_band_edges(self):
        for target in (DIP, RELEASE):
            movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, target),), LATCH_UNLATCHED)
            self.assertIsNone(check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_n1_applies_to_a_normal_plan_whatever_belief_it_commits(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 41.0),), LATCH_UNKNOWN)
        self.assertIn("N1", check_plan(ZONE, _belief(80.0), movement))

    def test_r1_rejects_a_release_claimed_by_rising_to_the_release_height(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, RELEASE),), LATCH_UNLATCHED)
        # From below the lower edge the rise latches; from an uncertain belief it may already be.
        self.assertIn("R1", check_plan(ZONE, _belief(20.0, LATCH_UNLATCHED), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(DIP, LATCH_UNLATCHED), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(None, LATCH_UNKNOWN), movement))

    def test_r1_allows_what_cannot_have_crossed_the_lower_edge_latched(self):
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, RELEASE),), LATCH_UNLATCHED)
        # A descent onto the release height, and a rise that started at or above the lower edge
        # from a known release.
        self.assertIsNone(check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))
        self.assertIsNone(check_plan(ZONE, _belief(LOWER, LATCH_UNLATCHED), movement))
        # A rise referenced by a full open first: the mechanism is released by construction.
        guarded = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
                                     Step(STEP_MOVE_TO, RELEASE)), LATCH_UNLATCHED)
        self.assertIsNone(check_plan(ZONE, _belief(20.0, LATCH_UNKNOWN), guarded))
        # And the same rise committing an unknown belief makes no claim at all.
        honest = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, RELEASE),), LATCH_UNKNOWN)
        self.assertIsNone(check_plan(ZONE, _belief(20.0, LATCH_UNLATCHED), honest))

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
        movement = Plan(PLAN_NORMAL, (Step(STEP_MOVE_TO, 80.0),), LATCH_LATCHED)
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

    def test_e1_rejects_a_fourth_step_outside_the_zone(self):
        for target in (DIP, RELEASE, LOWER - 0.1, UPPER + 0.1):
            with self.subTest(target=target):
                movement = Plan(PLAN_ENTER, (
                    Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
                    Step(STEP_MOVE_TO, DIP),
                    Step(STEP_MOVE_TO, UPPER),
                    Step(STEP_MOVE_TO, target),
                ), LATCH_LATCHED)
                self.assertIn("E1", check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_e1_allows_any_in_zone_fourth_step(self):
        # The configured landing is a slat angle, not necessarily an edge.
        for target in (LOWER, 41.0, 43.0, UPPER):
            with self.subTest(target=target):
                movement = Plan(PLAN_ENTER, (
                    Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
                    Step(STEP_MOVE_TO, DIP),
                    Step(STEP_MOVE_TO, UPPER),
                    Step(STEP_MOVE_TO, target),
                ), LATCH_LATCHED)
                self.assertIsNone(check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_e1_rejects_a_fifth_step(self):
        movement = Plan(PLAN_ENTER, (
            Step(STEP_MOVE_TO, 100.0, COMMAND_OPEN),
            Step(STEP_MOVE_TO, DIP),
            Step(STEP_MOVE_TO, UPPER),
            Step(STEP_MOVE_TO, 41.0),
            Step(STEP_MOVE_TO, 42.0),
        ), LATCH_LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80.0, LATCH_UNLATCHED), movement))

    def test_x1_rejects_the_exit_from_an_uncertain_belief(self):
        movement = _leave_plan(RELEASE)
        self.assertIn("X1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))

    def test_x1_rejects_an_exit_that_does_not_clear_the_upper_edge(self):
        movement = _leave_plan(UPPER)
        self.assertIn("X1", check_plan(ZONE, _belief(UPPER, LATCH_LATCHED), movement))

    def test_x1_rejects_an_exit_short_of_a_configured_release_height(self):
        # The bare clearance is no longer enough once the true release height has been measured.
        movement = _leave_plan(RELEASE)
        self.assertIn("X1", check_plan(CUSTOM_ZONE, _belief(UPPER, LATCH_LATCHED), movement))

    def test_x1_rejects_an_exit_that_stops_below_full_travel(self):
        # Parking in the band leaves the slats shut and makes the release depend on the actuator's
        # settling accuracy rather than on its top limit switch.
        movement = Plan(PLAN_LEAVE,
                        (Step(STEP_RISE_TO_AT_LEAST, RELEASE, COMMAND_POSITION,
                              command_pct=RELEASE + 2.0),), LATCH_UNLATCHED)
        violation = check_plan(ZONE, _belief(UPPER, LATCH_LATCHED), movement)
        self.assertIn("X1", violation)
        self.assertIn("fully open", violation)

    def test_x1_rejects_an_exit_that_reaches_full_travel_by_position(self):
        # The open *command* is what runs the actuator against its limit switch; a position command
        # of 100 is still referenced against whatever the actuator believes its travel to be.
        movement = Plan(PLAN_LEAVE,
                        (Step(STEP_RISE_TO_AT_LEAST, RELEASE, COMMAND_POSITION,
                              command_pct=100.0),), LATCH_UNLATCHED)
        self.assertIn("X1", check_plan(ZONE, _belief(UPPER, LATCH_LATCHED), movement))

    def test_x1_allows_the_exit_the_planner_builds(self):
        movement = _leave_plan(RELEASE, command=COMMAND_OPEN, command_pct=100.0)
        self.assertIsNone(check_plan(ZONE, _belief(UPPER, LATCH_LATCHED), movement))

    def test_n1_rejects_a_commanded_position_inside_the_band(self):
        # The hazard is where the blind physically stops, so the commanded position counts too.
        movement = Plan(PLAN_NORMAL,
                        (Step(STEP_MOVE_TO, DIP, COMMAND_POSITION, command_pct=41.0),),
                        LATCH_UNLATCHED)
        self.assertIn("N1", check_plan(ZONE, _belief(80.0), movement))

    def test_t1_rejects_a_commanded_slat_position_outside_the_zone(self):
        movement = Plan(PLAN_SLAT,
                        (Step(STEP_MOVE_TO, 41.0, COMMAND_POSITION, command_pct=30.0),),
                        LATCH_LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41.0, LATCH_LATCHED), movement))

    def test_l1_rejects_a_commanded_descent_below_the_lower_edge(self):
        movement = Plan(PLAN_NORMAL,
                        (Step(STEP_MOVE_TO, RELEASE, COMMAND_POSITION, command_pct=10.0),),
                        LATCH_UNLATCHED)
        self.assertIn("L1", check_plan(ZONE, _belief(41.0, LATCH_UNKNOWN), movement))


if __name__ == "__main__":
    unittest.main()
