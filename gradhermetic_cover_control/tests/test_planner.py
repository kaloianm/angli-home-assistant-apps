import unittest

from gradhermetic_cover_control.cover_types import CommandKind, Direction
from gradhermetic_cover_control.geometry import Zone
from gradhermetic_cover_control.planner import (
    Belief,
    Intent,
    IntentKind,
    Latch,
    Plan,
    PlanKind,
    Step,
    StepKind,
    can_change_latch,
    check_plan,
    plan,
)

# Zone [38, 44], epsilon 2, release 46, step 2 real travel percent -> band [36, 46]. Every
# configured number is a whole percent of real blind travel; span 6 makes the 2% step a third of
# the zone.
UPPER = 44
LOWER = 38
EPSILON = 2
DIP = LOWER - EPSILON
RELEASE = 46

ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
            tilt_zone_release_pct=RELEASE, tilt_zone_enter_pct=UPPER, height_step_pct=2)

# The same zone with a 5% height grid, which leaves the band edges off the grid, so a step from
# outside can land inside the band.
COARSE_STEP_ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                        tilt_zone_epsilon_pct=EPSILON, tilt_zone_release_pct=RELEASE,
                        tilt_zone_enter_pct=UPPER, height_step_pct=5)

# The same zone with a measured release height well above the zone, and an entry that
# finishes at a slightly-open slat angle (real 42, i.e. virtual 33) instead of the closed edge.
# Band = [36, 55].
CUSTOM_RELEASE = 55
CUSTOM_LANDING = 42
CUSTOM_ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                   tilt_zone_epsilon_pct=EPSILON, tilt_zone_release_pct=CUSTOM_RELEASE,
                   tilt_zone_enter_pct=CUSTOM_LANDING, height_step_pct=2)

# Representative starts: above, at the band edges, inside the zone, below, and unknown.
STARTS = (100, 80, 47, RELEASE, 45, UPPER, 41, LOWER, DIP, 30, 0, None)


def _belief(position, latch=Latch.UNLATCHED):
    return Belief(position=position, latch=latch)


def _targets(movement):
    return [step.target for step in movement.steps]


def _commanded(movement):
    return [step.command_position for step in movement.steps]


def _commands(movement):
    return [step.command for step in movement.steps]


def _leave_plan(target, command=CommandKind.POSITION, command_pct=None):
    """A hand-built exit plan, for the X1 checks that need shapes the planner will not produce."""
    step = Step(StepKind.RISE_TO_AT_LEAST, target, command, command_pct=command_pct)
    return Plan(PlanKind.LEAVE, (step, ), Latch.UNLATCHED)


