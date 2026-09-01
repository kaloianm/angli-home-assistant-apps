"""
Model-based tests: the whole app driven against a ground-truth simulator of the blind.

Positions are whole percent, so the state space is small enough to enumerate rather than sample.
Every run asserts the same properties:

- the simulator recorded no violation -- the blind was never asked to travel below the lower zone
  edge while latched. This is the safety proof;
- the app's belief is never more confident than the truth (it may be conservatively unsure, never
  wrongly sure);
- the app's position belief equals what the actuator reports, and the published virtual position is
  the spec mapping of it;
- the plan completed *without the settle timer firing* -- nominal flows must never depend on the
  45-second fallback, which is the regression gate for the old no-op-waypoint stall;
- the timer is left disarmed, and the number of real-cover commands is bounded.
"""

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
    virtual_position,
)
from gradhermetic_cover_control.geometry import Zone, to_command
from gradhermetic_cover_control.logic import GradhermeticCoverLogic
from gradhermetic_cover_control.planner import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    EXIT_OVERSHOOT_PCT,
    LATCH_UNLATCHED,
)
from gradhermetic_cover_control.tests.simulator import BlindSimulator, Quirks

UPPER = 44.0
LOWER = 38.0
EPSILON = 2.0
STEP = 20.0
DIP = LOWER - EPSILON
RELEASE = UPPER + EPSILON

ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
            tilt_step_pct=STEP)

# The same mechanism on a blind whose latch only genuinely lets go well above the bare clearance,
# and whose slats are not visible until a little way open. Band = [36, 55].
CUSTOM_RELEASE = 55.0
CUSTOM_LANDING = 40.0
CUSTOM_ZONE = Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER,
                   tilt_zone_epsilon_pct=EPSILON, tilt_step_pct=STEP,
                   tilt_zone_release_pct=CUSTOM_RELEASE, tilt_enter_landing_pct=CUSTOM_LANDING)

# Enough ticks for the longest sequence (a full open plus a dip plus a rise) with room to spare.
MAX_TICKS = 1000
# An intent may not command the real cover more than this. The enter sequence is the longest at
# four; anything more means a replan loop.
MAX_COMMANDS_PER_INTENT = 8


class Harness:
    """
    One app instance wired to one simulated blind, plus the side effects the adapter would perform.
    """

    def __init__(self, position=100.0, latched=False, quirks=None, stride=1.0, zone=ZONE):
        """
        Create an app and a blind resting at ``position``.
        """
        self.zone = zone
        self.sim = BlindSimulator(zone, position=position, latched=latched, quirks=quirks,
                                  stride=stride)
        self.logic = GradhermeticCoverLogic(zone)
        self.published = []
        self.notifications = []
        self.timer_armed = False
        self.timer_firings = 0
        self.commands = 0

    # -- Driving -----------------------------------------------------------------------------------

    def run(self, actions=()):
        """
        Perform ``actions`` and drive the blind until it comes to rest.
        """
        self.apply(actions)
        self._drain()

    def run_partial(self, actions, reports):
        """
        Perform ``actions`` and deliver at most ``reports`` position updates, leaving the rest.
        """
        self.apply(actions)
        delivered = 0
        for _ in range(MAX_TICKS):
            if not self.sim.busy or delivered >= reports:
                return delivered
            for report in self.sim.tick():
                self.apply(self.logic.on_real_position(report.position, report.is_moving))
                delivered += 1
        raise AssertionError("the blind never came to rest")

    def fire_timer(self):
        """
        Fire the settle timer, reading the controller state exactly as the adapter would.
        """
        self.timer_armed = False
        self.timer_firings += 1
        self.apply(self.logic.on_settle_timer(float(self.sim.reported), self.sim.is_moving))
        self._drain()

    def go_unavailable(self):
        """
        Report the cover as unavailable, as Home Assistant would when it drops off the bus.
        """
        self.apply(self.logic.on_real_position(None, False))

    def restart(self):
        """
        Replace the app with a freshly started one, as an AppDaemon restart would.
        """
        self.logic = GradhermeticCoverLogic(self.zone)
        self.timer_armed = False
        self.published = []
        self.run(self.logic.on_startup(float(self.sim.reported), self.sim.is_moving))

    def _drain(self):
        """
        Deliver reports until the blind is at rest and has nothing left to say.
        """
        for _ in range(MAX_TICKS):
            if not self.sim.busy:
                return
            for report in self.sim.tick():
                self.apply(self.logic.on_real_position(report.position, report.is_moving))
        raise AssertionError("the blind never came to rest")

    def apply(self, actions):
        """
        Perform the adapter's side of one action list.
        """
        for action in actions:
            if action.kind == ACTION_MOVE_TO:
                self.commands += 1
                self.sim.set_position(to_command(action.position))
            elif action.kind == ACTION_OPEN_FULL:
                self.commands += 1
                self.sim.open_cover()
            elif action.kind == ACTION_CLOSE_FULL:
                self.commands += 1
                self.sim.close_cover()
            elif action.kind == ACTION_STOP:
                self.commands += 1
                self.sim.stop_cover()
            elif action.kind == ACTION_PUBLISH_POSITION:
                self.published.append(action.position)
            elif action.kind == ACTION_ARM_SETTLE_TIMER:
                self.timer_armed = True
            elif action.kind == ACTION_CANCEL_SETTLE_TIMER:
                self.timer_armed = False
            elif action.kind == ACTION_NOTIFY:
                self.notifications.append(action)