class TestEnterTilt(unittest.TestCase):
    """One canonical sequence from any start: full open, dip, latching rise."""

    def test_same_sequence_from_every_start(self):
        for start in STARTS:
            for latch in (Latch.UNLATCHED, Latch.UNKNOWN):
                with self.subTest(start=start, latch=latch):
                    movement = plan(ZONE, _belief(start, latch), Intent(IntentKind.ENTER_TILT))
                    self.assertEqual(PlanKind.ENTER, movement.kind)
                    self.assertEqual([100, DIP, UPPER], _targets(movement))
                    self.assertEqual([CommandKind.OPEN, CommandKind.POSITION, CommandKind.POSITION],
                                     _commands(movement))
                    self.assertEqual(Latch.LATCHED, movement.final_latch)

    def test_near_open_edge_continues_to_the_lower_edge(self):
        movement = plan(ZONE, _belief(30), Intent(IntentKind.ENTER_TILT, landing_virtual=100))
        self.assertEqual([100, DIP, UPPER, LOWER], _targets(movement))

    def test_a_landing_of_zero_is_the_bare_three_step_sequence(self):
        # Virtual 0 is the closed edge, which is exactly where the latching rise ends.
        movement = plan(ZONE, _belief(80), Intent(IntentKind.ENTER_TILT, landing_virtual=0))
        self.assertEqual([100, DIP, UPPER], _targets(movement))

    def test_a_mid_zone_landing_adds_one_in_zone_step(self):
        movement = plan(ZONE, _belief(80), Intent(IntentKind.ENTER_TILT, landing_virtual=67))
        self.assertEqual([100, DIP, UPPER, 40], _targets(movement))
        self.assertEqual(
            [CommandKind.OPEN, CommandKind.POSITION, CommandKind.POSITION, CommandKind.POSITION],
            _commands(movement))
        self.assertEqual(Latch.LATCHED, movement.final_latch)
        self.assertTrue(ZONE.in_zone(movement.steps[3].target))

    def test_a_landing_that_snaps_to_the_upper_edge_adds_nothing(self):
        # Virtual 5 snaps to slat 0%, here the upper edge, which the rise already reached. A
        # command that moves nothing is not worth sending.
        movement = plan(ZONE, _belief(80), Intent(IntentKind.ENTER_TILT, landing_virtual=5))
        self.assertEqual([100, DIP, UPPER], _targets(movement))

    def test_a_landing_of_a_hundred_is_the_open_edge(self):
        movement = plan(ZONE, _belief(80), Intent(IntentKind.ENTER_TILT, landing_virtual=100))
        self.assertEqual([100, DIP, UPPER, LOWER], _targets(movement))

    def test_the_dip_is_a_pure_descent_from_fully_open(self):
        # Nothing in the sequence rises into the zone before the dip, so the dip cannot be made
        # while latched no matter where the blind started.
        movement = plan(ZONE, _belief(None, Latch.UNKNOWN), Intent(IntentKind.ENTER_TILT))
        self.assertEqual(CommandKind.OPEN, movement.steps[0].command)
        self.assertLess(movement.steps[1].target, LOWER)


class TestLeaveTilt(unittest.TestCase):

    def test_latched_leaves_by_driving_fully_open(self):
        movement = plan(ZONE, _belief(UPPER, Latch.LATCHED), Intent(IntentKind.LEAVE_TILT))
        self.assertEqual(PlanKind.LEAVE, movement.kind)
        self.assertEqual([CommandKind.OPEN], _commands(movement))
        self.assertEqual([100], _commanded(movement))
        self.assertEqual(StepKind.RISE_TO_AT_LEAST, movement.steps[0].kind)
        self.assertEqual(Latch.UNLATCHED, movement.final_latch)

    def test_the_exit_is_satisfied_at_the_release_height_it_travels_past(self):
        # The blind runs to the top limit, but the step has done its job the moment the reported
        # position clears the height at which the mechanism lets go.
        movement = plan(ZONE, _belief(UPPER, Latch.LATCHED), Intent(IntentKind.LEAVE_TILT))
        self.assertEqual([RELEASE], _targets(movement))
        self.assertGreater(movement.steps[0].command_position, movement.steps[0].target)
        self.assertEqual(StepKind.RISE_TO_AT_LEAST, movement.steps[0].kind)

    def test_the_exit_accepts_no_lower_than_a_configured_release_height(self):
        movement = plan(CUSTOM_ZONE, _belief(UPPER, Latch.LATCHED), Intent(IntentKind.LEAVE_TILT))
        self.assertEqual([CUSTOM_RELEASE], _targets(movement))
        self.assertEqual([100], _commanded(movement))

    def test_uncertain_belief_has_nothing_to_leave(self):
        for latch in (Latch.UNLATCHED, Latch.UNKNOWN):
            self.assertIsNone(plan(ZONE, _belief(41, latch), Intent(IntentKind.LEAVE_TILT)))


class TestWholeHeight(unittest.TestCase):

    def test_open_drives_fully_open_with_the_open_command(self):
        movement = plan(ZONE, _belief(50), Intent(IntentKind.OPEN))
        self.assertEqual(PlanKind.NORMAL, movement.kind)
        self.assertEqual([100], _targets(movement))
        self.assertEqual([CommandKind.OPEN], _commands(movement))

    def test_close_from_a_known_release_descends_directly(self):
        movement = plan(ZONE, _belief(80, Latch.UNLATCHED), Intent(IntentKind.CLOSE))
        self.assertEqual([0], _targets(movement))
        self.assertEqual([CommandKind.CLOSE], _commands(movement))

    def test_close_right_after_leaving_tilt_descends_directly(self):
        # Resting at the release target, known unlatched: no full-open detour, no no-op waypoint.
        movement = plan(ZONE, _belief(RELEASE, Latch.UNLATCHED), Intent(IntentKind.CLOSE))
        self.assertEqual([0], _targets(movement))

    def test_close_from_an_uncertain_belief_releases_by_opening_fully(self):
        movement = plan(ZONE, _belief(41, Latch.UNKNOWN), Intent(IntentKind.CLOSE))
        self.assertEqual([100, 0], _targets(movement))
        self.assertEqual([CommandKind.OPEN, CommandKind.CLOSE], _commands(movement))

    def test_close_with_an_unknown_position_releases_by_opening_fully(self):
        movement = plan(ZONE, _belief(None, Latch.UNKNOWN), Intent(IntentKind.CLOSE))
        self.assertEqual([100, 0], _targets(movement))

    def test_set_position_upward_needs_no_release(self):
        movement = plan(ZONE, _belief(41, Latch.UNKNOWN),
                        Intent(IntentKind.SET_POSITION, virtual_pct=90))
        self.assertEqual([90], _targets(movement))

    def test_set_position_downward_from_an_uncertain_belief_releases_first(self):
        movement = plan(ZONE, _belief(41, Latch.UNKNOWN),
                        Intent(IntentKind.SET_POSITION, virtual_pct=10))
        self.assertEqual([100, 10], _targets(movement))
        self.assertEqual(CommandKind.OPEN, movement.steps[0].command)

    def test_set_position_downward_from_a_known_release_descends_directly(self):
        movement = plan(ZONE, _belief(80, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=10))
        self.assertEqual([10], _targets(movement))

    def test_set_position_inside_the_band_snaps_to_an_edge(self):
        movement = plan(ZONE, _belief(80, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=37))
        self.assertEqual([DIP], _targets(movement))
        movement = plan(ZONE, _belief(80, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=45))
        self.assertEqual([RELEASE], _targets(movement))

    def test_set_position_to_the_current_position_is_not_a_descent(self):
        # Equal targets must not trip the descent guard; the executor skips the step instead.
        movement = plan(ZONE, _belief(50, Latch.UNKNOWN),
                        Intent(IntentKind.SET_POSITION, virtual_pct=50))
        self.assertEqual([50], _targets(movement))

    def test_set_position_from_a_band_edge_into_the_band_goes_to_the_far_edge(self):
        # Snapping back to the edge the blind already rests on would move nothing.
        movement = plan(ZONE, _belief(DIP, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=37))
        self.assertEqual([RELEASE], _targets(movement))
        movement = plan(ZONE, _belief(RELEASE, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=45))
        self.assertEqual([DIP], _targets(movement))

    def test_a_rise_ending_on_the_release_height_does_not_claim_a_release(self):
        # From below the lower edge the rise latches on the way up and reaches the release height
        # with no margin at all; a position command cannot vouch for the release, so the belief it
        # commits is unknown and the next descent buys the full-open release.
        for start, latch in ((20, Latch.UNLATCHED), (DIP, Latch.UNLATCHED), (41, Latch.UNKNOWN),
                             (None, Latch.UNKNOWN)):
            with self.subTest(start=start, latch=latch):
                movement = plan(ZONE, _belief(start, latch),
                                Intent(IntentKind.SET_POSITION, virtual_pct=45))
                self.assertEqual(RELEASE, movement.steps[-1].target)
                if movement.steps[0].command == CommandKind.OPEN:
                    # An unknown position is guarded, which references the actuator: honest.
                    self.assertEqual(Latch.UNLATCHED, movement.final_latch)
                else:
                    self.assertEqual(Latch.UNKNOWN, movement.final_latch)

    def test_a_rise_ending_on_the_release_height_from_a_known_release_at_the_lower_edge_is_clear(
            self):
        # It never crosses the lower edge from below, so it cannot have latched.
        movement = plan(ZONE, _belief(LOWER, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=45))
        self.assertEqual([RELEASE], _targets(movement))
        self.assertEqual(Latch.UNLATCHED, movement.final_latch)

    def test_a_rise_past_the_release_height_commits_a_release(self):
        movement = plan(ZONE, _belief(20, Latch.UNLATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=60))
        self.assertEqual([60], _targets(movement))
        self.assertEqual(Latch.UNLATCHED, movement.final_latch)