# -- Start states ------------------------------------------------------------------------------


def fresh(position, **kwargs):
    """
    A freshly started app whose belief comes from one position reading, on an unlatched blind.
    """
    harness = Harness(position=float(position), **kwargs)
    harness.logic.seed_state(float(position))
    return harness


def latched_at(position, **kwargs):
    """
    An app and a blind driven into a genuine latched state with the slats at ``position``.
    """
    harness = Harness(position=100.0, **kwargs)
    harness.logic.seed_state(100.0)
    harness.run(harness.logic.on_set_tilt_mode(True))
    # Entry finishes at the configured landing, which is not necessarily the closed edge.
    if to_command(position) != harness.sim.reported:
        harness.run(harness.logic.on_set_position(harness.zone.real_to_virtual(float(position))))
    return harness


def interrupted_enter(stop_at, **kwargs):
    """
    An enter sequence stopped part-way up its latching rise: the classic ambiguous state.
    """
    harness = Harness(position=100.0, **kwargs)
    harness.logic.seed_state(100.0)
    # Already fully open, so the first command is the dip.
    harness.apply(harness.logic.on_set_tilt_mode(True))
    _drive_until(harness, lambda: harness.sim.physical <= harness.zone.dip_target + 1e-9)
    _drive_until(harness, lambda: harness.sim.physical >= float(stop_at) - 1e-9)
    harness.run(harness.logic.on_stop())
    return harness


def after_leaving_tilt(**kwargs):
    """
    Resting just above the release target, known released -- the state the old code stalled in.
    """
    harness = latched_at(UPPER, **kwargs)
    harness.run(harness.logic.on_set_tilt_mode(False))
    return harness


def _drive_until(harness, predicate):
    """
    Deliver reports until ``predicate`` holds or the blind stops.
    """
    for _ in range(MAX_TICKS):
        if predicate() or not harness.sim.busy:
            return
        for report in harness.sim.tick():
            harness.apply(harness.logic.on_real_position(report.position, report.is_moving))
    raise AssertionError("the blind never reached the requested point")


# -- Intents -----------------------------------------------------------------------------------

INTENTS = [
    ("open", lambda logic: logic.on_open()),
    ("close", lambda logic: logic.on_close()),
    ("stop", lambda logic: logic.on_stop()),
    ("tilt_on", lambda logic: logic.on_set_tilt_mode(True)),
    ("tilt_off", lambda logic: logic.on_set_tilt_mode(False)),
    ("slat_up", lambda logic: logic.on_slat_step(DIRECTION_UP)),
    ("slat_down", lambda logic: logic.on_slat_step(DIRECTION_DOWN)),
    ("short_up", lambda logic: logic.on_knx_short(DIRECTION_UP)),
    ("short_down", lambda logic: logic.on_knx_short(DIRECTION_DOWN)),
    ("long_up", lambda logic: logic.on_knx_long(DIRECTION_UP)),
    ("long_down", lambda logic: logic.on_knx_long(DIRECTION_DOWN)),
    ("recover", lambda logic: logic.on_recover()),
] + [(f"set_position_{value}", (lambda v: lambda logic: logic.on_set_position(float(v)))(value))
     for value in (0, 25, 37, 41, 45, 50, 75, 100)]


class ModelTestCase(unittest.TestCase):
    """
    Shared assertions about a run that has come to rest.
    """

    def assert_no_violation(self, harness):
        """
        The mechanism was never asked to descend while latched.
        """
        self.assertEqual([], harness.sim.violations)

    def assert_belief_is_sound(self, harness):
        """
        The app is never more confident than the truth.
        """
        if harness.logic.in_tilt:
            self.assertTrue(harness.sim.latched,
                            "the app believes it is latched but the mechanism is not")
        if harness.logic.latch == LATCH_UNLATCHED:
            self.assertFalse(harness.sim.latched,
                             "the app believes it is released but the mechanism is latched")

    def assert_at_rest(self, harness):
        """
        The plan finished, the timer is disarmed, and the published position matches the blind.
        """
        self.assertFalse(harness.logic.has_pending_plan, "a plan is still pending")
        self.assertFalse(harness.timer_armed, "the settle timer was left armed")
        self.assertEqual(harness.sim.reported, to_command(harness.logic.last_position))
        if harness.published:
            expected = virtual_position(harness.zone, harness.logic.latch,
                                        harness.logic.last_position)
            self.assertAlmostEqual(expected, harness.published[-1])

    def assert_nominal(self, harness):
        """
        Everything above, plus: no fallback timer was needed and the command count stayed bounded.
        """
        self.assert_no_violation(harness)
        self.assert_belief_is_sound(harness)
        self.assert_at_rest(harness)
        self.assertEqual(0, harness.timer_firings)
        self.assertEqual([], harness.notifications)
        self.assertLessEqual(harness.commands, MAX_COMMANDS_PER_INTENT)


class TestSingleIntents(ModelTestCase):
    """Every intent, from every physically consistent start."""

    def _exercise(self, make, name, intent, **case):
        with self.subTest(intent=name, **case):
            harness = make()
            harness.published = []
            harness.commands = 0
            harness.run(intent(harness.logic))
            self.assert_nominal(harness)

    def test_from_every_unlatched_position(self):
        for position in range(0, 101):
            for name, intent in INTENTS:
                self._exercise(lambda p=position: fresh(p), name, intent, start=position)

    def test_from_every_latched_slat_position(self):
        for position in range(int(LOWER), int(UPPER) + 1):
            for name, intent in INTENTS:
                self._exercise(lambda p=position: latched_at(p), name, intent, latched_at=position)

    def test_from_every_interrupted_latch_sequence(self):
        for stop_at in range(int(DIP) + 1, int(UPPER)):
            # Above the lower edge the conservative model says the rise has already latched.
            self.assertEqual(stop_at >= LOWER, interrupted_enter(stop_at).sim.latched)
            for name, intent in INTENTS:
                self._exercise(lambda s=stop_at: interrupted_enter(s), name, intent,
                               interrupted_at=stop_at)

    def test_from_the_state_that_used_to_stall(self):
        for name, intent in INTENTS:
            self._exercise(after_leaving_tilt, name, intent, start="after_leave")

    def test_closing_after_leaving_tilt_needs_one_command(self):
        harness = after_leaving_tilt()
        harness.commands = 0
        harness.run(harness.logic.on_close())
        self.assertEqual(1, harness.commands)
        self.assert_nominal(harness)