class TestSlatMoves(unittest.TestCase):

    def test_open_and_close_become_in_zone_slat_moves(self):
        latched = _belief(UPPER, Latch.LATCHED)
        self.assertEqual([LOWER], _targets(plan(ZONE, latched, Intent(IntentKind.OPEN))))
        self.assertEqual([UPPER], _targets(plan(ZONE, latched, Intent(IntentKind.CLOSE))))
        self.assertEqual(PlanKind.SLAT, plan(ZONE, latched, Intent(IntentKind.OPEN)).kind)

    def test_set_position_snaps_to_the_nearest_slat_position(self):
        movement = plan(ZONE, _belief(UPPER, Latch.LATCHED),
                        Intent(IntentKind.SET_POSITION, virtual_pct=50))
        self.assertEqual(PlanKind.SLAT, movement.kind)
        self.assertEqual(42, movement.steps[0].target)

    def test_step_up_moves_toward_the_open_edge(self):
        # Slat positions are (44, 42, 40, 38); up (more light) is real-downward.
        movement = plan(ZONE, _belief(UPPER, Latch.LATCHED),
                        Intent(IntentKind.SLAT_STEP, direction=Direction.UP))
        self.assertEqual(42, movement.steps[0].target)

    def test_step_down_moves_toward_the_closed_edge(self):
        movement = plan(ZONE, _belief(LOWER, Latch.LATCHED),
                        Intent(IntentKind.SLAT_STEP, direction=Direction.DOWN))
        self.assertEqual(40, movement.steps[0].target)

    def test_a_step_from_between_positions_goes_to_the_next_one_ahead(self):
        for direction, expected in ((Direction.UP, 40), (Direction.DOWN, 42)):
            with self.subTest(direction=direction):
                movement = plan(ZONE, _belief(41, Latch.LATCHED),
                                Intent(IntentKind.SLAT_STEP, direction=direction))
                self.assertEqual(expected, movement.steps[0].target)

    def test_step_down_at_the_closed_edge_clamps(self):
        self.assertIsNone(
            plan(ZONE, _belief(UPPER, Latch.LATCHED),
                 Intent(IntentKind.SLAT_STEP, direction=Direction.DOWN)))

    def test_step_up_at_the_open_edge_clamps(self):
        self.assertIsNone(
            plan(ZONE, _belief(LOWER, Latch.LATCHED),
                 Intent(IntentKind.SLAT_STEP, direction=Direction.UP)))

    def test_no_slat_move_without_a_latch_belief(self):
        for latch in (Latch.UNLATCHED, Latch.UNKNOWN):
            self.assertIsNone(
                plan(ZONE, _belief(41, latch), Intent(IntentKind.SLAT_STEP,
                                                      direction=Direction.UP)))

    def test_no_slat_move_without_a_position(self):
        self.assertIsNone(
            plan(ZONE, _belief(None, Latch.LATCHED),
                 Intent(IntentKind.SLAT_STEP, direction=Direction.UP)))