class TestInterruptions(ModelTestCase):
    """Q4: interrupting a multi-step sequence at every feedback point, in every way."""

    SEQUENCES = [
        ("enter_from_below", lambda: fresh(10.0, stride=8.0), lambda l: l.on_set_tilt_mode(True)),
        ("enter_from_above", lambda: fresh(80.0, stride=8.0), lambda l: l.on_set_tilt_mode(True)),
        ("enter_from_band", lambda: fresh(41.0, stride=8.0), lambda l: l.on_set_tilt_mode(True)),
        ("guarded_close", lambda: fresh(41.0, stride=8.0), lambda l: l.on_close()),
        ("guarded_descent", lambda: fresh(41.0, stride=8.0), lambda l: l.on_set_position(10.0)),
        # The tilt exit is a short move, so it needs a finer stride to have interior points at all.
        ("leave_tilt", lambda: latched_at(LOWER, stride=2.0), lambda l: l.on_set_tilt_mode(False)),
        ("slat_step", lambda: latched_at(UPPER, stride=0.25),
         lambda l: l.on_slat_step(DIRECTION_UP)),
        # The same two sequences on the geometry the optional settings produce: a four-step entry
        # that finishes mid-zone, and a much longer exit through a much wider ambiguity band.
        ("enter_custom_zone", lambda: fresh(10.0, stride=8.0, zone=CUSTOM_ZONE),
         lambda l: l.on_set_tilt_mode(True)),
        ("leave_custom_zone", lambda: latched_at(LOWER, stride=2.0, zone=CUSTOM_ZONE),
         lambda l: l.on_set_tilt_mode(False)),
    ]

    INTERRUPTIONS = [
        ("stop", lambda h: h.run(h.logic.on_stop())),
        ("open", lambda h: h.run(h.logic.on_open())),
        ("close", lambda h: h.run(h.logic.on_close())),
        ("tilt_on", lambda h: h.run(h.logic.on_set_tilt_mode(True))),
        ("tilt_off", lambda h: h.run(h.logic.on_set_tilt_mode(False))),
        ("long_down", lambda h: h.run(h.logic.on_knx_long(DIRECTION_DOWN))),
        ("short_down", lambda h: h.run(h.logic.on_knx_short(DIRECTION_DOWN))),
        ("set_position", lambda h: h.run(h.logic.on_set_position(70.0))),
        ("restart", lambda h: h.restart()),
        ("unavailable_then_restart", lambda h: (h.go_unavailable(), h.restart())),
    ]

    def test_every_injection_point(self):
        for name, make, start in self.SEQUENCES:
            reports = self._count_reports(make, start)
            self.assertGreater(reports, 2, f"{name} has too few points to be worth injecting into")
            for index in range(reports):
                for label, interrupt in self.INTERRUPTIONS:
                    with self.subTest(sequence=name, after_reports=index, interruption=label):
                        harness = make()
                        harness.run_partial(start(harness.logic), index)
                        harness.published = []
                        interrupt(harness)
                        harness.run()
                        self.assert_no_violation(harness)
                        self.assert_belief_is_sound(harness)
                        self.assert_at_rest(harness)

    def test_an_interrupted_sequence_recovers_to_a_known_state(self):
        # After a restart the app must end up believing exactly what the blind is doing.
        for name, make, start in self.SEQUENCES:
            reports = self._count_reports(make, start)
            for index in range(reports):
                with self.subTest(sequence=name, after_reports=index):
                    harness = make()
                    harness.run_partial(start(harness.logic), index)
                    harness.restart()
                    self.assert_no_violation(harness)
                    self.assert_belief_is_sound(harness)
                    self.assert_at_rest(harness)

    @staticmethod
    def _count_reports(make, start):
        """
        How many position updates a sequence produces when nothing interrupts it.
        """
        harness = make()
        return harness.run_partial(start(harness.logic), MAX_TICKS)


class TestFeedbackQuirks(ModelTestCase):
    """Controllers that report oddly must not change any outcome."""

    QUIRK_SETS = [
        ("duplicate_settled", Quirks(duplicate_settled=True)),
        ("no_motion_state", Quirks(report_motion_state=False)),
        ("final_report_only", Quirks(report_intermediate=False)),
        ("final_report_only_no_motion", Quirks(report_intermediate=False,
                                               report_motion_state=False)),
        # The report that used to complete a slat step before the blind had moved: a settled-looking
        # echo of the position the move started from.
        ("stale_echo", Quirks(report_motion_state=False, echo_before_moving=True)),
        ("everything_at_once", Quirks(report_intermediate=False, report_motion_state=False,
                                      duplicate_settled=True, echo_before_moving=True)),
    ]

    def test_every_intent_under_every_quirk(self):
        for label, quirks in self.QUIRK_SETS:
            for position in (0, 10, 36, 37, 38, 41, 44, 45, 46, 50, 100):
                for name, intent in INTENTS:
                    with self.subTest(quirks=label, position=position, intent=name):
                        harness = fresh(position, quirks=quirks)
                        harness.run(intent(harness.logic))
                        self.assert_nominal(harness)

    def test_latched_slat_steps_under_every_quirk(self):
        # The smallest moves in the app: one slat step is barely more than a whole percent, which is
        # exactly where a duplicate report used to complete the plan before the blind moved.
        for label, quirks in self.QUIRK_SETS:
            with self.subTest(quirks=label):
                harness = latched_at(UPPER, quirks=quirks)
                for _ in range(6):
                    harness.published = []
                    harness.run(harness.logic.on_slat_step(DIRECTION_UP))
                    self.assert_nominal(harness)
                self.assertAlmostEqual(100.0, harness.logic.current_virtual_position())
                self.assertEqual(to_command(LOWER), harness.sim.reported)


class TestAlternateGeometries(ModelTestCase):
    """
    The same proofs on the geometries the two optional settings produce.

    A configured ``tilt_zone_release_pct`` widens the ambiguity band far past the zone (so band
    snapping, startup recovery and latch-belief clearing all reach further), and a configured
    ``tilt_enter_landing_pct`` gives the enter sequence a fourth step that is neither zone edge.
    Both change what the planner emits, so both have to survive the same sweep.
    """

    ZONES = [
        # Releases high up, entry lands slightly open. Band [36, 55].
        ("custom_release_and_landing", CUSTOM_ZONE),
        # Releases at the bare clearance, but the slats have to end wide open.
        ("landing_only",
         Zone(tilt_zone_upper_pct=UPPER, tilt_zone_lower_pct=LOWER, tilt_zone_epsilon_pct=EPSILON,
              tilt_step_pct=STEP, tilt_enter_landing_pct=100.0)),
        # A low zone that needs almost the whole remaining travel to release. Band [18, 95].
        ("release_far_above_the_zone",
         Zone(tilt_zone_upper_pct=30.0, tilt_zone_lower_pct=20.0, tilt_zone_epsilon_pct=2.0,
              tilt_step_pct=25.0, tilt_zone_release_pct=95.0, tilt_enter_landing_pct=25.0)),
    ]

    POSITIONS = (0, 10, 19, 20, 25, 30, 36, 38, 41, 44, 46, 50, 55, 56, 80, 95, 96, 100)

    def test_every_intent_from_every_position(self):
        for label, zone in self.ZONES:
            for position in self.POSITIONS:
                for name, intent in INTENTS:
                    with self.subTest(zone=label, start=position, intent=name):
                        harness = fresh(position, zone=zone)
                        harness.run(intent(harness.logic))
                        self.assert_nominal(harness)

    def test_entry_lands_on_the_configured_slat_angle(self):
        for label, zone in self.ZONES:
            with self.subTest(zone=label):
                harness = fresh(80.0, zone=zone)
                harness.run(harness.logic.on_set_tilt_mode(True))
                self.assertTrue(harness.sim.latched)
                self.assertTrue(zone.in_zone(harness.sim.physical))
                self.assertAlmostEqual(to_command(zone.virtual_to_real(zone.enter_landing)),
                                       harness.sim.reported)
                self.assert_nominal(harness)

    def test_leaving_tilt_physically_clears_the_release_height(self):
        for label, zone in self.ZONES:
            with self.subTest(zone=label):
                harness = fresh(80.0, zone=zone)
                harness.run(harness.logic.on_set_tilt_mode(True))
                harness.published = []
                harness.commands = 0
                harness.run(harness.logic.on_set_tilt_mode(False))
                self.assertFalse(harness.sim.latched)
                self.assertGreaterEqual(harness.sim.physical, zone.release_target)
                self.assertEqual(LATCH_UNLATCHED, harness.logic.latch)
                self.assert_nominal(harness)

    def test_every_intent_from_every_latched_slat_position(self):
        for label, zone in self.ZONES:
            for position in range(int(zone.lower), int(zone.upper) + 1):
                for name, intent in INTENTS:
                    with self.subTest(zone=label, latched_at=position, intent=name):
                        harness = latched_at(position, zone=zone)
                        harness.published = []
                        harness.commands = 0
                        harness.run(intent(harness.logic))
                        self.assert_nominal(harness)

    def test_every_intent_under_every_quirk(self):
        for label, zone in self.ZONES:
            for quirk_label, quirks in TestFeedbackQuirks.QUIRK_SETS:
                for position in (0, 36, 41, 46, 50, 100):
                    for name, intent in INTENTS:
                        with self.subTest(zone=label, quirks=quirk_label, position=position,
                                          intent=name):
                            harness = fresh(position, quirks=quirks, zone=zone)
                            harness.run(intent(harness.logic))
                            self.assert_nominal(harness)

    def test_an_interrupted_entry_recovers_to_a_known_state(self):
        for label, zone in self.ZONES:
            harness = fresh(10.0, stride=8.0, zone=zone)
            reports = harness.run_partial(harness.logic.on_set_tilt_mode(True), MAX_TICKS)
            for index in range(reports):
                with self.subTest(zone=label, after_reports=index):
                    harness = fresh(10.0, stride=8.0, zone=zone)
                    harness.run_partial(harness.logic.on_set_tilt_mode(True), index)
                    harness.restart()
                    self.assert_no_violation(harness)
                    self.assert_belief_is_sound(harness)
                    self.assert_at_rest(harness)