class TestHeightStep(unittest.TestCase):
    """A step outside tilt moves the height to the next multiple of ``height_step_pct``, skipping
    the band."""

    def _step(self, position, direction, latch=Latch.UNLATCHED, zone=ZONE):
        return plan(zone, _belief(position, latch),
                    Intent(IntentKind.HEIGHT_STEP, direction=direction))

    def test_a_step_is_one_configured_increment(self):
        self.assertEqual([82], _targets(self._step(80, Direction.UP)))
        self.assertEqual([78], _targets(self._step(80, Direction.DOWN)))
        self.assertEqual([12], _targets(self._step(10, Direction.UP)))
        self.assertEqual([8], _targets(self._step(10, Direction.DOWN)))
        self.assertEqual([75], _targets(self._step(80, Direction.DOWN, zone=COARSE_STEP_ZONE)))

    def test_a_step_from_off_the_grid_lands_on_the_next_multiple(self):
        cases = ((67, Direction.UP, 70), (67, Direction.DOWN, 65), (65, Direction.DOWN, 60),
                 (65, Direction.UP, 70), (98, Direction.UP, 100), (3, Direction.DOWN, 0))
        for start, direction, expected in cases:
            with self.subTest(start=start, direction=direction):
                self.assertEqual([expected],
                                 _targets(self._step(start, direction, zone=COARSE_STEP_ZONE)))
        self.assertEqual([82], _targets(self._step(81, Direction.UP)))
        self.assertEqual([80], _targets(self._step(81, Direction.DOWN)))

    def test_a_step_is_a_normal_plan_that_commits_a_release(self):
        movement = self._step(80, Direction.DOWN)
        self.assertEqual(PlanKind.NORMAL, movement.kind)
        self.assertEqual(Latch.UNLATCHED, movement.final_latch)

    def test_a_step_at_a_travel_limit_is_nothing(self):
        self.assertIsNone(self._step(100, Direction.UP))
        self.assertIsNone(self._step(0, Direction.DOWN))
        self.assertEqual([100], _targets(self._step(99, Direction.UP)))
        self.assertEqual([0], _targets(self._step(1, Direction.DOWN)))

    def test_a_step_into_the_band_continues_to_the_edge_ahead(self):
        # Down from above the band: 45 is inside, so the step lands on the bottom.
        self.assertEqual([DIP], _targets(self._step(50, Direction.DOWN, zone=COARSE_STEP_ZONE)))
        # Up from below: 40 is inside, so the step lands on the top.
        self.assertEqual([RELEASE], _targets(self._step(35, Direction.UP, zone=COARSE_STEP_ZONE)))
        # A band edge on the grid is an ordinary landing.
        self.assertEqual([RELEASE], _targets(self._step(47, Direction.DOWN)))
        self.assertEqual([DIP], _targets(self._step(35, Direction.UP)))
        # From the band edges themselves, the next step crosses the whole band.
        self.assertEqual([RELEASE], _targets(self._step(DIP, Direction.UP)))
        self.assertEqual([DIP], _targets(self._step(RELEASE, Direction.DOWN)))

    def test_a_step_up_that_crosses_the_lower_edge_commits_no_release(self):
        # Rising from below the lower edge to exactly the release height may leave the mechanism
        # latched; only a step that provably stayed above the lower edge keeps a known release.
        self.assertEqual(Latch.UNKNOWN,
                         self._step(35, Direction.UP, zone=COARSE_STEP_ZONE).final_latch)
        self.assertEqual(Latch.UNKNOWN, self._step(DIP, Direction.UP).final_latch)
        self.assertEqual(Latch.UNKNOWN,
                         self._step(41, Direction.UP, latch=Latch.UNKNOWN).final_latch)
        self.assertEqual(Latch.UNLATCHED, self._step(RELEASE, Direction.UP).final_latch)

    def test_a_step_down_while_possibly_latched_is_guarded(self):
        movement = self._step(41, Direction.DOWN, latch=Latch.UNKNOWN)
        self.assertEqual([100, DIP], _targets(movement))
        self.assertEqual(CommandKind.OPEN, movement.steps[0].command)
        self.assertEqual(Latch.UNLATCHED, movement.final_latch)

    def test_a_step_up_while_possibly_latched_needs_no_guard(self):
        self.assertEqual([RELEASE], _targets(self._step(41, Direction.UP, latch=Latch.UNKNOWN)))

    def test_no_height_step_while_latched_or_without_a_position(self):
        self.assertIsNone(self._step(41, Direction.UP, latch=Latch.LATCHED))
        self.assertIsNone(self._step(None, Direction.UP, latch=Latch.UNKNOWN))
        self.assertIsNone(self._step(None, Direction.DOWN, latch=Latch.UNKNOWN))