class TestSettleTimerAgainstTheModel(ModelTestCase):
    """The fallback timer must be inert on healthy runs and honest on jammed ones."""

    def test_firing_at_every_point_of_a_sequence_changes_nothing(self):
        probe = fresh(10.0, stride=8.0)
        reports = probe.run_partial(probe.logic.on_set_tilt_mode(True), MAX_TICKS)
        self.assertGreater(reports, 5)
        for index in range(reports):
            with self.subTest(after_reports=index):
                harness = fresh(10.0, stride=8.0)
                harness.run_partial(harness.logic.on_set_tilt_mode(True), index)
                harness.fire_timer()
                harness.run()
                self.assert_no_violation(harness)
                self.assert_belief_is_sound(harness)
                self.assert_at_rest(harness)
                self.assertEqual([], harness.notifications, "a healthy move was called a stall")
                self.assertTrue(harness.logic.in_tilt)

    def test_a_jammed_blind_is_reported_as_a_stall(self):
        harness = fresh(80.0, stride=8.0)
        harness.run_partial(harness.logic.on_close(), 3)
        harness.sim.jam()
        harness.run()
        harness.fire_timer()
        self.assertEqual(1, len(harness.notifications))
        self.assertIn("did not reach", harness.notifications[0].message)
        self.assertFalse(harness.logic.has_pending_plan)
        self.assertFalse(harness.timer_armed)
        self.assert_no_violation(harness)

    def test_an_unavailable_cover_mid_plan_is_reported_as_a_stall(self):
        harness = fresh(80.0, stride=8.0)
        harness.run_partial(harness.logic.on_close(), 2)
        harness.go_unavailable()
        harness.apply(harness.logic.on_settle_timer(None, False))
        self.assertEqual(1, len(harness.notifications))
        self.assertIn("unreadable", harness.notifications[0].message)
        self.assertFalse(harness.logic.has_pending_plan)