class TestCanChangeLatch(unittest.TestCase):

    def test_in_zone_plans_cannot(self):
        self.assertFalse(
            can_change_latch(ZONE, plan(ZONE, _belief(UPPER, Latch.LATCHED),
                                        Intent(IntentKind.OPEN))))

    def test_a_step_commanding_outside_the_zone_can(self):
        # The blind travels to the commanded position, so an in-zone target does not make a plan
        # harmless if what it actually sends leaves the zone.
        movement = Plan(PlanKind.SLAT,
                        (Step(StepKind.MOVE_TO, 41, CommandKind.POSITION, command_pct=RELEASE), ),
                        Latch.LATCHED)
        self.assertTrue(can_change_latch(ZONE, movement))

    def test_sequences_crossing_an_edge_can(self):
        self.assertTrue(
            can_change_latch(ZONE, plan(ZONE, _belief(80), Intent(IntentKind.ENTER_TILT))))
        self.assertTrue(
            can_change_latch(
                ZONE, plan(ZONE, _belief(UPPER, Latch.LATCHED), Intent(IntentKind.LEAVE_TILT))))
        self.assertTrue(can_change_latch(ZONE, plan(ZONE, _belief(80), Intent(IntentKind.CLOSE))))


class TestInvariantsHoldForEveryPlan(unittest.TestCase):
    """Every plan the planner can produce satisfies check_plan, over the whole state space."""

    # Every geometry the config can express in kind: the base one, one whose release height is
    # measured well above the zone (so the band is much wider than the zone), and one
    # whose entry lands mid-zone (so an enter plan has a fourth step that is neither zone edge).
    # The release can go no higher than 99: the band has to stop short of the top limit.
    ZONES = [
        ("default", ZONE),
        ("custom_release_and_landing", CUSTOM_ZONE),
        ("release_just_below_full_travel",
         Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
              tilt_zone_release_pct=99, tilt_zone_enter_pct=LOWER + 3, height_step_pct=2)),
    ]

    @staticmethod
    def _intents(zone):
        intents = [
            Intent(IntentKind.OPEN),
            Intent(IntentKind.CLOSE),
            Intent(IntentKind.ENTER_TILT),
            Intent(IntentKind.ENTER_TILT, landing_virtual=zone.enter_landing_virtual),
            Intent(IntentKind.LEAVE_TILT),
        ]
        intents += [Intent(IntentKind.ENTER_TILT, landing_virtual=v) for v in range(0, 101, 5)]
        intents += [Intent(IntentKind.SET_POSITION, virtual_pct=v) for v in range(0, 101, 5)]
        for direction in (Direction.UP, Direction.DOWN):
            intents += [
                Intent(IntentKind.HEIGHT_STEP, direction=direction),
                Intent(IntentKind.SLAT_STEP, direction=direction),
            ]
        return intents

    def test_sweep(self):
        for label, zone in self.ZONES:
            intents = self._intents(zone)
            checked = 0
            for position in [None] + list(range(0, 101)):
                for latch in (Latch.LATCHED, Latch.UNLATCHED, Latch.UNKNOWN):
                    # A latched mechanism can only rest inside the zone.
                    if latch == Latch.LATCHED and (position is None or not zone.in_zone(position)):
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
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 41), ), Latch.UNLATCHED)
        self.assertIn("N1", check_plan(ZONE, _belief(80), movement))

    def test_n1_allows_the_band_edges(self):
        for target in (DIP, RELEASE):
            movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, target), ), Latch.UNLATCHED)
            self.assertIsNone(check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_n1_applies_to_a_normal_plan_whatever_belief_it_commits(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 41), ), Latch.UNKNOWN)
        self.assertIn("N1", check_plan(ZONE, _belief(80), movement))

    def test_r1_rejects_a_release_claimed_by_rising_to_the_release_height(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, RELEASE), ), Latch.UNLATCHED)
        # From below the lower edge the rise latches; from an uncertain belief it may already be.
        self.assertIn("R1", check_plan(ZONE, _belief(20, Latch.UNLATCHED), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(DIP, Latch.UNLATCHED), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(41, Latch.UNKNOWN), movement))
        self.assertIn("R1", check_plan(ZONE, _belief(None, Latch.UNKNOWN), movement))

    def test_r1_allows_what_cannot_have_crossed_the_lower_edge_latched(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, RELEASE), ), Latch.UNLATCHED)
        # A descent onto the release height, and a rise that started at or above the lower edge
        # from a known release.
        self.assertIsNone(check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))
        self.assertIsNone(check_plan(ZONE, _belief(LOWER, Latch.UNLATCHED), movement))
        # A rise referenced by a full open first: the mechanism is released by construction.
        guarded = Plan(
            PlanKind.NORMAL,
            (Step(StepKind.MOVE_TO, 100, CommandKind.OPEN), Step(StepKind.MOVE_TO, RELEASE)),
            Latch.UNLATCHED)
        self.assertIsNone(check_plan(ZONE, _belief(20, Latch.UNKNOWN), guarded))
        # And the same rise committing an unknown belief makes no claim at all.
        honest = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, RELEASE), ), Latch.UNKNOWN)
        self.assertIsNone(check_plan(ZONE, _belief(20, Latch.UNLATCHED), honest))

    def test_t1_rejects_a_slat_move_without_a_latch_belief(self):
        movement = Plan(PlanKind.SLAT, (Step(StepKind.MOVE_TO, 41), ), Latch.LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41, Latch.UNKNOWN), movement))

    def test_t1_rejects_a_slat_target_outside_the_zone(self):
        movement = Plan(PlanKind.SLAT, (Step(StepKind.MOVE_TO, 30), ), Latch.LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41, Latch.LATCHED), movement))

    def test_l1_rejects_an_unguarded_descent_from_an_uncertain_belief(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 0, CommandKind.CLOSE), ),
                        Latch.UNLATCHED)
        self.assertIn("L1", check_plan(ZONE, _belief(41, Latch.UNKNOWN), movement))
        self.assertIn("L1", check_plan(ZONE, _belief(41, Latch.LATCHED), movement))

    def test_l1_allows_a_descent_from_a_known_release(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 0, CommandKind.CLOSE), ),
                        Latch.UNLATCHED)
        self.assertIsNone(check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_e1_rejects_latching_outside_the_canonical_sequence(self):
        movement = Plan(PlanKind.NORMAL, (Step(StepKind.MOVE_TO, 80), ), Latch.LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(41, Latch.UNLATCHED), movement))

    def test_e1_rejects_an_enter_sequence_that_does_not_start_fully_open(self):
        # README's old from-above entry: dip straight down without re-referencing at the top.
        movement = Plan(PlanKind.ENTER, (
            Step(StepKind.MOVE_TO, RELEASE),
            Step(StepKind.MOVE_TO, DIP),
            Step(StepKind.MOVE_TO, UPPER),
        ), Latch.LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_e1_rejects_a_dip_that_does_not_clear_the_lower_edge(self):
        movement = Plan(PlanKind.ENTER, (
            Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
            Step(StepKind.MOVE_TO, LOWER),
            Step(StepKind.MOVE_TO, UPPER),
        ), Latch.LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_e1_rejects_a_fourth_step_outside_the_zone(self):
        for target in (DIP, RELEASE, LOWER - 1, UPPER + 1):
            with self.subTest(target=target):
                movement = Plan(PlanKind.ENTER, (
                    Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
                    Step(StepKind.MOVE_TO, DIP),
                    Step(StepKind.MOVE_TO, UPPER),
                    Step(StepKind.MOVE_TO, target),
                ), Latch.LATCHED)
                self.assertIn("E1", check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_e1_allows_any_in_zone_fourth_step(self):
        # The configured landing is a slat angle, not necessarily an edge.
        for target in (LOWER, 41, 43, UPPER):
            with self.subTest(target=target):
                movement = Plan(PlanKind.ENTER, (
                    Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
                    Step(StepKind.MOVE_TO, DIP),
                    Step(StepKind.MOVE_TO, UPPER),
                    Step(StepKind.MOVE_TO, target),
                ), Latch.LATCHED)
                self.assertIsNone(check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_e1_rejects_a_fifth_step(self):
        movement = Plan(PlanKind.ENTER, (
            Step(StepKind.MOVE_TO, 100, CommandKind.OPEN),
            Step(StepKind.MOVE_TO, DIP),
            Step(StepKind.MOVE_TO, UPPER),
            Step(StepKind.MOVE_TO, 41),
            Step(StepKind.MOVE_TO, 42),
        ), Latch.LATCHED)
        self.assertIn("E1", check_plan(ZONE, _belief(80, Latch.UNLATCHED), movement))

    def test_x1_rejects_the_exit_from_an_uncertain_belief(self):
        movement = _leave_plan(RELEASE)
        self.assertIn("X1", check_plan(ZONE, _belief(41, Latch.UNKNOWN), movement))

    def test_x1_rejects_an_exit_that_does_not_clear_the_upper_edge(self):
        movement = _leave_plan(UPPER)
        self.assertIn("X1", check_plan(ZONE, _belief(UPPER, Latch.LATCHED), movement))

    def test_x1_rejects_an_exit_short_of_a_configured_release_height(self):
        # The base fixture's release height is not enough on a blind that releases higher up.
        movement = _leave_plan(RELEASE)
        self.assertIn("X1", check_plan(CUSTOM_ZONE, _belief(UPPER, Latch.LATCHED), movement))

    def test_x1_rejects_an_exit_that_stops_below_full_travel(self):
        # Parking in the band leaves the slats shut and makes the release depend on the actuator's
        # settling accuracy rather than on its top limit switch.
        movement = Plan(PlanKind.LEAVE, (Step(StepKind.RISE_TO_AT_LEAST, RELEASE,
                                              CommandKind.POSITION, command_pct=RELEASE + 2), ),
                        Latch.UNLATCHED)
        violation = check_plan(ZONE, _belief(UPPER, Latch.LATCHED), movement)
        self.assertIn("X1", violation)
        self.assertIn("fully open", violation)

    def test_x1_rejects_an_exit_that_reaches_full_travel_by_position(self):
        # The open *command* is what runs the actuator against its limit switch; a position command
        # of 100 is still referenced against whatever the actuator believes its travel to be.
        movement = Plan(
            PlanKind.LEAVE,
            (Step(StepKind.RISE_TO_AT_LEAST, RELEASE, CommandKind.POSITION, command_pct=100), ),
            Latch.UNLATCHED)
        self.assertIn("X1", check_plan(ZONE, _belief(UPPER, Latch.LATCHED), movement))

    def test_x1_allows_the_exit_the_planner_builds(self):
        movement = _leave_plan(RELEASE, command=CommandKind.OPEN, command_pct=100)
        self.assertIsNone(check_plan(ZONE, _belief(UPPER, Latch.LATCHED), movement))

    def test_n1_rejects_a_commanded_position_inside_the_band(self):
        # The hazard is where the blind physically stops, so the commanded position counts too.
        movement = Plan(PlanKind.NORMAL,
                        (Step(StepKind.MOVE_TO, DIP, CommandKind.POSITION, command_pct=41), ),
                        Latch.UNLATCHED)
        self.assertIn("N1", check_plan(ZONE, _belief(80), movement))

    def test_t1_rejects_a_commanded_slat_position_outside_the_zone(self):
        movement = Plan(PlanKind.SLAT,
                        (Step(StepKind.MOVE_TO, 41, CommandKind.POSITION, command_pct=30), ),
                        Latch.LATCHED)
        self.assertIn("T1", check_plan(ZONE, _belief(41, Latch.LATCHED), movement))

    def test_l1_rejects_a_commanded_descent_below_the_lower_edge(self):
        movement = Plan(PlanKind.NORMAL,
                        (Step(StepKind.MOVE_TO, RELEASE, CommandKind.POSITION, command_pct=10), ),
                        Latch.UNLATCHED)
        self.assertIn("L1", check_plan(ZONE, _belief(41, Latch.UNKNOWN), movement))


if __name__ == "__main__":
    unittest.main()