class TestCalibrationDrift(ModelTestCase):
    """Why every latch sequence starts from the top limit."""

    # Small enough that the error accruing over one entry plus a handful of slat steps stays well
    # inside the epsilon margin -- the condition the cheap tilt exit assumes.
    DRIFT = Quirks(drift_per_move=0.1)

    def test_entry_lands_correctly_however_much_error_preceded_it(self):
        # Entry is referenced from the top limit, so the dip and the latching rise land where they
        # intend no matter what the earlier travel accumulated.
        harness = fresh(20.0, quirks=Quirks(drift_per_move=1.5))
        harness.run(harness.logic.on_set_tilt_mode(True))
        self.assertTrue(harness.sim.latched)
        self.assertTrue(ZONE.in_zone(harness.sim.physical))
        self.assert_nominal(harness)

    def test_slat_stepping_and_the_cheap_exit_survive_small_drift(self):
        harness = fresh(20.0, quirks=self.DRIFT)
        harness.run(harness.logic.on_set_tilt_mode(True))
        for _ in range(3):
            harness.published = []
            harness.commands = 0
            harness.run(harness.logic.on_slat_step(DIRECTION_UP))
            self.assert_nominal(harness)

        harness.published = []
        harness.commands = 0
        harness.run(harness.logic.on_set_tilt_mode(False))
        self.assertFalse(harness.sim.latched)
        self.assert_nominal(harness)

    def test_a_guarded_descent_is_immune_to_drift_entirely(self):
        harness = fresh(20.0, quirks=Quirks(drift_per_move=1.5))
        harness.run(harness.logic.on_set_tilt_mode(True))
        harness.published = []
        harness.commands = 0
        harness.run(harness.logic.on_knx_long(DIRECTION_DOWN))
        self.assertFalse(harness.sim.latched)
        self.assertEqual(0, harness.sim.reported)
        self.assert_nominal(harness)

    def test_commanding_exactly_the_release_target_can_stop_short_of_releasing(self):
        # The field failure the exit overshoot exists for: the actuator reports the setpoint it was
        # given, so a percent of error leaves the blind physically below the release height while
        # the feedback says it arrived. Commanding higher turns that shortfall into slack.
        sim = BlindSimulator(ZONE, position=UPPER, latched=True)
        sim.drift = 1.0
        sim.set_position(to_command(RELEASE))
        _run_out(sim)
        self.assertTrue(sim.latched, "the bare release command released a blind it should not have")
        self.assertEqual(to_command(RELEASE), sim.reported)

        sim.set_position(to_command(RELEASE + EXIT_OVERSHOOT_PCT))
        _run_out(sim)
        self.assertFalse(sim.latched)
        self.assertEqual([], sim.violations)

    def test_the_exit_releases_despite_an_actuator_that_settles_low(self):
        # The same error, end to end. The blind enters cleanly and only the exit move settles low,
        # by the ~1.5% the real controller's position feedback is good to: the app's exit still
        # physically clears the release height, and the belief it commits is the truth.
        harness = fresh(20.0)
        harness.run(harness.logic.on_set_tilt_mode(True))
        self.assertTrue(harness.sim.latched)

        harness.sim.quirks.drift_per_move = 1.5
        harness.published = []
        harness.commands = 0
        harness.run(harness.logic.on_set_tilt_mode(False))
        self.assertFalse(harness.sim.latched)
        self.assertGreaterEqual(harness.sim.physical, RELEASE)
        self.assertEqual(LATCH_UNLATCHED, harness.logic.latch)
        self.assert_nominal(harness)

    def test_a_short_rise_from_an_uncalibrated_state_would_not_release(self):
        # The cheap release the descent guard deliberately does not use, and the bound the tilt exit
        # does depend on: once the reported position has drifted by the epsilon margin, rising to a
        # *reported* upper + epsilon leaves the blind physically at the upper edge, still latched,
        # while looking like it worked.
        sim = BlindSimulator(ZONE, position=41.0, latched=True)
        sim.drift = EPSILON
        sim.set_position(to_command(RELEASE))
        _run_out(sim)
        self.assertTrue(sim.latched)
        self.assertEqual(to_command(RELEASE), sim.reported)

        # The full open is referenced against the limit switch, so it cannot be fooled the same way
        # however large the error.
        sim.open_cover()
        _run_out(sim)
        self.assertFalse(sim.latched)
        self.assertEqual(0.0, sim.drift)
        self.assertEqual([], sim.violations)


def _run_out(sim):
    """
    Drive a bare simulator to rest, discarding its reports.
    """
    for _ in range(MAX_TICKS):
        if not sim.busy:
            return
        sim.tick()
    raise AssertionError("the blind never came to rest")


if __name__ == "__main__":
    unittest.main()
